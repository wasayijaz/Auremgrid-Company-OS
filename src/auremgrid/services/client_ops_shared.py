from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable

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


ROSTER_ROLE_KEYS = {
    "client_success_dri",
    "client_success_backup",
    "account_lead",
    "account_executive",
    "wing_lead",
    "wing_executive",
    "cadence_owner",
    "escalation_owner",
    "default_meeting_facilitator",
    "default_meeting_note_taker",
}
WING_ROLES = {"wing_lead", "wing_executive"}
OPPORTUNITY_ACTIVE_STATUSES = {"open", "qualified", "proposed", "deferred"}
OPPORTUNITY_TERMINAL_STATUSES = {"won", "lost", "closed"}


def _now() -> datetime:
    shell_now = getattr(sys.modules.get("auremgrid.services.client_ops"), "_now", None)
    if shell_now is not None and shell_now is not _now:
        return shell_now()
    return datetime.now(timezone.utc)

def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))

def _parse_dt(value: datetime | str | None) -> datetime:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)

def _norm_role(value: Any) -> str:
    role = str(value or "").strip().lower()
    if role not in ROSTER_ROLE_KEYS:
        raise ValidationError("unsupported client account roster role")
    return role

def _norm_wing(value: Any) -> str | None:
    if value is None:
        return None
    wing = " ".join(str(value).strip().lower().split())
    return wing or None

def _load_json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}

def _usage_totals(row: Any) -> dict[str, Any]:
    used_quantity = float(row["delivered_quantity"] or 0) + float(row["in_review_quantity"] or 0) + float(row["requested_quantity"] or 0)
    included_quantity = float(row["included_quantity"] or 0)
    included_hours = float(row["included_hours"] or 0)
    used_hours = float(row["used_hours"] or 0)
    if included_hours:
        return {
            "basis": "hours",
            "included": included_hours,
            "used": used_hours,
            "percentage": round(used_hours / included_hours * 100, 3),
        }
    if included_quantity:
        return {
            "basis": "quantity",
            "included": included_quantity,
            "used": used_quantity,
            "percentage": round(used_quantity / included_quantity * 100, 3),
        }
    return {"basis": None, "included": None, "used": used_hours or used_quantity or None, "percentage": None}


