"""Shared constants and helper methods for integration operations."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from auremgrid.connectors.catalog import TARGET_CONNECTORS
from auremgrid.connectors.figma import FIGMA_OPTIONAL_PERMISSIONS, FIGMA_REQUIRED_PERMISSIONS
from auremgrid.connectors.fireflies import FIREFLIES_REQUIRED_SCOPES
from auremgrid.connectors.http import ConnectorTransportError
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.secrets import redact


GOOGLE_DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

CONFIGURABLE_SOURCES = frozenset({"slack", "clickup", "google_drive", "gmail", "figma", "fireflies"})
LIVE_SOURCES = frozenset({"slack", "clickup", "google_drive", "gmail", "figma", "fireflies"})
BOUNDARY_STATUSES = {item.source: item.boundary_status for item in TARGET_CONNECTORS}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class IntegrationOpsSharedMixin:
    @staticmethod
    def _live_sources() -> frozenset[str]:
        from auremgrid.services import integration_ops

        return getattr(integration_ops, "LIVE_SOURCES", LIVE_SOURCES)

    def _resolve(self, identity: AuthenticatedIdentity, integration: dict[str, Any]) -> str:
        row = self.conn.execute(
            """SELECT id FROM secret_bindings WHERE integration_id=? AND organization_id=?
            AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1""",
            (integration["id"], identity.organization_id),
        ).fetchone()
        if row is None:
            raise ValidationError("integration credential binding is required")
        return self.os.secrets.resolve_for_use(identity, row["id"], f"connector:{integration['source']}")

    def _verified_binding(self,identity: AuthenticatedIdentity,integration: dict[str,Any]) -> dict[str,Any]:
        row=self.conn.execute(
            """SELECT * FROM secret_bindings WHERE integration_id=? AND organization_id=?
            AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1""",
            (integration["id"],identity.organization_id),
        ).fetchone()
        if row is None or row["status"]!="active":
            raise ValidationError("provider-verified integration credential is required")
        return dict(row)

    def _get_for_job(self, identity: AuthenticatedIdentity, integration_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM integrations WHERE id=? AND organization_id=?", (integration_id, identity.organization_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("integration not found")
        item = dict(row)
        item["workspace_mappings"] = json.loads(item["workspace_mappings"])
        item["permissions"] = json.loads(item["permissions"])
        item["granted_permissions"] = json.loads(item.get("granted_permissions") or "[]")
        if identity.workspace_id is None or set(item["workspace_mappings"].values()) >= {identity.workspace_id}:
            return item
        raise AuthorizationError("integration is outside the job workspace scope")

    def _has_active_sync(self, integration_id: str, external_key: str | None = None) -> bool:
        rows=self.conn.execute(
            "SELECT account_key,job_id FROM connector_stream_locks WHERE status='active' AND account_key LIKE ?",
            (f"{integration_id}:%",),
        ).fetchall()
        if external_key is None:
            return bool(rows)
        for row in rows:
            job=self.conn.execute("SELECT payload FROM jobs WHERE id=?",(row["job_id"],)).fetchone()
            if job is not None and json.loads(job["payload"]).get("external_key")==external_key:
                return True
        return False

    def _stream_rollup(self, integration: dict[str, Any]) -> dict[str,Any]:
        all_completed=True;backfilling=False;quarantined=0
        for external_key,workspace_id in integration["workspace_mappings"].items():
            account_key=f"{integration['id']}:{self._mapping_hash(integration['source'],external_key,workspace_id)}"
            row=self.conn.execute(
                """SELECT 1 FROM connector_ingest_batches WHERE organization_id=? AND workspace_id=?
                AND connector=? AND account_key=? AND status='completed' LIMIT 1""",
                (integration["organization_id"],workspace_id,integration["source"],account_key),
            ).fetchone()
            if row is None:
                all_completed=False
            cursor=self.inbox.get_cursor(integration["organization_id"],workspace_id,integration["source"],account_key)
            if self._cursor_has_more(integration["source"],cursor):
                backfilling=True
            quarantined += self.conn.execute(
                """SELECT COUNT(*) FROM connector_source_events WHERE organization_id=? AND workspace_id=?
                AND connector=? AND account_key=? AND status='quarantined'""",
                (integration["organization_id"],workspace_id,integration["source"],account_key),
            ).fetchone()[0]
        return {"all_completed":all_completed,"backfilling":backfilling,"quarantined":quarantined}

    @staticmethod
    def _cursor_has_more(source: str,cursor: str | None) -> bool:
        if not cursor:
            return False
        if source=="clickup":
            return cursor!="0"
        if source=="slack":
            try:
                value=json.loads(cursor)
            except (TypeError,ValueError):
                return True
            if isinstance(value,dict) and "page_cursor" in value:
                return bool(value.get("page_cursor"))
            if isinstance(value,dict):
                return any(isinstance(item,dict) and item.get("page_cursor") for item in value.values())
        return False

    @staticmethod
    def _mapping_hash(source: str, external_key: str, workspace_id: str) -> str:
        value = json.dumps([source, external_key, workspace_id], separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _credential_metadata(self, integration_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT id,name,provider,scopes,fingerprint,status,last_verified_at,created_at,updated_at,revoked_at,generation
            FROM secret_bindings WHERE integration_id=? ORDER BY created_at DESC LIMIT 1""", (integration_id,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["scopes"] = json.loads(item["scopes"])
        return item

    def _record_failure(self, integration_id: str, health: str, error: str, status: str | None = None) -> None:
        if status is None:
            self.conn.execute("UPDATE integrations SET health=?,last_error=? WHERE id=?", (health, error, integration_id))
        else:
            self.conn.execute(
                "UPDATE integrations SET status=?,health=?,last_error=? WHERE id=?",
                (status, health, error, integration_id),
            )
        self.conn.commit()

    @staticmethod
    def _verified_account(source: str, value: Any) -> tuple[str, str, tuple[str, ...]]:
        if isinstance(value, dict):
            account_id=str(value.get("account_id") or value.get("team_id") or "")
            account_name=str(value.get("account_name") or value.get("team_name") or "")
            granted=tuple(str(item) for item in value.get("granted_permissions",()))
            return account_id,account_name,granted
        if source == "slack":
            return value.team_id,value.team_name,tuple(value.granted_scopes)
        if source == "clickup":
            return value.team_id,value.team_name,("authorized_team",)
        if source == "google_drive":
            return value.account_id,value.display_name,tuple(
                getattr(value, "granted_scopes", getattr(value, "scopes", ()))
            )
        if source == "gmail":
            return value.email_address,value.email_address,tuple(
                getattr(value, "granted_scopes", getattr(value, "scopes", ()))
            )
        if source == "figma":
            return value.user_id,value.email or value.user_id,tuple(value.granted_permissions)
        if source == "fireflies":
            return value.user_id,value.email or value.user_id,tuple(value.granted_scopes)
        raise ValidationError("unsupported provider identity")

    @staticmethod
    def _validate_permissions(source: str, permissions: set[str]) -> None:
        if source == "slack" and not permissions.intersection(
            {"channels:history", "groups:history", "im:history", "mpim:history"}
        ):
            raise ValidationError("Slack synchronization requires an explicit conversation history permission")
        if source == "clickup" and permissions != {"authorized_team"}:
            raise ValidationError("ClickUp synchronization requires authorized_team permission")
        if source == "google_drive" and GOOGLE_DRIVE_READ_SCOPE not in permissions:
            raise ValidationError("Google Drive synchronization requires drive.readonly permission")
        if source == "gmail" and GMAIL_READ_SCOPE not in permissions:
            raise ValidationError("Gmail synchronization requires gmail.readonly permission")
        if source == "figma" and (
            not FIGMA_REQUIRED_PERMISSIONS.issubset(permissions)
            or not permissions.issubset(FIGMA_REQUIRED_PERMISSIONS | FIGMA_OPTIONAL_PERMISSIONS)
        ):
            raise ValidationError("Figma synchronization requires current_user:read, file_metadata:read, and file_content:read")
        if source == "fireflies" and permissions != FIREFLIES_REQUIRED_SCOPES:
            raise ValidationError("Fireflies synchronization requires transcripts:read permission")

    @staticmethod
    def _validate_mapping_keys(source: str, workspace_mappings: dict[str, str]) -> None:
        keys = {str(key).strip() for key in workspace_mappings}
        if source == "google_drive":
            invalid = [key for key in keys if not (
                key.startswith("folder:") and key[7:].strip()
                or key.startswith("drive:") and key[6:].strip()
            )]
            if invalid:
                raise ValidationError("Google Drive mappings must use folder:<id> or drive:<id>")
        if source == "gmail" and any(not key.startswith("label:") or not key[6:].strip() for key in keys):
            raise ValidationError("Gmail mappings must use label:<id>")
        if source == "figma" and any(not key.startswith("file:") or not key[5:].strip() for key in keys):
            raise ValidationError("Figma mappings must use file:<key>")
        if source == "fireflies":
            if len(keys) != 1 or any(not key.startswith("account:") or not key[8:].strip() for key in keys):
                raise ValidationError("Fireflies requires exactly one account:<id> mapping")

    @staticmethod
    def _canonicalize_mappings(workspace_mappings: dict[str, str]) -> dict[str, str]:
        if not isinstance(workspace_mappings, dict) or not workspace_mappings:
            raise ValidationError("at least one external-container to workspace mapping is required")
        canonical: dict[str, str] = {}
        for raw_key, raw_workspace_id in workspace_mappings.items():
            if not isinstance(raw_key, str) or not isinstance(raw_workspace_id, str):
                raise ValidationError("mapping keys and workspace IDs must be strings")
            key = raw_key.strip()
            workspace_id = raw_workspace_id.strip()
            if not key or not workspace_id:
                raise ValidationError("mapping keys and workspace IDs must be non-empty")
            if key in canonical:
                raise ValidationError("mapping keys must be unique after normalization")
            canonical[key] = workspace_id
        return canonical

    @staticmethod
    def _canonicalize_permissions(permissions: list[str]) -> set[str]:
        if not isinstance(permissions, list):
            raise ValidationError("permissions must be a list of non-empty strings")
        canonical: set[str] = set()
        for value in permissions:
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("permissions must contain only non-empty strings")
            canonical.add(value.strip())
        return canonical

    @staticmethod
    def _normalize_expected_account_id(source: str, expected_account_id: str) -> str:
        if not isinstance(expected_account_id, str):
            return ""
        value = expected_account_id.strip()
        if source == "google_drive" and "@" in value:
            raise ValidationError("Google Drive expected account ID must be the stable permissionId, not an email")
        return value.casefold() if source == "gmail" else value

    @staticmethod
    def _account_ids_match(source: str, actual: str, expected: str) -> bool:
        if source == "gmail":
            return str(actual).strip().casefold() == str(expected).strip().casefold()
        return str(actual).strip() == str(expected).strip()

    @staticmethod
    def _missing_permissions(source: str, required: set[str], granted: set[str]) -> set[str]:
        missing = set(required) - set(granted)
        if source == "google_drive" and GOOGLE_DRIVE_READ_SCOPE in missing and granted.intersection({
            GOOGLE_DRIVE_READ_SCOPE, "https://www.googleapis.com/auth/drive"
        }):
            missing.remove(GOOGLE_DRIVE_READ_SCOPE)
        if source == "gmail" and GMAIL_READ_SCOPE in missing and granted.intersection({
            GMAIL_READ_SCOPE,
            "https://www.googleapis.com/auth/gmail.modify",
            "https://mail.google.com/",
        }):
            missing.remove(GMAIL_READ_SCOPE)
        return missing

    @staticmethod
    def _transport_failure_state(exc: ConnectorTransportError) -> tuple[str, str | None]:
        health = "rate_limited" if exc.status == 429 else "degraded"
        status = "reauth_required" if exc.status == 401 else (
            "action_required" if exc.status == 403 else None
        )
        return health, status

    @staticmethod
    def _stable_error(exc: Exception) -> str:
        if isinstance(exc, ConnectorTransportError):
            return f"connector_transport:{exc.status or 'network'}"
        return exc.__class__.__name__

    @staticmethod
    def _observed_at(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _public_identity(value: Any) -> Any:
        def json_safe(item: Any) -> Any:
            if hasattr(item,"__dict__"):
                return json_safe(dict(item.__dict__))
            if isinstance(item,dict):
                return {str(key):json_safe(entry) for key,entry in item.items()}
            if isinstance(item,(set,frozenset)):
                return sorted(json_safe(entry) for entry in item)
            if isinstance(item,(list,tuple)):
                return [json_safe(entry) for entry in item]
            return item
        return redact(json_safe(value))
