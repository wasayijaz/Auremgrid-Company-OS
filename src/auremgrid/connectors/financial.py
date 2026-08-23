"""Read-only Stripe and Meta Ads import adapters.

Adapters normalize provider responses into immutable records; they do not send,
mutate, or invent provider data.  Network access is always injected by callers.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from auremgrid.domain.errors import ValidationError


@dataclass(frozen=True)
class ProviderRecord:
    provider: str
    object_type: str
    external_id: str
    account_id: str
    workspace_id: str
    occurred_at: str | None
    amount: float | None
    currency: str | None
    status: str | None
    payload: Mapping[str, Any]
    source: str

    @property
    def dedupe_key(self) -> str:
        return f"{self.provider}:{self.object_type}:{self.external_id}"

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ImportPage:
    records: tuple[ProviderRecord, ...]
    next_cursor: str | None
    quarantined: tuple[dict[str, Any], ...]


class ReadOnlyProviderAdapter:
    provider = ""
    resources: tuple[str, ...] = ()

    def __init__(self, transport: Callable[..., Mapping[str, Any]] | None = None) -> None:
        self.transport = transport

    @property
    def status(self) -> str:
        return "configured" if self.transport is not None else "not_connected"

    def pull(self, resource: str, cursor: str | None, account_id: str, workspace_mappings: Mapping[str, str]) -> ImportPage:
        if resource not in self.resources:
            raise ValidationError("unsupported provider import resource")
        if not account_id.strip() or account_id not in workspace_mappings:
            raise ValidationError("provider account must map to a workspace before import")
        if self.transport is None:
            return ImportPage((), cursor, ())
        raw = self.transport(resource=resource, cursor=cursor, account_id=account_id)
        if not isinstance(raw, Mapping):
            raise ValidationError("provider transport returned an invalid page")
        values = raw.get("data", raw.get("records", []))
        if not isinstance(values, list):
            raise ValidationError("provider page records must be a list")
        records: list[ProviderRecord] = []
        quarantined: list[dict[str, Any]] = []
        seen: dict[str, str] = {}
        for item in values:
            index = len(records) + len(quarantined)
            if not isinstance(item, Mapping):
                quarantined.append({"reason": "invalid_record", "index": index, "record": repr(item)[:240]})
                continue
            try:
                record = self._normalize(resource, item, account_id, workspace_mappings[account_id])
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                quarantined.append({
                    "reason": "invalid_record",
                    "index": index,
                    "external_id": str(item.get("id") or item.get("external_id") or ""),
                    "error": str(exc),
                    "record": dict(item),
                })
                continue
            previous = seen.get(record.dedupe_key)
            if previous is not None:
                if previous != record.payload_hash:
                    quarantined.append({
                        "reason": "conflicting_duplicate",
                        "dedupe_key": record.dedupe_key,
                        "external_id": record.external_id,
                        "record": dict(item),
                    })
                continue
            seen[record.dedupe_key] = record.payload_hash
            records.append(record)
        next_cursor = raw.get("next_cursor")
        return ImportPage(tuple(records), str(next_cursor) if next_cursor is not None else None, tuple(quarantined))

    def _normalize(self, resource: str, item: Mapping[str, Any], account_id: str, workspace_id: str) -> ProviderRecord:
        external_id = str(item.get("id") or item.get("external_id") or "").strip()
        if not external_id:
            raise ValidationError("provider record id is required")
        occurred = item.get("created") or item.get("created_at") or item.get("date_start")
        occurred_at = self._timestamp(occurred)
        amount = item.get("amount")
        if amount is None:
            amount = item.get("amount_paid") or item.get("spend") or item.get("value")
        amount_value = float(amount) if amount is not None else None
        currency = item.get("currency")
        status = item.get("status")
        return ProviderRecord(self.provider, resource, external_id, account_id, workspace_id,
                              occurred_at, amount_value, str(currency).upper() if currency else None,
                              str(status) if status is not None else None, dict(item), self.provider)

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat()
        text = str(value).strip()
        if not text:
            return None
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).replace(microsecond=0).isoformat()


class StripeReadOnlyAdapter(ReadOnlyProviderAdapter):
    provider = "stripe_accounting"
    resources = ("invoices", "payments", "charges")


class MetaAdsReadOnlyAdapter(ReadOnlyProviderAdapter):
    provider = "meta_ads"
    resources = ("campaigns", "insights")
