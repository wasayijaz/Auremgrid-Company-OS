"""Agency asset layer: media assets, versions, and rich review threads."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain_shared import new_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssetLayerService:
    APPROVAL_STATES = ("draft", "in_review", "approved", "revision_requested", "rejected")
    ASSET_KINDS = ("image", "video", "document", "deck", "file", "link")
    THREAD_KINDS = ("region", "timestamp", "page", "general")
    APPROVAL_TRANSITIONS = {
        "draft": {"in_review"},
        "in_review": {"approved", "revision_requested", "rejected"},
        "revision_requested": {"in_review", "draft"},
        "rejected": {"draft"},
        "approved": set(),
    }

    def __init__(self, company_os: Any) -> None:
        self.os = company_os
        self.conn = company_os.store.conn

    def _scope(self, os_scope: Any) -> tuple[str, str | None, str | None]:
        scope = os_scope if isinstance(os_scope, dict) else dict(os_scope)
        organization_id = scope.get("organization_id")
        if not organization_id:
            raise ValidationError("organization scope is required")
        return str(organization_id), scope.get("workspace_id"), scope.get("person_id")

    def _authorize(self, organization_id: str, workspace_id: str | None, person_id: str | None) -> None:
        self.os._require_person_access(organization_id, workspace_id, person_id)

    def _asset(self, organization_id: str, asset_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM agency_assets WHERE id=? AND organization_id=?", (asset_id, organization_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("asset not found")
        return dict(row)

    def register_asset(self, os_scope: Any, *, title: str, asset_kind: str, locator: str,
                       workspace_id: str | None = None, storage_provider: str = "local",
                       mime_type: str | None = None, checksum_sha256: str | None = None,
                       project_id: str | None = None, campaign_id: str | None = None,
                       deliverable_id: str | None = None) -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope)
        self._authorize(organization_id, workspace_id, person_id)
        if asset_kind not in self.ASSET_KINDS:
            raise ValidationError("unknown asset kind")
        if not title or not locator:
            raise ValidationError("title and locator are required")
        if not person_id:
            raise ValidationError("person scope is required to register assets")
        asset_id = new_id("agency_asset")
        now = _now()
        self.conn.execute(
            "INSERT INTO agency_assets (id,organization_id,workspace_id,title,asset_kind,storage_provider,locator,"
            "mime_type,checksum_sha256,project_id,campaign_id,deliverable_id,creator_person_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (asset_id, organization_id, workspace_id, title, asset_kind, storage_provider, locator,
             mime_type, checksum_sha256, project_id, campaign_id, deliverable_id, person_id, now, now),
        )
        self.conn.execute(
            "INSERT INTO agency_asset_versions (id,asset_id,version,storage_provider,locator,mime_type,"
            "checksum_sha256,dimensions,duration_seconds,preview_url,notes,created_by_person_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("agency_asset_version"), asset_id, 1, storage_provider, locator, mime_type, checksum_sha256,
             None, None, None, "initial", str(person_id), now),
        )
        self.os.store.conn.commit()
        return self._asset(organization_id, asset_id)

    def add_version(self, os_scope: Any, asset_id: str, *, locator: str, mime_type: str | None = None,
                    checksum_sha256: str | None = None, dimensions: str | None = None,
                    duration_seconds: float | None = None, preview_url: str | None = None,
                    notes: str = "") -> dict[str, Any]:
        organization_id, _, person_id = self._scope(os_scope)
        asset = self._asset(organization_id, asset_id)
        if not person_id:
            raise ValidationError("person scope is required to add versions")
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS nxt FROM agency_asset_versions WHERE asset_id=?", (asset_id,)
        ).fetchone()
        version = int(row["nxt"])
        version_id = new_id("agency_asset_version")
        self.conn.execute(
            "INSERT INTO agency_asset_versions (id,asset_id,version,storage_provider,locator,mime_type,"
            "checksum_sha256,dimensions,duration_seconds,preview_url,notes,created_by_person_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, asset_id, version, asset["storage_provider"], locator, mime_type, checksum_sha256,
             dimensions, duration_seconds, preview_url, notes, str(person_id), _now()),
        )
        self.conn.execute("UPDATE agency_assets SET updated_at=? WHERE id=?", (_now(), asset_id))
        self.os.store.conn.commit()
        return dict(self.conn.execute("SELECT * FROM agency_asset_versions WHERE id=?", (version_id,)).fetchone())

    def set_approval(self, os_scope: Any, asset_id: str, *, state: str) -> dict[str, Any]:
        organization_id, _, person_id = self._scope(os_scope)
        asset = self._asset(organization_id, asset_id)
        if state not in self.APPROVAL_STATES:
            raise ValidationError("unknown approval state")
        if state not in self.APPROVAL_TRANSITIONS[asset["approval_state"]]:
            raise ValidationError("invalid approval transition")
        reviewer = person_id or asset["reviewer_person_id"]
        self.conn.execute(
            "UPDATE agency_assets SET approval_state=?, reviewer_person_id=?, updated_at=? WHERE id=?",
            (state, reviewer, _now(), asset_id),
        )
        self.os.store.conn.commit()
        return self._asset(organization_id, asset_id)

    def versions(self, os_scope: Any, asset_id: str) -> list[dict[str, Any]]:
        organization_id, _, _ = self._scope(os_scope)
        self._asset(organization_id, asset_id)
        rows = self.conn.execute(
            "SELECT * FROM agency_asset_versions WHERE asset_id=? ORDER BY version", (asset_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def compare_versions(self, os_scope: Any, asset_id: str, *, from_version: int, to_version: int) -> dict[str, Any]:
        all_versions = {v["version"]: v for v in self.versions(os_scope, asset_id)}
        if from_version not in all_versions or to_version not in all_versions:
            raise NotFoundError("version not found")
        a, b = all_versions[from_version], all_versions[to_version]
        fields = ("locator", "mime_type", "checksum_sha256", "dimensions", "duration_seconds", "preview_url")
        return {"from_version": from_version, "to_version": to_version,
                "changes": {f: (a[f], b[f]) for f in fields if a[f] != b[f]}}

    def create_thread(self, os_scope: Any, asset_id: str, *, kind: str, body: str,
                      version_id: str | None = None, anchor: dict[str, Any] | None = None) -> dict[str, Any]:
        organization_id, _, person_id = self._scope(os_scope)
        self._asset(organization_id, asset_id)
        if kind not in self.THREAD_KINDS:
            raise ValidationError("unknown thread kind")
        if not person_id:
            raise ValidationError("person scope is required to comment")
        if not body:
            raise ValidationError("comment body is required")
        thread_id = new_id("asset_thread")
        now = _now()
        self.conn.execute(
            "INSERT INTO asset_review_threads (id,organization_id,asset_id,version_id,kind,anchor_json,body,"
            "author_person_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (thread_id, organization_id, asset_id, version_id, kind, json.dumps(anchor or {}), body,
             str(person_id), "open", now, now),
        )
        self.os.store.conn.commit()
        return dict(self.conn.execute("SELECT * FROM asset_review_threads WHERE id=?", (thread_id,)).fetchone())

    def set_thread_status(self, os_scope: Any, thread_id: str, *, status: str) -> dict[str, Any]:
        organization_id, _, _ = self._scope(os_scope)
        row = self.conn.execute(
            "SELECT * FROM asset_review_threads WHERE id=? AND organization_id=?", (thread_id, organization_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("thread not found")
        allowed = {"open": {"resolved"}, "resolved": {"reopened"}, "reopened": {"resolved"}}
        if status not in allowed[row["status"]]:
            raise ValidationError("invalid thread transition")
        self.conn.execute("UPDATE asset_review_threads SET status=?, updated_at=? WHERE id=?", (status, _now(), thread_id))
        self.os.store.conn.commit()
        return dict(self.conn.execute("SELECT * FROM asset_review_threads WHERE id=?", (thread_id,)).fetchone())
