from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.client_ops import (
    ClientAccountRoster,
    ClientAccountRosterRole,
    ClientHealthSnapshot,
    Conversation,
    Meeting,
    MeetingResponsibilities,
    Message,
    Opportunity,
    Risk,
    Signal,
)
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError

from .client_ops_shared import (
    OPPORTUNITY_ACTIVE_STATUSES,
    OPPORTUNITY_TERMINAL_STATUSES,
    WING_ROLES,
    _json,
    _load_json_object,
    _norm_role,
    _norm_wing,
    _now,
    _parse_dt,
    _usage_totals,
)


class ClientOperationsOpportunitiesMixin:
    def create_opportunity(self, organization_id: str, workspace_id: str, person_id: str, type: str,
        reason: str, evidence: str, recommendation: str, estimated_value: float | None = None) -> Opportunity:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if type not in {"upsell","cross_sell","campaign_optimization","workflow_improvement","cost_saving","retention","automation","scope_expansion"}:
            raise ValidationError("unsupported opportunity type")
        item=Opportunity(self.new_id("opportunity"),organization_id,workspace_id,type,estimated_value,reason,evidence,recommendation,person_id,"open",_now())
        self.conn.execute("INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?)",(
            item.id,item.organization_id,item.workspace_id,item.type,item.estimated_value,item.reason,item.evidence,
            item.recommendation,item.owner_person_id,item.status,item.created_at.isoformat()))
        self._record_opportunity_event(
            organization_id, workspace_id, person_id, item.id, "created", None, "open",
            "Opportunity created", {"type": type},
        )
        self.conn.commit(); return item

    def list_opportunities(self, organization_id: str, workspace_id: str, person_id: str, open_only: bool = False) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id)
        sql = "SELECT * FROM opportunities WHERE organization_id=? AND workspace_id=?"
        values: list[Any] = [organization_id, workspace_id]
        if open_only:
            sql += " AND status NOT IN ('won','lost','closed')"
        return [dict(row) for row in self.conn.execute(sql + " ORDER BY created_at DESC", values).fetchall()]

    def advance_opportunity(self, organization_id: str, workspace_id: str, person_id: str,
        opportunity_id: str, to_status: str, note: str = "") -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        status = str(to_status or "").strip().lower()
        if status not in OPPORTUNITY_ACTIVE_STATUSES:
            raise ValidationError("opportunity advance status must be open, qualified, proposed, or deferred")
        row = self.conn.execute(
            "SELECT * FROM opportunities WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, opportunity_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("opportunity not found")
        if row["status"] in OPPORTUNITY_TERMINAL_STATUSES:
            raise ValidationError("closed opportunities cannot advance")
        if row["status"] == status:
            raise ValidationError("opportunity is already in that status")
        text = note.strip() or f"Advanced opportunity to {status}"
        self.conn.execute("UPDATE opportunities SET status=? WHERE id=?", (status, opportunity_id))
        self._record_opportunity_event(organization_id, workspace_id, person_id, opportunity_id, "advanced", row["status"], status, text)
        self.conn.commit()
        return self.opportunity_detail(organization_id, workspace_id, person_id, opportunity_id)

    def close_opportunity(self, organization_id: str, workspace_id: str, person_id: str,
        opportunity_id: str, outcome: str, note: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        status = str(outcome or "").strip().lower()
        text = note.strip()
        if status not in OPPORTUNITY_TERMINAL_STATUSES:
            raise ValidationError("opportunity outcome must be won, lost, or closed")
        if not text:
            raise ValidationError("opportunity close note is required")
        row = self.conn.execute(
            "SELECT * FROM opportunities WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, opportunity_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("opportunity not found")
        if row["status"] in OPPORTUNITY_TERMINAL_STATUSES:
            raise ValidationError("opportunity is already closed")
        self.conn.execute("UPDATE opportunities SET status=? WHERE id=?", (status, opportunity_id))
        self._record_opportunity_event(organization_id, workspace_id, person_id, opportunity_id, "closed", row["status"], status, text)
        self.conn.commit()
        return self.opportunity_detail(organization_id, workspace_id, person_id, opportunity_id)

    def opportunity_detail(self, organization_id: str, workspace_id: str, person_id: str, opportunity_id: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id)
        row = self.conn.execute(
            "SELECT * FROM opportunities WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, opportunity_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("opportunity not found")
        events = [dict(item) for item in self.conn.execute(
            "SELECT * FROM opportunity_events WHERE organization_id=? AND workspace_id=? AND opportunity_id=? ORDER BY created_at,rowid",
            (organization_id, workspace_id, opportunity_id),
        ).fetchall()]
        return {**dict(row), "events": events}