class ClientOperationsSharedMixin:
    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any]) -> None:
        self.conn, self.new_id, self.authorize = conn, new_id, authorize

    def _require_client_workspace(self, organization_id: str, workspace_id: str) -> None:
        row = self.conn.execute(
            "SELECT kind FROM workspace_organization WHERE organization_id=? AND workspace_id=?",
            (organization_id, workspace_id),
        ).fetchone()
        if row is None or row["kind"] != "client":
            raise ValidationError("client account rosters require a client workspace")

    def _require_active_workspace_person(self, organization_id: str, workspace_id: str, target_person_id: str) -> None:
        row = self.conn.execute(
            """SELECT p.id FROM people p JOIN workspace_memberships wm ON wm.person_id=p.id
               WHERE p.organization_id=? AND p.id=? AND p.status='active' AND wm.workspace_id=?""",
            (organization_id, target_person_id, workspace_id),
        ).fetchone()
        if row is None:
            raise ValidationError("client account roster people must be active workspace members in the organization")

    def _require_eligible_roster_agent(self, organization_id: str, workspace_id: str, agent_id: str) -> None:
        row = self.conn.execute(
            "SELECT status,allowed_workspace_ids,capability_tags FROM agents WHERE organization_id=? AND id=?",
            (organization_id, agent_id),
        ).fetchone()
        if row is None or row["status"] not in {"idle", "running"}:
            raise ValidationError("client account roster agent must be active")
        try:
            allowed = json.loads(row["allowed_workspace_ids"] or "[]")
        except (TypeError, ValueError):
            allowed = []
        if workspace_id not in {str(item) for item in allowed}:
            raise ValidationError("client account roster agent must be allowed in workspace")
        try:
            capabilities = set(json.loads(row["capability_tags"] or "[]"))
        except (TypeError, ValueError):
            capabilities = set()
        if "workflow_run" not in capabilities:
            raise ValidationError("client account roster agent must have workflow_run capability")

    def _require_workspace_admin(self, organization_id: str, workspace_id: str, person_id: str) -> None:
        row = self.conn.execute(
            """SELECT wm.role FROM workspace_memberships wm
               JOIN people p ON p.id=wm.person_id
               WHERE p.organization_id=? AND p.id=? AND p.status='active' AND wm.workspace_id=?""",
            (organization_id, person_id, workspace_id),
        ).fetchone()
        if row is None or row["role"] != "admin":
            raise AuthorizationError("client account roster changes require workspace admin")

    @staticmethod
    def _roster_role_from_row(row: Any) -> ClientAccountRosterRole:
        return ClientAccountRosterRole(
            row["id"], row["roster_id"], row["organization_id"], row["workspace_id"],
            row["role_key"], row["wing"], row["person_id"], _parse_dt(row["created_at"]),
            str(row["principal_type"] or "person"), row["agent_id"],
        )

    def _roster_from_row(self, row: Any) -> ClientAccountRoster:
        role_rows = self.conn.execute(
            """SELECT * FROM client_account_roster_roles
               WHERE roster_id=? ORDER BY role_key,COALESCE(wing,''),id""",
            (row["id"],),
        ).fetchall()
        return ClientAccountRoster(
            row["id"], row["organization_id"], row["workspace_id"], _parse_dt(row["effective_at"]),
            int(row["version"]), _parse_dt(row["created_at"]), row["created_by_person_id"], row["note"],
            tuple(self._roster_role_from_row(role) for role in role_rows),
        )

    @staticmethod
    def _scope_usage_state(percentage: float | None) -> str:
        if percentage is None:
            return "unknown"
        return "over_scope" if percentage > 100 else "recorded"

    def _scope_generated_links(self, organization_id: str, workspace_id: str, allowance_id: str, usage_id: str | None) -> dict[str, list[str]]:
        if usage_id is None:
            return {"risk_ids": [], "opportunity_ids": []}
        risk_ids = []
        for row in self.conn.execute(
            """SELECT id,evidence FROM risks
               WHERE organization_id=? AND workspace_id=? AND type='scope'
               ORDER BY detected_at DESC,id""",
            (organization_id, workspace_id),
        ).fetchall():
            payload = _load_json_object(row["evidence"])
            if payload.get("allowance_id") == allowance_id and payload.get("id") == usage_id:
                risk_ids.append(row["id"])
        opportunity_ids = []
        for row in self.conn.execute(
            """SELECT id,evidence FROM opportunities
               WHERE organization_id=? AND workspace_id=? AND type='scope_expansion'
               ORDER BY created_at DESC,id""",
            (organization_id, workspace_id),
        ).fetchall():
            payload = _load_json_object(row["evidence"])
            if payload.get("allowance_id") == allowance_id and payload.get("id") == usage_id:
                opportunity_ids.append(row["id"])
        return {"risk_ids": risk_ids, "opportunity_ids": opportunity_ids}

    def _record_risk_event(self, organization_id: str, workspace_id: str, person_id: str, risk_id: str,
        action: str, from_status: str | None, to_status: str, note: str, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            """INSERT INTO risk_events(
                id,organization_id,workspace_id,risk_id,actor_person_id,action,from_status,to_status,note,payload_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (self.new_id("riskevent"), organization_id, workspace_id, risk_id, person_id, action,
             from_status, to_status, note, _json(payload or {}), _now().isoformat()),
        )

    def _record_opportunity_event(self, organization_id: str, workspace_id: str, person_id: str, opportunity_id: str,
        action: str, from_status: str | None, to_status: str, note: str, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            """INSERT INTO opportunity_events(
                id,organization_id,workspace_id,opportunity_id,actor_person_id,action,from_status,to_status,note,payload_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (self.new_id("opportunityevent"), organization_id, workspace_id, opportunity_id, person_id, action,
             from_status, to_status, note, _json(payload or {}), _now().isoformat()),
        )
