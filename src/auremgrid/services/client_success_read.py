"""Offline, organization-scoped client success read projection."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


class ClientSuccessReadService:
    """Compose client success health snapshots, risks, renewal dates, and interactions."""

    def __init__(self, company_os: Any) -> None:
        self.os = company_os
        self.conn = company_os.store.conn

    def get_client_health_snapshot(self, os_scope: Any, client_id: str) -> dict[str, Any]:
        """Return the per-client health snapshot for the client-success role."""
        organization_id, workspace_id, person_id = self._scope(os_scope, client_id)
        self._authorize(organization_id, workspace_id, person_id)

        workspace = self.conn.execute(
            """SELECT w.* FROM workspaces w JOIN workspace_organization wo ON wo.workspace_id=w.id
               WHERE w.id=? AND wo.organization_id=? AND wo.kind='client'""",
            (workspace_id, organization_id),
        ).fetchone()
        if workspace is None:
            raise NotFoundError("client workspace was not found")

        account_info = self._account_info(organization_id, workspace_id, str(workspace["name"] or ""), person_id)
        open_risks = self._open_risks(organization_id, workspace_id)
        renewal_info = self._renewal_dates(organization_id, workspace_id)
        interactions = self._recent_interactions(organization_id, workspace_id, person_id)
        rollup = self._status_rollup(organization_id, workspace_id, person_id, open_risks, renewal_info, interactions)

        return {
            "organization_id": organization_id,
            "client_id": client_id,
            "workspace_id": workspace_id,
            "client_name": str(workspace["name"] or ""),
            "account": account_info,
            "open_risks": open_risks,
            "renewal_dates": renewal_info,
            "recent_interactions": interactions,
            "status_rollup": rollup,
        }

    def list_client_health_snapshots(self, os_scope: Any) -> list[dict[str, Any]]:
        """Return client health snapshot summaries across all clients in the organization."""
        organization_id, scope_workspace, person_id = self._scope(os_scope, None)
        params: list[Any] = [organization_id]
        query = """SELECT w.id, w.name FROM workspaces w JOIN workspace_organization wo ON wo.workspace_id=w.id
                   WHERE wo.organization_id=? AND wo.kind='client'"""
        if scope_workspace:
            query += " AND w.id=?"
            params.append(scope_workspace)
        rows = self.conn.execute(query + " ORDER BY w.id", params).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            scope_dict = {
                "organization_id": organization_id,
                "workspace_id": row["id"],
                "person_id": person_id,
            }
            try:
                full_view = self.get_client_health_snapshot(scope_dict, row["id"])
                results.append(self._summary(full_view))
            except Exception:
                continue
        return results

    def _summary(self, view: Mapping[str, Any]) -> dict[str, Any]:
        rollup = view.get("status_rollup", {})
        open_risks = view.get("open_risks", {})
        renewals = view.get("renewal_dates", {})
        interactions = view.get("recent_interactions", {})

        return {
            "client_id": view["client_id"],
            "client_name": view["client_name"],
            "organization_id": view["organization_id"],
            "workspace_id": view["workspace_id"],
            "overall_status": rollup.get("overall_status", "healthy"),
            "health_score": rollup.get("health_score", 1.0),
            "open_risks_count": open_risks.get("count", 0),
            "critical_risks_count": open_risks.get("critical_or_high_count", 0),
            "nearest_renewal_date": renewals.get("nearest_renewal_date"),
            "days_until_renewal": renewals.get("days_until_renewal"),
            "unanswered_messages_count": interactions.get("unanswered_messages_count", 0),
        }

    def _account_info(
        self, organization_id: str, workspace_id: str, client_name: str, person_id: str | None
    ) -> dict[str, Any]:
        roster_data = None
        if person_id and hasattr(self.os, "client_ops"):
            try:
                roster = self.os.client_ops.get_client_roster(organization_id, workspace_id, person_id)
                if roster:
                    roster_data = roster if isinstance(roster, dict) else roster.to_dict()
            except Exception:
                pass

        contacts = []
        try:
            c_rows = self.conn.execute(
                """SELECT id, name, role, email, phone FROM contacts
                   WHERE organization_id=? AND workspace_id=? ORDER BY name ASC""",
                (organization_id, workspace_id),
            ).fetchall()
            contacts = [dict(r) for r in c_rows]
        except Exception:
            pass

        return {
            "workspace_id": workspace_id,
            "client_name": client_name,
            "roster": roster_data,
            "contacts": contacts,
        }

    def _open_risks(self, organization_id: str, workspace_id: str) -> dict[str, Any]:
        try:
            rows = self.conn.execute(
                """SELECT id, project_id, type, severity, probability, impact, owner_person_id,
                          detected_at, status, evidence, recommended_action
                   FROM risks
                   WHERE organization_id=? AND workspace_id=? AND status='open'
                   ORDER BY
                       CASE severity
                           WHEN 'severe' THEN 1
                           WHEN 'critical' THEN 2
                           WHEN 'high' THEN 3
                           WHEN 'medium' THEN 4
                           ELSE 5
                       END, detected_at DESC""",
                (organization_id, workspace_id),
            ).fetchall()
            risk_list = [dict(r) for r in rows]
        except Exception:
            risk_list = []

        critical_or_high = sum(
            1 for r in risk_list if str(r.get("severity", "")).lower() in ("critical", "high", "severe")
        )

        return {
            "count": len(risk_list),
            "critical_or_high_count": critical_or_high,
            "items": risk_list,
        }

    def _renewal_dates(self, organization_id: str, workspace_id: str) -> dict[str, Any]:
        try:
            rows = self.conn.execute(
                """SELECT id, kind, billing_model, value, currency, start_date, end_date, renewal_date, status
                   FROM contracts
                   WHERE organization_id=? AND workspace_id=?
                   ORDER BY COALESCE(renewal_date, end_date) ASC""",
                (organization_id, workspace_id),
            ).fetchall()
            contract_list = [dict(r) for r in rows]
        except Exception:
            contract_list = []

        today = date.today()
        nearest_date_str = None
        min_days = None

        for c in contract_list:
            d_str = c.get("renewal_date") or c.get("end_date")
            if d_str:
                try:
                    target_date = date.fromisoformat(str(d_str)[:10])
                    days = (target_date - today).days
                    if min_days is None or days < min_days:
                        min_days = days
                        nearest_date_str = str(d_str)[:10]
                except Exception:
                    pass

        active_count = sum(1 for c in contract_list if str(c.get("status", "")).lower() == "active")

        return {
            "contracts_count": len(contract_list),
            "active_contracts_count": active_count,
            "nearest_renewal_date": nearest_date_str,
            "days_until_renewal": min_days,
            "contracts": contract_list,
        }

    def _recent_interactions(
        self, organization_id: str, workspace_id: str, person_id: str | None
    ) -> dict[str, Any]:
        meetings = []
        try:
            m_rows = self.conn.execute(
                """SELECT id, title, occurred_at, summary, sentiment, source
                   FROM meetings
                   WHERE organization_id=? AND workspace_id=?
                   ORDER BY occurred_at DESC LIMIT 5""",
                (organization_id, workspace_id),
            ).fetchall()
            meetings = [dict(r) for r in m_rows]
        except Exception:
            pass

        unanswered = []
        if person_id and hasattr(self.os, "client_ops"):
            try:
                unanswered = self.os.client_ops.unanswered_messages(organization_id, workspace_id, person_id)
            except Exception:
                pass

        return {
            "recent_meetings": meetings,
            "recent_meetings_count": len(meetings),
            "unanswered_messages": unanswered,
            "unanswered_messages_count": len(unanswered),
        }

    def _status_rollup(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str | None,
        open_risks: Mapping[str, Any],
        renewal_info: Mapping[str, Any],
        interactions: Mapping[str, Any],
    ) -> dict[str, Any]:
        health_score = 1.0
        health_trend = "stable"
        if person_id and hasattr(self.os, "client_ops"):
            try:
                explained = self.os.client_ops.explain_health(organization_id, workspace_id, person_id)
                health_score = float(explained.get("score", 1.0))
            except Exception:
                pass
        else:
            try:
                row = self.conn.execute(
                    """SELECT overall, trend FROM client_health_snapshots
                       WHERE organization_id=? AND workspace_id=?
                       ORDER BY calculated_at DESC LIMIT 1""",
                    (organization_id, workspace_id),
                ).fetchone()
                if row:
                    health_score = float(row["overall"])
                    health_trend = str(row["trend"])
            except Exception:
                pass

        crit_risks = int(open_risks.get("critical_or_high_count", 0))
        total_risks = int(open_risks.get("count", 0))
        unanswered_count = int(interactions.get("unanswered_messages_count", 0))
        days_to_renewal = renewal_info.get("days_until_renewal")

        if crit_risks >= 2 or health_score < 0.50:
            overall_status = "critical"
        elif crit_risks >= 1 or total_risks >= 3 or health_score < 0.75 or unanswered_count >= 2:
            overall_status = "at_risk"
        else:
            overall_status = "healthy"

        return {
            "overall_status": overall_status,
            "health_score": round(health_score, 2),
            "health_trend": health_trend,
            "open_risks_count": total_risks,
            "critical_risks_count": crit_risks,
            "unanswered_messages_count": unanswered_count,
            "nearest_renewal_date": renewal_info.get("nearest_renewal_date"),
            "days_until_renewal": days_to_renewal,
        }

    def _authorize(self, organization_id: str, workspace_id: str, person_id: str | None) -> None:
        if person_id and hasattr(self.os, "_require_person_access"):
            self.os._require_person_access(organization_id, workspace_id, person_id, write=False)
            return
        scope = self.conn.execute(
            """SELECT 1 FROM workspace_organization
               WHERE organization_id=? AND workspace_id=? AND kind='client'""",
            (organization_id, workspace_id),
        ).fetchone()
        if scope is None:
            raise AuthorizationError("client success scope denied")

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
            raise AuthorizationError("client success is outside workspace scope")
        return organization_id, workspace_id, person_id


ClientSuccessRead = ClientSuccessReadService
