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


class ClientOperationsRisksMixin:
    def create_risk(self, organization_id: str, workspace_id: str, person_id: str, type: str,
        severity: str, probability: float, impact: str, evidence: str, recommended_action: str,
        project_id: str | None = None) -> Risk:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        if project_id and not self.conn.execute("SELECT id FROM projects WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, project_id)).fetchone():
            raise NotFoundError("project not found")
        if type not in {"churn","delivery","financial","relationship","performance","scope","team","security","compliance"}:
            raise ValidationError("unsupported risk type")
        if severity not in {"low","medium","high","critical"} or not 0 <= probability <= 1:
            raise ValidationError("invalid risk severity or probability")
        item = Risk(self.new_id("risk"),organization_id,workspace_id,project_id,type,severity,probability,impact,person_id,
            _now(),"open",evidence,recommended_action)
        self.conn.execute("INSERT INTO risks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
            item.id,item.organization_id,item.workspace_id,item.project_id,item.type,item.severity,item.probability,item.impact,
            item.owner_person_id,item.detected_at.isoformat(),item.status,item.evidence,item.recommended_action,None,None))
        self._record_risk_event(
            organization_id, workspace_id, person_id, item.id, "created", None, "open",
            "Risk created", {"type": type, "severity": severity, "project_id": project_id},
        )
        self.conn.commit(); return item

    def list_risks(self, organization_id: str, workspace_id: str, person_id: str, open_only: bool = True) -> list[dict[str, Any]]:
        self.authorize(organization_id,workspace_id,person_id)
        sql="SELECT * FROM risks WHERE workspace_id=?" + (" AND status='open'" if open_only else "")
        return [dict(row) for row in self.conn.execute(sql+" ORDER BY detected_at DESC",(workspace_id,)).fetchall()]

    def resolve_risk(self, organization_id: str, workspace_id: str, person_id: str, risk_id: str, resolution: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        note = resolution.strip()
        if not note:
            raise ValidationError("risk resolution is required")
        row = self.conn.execute(
            "SELECT * FROM risks WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, risk_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("risk not found")
        if row["status"] != "open":
            raise ValidationError("only open risks can be resolved")
        now = _now().isoformat()
        self.conn.execute(
            "UPDATE risks SET status='resolved',resolution=?,resolved_at=? WHERE id=?",
            (note, now, risk_id),
        )
        self._record_risk_event(organization_id, workspace_id, person_id, risk_id, "resolved", row["status"], "resolved", note)
        self.conn.commit()
        return self.risk_detail(organization_id, workspace_id, person_id, risk_id)

    def reopen_risk(self, organization_id: str, workspace_id: str, person_id: str, risk_id: str, reason: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        note = reason.strip()
        if not note:
            raise ValidationError("risk reopen reason is required")
        row = self.conn.execute(
            "SELECT * FROM risks WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, risk_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("risk not found")
        if row["status"] == "open":
            raise ValidationError("risk is already open")
        self.conn.execute("UPDATE risks SET status='open',resolution=NULL,resolved_at=NULL WHERE id=?", (risk_id,))
        self._record_risk_event(
            organization_id, workspace_id, person_id, risk_id, "reopened", row["status"], "open",
            note, {"previous_resolution": row["resolution"], "previous_resolved_at": row["resolved_at"]},
        )
        self.conn.commit()
        return self.risk_detail(organization_id, workspace_id, person_id, risk_id)

    def risk_detail(self, organization_id: str, workspace_id: str, person_id: str, risk_id: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id)
        row = self.conn.execute(
            "SELECT * FROM risks WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, risk_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("risk not found")
        events = [dict(item) for item in self.conn.execute(
            "SELECT * FROM risk_events WHERE organization_id=? AND workspace_id=? AND risk_id=? ORDER BY created_at,rowid",
            (organization_id, workspace_id, risk_id),
        ).fetchall()]
        return {**dict(row), "events": events}
