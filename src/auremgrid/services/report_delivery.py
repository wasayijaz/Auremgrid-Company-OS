from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except ValueError:
        return default


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


_INTERNAL_KEYS = {
    "citations", "evidence", "evidence_refs", "source_refs", "source_id",
    "source", "source_locator", "internal_notes", "reasoning", "debug", "trace",
}


class ReportDeliveryService:
    """Portal-only client report delivery over immutable approved snapshots."""

    def __init__(self, os: Any, new_id: Callable[[str], str]) -> None:
        self.os = os
        self.conn = os.store.conn
        self.new_id = new_id

    def publish(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        report_run_id: str,
        approval_request_id: str,
        title: str,
        reason: str = "",
    ) -> dict[str, Any]:
        self._require_staff(identity, organization_id, workspace_id)
        title = self._required(title, "title")
        reason = reason.strip() or "published to client portal"
        report = self._report_run(organization_id, workspace_id, report_run_id)
        approval = self._approval(organization_id, workspace_id, approval_request_id, report_run_id)
        if approval["status"] != "approved":
            raise ValidationError("approved human approval is required before portal publication")
        snapshot = self._client_snapshot(report, title)
        prior = self._current_version(organization_id, workspace_id, str(report["type"]))
        now = _now()
        item = {
            "id": self.new_id("portalreport"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "report_run_id": report_run_id,
            "version": self._next_version(organization_id, workspace_id, str(report["type"])),
            "title": title,
            "report_type": report["type"],
            "snapshot_json": _json(snapshot),
            "content_hash": _hash(snapshot),
            "approval_request_id": approval_request_id,
            "published_by_person_id": identity.person_id,
            "supersedes_version_id": prior["id"] if prior else None,
            "created_at": now,
        }
        self.conn.execute(
            """INSERT INTO portal_report_versions(
                id,organization_id,workspace_id,report_run_id,version,title,report_type,snapshot_json,
                content_hash,approval_request_id,published_by_person_id,supersedes_version_id,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        if prior is not None:
            self._event(identity.person_id, "staff", organization_id, workspace_id, prior["id"], "superseded", item["id"])
        self._event(identity.person_id, "staff", organization_id, workspace_id, item["id"], "published", reason)
        self._ledger(identity.person_id, organization_id, workspace_id, "publish", item["id"], reason)
        self.conn.commit()
        return self._version_dict(item, include_snapshot=True)

    def revoke(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        portal_report_version_id: str,
        reason: str,
    ) -> dict[str, Any]:
        self._require_staff(identity, organization_id, workspace_id)
        reason = self._required(reason, "reason")
        version = self._version_row(organization_id, workspace_id, portal_report_version_id)
        if self._is_revoked(version["id"]):
            return self._version_dict(version, include_snapshot=True)
        self._event(identity.person_id, "staff", organization_id, workspace_id, version["id"], "revoked", reason)
        self._ledger(identity.person_id, organization_id, workspace_id, "revoke", version["id"], reason)
        self.conn.commit()
        return self._version_dict(version, include_snapshot=True)

    def staff_list(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        self._require_staff(identity, organization_id, workspace_id, read_only=True)
        rows = self.conn.execute(
            """SELECT * FROM portal_report_versions
               WHERE organization_id=? AND workspace_id=?
               ORDER BY created_at DESC,id DESC""",
            (organization_id, workspace_id),
        ).fetchall()
        return [self._version_dict(row, include_snapshot=False) for row in rows]

    def portal_list(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        self._require_client(identity, organization_id, workspace_id)
        return [self._client_summary(row) for row in self._visible_client_rows(organization_id, workspace_id)]

    def portal_view(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        portal_report_version_id: str,
    ) -> dict[str, Any]:
        self._require_client(identity, organization_id, workspace_id)
        row = self._visible_version_row(organization_id, workspace_id, portal_report_version_id)
        self._event(identity.person_id, "client", organization_id, workspace_id, row["id"], "viewed", "client portal view")
        self.conn.commit()
        return self._client_detail(row, "view")

    def portal_download(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        portal_report_version_id: str,
    ) -> dict[str, Any]:
        self._require_client(identity, organization_id, workspace_id)
        row = self._visible_version_row(organization_id, workspace_id, portal_report_version_id)
        self._event(identity.person_id, "client", organization_id, workspace_id, row["id"], "downloaded", "client portal download")
        self.conn.commit()
        return self._client_detail(row, "download")

    def _client_snapshot(self, report: Any, title: str) -> dict[str, Any]:
        payload = self._sanitize(_decode(report["payload"], {}))
        return {
            "title": title,
            "report_type": report["type"],
            "generated_at": report["generated_at"],
            "payload": payload,
            "portal_only": True,
        }

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                if str(key) in _INTERNAL_KEYS or str(key).endswith("_evidence"):
                    continue
                output[str(key)] = self._sanitize(item)
            return output
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        return value

    def _visible_client_rows(self, organization_id: str, workspace_id: str) -> list[Any]:
        rows = self.conn.execute(
            """SELECT * FROM portal_report_versions
               WHERE organization_id=? AND workspace_id=?
               ORDER BY report_type, created_at DESC, id DESC""",
            (organization_id, workspace_id),
        ).fetchall()
        visible: list[Any] = []
        seen_types: set[str] = set()
        for row in rows:
            report_type = str(row["report_type"])
            if report_type in seen_types or self._is_revoked(str(row["id"])) or self._is_superseded(str(row["id"])):
                continue
            seen_types.add(report_type)
            visible.append(row)
        return sorted(visible, key=lambda item: (item["created_at"], item["id"]), reverse=True)

    def _visible_version_row(self, organization_id: str, workspace_id: str, version_id: str) -> Any:
        row = self._version_row(organization_id, workspace_id, version_id)
        if self._is_revoked(version_id) or self._is_superseded(version_id):
            raise NotFoundError("portal report not found")
        return row

    def _current_version(self, organization_id: str, workspace_id: str, report_type: str) -> Any:
        rows = self.conn.execute(
            """SELECT * FROM portal_report_versions
               WHERE organization_id=? AND workspace_id=? AND report_type=?
               ORDER BY version DESC,created_at DESC,id DESC""",
            (organization_id, workspace_id, report_type),
        ).fetchall()
        for row in rows:
            if not self._is_revoked(str(row["id"])) and not self._is_superseded(str(row["id"])):
                return row
        return None

    def _is_revoked(self, version_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM portal_report_events WHERE portal_report_version_id=? AND action='revoked' LIMIT 1",
            (version_id,),
        ).fetchone() is not None

    def _is_superseded(self, version_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM portal_report_events WHERE portal_report_version_id=? AND action='superseded' LIMIT 1",
            (version_id,),
        ).fetchone() is not None

    def _report_run(self, organization_id: str, workspace_id: str, report_run_id: str) -> Any:
        row = self.conn.execute(
            "SELECT * FROM report_runs WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, report_run_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("report run not found")
        if row["status"] != "completed":
            raise ValidationError("only completed report runs can be published")
        return row

    def _approval(self, organization_id: str, workspace_id: str, approval_request_id: str, report_run_id: str) -> Any:
        row = self.conn.execute(
            "SELECT * FROM approval_requests WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, approval_request_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("approval request not found")
        if row["policy"] == "auto":
            raise ValidationError("portal report publication requires human approval")
        if row["action_type"] != "report.portal_publish":
            raise ValidationError("approval action must be report.portal_publish")
        payload = _decode(row["payload"], {})
        if payload.get("report_run_id") != report_run_id:
            raise ValidationError("approval does not match report run")
        return row

    def _version_row(self, organization_id: str, workspace_id: str, version_id: str) -> Any:
        row = self.conn.execute(
            "SELECT * FROM portal_report_versions WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, version_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("portal report not found")
        return row

    def _next_version(self, organization_id: str, workspace_id: str, report_type: str) -> int:
        row = self.conn.execute(
            """SELECT COALESCE(MAX(version),0)+1 AS next_version FROM portal_report_versions
               WHERE organization_id=? AND workspace_id=? AND report_type=?""",
            (organization_id, workspace_id, report_type),
        ).fetchone()
        return int(row["next_version"] if row else 1)

    def _event(
        self, person_id: str, actor_role: str, organization_id: str, workspace_id: str,
        version_id: str, action: str, reason: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO portal_report_events(
                id,organization_id,workspace_id,portal_report_version_id,action,
                actor_person_id,actor_role,reason,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (self.new_id("portalreportevent"), organization_id, workspace_id, version_id, action, person_id, actor_role, reason, _now()),
        )

    def _ledger(self, person_id: str, organization_id: str, workspace_id: str, action: str, entity_id: str, detail: str) -> None:
        self.conn.execute(
            """INSERT INTO ledger_audit(
                id,organization_id,workspace_id,principal_type,principal_id,action,entity_type,entity_id,detail,recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (self.new_id("audit"), organization_id, workspace_id, "person", person_id, action, "portal_report", entity_id, detail, _now()),
        )

    def _version_dict(self, row: Any, include_snapshot: bool) -> dict[str, Any]:
        data = dict(row)
        data["status"] = "revoked" if self._is_revoked(str(row["id"])) else "superseded" if self._is_superseded(str(row["id"])) else "published"
        data["snapshot"] = _decode(data.pop("snapshot_json", "{}"), {}) if include_snapshot else None
        data["events"] = [dict(event) for event in self.conn.execute(
            "SELECT * FROM portal_report_events WHERE portal_report_version_id=? ORDER BY created_at,id",
            (row["id"],),
        ).fetchall()]
        return data

    def _client_summary(self, row: Any) -> dict[str, Any]:
        snapshot = _decode(row["snapshot_json"], {})
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "title": row["title"],
            "report_type": row["report_type"],
            "version": row["version"],
            "published_at": row["created_at"],
            "generated_at": snapshot.get("generated_at"),
            "allowed_actions": [
                {"action": "view_report", "label": "View report", "method": "GET", "route": "/client-portal/reports/view", "payload": {"portal_report_version_id": row["id"]}},
                {"action": "download_report", "label": "Download report", "method": "GET", "route": "/client-portal/reports/download", "payload": {"portal_report_version_id": row["id"]}},
            ],
        }

    def _client_detail(self, row: Any, mode: str) -> dict[str, Any]:
        return {**self._client_summary(row), "mode": mode, "snapshot": _decode(row["snapshot_json"], {})}

    def _require_staff(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str, read_only: bool = False
    ) -> None:
        if identity.organization_id != organization_id:
            raise AuthorizationError("identity scope mismatch")
        if identity.workspace_id not in {None, workspace_id}:
            raise AuthorizationError("identity scope mismatch")
        identity.require("workspace_read" if read_only else "workspace_write")
        self.os._require_person_access(organization_id, workspace_id, identity.person_id, write=not read_only)
        membership = self.os.company.workspace_membership(workspace_id, identity.person_id)
        if membership is None or membership.role == "client":
            raise AuthorizationError("staff workspace membership required")

    def _require_client(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str) -> None:
        if identity.organization_id != organization_id:
            raise AuthorizationError("identity scope mismatch")
        scoped = self.os.auth.scope_identity(identity, workspace_id)
        scoped.require("client_portal")
        self.os.client_portal._require_client_membership(organization_id, workspace_id, scoped.person_id)

    @staticmethod
    def _required(value: str, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{label} is required")
        return value.strip()
