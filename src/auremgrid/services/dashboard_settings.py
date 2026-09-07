from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.security import AuthenticatedIdentity

from .dashboard_shared import _health_status, _safe_scalar


class DashboardSettingsMixin:
    def _identity_view(self, organization_id: str, person_id: str) -> dict[str, Any]:
        organization = self.conn.execute(
            "SELECT id,name,created_at FROM organizations WHERE id=?", (organization_id,)
        ).fetchone()
        person = self.conn.execute(
            """SELECT p.id,p.name,p.email,p.title,p.department,p.status,om.role AS organization_role
               FROM people p JOIN organization_memberships om ON om.person_id=p.id AND om.organization_id=p.organization_id
               WHERE p.organization_id=? AND p.id=?""", (organization_id, person_id)
        ).fetchone()
        return {
            "organization": dict(organization) if organization else {"id": organization_id, "name": organization_id},
            "person": dict(person) if person else {"id": person_id},
        }

    def _ledger_health(self, organization_id: str, person_id: str) -> dict[str, Any]:
        def scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
            row = self.conn.execute(sql, params).fetchone()
            return int(row[0] or 0) if row else 0
        finance = self.os.agency_ops.finance_status(organization_id, person_id)
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()
        integrity_status = str(integrity[0]) if integrity else "unknown"
        graph_health = getattr(self.os, "graph_health", {}) or {}
        status = "healthy" if integrity_status == "ok" and graph_health.get("status", "healthy") in {"healthy", "ready"} else "degraded"
        return {
            "status": status,
            "integrity": integrity_status,
            "schema_version": self.os.store.schema_version,
            "audit_events": scalar("SELECT COUNT(*) FROM ledger_audit WHERE organization_id=?", (organization_id,)),
            "recent_audit_events": scalar(
                "SELECT COUNT(*) FROM ledger_audit WHERE organization_id=? AND recorded_at>=datetime('now','-24 hours')", (organization_id,)
            ),
            "finance": {"status": finance.get("status", "unavailable")},
        }

    def _capability_summary(self, organization_id: str, person_id: str) -> dict[str, Any]:
        return {
            "feedback_patterns": int(self.conn.execute("SELECT COUNT(*) FROM feedback_patterns WHERE organization_id=?", (organization_id,)).fetchone()[0]),
            "performance_insights": int(self.conn.execute("SELECT COUNT(*) FROM performance_insights WHERE organization_id=?", (organization_id,)).fetchone()[0]),
            "forecasts": int(self.conn.execute("SELECT COUNT(*) FROM forecasts WHERE organization_id=?", (organization_id,)).fetchone()[0]),
            "retention_policies": int(self.conn.execute("SELECT COUNT(*) FROM retention_policies WHERE organization_id=?", (organization_id,)).fetchone()[0]),
        }

    def _capability_modules(self, organization_id: str, person_id: str, workspace_ids: list[str]) -> dict[str, Any]:
        # The command payload is organization-scoped; workspace-specific modules
        # are intentionally left empty until an explicit workspace is selected.
        if not workspace_ids:
            return {"feedback": [], "performance": [], "forecasts": [], "retention": []}
        return {
            "feedback": [],
            "performance": [],
            "forecasts": self.os.forecasts.list_forecasts(organization_id, person_id),
            "retention": self.os.retention.list_policies(organization_id, person_id),
        }

    def settings(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None = None) -> dict[str, Any]:
        """Return authenticated, canonical settings and system health for the operator."""
        if identity.organization_id != organization_id:
            raise AuthorizationError("dashboard scope denied")
        identity.require("workspace_read")
        if workspace_id:
            self.os._require_person_access(organization_id, workspace_id, identity.person_id, write=False)
        person = self.conn.execute(
            """SELECT p.id,p.name,p.email,p.title,p.department,p.status,om.role AS organization_role
               FROM people p JOIN organization_memberships om ON om.person_id=p.id AND om.organization_id=p.organization_id
               WHERE p.organization_id=? AND p.id=?""", (organization_id, identity.person_id)
        ).fetchone()
        memberships = [dict(row) for row in self.conn.execute(
            """SELECT w.id,w.name,wo.kind,wm.role FROM workspaces w
               JOIN workspace_organization wo ON wo.workspace_id=w.id
               JOIN workspace_memberships wm ON wm.workspace_id=w.id
               WHERE wo.organization_id=? AND wm.person_id=? ORDER BY w.name""", (organization_id, identity.person_id)
        ).fetchall()]
        pending_approvals = [dict(row) for row in self.conn.execute(
            """SELECT id,workspace_id,requested_for,action_type,reason,approver_person_id,status,created_at
               FROM approval_requests WHERE organization_id=? AND status='pending' ORDER BY created_at DESC LIMIT 20""", (organization_id,)
        ).fetchall()]
        integrations = self.os.integrations.list(identity)
        return {
            "identity": {"organization": dict(self.conn.execute("SELECT id,name,created_at FROM organizations WHERE id=?", (organization_id,)).fetchone() or {"id": organization_id, "name": organization_id}), "person": dict(person) if person else {"id": identity.person_id}},
            "workspace": next((item for item in memberships if item["id"] == workspace_id), None) if workspace_id else None,
            "workspaces": memberships,
            "permissions": {"capabilities": sorted(identity.capabilities), "scopes": sorted(identity.scopes)},
            "approvals": {"pending": pending_approvals, "pending_count": len(pending_approvals)},
            "integrations": integrations,
            "brain_customization": self._brain_customization_surface(identity, organization_id, workspace_id),
            "health": self._ledger_health(organization_id, identity.person_id),
        }

    def _brain_customization_surface(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None
    ) -> dict[str, Any]:
        try:
            return self.os.brain_customizations.surface(identity, organization_id, workspace_id)
        except Exception as exc:
            if "brain_customization" not in str(exc):
                raise
            return {
                "status": "schema_pending",
                "active": [],
                "versions": [],
                "events": [],
                "can_manage": identity.can("brain_configure"),
                "allowed_actions": [],
            }

    def module(self, organization_id: str, workspace_id: str, person_id: str, module: str) -> dict[str,Any]:
        self.os._require_person_access(organization_id,workspace_id,person_id)
        if module in {"Feedback", "Performance Insights", "Forecasts", "Retention"}:
            if module == "Feedback":
                items = self.os.feedback.list_patterns(organization_id, workspace_id, person_id)
                return {"module": module, "source_table": "feedback_patterns", "items": items,
                        "allowed_actions": [{"action": "promote_pattern", "route": "/feedback/patterns/promote"}]}
            if module == "Performance Insights":
                items = self.os.performance.list_insights(organization_id, workspace_id, person_id)
                return {"module": module, "source_table": "performance_insights", "items": items,
                        "allowed_actions": [{"action": "generate_insights", "route": "/insights/performance/generate"}]}
            if module == "Forecasts":
                return {"module": module, "source_table": "forecasts", "items": self.os.forecasts.list_forecasts(organization_id, person_id),
                        "allowed_actions": [{"action": "generate_forecasts", "route": "/forecasts/generate"}]}
            return {"module": module, "source_table": "retention_policies", "items": self.os.retention.list_policies(organization_id, person_id),
                    "allowed_actions": [{"action": "create_policy", "route": "/retention/policies"}]}
        queries={
            "Campaigns":("campaigns","SELECT id,name,platform,status,budget,updated_at FROM campaigns WHERE workspace_id=? ORDER BY updated_at DESC"),
            "Content":("content_items","SELECT id,title,stage,objective,publish_at,updated_at FROM content_items WHERE workspace_id=? ORDER BY updated_at DESC"),
            "Creative":("creative_assets","SELECT id,title,format,platform,approval_state,revision_count,created_at FROM creative_assets WHERE workspace_id=? ORDER BY created_at DESC"),
            "Meetings":("meetings","SELECT id,title,occurred_at,summary,sentiment,source FROM meetings WHERE workspace_id=? ORDER BY occurred_at DESC"),
            "Automations":("automations","SELECT id,name,status,approval_policy,created_at FROM automations WHERE organization_id=? ORDER BY created_at DESC"),
            "Reports":("report_runs","SELECT id,type,status,generated_at FROM report_runs WHERE organization_id=? ORDER BY generated_at DESC"),
            "Integrations":("integrations","SELECT id,source,status,last_sync_at,last_error,object_count,health FROM integrations WHERE organization_id=? ORDER BY source"),
            "Workflows":("workflow_runs","SELECT id,definition_name,definition_version,status,due_at,updated_at FROM workflow_runs WHERE workspace_id=? ORDER BY updated_at DESC"),
        }
        if module not in queries:return {"module":module,"items":[]}
        table,sql=queries[module];scope=organization_id if module in {"Automations","Reports","Integrations"} else workspace_id
        return {"module":module,"source_table":table,"items":[dict(row) for row in self.conn.execute(sql,(scope,)).fetchall()]}
