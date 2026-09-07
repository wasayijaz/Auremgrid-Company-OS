"""Offline, organization-scoped client HQ read projection."""
from __future__ import annotations

from typing import Any, Mapping

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


class ClientHQReadService:
    """Compose existing read models into one deterministic client HQ view."""

    def __init__(self, company_os: Any, *, calendar_service: Any | None = None) -> None:
        self.os = company_os
        self.calendar = calendar_service
        self.conn = company_os.store.conn

    def get_client_hq(self, os_scope: Any, client_id: str) -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope, client_id)
        self._authorize(organization_id, workspace_id, person_id)
        workspace = self.conn.execute(
            """SELECT w.* FROM workspaces w JOIN workspace_organization wo ON wo.workspace_id=w.id
               WHERE w.id=? AND wo.organization_id=? AND wo.kind='client'""",
            (workspace_id, organization_id),
        ).fetchone()
        if workspace is None:
            raise NotFoundError("client workspace was not found")

        retainer = self._retainer(organization_id, workspace_id, person_id)
        health = self._health(organization_id, workspace_id, person_id)
        work_items = [item.to_dict() for item in self.os.store.list_work_items(workspace_id, open_only=True)]
        deliverables = [item.to_dict() for item in self.os.company.list_deliverables(workspace_id)][:10]
        reports = self._reports(organization_id, workspace_id)
        meetings = self._meetings(organization_id, workspace_id)
        return {
            "client_id": client_id,
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "client_name": str(workspace["name"] or ""),
            "retainer": retainer,
            "health": health,
            "open_work_items": work_items,
            "recent_deliverables": deliverables,
            "recent_reports": reports,
            "upcoming_meetings": meetings,
        }

    def list_client_hq_summaries(self, os_scope: Any) -> list[dict[str, Any]]:
        organization_id, scope_workspace, person_id = self._scope(os_scope, None)
        params: list[Any] = [organization_id]
        query = """SELECT w.id,w.name FROM workspaces w JOIN workspace_organization wo ON wo.workspace_id=w.id
                   WHERE wo.organization_id=? AND wo.kind='client'"""
        if scope_workspace:
            query += " AND w.id=?"
            params.append(scope_workspace)
        rows = self.conn.execute(query + " ORDER BY w.id", params).fetchall()
        return [self._summary(self.get_client_hq({"organization_id": organization_id, "workspace_id": row["id"], "person_id": person_id}, row["id"])) for row in rows]

    def _summary(self, view: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "client_id": view["client_id"], "organization_id": view["organization_id"],
            "workspace_id": view["workspace_id"], "client_name": view["client_name"],
            "retainer_status": view["retainer"].get("status"), "health": view["health"],
            "open_work_items_count": len(view["open_work_items"]),
            "recent_deliverables_count": len(view["recent_deliverables"]),
            "recent_reports_count": len(view["recent_reports"]),
            "upcoming_meetings_count": len(view["upcoming_meetings"]),
        }

    def _retainer(self, organization_id: str, workspace_id: str, person_id: str | None) -> dict[str, Any]:
        if person_id and hasattr(self.os, "revenue"):
            return self.os.revenue.retainer_read_model(organization_id, workspace_id, person_id)
        rows = [dict(row) for row in self.conn.execute(
            "SELECT * FROM contracts WHERE organization_id=? AND workspace_id=? ORDER BY start_date DESC",
            (organization_id, workspace_id),
        ).fetchall()]
        return {"workspace_id": workspace_id, "status": "ok" if rows else "no_contract", "contracts": rows}

    def _health(self, organization_id: str, workspace_id: str, person_id: str | None) -> dict[str, Any] | None:
        if person_id and hasattr(self.os, "client_ops"):
            return self.os.client_ops.explain_health(organization_id, workspace_id, person_id)
        row = self.conn.execute(
            "SELECT * FROM client_health_snapshots WHERE organization_id=? AND workspace_id=? ORDER BY calculated_at DESC LIMIT 1",
            (organization_id, workspace_id),
        ).fetchone()
        return dict(row) if row else None

    def _reports(self, organization_id: str, workspace_id: str) -> list[dict[str, Any]]:
        try:
            rows = self.conn.execute(
                """SELECT id,report_type,version,title,generated_at,created_at FROM portal_report_versions
                   WHERE organization_id=? AND workspace_id=? ORDER BY created_at DESC,id DESC LIMIT 10""",
                (organization_id, workspace_id),
            ).fetchall()
        except Exception:
            return []
        return [dict(row) for row in rows]

    def _meetings(self, organization_id: str, workspace_id: str) -> list[dict[str, Any]]:
        if self.calendar is not None and hasattr(self.calendar, "list_meetings"):
            return [dict(item) for item in self.calendar.list_meetings(organization_id, workspace_id, status="scheduled")]
        try:
            rows = self.conn.execute(
                """SELECT * FROM meetings WHERE organization_id=? AND workspace_id=?
                   AND occurred_at >= datetime('now') ORDER BY occurred_at ASC LIMIT 10""",
                (organization_id, workspace_id),
            ).fetchall()
        except Exception:
            return []
        return [dict(row) for row in rows]

    def _authorize(self, organization_id: str, workspace_id: str, person_id: str | None) -> None:
        if person_id and hasattr(self.os, "_require_person_access"):
            self.os._require_person_access(organization_id, workspace_id, person_id, write=False)
            return
        scope = self.conn.execute(
            "SELECT 1 FROM workspace_organization WHERE organization_id=? AND workspace_id=? AND kind='client'",
            (organization_id, workspace_id),
        ).fetchone()
        if scope is None:
            raise AuthorizationError("client HQ scope denied")

    @staticmethod
    def _scope(os_scope: Any, client_id: str | None) -> tuple[str, str | None, str | None]:
        def value(key: str, default: Any = None) -> Any:
            if isinstance(os_scope, Mapping):
                return os_scope.get(key, default)
            return getattr(os_scope, key, default)

        organization_id = str(value("organization_id", "") or "").strip()
        workspace_id = value("workspace_id")
        person_id = value("person_id")
        workspace_id = str(workspace_id or client_id or "").strip() or None
        person_id = str(person_id or "").strip() or None
        if not organization_id:
            raise ValidationError("organization is required")
        if client_id and not workspace_id:
            raise ValidationError("client is required")
        if client_id and workspace_id != client_id:
            raise AuthorizationError("client HQ is outside workspace scope")
        return organization_id, workspace_id, person_id


ClientHQRead = ClientHQReadService
