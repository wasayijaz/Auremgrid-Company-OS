"""Offline Figma comment/version evidence synchronization.

This adapter deliberately stores only bounded evidence references.  It accepts
provider-shaped responses supplied by a caller; it never creates a transport or
fetches design content.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from auremgrid.domain.errors import AuthorizationError, ValidationError

MAX_QUOTED_COMMENT = 4_000
MAX_VERSION_LABEL = 240
MAX_VERSION_DESCRIPTION = 1_200
REQUIRED_SCOPES = frozenset({"file_metadata:read", "comments:read", "file_versions:read"})


@dataclass(frozen=True)
class FigmaEvidence:
    organization_id: str
    workspace_id: str
    file_id: str
    object_type: str
    external_id: str
    provider_version: str
    locator: str
    quoted_text: str | None
    payload_hash: str

    @property
    def dedupe_key(self) -> str:
        return ":".join((self.file_id, self.object_type, self.external_id, self.provider_version))


class FigmaConnectorSubstanceService:
    """Small, deterministic, organization-scoped offline Figma adapter."""

    provider = "figma"

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], FigmaEvidence] = {}
        self.cursors: dict[tuple[str, str, str], str | None] = {}
        self.quarantines: list[dict[str, Any]] = []

    def verify_provider(
        self, response: Mapping[str, Any], *, account_id: str, file_id: str,
    ) -> dict[str, Any]:
        if not isinstance(response, Mapping) or response.get("provider", self.provider) != self.provider:
            raise ValidationError("provider identity verification failed")
        if str(response.get("account_id", account_id)) != account_id:
            raise AuthorizationError("provider account fence verification failed")
        mapped_file = str(response.get("file_id", file_id))
        if mapped_file != file_id:
            raise AuthorizationError("provider file fence verification failed")
        scopes = response.get("scopes", response.get("read_scopes", ()))
        granted = frozenset(str(scope) for scope in scopes) if isinstance(scopes, (list, tuple, set)) else frozenset()
        missing = sorted(REQUIRED_SCOPES - granted)
        if missing:
            raise ValidationError("provider read scope verification failed")
        return {"provider": self.provider, "account_id": account_id, "file_id": file_id,
                "required_scopes": sorted(REQUIRED_SCOPES), "granted_scopes": sorted(granted),
                "read_scope_verified": True}

    def sync(
        self, organization_id: str, workspace_id: str, account_id: str, file_id: str,
        response: Mapping[str, Any], *, cursor: str | None = None,
        workspace_mappings: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Map one simulated page and return a reconciliation envelope."""
        if not organization_id or not workspace_id or not file_id:
            raise ValidationError("organization, workspace, and file are required")
        mappings = workspace_mappings or {file_id: workspace_id}
        if mappings.get(file_id) != workspace_id:
            raise AuthorizationError("Figma file mapping is outside workspace scope")
        verification = self.verify_provider(response, account_id=account_id, file_id=file_id)
        provider_version = str(response.get("provider_version") or response.get("version") or "").strip()
        if not provider_version:
            raise ValidationError("Figma provider version is required")
        cursor_before = cursor if cursor is not None else self.cursors.get((organization_id, workspace_id, file_id))
        if cursor_before and self._cursor_version(cursor_before) == provider_version:
            return self._result(cursor_before, cursor_before, verification, provider_version, 0, 0, 0, 0, 0, [])

        values = response.get("data", response.get("records", []))
        if not isinstance(values, list):
            raise ValidationError("Figma page records must be a list")
        baseline = len(values)
        imported = duplicates = quarantined = 0
        details: list[dict[str, Any]] = []
        for index, item in enumerate(values):
            try:
                evidence = self._map_record(organization_id, workspace_id, file_id, provider_version, item)
            except (TypeError, ValueError, ValidationError) as exc:
                detail = {"index": index, "reason": "invalid_record", "error": str(exc)}
                quarantined += 1; details.append(detail); self.quarantines.append({**detail, "organization_id": organization_id, "file_id": file_id})
                continue
            key = (organization_id, evidence.dedupe_key)
            prior = self.records.get(key)
            if prior is not None:
                if prior.payload_hash != evidence.payload_hash:
                    detail = {"index": index, "reason": "conflicting_update", "external_id": evidence.external_id, "dedupe_key": evidence.dedupe_key}
                    quarantined += 1; details.append(detail); self.quarantines.append({**detail, "organization_id": organization_id, "file_id": file_id})
                else:
                    duplicates += 1
                continue
            self.records[key] = evidence
            imported += 1
        cursor_after = self._cursor(file_id, provider_version)
        self.cursors[(organization_id, workspace_id, file_id)] = cursor_after
        return self._result(cursor_before, cursor_after, verification, provider_version, baseline, imported, duplicates, quarantined, len(values), details)

    def replay_quarantine(self, quarantine: Mapping[str, Any], corrected_record: Mapping[str, Any]) -> dict[str, Any]:
        """Replay one quarantined record after an operator supplies a correction."""
        organization_id = str(quarantine.get("organization_id") or "")
        workspace_id = str(quarantine.get("workspace_id") or "")
        account_id = str(quarantine.get("account_id") or "offline")
        file_id = str(quarantine.get("file_id") or "")
        version = str(quarantine.get("provider_version") or "")
        if not all((organization_id, workspace_id, file_id, version)):
            raise ValidationError("quarantine replay fence is incomplete")
        response = {"provider": self.provider, "account_id": account_id, "file_id": file_id,
                    "scopes": sorted(REQUIRED_SCOPES), "provider_version": version, "data": [corrected_record]}
        # A replay is an explicit operator action and must re-evaluate the
        # corrected payload even when its provider version is unchanged.
        return self.sync(organization_id, workspace_id, account_id, file_id, response, cursor="")

    def _map_record(self, org: str, workspace: str, file_id: str, version: str, item: Any) -> FigmaEvidence:
        if not isinstance(item, Mapping):
            raise ValidationError("Figma record must be an object")
        kind = str(item.get("object_type") or item.get("type") or "").lower()
        if kind not in {"comment", "version"}:
            raise ValidationError("Figma evidence type must be comment or version")
        external_id = str(item.get("id") or item.get("comment_id") or item.get("version_id") or "").strip()
        if not external_id:
            raise ValidationError("Figma evidence id is required")
        if kind == "comment":
            quote = str(item.get("body") or item.get("message") or "").strip()
            if not quote:
                raise ValidationError("Figma comment quote is required")
            quote = quote[:MAX_QUOTED_COMMENT]
            locator = f"https://www.figma.com/file/{file_id}#comment-{external_id}"
        else:
            quote = None
            locator = f"https://www.figma.com/file/{file_id}?version-id={external_id}"
        bounded = {"file_id": file_id, "provider_version": version, "object_type": kind,
                   "external_id": external_id, "locator": locator, "quoted_text": quote}
        digest = hashlib.sha256(json.dumps(bounded, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return FigmaEvidence(org, workspace, file_id, kind, external_id, version, locator, quote, digest)

    @staticmethod
    def _cursor(file_id: str, version: str) -> str:
        return json.dumps({"v": 1, "file_id": file_id, "provider_version": version}, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _cursor_version(cursor: str) -> str | None:
        try:
            value = json.loads(cursor)
            return value.get("provider_version") if isinstance(value, dict) else None
        except (TypeError, ValueError):
            raise ValidationError("Figma cursor is invalid")

    @staticmethod
    def _result(before: str | None, after: str | None, verification: Mapping[str, Any], version: str,
                baseline: int, imported: int, duplicates: int, quarantined: int, provider_count: int,
                details: list[dict[str, Any]]) -> dict[str, Any]:
        return {"status": "degraded" if quarantined else "configured", "cursor_before": before,
                "cursor_after": after, "verification": dict(verification), "baseline": {"provider_count": baseline},
                "imported": imported, "duplicates": duplicates, "quarantined": quarantined,
                "quarantine_details": details, "reconcile": {"provider_count": provider_count,
                "accepted": imported, "duplicates_on_page": duplicates, "quarantined": quarantined}}


FigmaSubstanceService = FigmaConnectorSubstanceService
