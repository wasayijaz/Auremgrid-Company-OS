"""Canonical, read-only provider import application with cursor/dedupe safeguards."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from typing import Any

from auremgrid.connectors.financial import ImportPage, MetaAdsReadOnlyAdapter, ProviderRecord, StripeReadOnlyAdapter
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


class ProviderImportService:
    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn

    def pull(self, identity: Any, provider: str, account_id: str, workspace_mappings: dict[str, str],
             resource: str, cursor: str | None = None, adapter: Any | None = None) -> dict[str, Any]:
        identity.require("integration_sync")
        if identity.workspace_id is not None and any(ws != identity.workspace_id for ws in workspace_mappings.values()):
            raise AuthorizationError("provider import mapping is outside workspace scope")
        adapter = adapter or {"stripe_accounting": StripeReadOnlyAdapter(), "meta_ads": MetaAdsReadOnlyAdapter()}.get(provider)
        if adapter is None:
            raise ValidationError("unsupported provider import")
        workspace_id = workspace_mappings.get(account_id)
        if workspace_id is None:
            raise ValidationError("provider account must map to a workspace before import")
        self.os._require_person_access(identity.organization_id, workspace_id, identity.person_id, write=True)
        if getattr(adapter, "transport", None) is None or getattr(adapter, "status", "not_connected") != "configured":
            self._cursor(identity.organization_id, workspace_id, provider, account_id, resource, cursor, "not_connected", None)
            return {"provider": provider, "resource": resource, "account_id": account_id,
                    "cursor_before": cursor, "cursor_after": cursor, "status": "not_connected",
                    "imported": 0, "duplicates": 0, "quarantined": 0, "canonical_written": 0,
                    "unsupported": 0, "quarantine_details": []}
        page: ImportPage = adapter.pull(resource, cursor, account_id, workspace_mappings)
        result = {"provider": provider, "resource": resource, "account_id": account_id,
                  "cursor_before": cursor, "cursor_after": page.next_cursor,
                  "imported": 0, "duplicates": 0, "quarantined": len(page.quarantined),
                  "canonical_written": 0, "unsupported": 0,
                  "quarantine_details": list(page.quarantined)}
        for detail in page.quarantined:
            self._quarantine(identity.organization_id, provider, resource, str(detail.get("external_id") or ""),
                             str(detail.get("reason") or "invalid_record"), detail)
        for record in page.records:
            action = self._apply(identity.organization_id, record)
            result[action] += 1
            if action == "imported":
                canonical = self._apply_canonical(identity, record)
                result[canonical] += 1
        status = "degraded" if result["quarantined"] else "configured"
        self._cursor(identity.organization_id, workspace_id, provider, account_id, resource, page.next_cursor,
                     status, "conflicting_update" if result["quarantined"] else None)
        return result

    def _cursor(self, organization_id: str, workspace_id: str, provider: str, account_id: str,
                resource: str, cursor: str | None, status: str, last_error: str | None) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.conn.execute(
            "INSERT INTO provider_import_cursors (id,organization_id,workspace_id,provider,account_id,resource,cursor_value,status,last_error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(organization_id,workspace_id,provider,account_id,resource) DO UPDATE SET cursor_value=excluded.cursor_value,status=excluded.status,last_error=excluded.last_error,updated_at=excluded.updated_at",
            (self.os.jobs.new_id("import_cursor"), organization_id, workspace_id, provider, account_id,
             resource, cursor, status, last_error, now),
        )
        self.conn.commit()

    def _apply(self, organization_id: str, record: ProviderRecord) -> str:
        digest = record.payload_hash
        existing = self.conn.execute(
            "SELECT payload_hash FROM provider_import_records WHERE organization_id=? AND provider=? AND object_type=? AND external_id=?",
            (organization_id, record.provider, record.object_type, record.external_id),
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] != digest:
                self.conn.execute(
                    "INSERT INTO provider_import_quarantines (id,organization_id,provider,object_type,external_id,reason,evidence_digest,quarantine_details,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.os.jobs.new_id("import_q"), organization_id, record.provider, record.object_type,
                     record.external_id, "conflicting_update", digest,
                     json.dumps({"payload_hash": digest, "dedupe_key": record.dedupe_key}, sort_keys=True),
                     datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
                )
                self.conn.commit()
                return "quarantined"
            return "duplicates"
        self.conn.execute(
            "INSERT INTO provider_import_records (id,organization_id,workspace_id,provider,object_type,external_id,account_id,occurred_at,amount,currency,payload_hash,source,imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.os.jobs.new_id("import"), organization_id, record.workspace_id, record.provider,
             record.object_type, record.external_id, record.account_id, record.occurred_at,
             record.amount, record.currency, digest, record.source,
             datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        )
        # Canonical finance/campaign tables are append-only snapshots; raw provider
        # payload remains in source evidence storage for downstream reconciliation.
        self.conn.commit()
        return "imported"

    def _quarantine(self, organization_id: str, provider: str, object_type: str, external_id: str,
                    reason: str, details: dict[str, Any]) -> None:
        digest = _digest(details)
        self.conn.execute(
            "INSERT INTO provider_import_quarantines (id,organization_id,provider,object_type,external_id,reason,evidence_digest,quarantine_details,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.os.jobs.new_id("import_q"), organization_id, provider, object_type, external_id,
             reason, digest, json.dumps(details, sort_keys=True, default=str),
             datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        )

    def _apply_canonical(self, identity: Any, record: ProviderRecord) -> str:
        try:
            if record.provider == "stripe_accounting":
                return self._apply_stripe(identity, record)
            if record.provider == "meta_ads":
                return self._apply_meta(identity, record)
        except (AuthorizationError, NotFoundError, ValidationError, TypeError, ValueError) as exc:
            self._quarantine(identity.organization_id, record.provider, record.object_type, record.external_id,
                             "canonical_write_rejected", {"error": str(exc), "payload": dict(record.payload)})
            return "quarantined"
        self._quarantine(identity.organization_id, record.provider, record.object_type, record.external_id,
                         "unsupported_resource", {"resource": record.object_type, "payload": dict(record.payload)})
        return "unsupported"

    def _apply_stripe(self, identity: Any, record: ProviderRecord) -> str:
        if record.amount is None or record.currency is None or record.occurred_at is None:
            raise ValidationError("stripe record requires amount, currency, and timestamp")
        source = f"{record.provider}:{record.object_type}:{record.external_id}"
        if record.object_type == "invoices":
            due_at = str(record.payload.get("due_at") or record.payload.get("due_date") or record.occurred_at)
            self.os.agency_ops.record_invoice(identity.organization_id, record.workspace_id, identity.person_id,
                                              record.amount, record.occurred_at, due_at, source,
                                              record.currency, record.external_id,
                                              str(record.status or "issued"))
            return "canonical_written"
        if record.object_type in {"payments", "charges"}:
            self.os.agency_ops.record_revenue(identity.organization_id, record.workspace_id, identity.person_id,
                                              record.amount, record.occurred_at, source,
                                              record.object_type.rstrip("s"), record.currency)
            return "canonical_written"
        return "unsupported"

    def _apply_meta(self, identity: Any, record: ProviderRecord) -> str:
        source = f"{record.provider}:{record.object_type}:{record.external_id}"
        if record.object_type == "campaigns":
            if not str(record.payload.get("name") or "").strip():
                raise ValidationError("meta campaign requires name")
            self.os.agency_ops.create_campaign(identity.organization_id, record.workspace_id, identity.person_id,
                                               str(record.payload["name"]), str(record.payload.get("objective") or "provider import"),
                                               "meta", budget=record.amount, currency=record.currency or "USD")
            return "canonical_written"
        if record.object_type == "insights":
            campaign_id = str(record.payload.get("campaign_id") or record.payload.get("canonical_campaign_id") or "").strip()
            if not campaign_id:
                self._quarantine(identity.organization_id, record.provider, record.object_type, record.external_id,
                                 "unsupported_without_campaign_mapping", {"payload": dict(record.payload)})
                return "unsupported"
            self.os.agency_ops.record_campaign_metrics(
                identity.organization_id, record.workspace_id, identity.person_id, campaign_id, source,
                _num(record.payload.get("spend")), _num(record.payload.get("revenue") or record.payload.get("value")),
                _num(record.payload.get("leads") or record.payload.get("conversions")),
                _num(record.payload.get("impressions")), _num(record.payload.get("clicks")),
            )
            return "canonical_written"
        return "unsupported"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None
