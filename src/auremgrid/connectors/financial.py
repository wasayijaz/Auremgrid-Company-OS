"""Read-only Stripe, Meta Ads, Google Ads, and CRM import adapters.

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
    verification: Mapping[str, Any] | None = None
    baseline: Mapping[str, Any] | None = None
    reconcile: Mapping[str, Any] | None = None
    fence: Mapping[str, Any] | None = None


class ReadOnlyProviderAdapter:
    provider = ""
    resources: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()

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
            return ImportPage((), cursor, (), self._verification(account_id, (), "not_connected"), None, None, None)
        raw = self.transport(resource=resource, cursor=cursor, account_id=account_id)
        if not isinstance(raw, Mapping):
            raise ValidationError("provider transport returned an invalid page")
        declared_scopes = raw.get("scopes", raw.get("read_scopes"))
        verification = self._verification(account_id, declared_scopes, "configured")
        declared_provider = str(raw.get("provider") or self.provider).strip()
        declared_account = str(raw.get("account_id") or raw.get("account") or account_id).strip()
        if declared_provider != self.provider:
            raise ValidationError("provider identity verification failed")
        if declared_account != account_id:
            raise ValidationError("provider account fence verification failed")
        missing_scopes = [scope for scope in self.required_scopes if scope not in verification["granted_scopes"]]
        if missing_scopes:
            raise ValidationError("provider read scope verification failed")
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
        baseline = {"provider_count": len(values), "record_count": len(records), "quarantine_count": len(quarantined)}
        reconcile = {
            "provider_count": len(values),
            "accepted": len(records),
            "quarantined": len(quarantined),
            "duplicates_on_page": len(values) - len(records) - len(quarantined),
        }
        fence = {"account_id": account_id, "workspace_id": workspace_mappings[account_id], "resource": resource}
        return ImportPage(
            tuple(records),
            str(next_cursor) if next_cursor is not None else None,
            tuple(quarantined),
            verification,
            baseline,
            reconcile,
            fence,
        )

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
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()

    def _verification(self, account_id: str, scopes: Any, status: str) -> dict[str, Any]:
        granted = (
            tuple(str(scope).strip() for scope in scopes)
            if isinstance(scopes, (list, tuple, set))
            else self.required_scopes
        )
        return {
            "provider": self.provider,
            "account_id": account_id,
            "status": status,
            "required_scopes": self.required_scopes,
            "granted_scopes": granted,
            "read_scope_verified": all(scope in granted for scope in self.required_scopes),
        }


class StripeReadOnlyAdapter(ReadOnlyProviderAdapter):
    provider = "stripe_accounting"
    resources = ("invoices", "payments", "charges")


class MetaAdsReadOnlyAdapter(ReadOnlyProviderAdapter):
    provider = "meta_ads"
    resources = ("campaigns", "insights")
    required_scopes = ("ads_read",)


class GoogleAdsReadOnlyAdapter(ReadOnlyProviderAdapter):
    provider = "google_ads"
    resources = ("campaigns", "ad_groups", "ads", "metrics")
    required_scopes = ("google_ads.readonly",)

    def _normalize(self, resource: str, item: Mapping[str, Any], account_id: str, workspace_id: str) -> ProviderRecord:
        external_id = str(
            _first(item, "id", "external_id", f"{resource[:-1]}.id", f"{resource[:-1]}.resourceName", "resourceName")
            or ""
        ).strip()
        campaign_id = str(_first(item, "campaign_id", "campaign.id", "campaignId") or "").strip()
        occurred = _first(item, "created", "created_at", "date_start", "segments.date", "date")
        occurred_at = self._timestamp(occurred)
        if not external_id and resource == "metrics" and campaign_id and occurred_at:
            external_id = f"campaign:{campaign_id}:date:{occurred_at[:10]}"
        if not external_id:
            raise ValidationError("provider record id is required")
        amount = _first(item, "amount", "amount_paid", "spend", "value", "metrics.cost", "metrics.value")
        micros = _first(item, "cost_micros", "costMicros", "metrics.costMicros", "metrics.cost_micros")
        amount_value = float(amount) if amount is not None else (float(micros) / 1_000_000 if micros is not None else None)
        currency = _first(item, "currency", "currency_code", "currencyCode", "customer.currencyCode")
        status = _first(item, "status", f"{resource[:-1]}.status")
        return ProviderRecord(
            self.provider,
            resource,
            external_id,
            account_id,
            workspace_id,
            occurred_at,
            amount_value,
            str(currency).upper() if currency else None,
            str(status) if status is not None else None,
            dict(item),
            self.provider,
        )


class CRMReadOnlyAdapter(ReadOnlyProviderAdapter):
    provider = "crm"
    resources = ("contacts", "opportunities")
    required_scopes = ("crm.read",)

    def _normalize(self, resource: str, item: Mapping[str, Any], account_id: str, workspace_id: str) -> ProviderRecord:
        external_id = str(
            _first(
                item,
                "id",
                "external_id",
                "contact.id",
                "person.id",
                "opportunity.id",
                "deal.id",
            )
            or ""
        ).strip()
        if not external_id:
            raise ValidationError("provider record id is required")
        occurred = _first(item, "created", "created_at", "updated", "updated_at", "last_modified", "close_date")
        occurred_at = self._timestamp(occurred)
        amount = _first(item, "amount", "value", "estimated_value", "opportunity.amount", "deal.amount")
        currency = _first(item, "currency", "currency_code", "currencyCode", "opportunity.currency", "deal.currency")
        status = _first(item, "status", "stage", "opportunity.status", "opportunity.stage", "deal.status", "deal.stage")
        return ProviderRecord(
            self.provider,
            resource,
            external_id,
            account_id,
            workspace_id,
            occurred_at,
            float(amount) if amount is not None else None,
            str(currency).upper() if currency else None,
            str(status) if status is not None else None,
            dict(item),
            self.provider,
        )


class GA4AnalyticsReadOnlyAdapter(ReadOnlyProviderAdapter):
    provider = "ga4_analytics"
    resources = ("metrics", "events")
    required_scopes = ("analytics.readonly",)

    def _normalize(self, resource: str, item: Mapping[str, Any], account_id: str, workspace_id: str) -> ProviderRecord:
        campaign_id = str(_first(item, "canonical_campaign_id", "campaign_id", "campaign.id") or "").strip()
        date_value = _first(item, "date", "event_date", "segments.date", "date_start")
        occurred_at = self._timestamp(date_value)
        external_id = str(_first(item, "id", "external_id") or "").strip()
        if not external_id and campaign_id and occurred_at:
            external_id = f"campaign:{campaign_id}:date:{occurred_at[:10]}"
        if not external_id:
            raise ValidationError("provider record id is required")
        revenue = _first(item, "revenue", "purchase_revenue", "totalRevenue", "metrics.totalRevenue")
        currency = _first(item, "currency", "currency_code", "currencyCode")
        return ProviderRecord(
            self.provider,
            resource,
            external_id,
            account_id,
            workspace_id,
            occurred_at,
            float(revenue) if revenue is not None else None,
            str(currency).upper() if currency else None,
            str(_first(item, "status") or "reported"),
            dict(item),
            self.provider,
        )


class SearchConsoleReadOnlyAdapter(ReadOnlyProviderAdapter):
    provider = "search_console"
    resources = ("metrics", "queries")
    required_scopes = ("webmasters.readonly",)

    def _normalize(self, resource: str, item: Mapping[str, Any], account_id: str, workspace_id: str) -> ProviderRecord:
        campaign_id = str(_first(item, "canonical_campaign_id", "campaign_id", "campaign.id") or "").strip()
        date_value = _first(item, "date", "segments.date", "date_start")
        occurred_at = self._timestamp(date_value)
        query = str(_first(item, "query", "keys.0") or "").strip()
        external_id = str(_first(item, "id", "external_id") or "").strip()
        if not external_id and campaign_id and occurred_at:
            suffix = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12] if query else "all"
            external_id = f"campaign:{campaign_id}:date:{occurred_at[:10]}:query:{suffix}"
        if not external_id:
            raise ValidationError("provider record id is required")
        return ProviderRecord(
            self.provider,
            resource,
            external_id,
            account_id,
            workspace_id,
            occurred_at,
            None,
            None,
            str(_first(item, "status") or "reported"),
            dict(item),
            self.provider,
        )


def _first(item: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = _get_path(item, key)
        if value is not None:
            return value
    return None


def _get_path(item: Mapping[str, Any], key: str) -> Any:
    current: Any = item
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current
