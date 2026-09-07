"""Offline, disk-backed CRM read model.

The caller owns the storage file.  This service only reads bounded CRM records
from that file and exposes organization-scoped projections for agency work.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


Clock = Callable[[], datetime]


class CrmReadService:
    """Small, deterministic, organization-scoped offline CRM reader."""

    def __init__(self, storage_path: str | Path, *, clock: Clock | None = None) -> None:
        self.storage_path = Path(storage_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def list_clients(self, organization_id: str) -> list[dict[str, Any]]:
        """Return agency clients with their computed health summaries."""
        if not organization_id:
            raise ValidationError("organization is required")
        data = self._read_store()
        clients = [client for client in self._records(data, "clients") if self._record_org(client) == organization_id]
        interactions = [item for item in self._records(data, "interactions") if self._record_org(item) == organization_id]
        open_items = [item for item in self._records(data, "open_items") if self._record_org(item) == organization_id]

        result = []
        for client in sorted(clients, key=lambda item: str(item.get("id") or "")):
            client_id = self._client_id(client)
            client_interactions = [item for item in interactions if str(item.get("client_id") or "") == client_id]
            client_open_items = [
                item for item in open_items
                if str(item.get("client_id") or "") == client_id and str(item.get("status") or "open").lower() == "open"
            ]
            result.append({
                "id": client_id,
                "name": str(client.get("name") or ""),
                "organization_id": organization_id,
                "health": self._health(client_interactions, len(client_open_items)),
            })
        return result

    def contact_directory(self, organization_id: str, client_id: str) -> list[dict[str, Any]]:
        """Return the contact directory for one organization-scoped client."""
        self._require_client(organization_id, client_id)
        data = self._read_store()
        contacts = [
            contact for contact in self._records(data, "contacts")
            if self._record_org(contact) == organization_id and str(contact.get("client_id") or "") == client_id
        ]
        return [
            {
                "id": self._record_id(contact, "contact id is required"),
                "client_id": client_id,
                "organization_id": organization_id,
                "name": str(contact.get("name") or ""),
                "role": str(contact.get("role") or ""),
                "email": str(contact.get("email") or ""),
            }
            for contact in sorted(contacts, key=lambda item: str(item.get("name") or item.get("id") or ""))
        ]

    def interaction_timeline(self, organization_id: str, client_id: str) -> list[dict[str, Any]]:
        """Return newest-first interactions for one organization-scoped client."""
        self._require_client(organization_id, client_id)
        data = self._read_store()
        interactions = [
            item for item in self._records(data, "interactions")
            if self._record_org(item) == organization_id and str(item.get("client_id") or "") == client_id
        ]
        ordered = sorted(interactions, key=lambda item: self._parse_time(item.get("occurred_at")), reverse=True)
        return [
            {
                "id": self._record_id(item, "interaction id is required"),
                "client_id": client_id,
                "organization_id": organization_id,
                "occurred_at": self._parse_time(item.get("occurred_at")).isoformat().replace("+00:00", "Z"),
                "kind": str(item.get("kind") or ""),
                "summary": str(item.get("summary") or ""),
                "contact_id": str(item.get("contact_id") or ""),
            }
            for item in ordered
        ]

    def _require_client(self, organization_id: str, client_id: str) -> Mapping[str, Any]:
        if not organization_id or not client_id:
            raise ValidationError("organization and client are required")
        data = self._read_store()
        scoped = [
            client for client in self._records(data, "clients")
            if str(client.get("id") or "") == client_id
        ]
        if not scoped:
            raise NotFoundError("CRM client was not found")
        for client in scoped:
            if self._record_org(client) == organization_id:
                return client
        raise AuthorizationError("CRM client is outside organization scope")

    def _read_store(self) -> Mapping[str, Any]:
        if not self.storage_path.exists():
            raise ValidationError("CRM storage file is required")
        try:
            with self.storage_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValidationError("CRM storage file is invalid JSON") from exc
        if not isinstance(data, Mapping):
            raise ValidationError("CRM storage root must be an object")
        return data

    def _health(self, interactions: list[Mapping[str, Any]], open_items_count: int) -> dict[str, Any]:
        last_interaction = max((self._parse_time(item.get("occurred_at")) for item in interactions), default=None)
        if last_interaction is None:
            age_days = None
            last_at = None
        else:
            now = self._coerce_clock(self.clock())
            age_days = max((now - last_interaction).days, 0)
            last_at = last_interaction.isoformat().replace("+00:00", "Z")
        return {
            "last_interaction_at": last_at,
            "last_interaction_age_days": age_days,
            "open_items_count": open_items_count,
        }

    @staticmethod
    def _records(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
        values = data.get(key, [])
        if not isinstance(values, list):
            raise ValidationError(f"CRM {key} must be a list")
        if not all(isinstance(item, Mapping) for item in values):
            raise ValidationError(f"CRM {key} records must be objects")
        return values

    @staticmethod
    def _client_id(client: Mapping[str, Any]) -> str:
        return CrmReadService._record_id(client, "client id is required")

    @staticmethod
    def _record_id(record: Mapping[str, Any], message: str) -> str:
        value = str(record.get("id") or "").strip()
        if not value:
            raise ValidationError(message)
        return value

    @staticmethod
    def _record_org(record: Mapping[str, Any]) -> str:
        value = str(record.get("organization_id") or "").strip()
        if not value:
            raise ValidationError("organization id is required")
        return value

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        text = str(value or "").strip()
        if not text:
            raise ValidationError("CRM timestamp is required")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("CRM timestamp is invalid") from exc
        return CrmReadService._coerce_clock(parsed)

    @staticmethod
    def _coerce_clock(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


CRMReadService = CrmReadService
