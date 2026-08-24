"""Scoped asset registry, independent backup verification, and recovery plans."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlparse, urlunparse

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.secrets import redact
from auremgrid.storage.backup import verify_backup

_SECRET_QUERY = re.compile(r"(token|secret|signature|sig|access[_-]?key|credential|auth|expires)", re.I)
_RETENTION = {"ephemeral", "standard", "critical", "legal_hold"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _scope(identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None, capability: str) -> None:
    if identity.organization_id != organization_id:
        raise AuthorizationError("identity scope mismatch")
    if workspace_id is not None and identity.workspace_id not in {None, workspace_id}:
        raise AuthorizationError("identity workspace mismatch")
    identity.require(capability)


def validate_locator(locator: str) -> str:
    """Allow stable paths/URLs while refusing signed or credential-bearing URLs."""
    value = str(locator or "").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValidationError("asset locator is required")
    # Windows drive paths (``C:\\...``) are local paths, not URL schemes.
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return value
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        if not parsed.netloc:
            raise ValidationError("asset locator URL is invalid")
        if any(_SECRET_QUERY.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValidationError("signed or credential-bearing URLs cannot be persisted")
        if parsed.fragment:
            raise ValidationError("asset locator fragments are not stable")
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, parsed.query, ""))
    if parsed.scheme:
        if parsed.scheme not in {"s3", "gs", "az", "file"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValidationError("asset locator scheme is unsupported or credential-bearing")
        return value
    if value.startswith("//"):
        raise ValidationError("asset locator must be an explicit path or URL")
    return value


class AssetRecoveryService:
    def __init__(self, conn: Any, new_id: Callable[[str], str]) -> None:
        self.conn, self.new_id = conn, new_id

    def _read_scope(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str) -> None:
        """Authorize a workspace-scoped, metadata-only registry read."""
        if identity.organization_id != organization_id:
            raise AuthorizationError("identity scope mismatch")
        if identity.workspace_id not in {None, workspace_id}:
            raise AuthorizationError("identity workspace mismatch")
        identity.require("workspace_read")

    @staticmethod
    def _asset_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except (TypeError, ValueError):
            item["metadata"] = {}
            item.pop("metadata_json", None)
        # Keep the canonical digest name and provide an explicit API alias.
        item["checksum"] = item.get("sha256")
        return item

    def _review_status(self, organization_id: str, workspace_id: str, asset_id: str) -> dict[str, Any]:
        """Return creative review state when this registry id has a creative record."""
        row = self.conn.execute(
            "SELECT id,approval_state,reviewer_person_id,revision_count FROM creative_assets "
            "WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, asset_id),
        ).fetchone()
        if row is None:
            return {"review_status": None, "reviewer_person_id": None, "review_event_count": 0}
        count = self.conn.execute(
            "SELECT COUNT(*) FROM creative_review_events WHERE organization_id=? AND workspace_id=? AND asset_id=?",
            (organization_id, workspace_id, asset_id),
        ).fetchone()[0]
        return {
            "review_status": row["approval_state"],
            "reviewer_person_id": row["reviewer_person_id"],
            "review_event_count": int(count),
            "revision_count": row["revision_count"],
        }

    def list_assets(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        status: str | None = None,
        retention_class: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._read_scope(identity, organization_id, workspace_id)
        if not 1 <= int(limit) <= 200:
            raise ValidationError("limit must be between 1 and 200")
        sql = "SELECT * FROM asset_registry WHERE organization_id=? AND workspace_id=?"
        args: list[Any] = [organization_id, workspace_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        if retention_class:
            if retention_class not in _RETENTION:
                raise ValidationError("invalid retention class")
            sql += " AND retention_class=?"
            args.append(retention_class)
        sql += " ORDER BY updated_at DESC,id LIMIT ?"
        args.append(int(limit))
        rows = self.conn.execute(sql, args).fetchall()
        return [{**self._asset_row(row), **self._review_status(organization_id, workspace_id, row["id"])} for row in rows]

    def asset_detail(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        asset_id: str,
    ) -> dict[str, Any]:
        self._read_scope(identity, organization_id, workspace_id)
        row = self.conn.execute(
            "SELECT * FROM asset_registry WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, asset_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("asset not found")
        audit_rows = self.conn.execute(
            "SELECT id,actor_id,action,entity_type,entity_id,detail_json,created_at "
            "FROM asset_recovery_audit WHERE organization_id=? AND workspace_id=? AND entity_type='asset' AND entity_id=? "
            "ORDER BY created_at,id",
            (organization_id, workspace_id, asset_id),
        ).fetchall()
        audit: list[dict[str, Any]] = []
        for event in audit_rows:
            item = dict(event)
            try:
                item["detail"] = json.loads(item.pop("detail_json") or "{}")
            except (TypeError, ValueError):
                item["detail"] = {}
                item.pop("detail_json", None)
            audit.append(item)
        return {
            "asset": {**self._asset_row(row), **self._review_status(organization_id, workspace_id, asset_id)},
            "recovery_audit": audit,
        }

    def _audit(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None, action: str, entity_type: str, entity_id: str, detail: dict[str, Any] | None = None) -> None:
        self.conn.execute("INSERT INTO asset_recovery_audit VALUES (?,?,?,?,?,?,?,?,?)", (self.new_id("asset_audit"), organization_id, workspace_id, identity.person_id, action, entity_type, entity_id, json.dumps(redact(detail or {}), sort_keys=True, separators=(",", ":")), _now()))

    def register_asset(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None, name: str, asset_type: str, locator: str, retention_class: str = "standard", metadata: dict[str, Any] | None = None, size_bytes: int | None = None, sha256: str | None = None) -> dict[str, Any]:
        _scope(identity, organization_id, workspace_id, "backup_create")
        if not name.strip() or not asset_type.strip():
            raise ValidationError("asset name and type are required")
        if retention_class not in _RETENTION:
            raise ValidationError("invalid retention class")
        safe_locator = validate_locator(locator)
        safe_meta = redact(metadata or {})
        if safe_meta != (metadata or {}):
            raise ValidationError("asset metadata contains credential material")
        local_path = re.match(r"^[A-Za-z]:[\\/]", safe_locator) or not urlparse(safe_locator).scheme
        path = Path(safe_locator) if local_path else None
        if path is not None and path.is_file():
            digest = hashlib.sha256(); size = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(chunk); digest.update(chunk)
            digest_value = digest.hexdigest()
        else:
            if size_bytes is None or sha256 is None:
                raise ValidationError("external assets require size_bytes and sha256 metadata")
            size, digest_value = int(size_bytes), str(sha256).lower()
            if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", digest_value):
                raise ValidationError("size_bytes or sha256 metadata is invalid")
        now = _now(); item = {"id": self.new_id("asset"), "organization_id": organization_id, "workspace_id": workspace_id, "name": name.strip(), "asset_type": asset_type.strip(), "size_bytes": size, "sha256": digest_value, "locator": safe_locator, "retention_class": retention_class, "metadata_json": json.dumps(safe_meta, sort_keys=True, separators=(",", ":")), "status": "active", "created_at": now, "updated_at": now}
        with self.conn:
            self.conn.execute("INSERT INTO asset_registry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values()))
            self._audit(identity, organization_id, workspace_id, "register", "asset", item["id"], {"sha256": item["sha256"], "size_bytes": size, "retention_class": retention_class})
        return {**item, "metadata": safe_meta}

    def record_backup(self, identity: AuthenticatedIdentity, organization_id: str, locator: str) -> dict[str, Any]:
        _scope(identity, organization_id, None, "backup_create")
        safe_locator = validate_locator(locator)
        path = Path(safe_locator)
        if not path.is_file():
            raise ValidationError("backup locator must point to a local backup file")
        verification = verify_backup(path)
        now = _now(); item = {"id": self.new_id("backup_manifest"), "organization_id": organization_id, "locator": safe_locator, "sha256": verification["sha256"], "size_bytes": verification["size_bytes"], "schema_version": verification["schema_version"], "integrity": verification["integrity"], "manifest_json": json.dumps(verification, sort_keys=True, separators=(",", ":")), "status": "verified", "created_at": now, "verified_at": now}
        with self.conn:
            self.conn.execute("INSERT INTO backup_manifests VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values()))
            self._audit(identity, organization_id, None, "record_verified", "backup_manifest", item["id"], {"sha256": item["sha256"], "size_bytes": item["size_bytes"]})
        return item

    def verify_manifest(self, identity: AuthenticatedIdentity, organization_id: str, manifest_id: str) -> dict[str, Any]:
        _scope(identity, organization_id, None, "backup_restore")
        row = self.conn.execute("SELECT * FROM backup_manifests WHERE id=? AND organization_id=?", (manifest_id, organization_id)).fetchone()
        if row is None: raise NotFoundError("backup manifest not found")
        verification = verify_backup(row["locator"], row["sha256"])
        now = _now()
        with self.conn:
            self.conn.execute("UPDATE backup_manifests SET status='verified',verified_at=?,integrity=?,size_bytes=?,manifest_json=? WHERE id=?", (now, verification["integrity"], verification["size_bytes"], json.dumps(verification, sort_keys=True, separators=(",", ":")), manifest_id))
            self._audit(identity, organization_id, None, "verify", "backup_manifest", manifest_id, {"sha256": verification["sha256"], "size_bytes": verification["size_bytes"]})
        return dict(self.conn.execute("SELECT * FROM backup_manifests WHERE id=?", (manifest_id,)).fetchone())

    def create_recovery_plan(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None, backup_manifest_id: str, external_provider: str, target_locator: str, rpo_minutes: int, rto_minutes: int, notes: str = "") -> dict[str, Any]:
        _scope(identity, organization_id, workspace_id, "backup_restore")
        if not external_provider.strip() or rpo_minutes < 0 or rto_minutes < 0:
            raise ValidationError("provider and non-negative RPO/RTO are required")
        safe_target = validate_locator(target_locator)
        if self.conn.execute("SELECT 1 FROM backup_manifests WHERE id=? AND organization_id=? AND status='verified'", (backup_manifest_id, organization_id)).fetchone() is None:
            raise ValidationError("a verified backup manifest is required")
        now = _now(); item = {"id": self.new_id("recovery_plan"), "organization_id": organization_id, "workspace_id": workspace_id, "backup_manifest_id": backup_manifest_id, "external_provider": external_provider.strip(), "target_locator": safe_target, "rpo_minutes": int(rpo_minutes), "rto_minutes": int(rto_minutes), "status": "planned", "notes": notes.strip(), "created_at": now, "updated_at": now}
        with self.conn:
            self.conn.execute("INSERT INTO recovery_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values()))
            self._audit(identity, organization_id, workspace_id, "create", "recovery_plan", item["id"], {"provider": item["external_provider"], "status": item["status"]})
        return item

    def update_recovery_status(self, identity: AuthenticatedIdentity, organization_id: str, plan_id: str, status: str, detail: str = "") -> dict[str, Any]:
        _scope(identity, organization_id, None, "backup_restore")
        if status not in {"planned", "ready", "executing", "completed", "failed", "blocked"}:
            raise ValidationError("invalid recovery status")
        row = self.conn.execute("SELECT * FROM recovery_plans WHERE id=? AND organization_id=?", (plan_id, organization_id)).fetchone()
        if row is None: raise NotFoundError("recovery plan not found")
        now = _now()
        with self.conn:
            self.conn.execute("UPDATE recovery_plans SET status=?,notes=?,updated_at=? WHERE id=?", (status, detail.strip(), now, plan_id))
            self._audit(identity, organization_id, row["workspace_id"], "status", "recovery_plan", plan_id, {"status": status})
        return dict(self.conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone())
