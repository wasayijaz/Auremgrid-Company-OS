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


class ClientOperationsRosterMixin:
    def create_client_roster(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        roles: list[dict[str, Any]],
        effective_at: datetime | str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        self._require_client_workspace(organization_id, workspace_id)
        self._require_active_workspace_person(organization_id, workspace_id, person_id)
        self._require_workspace_admin(organization_id, workspace_id, person_id)
        if not roles:
            raise ValidationError("client account roster roles are required")
        normalized: list[dict[str, str | None]] = []
        seen: set[tuple[str, str]] = set()
        for role in roles:
            role_key = _norm_role(role.get("role_key", role.get("role")))
            wing = _norm_wing(role.get("wing"))
            if role_key in WING_ROLES and wing is None:
                raise ValidationError("wing is required for wing roster roles")
            if role_key not in WING_ROLES and wing is not None:
                raise ValidationError("wing is only valid for wing roster roles")
            principal_type = str(role.get("principal_type") or ("agent" if role.get("agent_id") else "person")).strip().lower()
            target_id = str(role.get("agent_id") if principal_type == "agent" else role.get("person_id") or "").strip()
            if not target_id:
                raise ValidationError("client account roster role target is required")
            key = (role_key, wing or "")
            if key in seen:
                raise ValidationError("client account roster roles must be singletons per role and wing")
            seen.add(key)
            if principal_type == "agent":
                self._require_eligible_roster_agent(organization_id, workspace_id, target_id)
            elif principal_type == "person":
                self._require_active_workspace_person(organization_id, workspace_id, target_id)
            else:
                raise ValidationError("client account roster principal_type must be person or agent")
            normalized.append({"role_key": role_key, "wing": wing, "person_id": person_id if principal_type == "agent" else target_id, "principal_type": principal_type, "agent_id": target_id if principal_type == "agent" else None})
        dri = [role for role in normalized if role["role_key"] == "client_success_dri"]
        backup = [role for role in normalized if role["role_key"] == "client_success_backup"]
        if len(dri) != 1 or len(backup) != 1:
            raise ValidationError("exactly one client success DRI and one backup are required")
        if (dri[0]["principal_type"], dri[0]["person_id"]) == (backup[0]["principal_type"], backup[0]["person_id"]):
            raise ValidationError("client success DRI and backup must be distinct")
        created_at = _now()
        effective = _parse_dt(effective_at)
        roster_id = self.new_id("roster")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.conn.execute(
                "SELECT 1 FROM client_account_rosters WHERE workspace_id=? AND effective_at=?",
                (workspace_id, effective.isoformat()),
            ).fetchone():
                raise ValidationError("client account roster effective_at must be unique per workspace")
            version = int(self.conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM client_account_rosters WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0])
            self.conn.execute(
                """INSERT INTO client_account_rosters(
                    id,organization_id,workspace_id,version,effective_at,created_at,created_by_person_id,note
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    roster_id, organization_id, workspace_id, version, effective.isoformat(),
                    created_at.isoformat(), person_id, str(note or "").strip(),
                ),
            )
            for role in normalized:
                self.conn.execute(
                    """INSERT INTO client_account_roster_roles(
                    id,roster_id,organization_id,workspace_id,role_key,wing,person_id,created_at,principal_type,agent_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.new_id("rosterrole"), roster_id, organization_id, workspace_id,
                        role["role_key"], role["wing"], role["person_id"], created_at.isoformat(), role["principal_type"], role["agent_id"],
                    ),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        row = self.conn.execute("SELECT * FROM client_account_rosters WHERE id=?", (roster_id,)).fetchone()
        return self._roster_from_row(row).to_dict()

    def get_client_roster(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        roster_id: str | None = None,
        *,
        as_of: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        self.authorize(organization_id, workspace_id, person_id)
        self._require_client_workspace(organization_id, workspace_id)
        if roster_id:
            row = self.conn.execute(
                "SELECT * FROM client_account_rosters WHERE organization_id=? AND workspace_id=? AND id=?",
                (organization_id, workspace_id, roster_id),
            ).fetchone()
        else:
            at = _parse_dt(as_of).isoformat()
            row = self.conn.execute(
                """SELECT * FROM client_account_rosters
                   WHERE organization_id=? AND workspace_id=? AND effective_at<=?
                   ORDER BY effective_at DESC,created_at DESC,id DESC LIMIT 1""",
                (organization_id, workspace_id, at),
            ).fetchone()
        return self._roster_from_row(row).to_dict() if row else None

    def resolve_account_role(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        role_key: str,
        *,
        wing: str | None = None,
        as_of: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        role = _norm_role(role_key)
        normalized_wing = _norm_wing(wing)
        if role in WING_ROLES and normalized_wing is None:
            raise ValidationError("wing is required for wing roster roles")
        if role not in WING_ROLES and normalized_wing is not None:
            raise ValidationError("wing is only valid for wing roster roles")
        roster = self.get_client_roster(organization_id, workspace_id, person_id, as_of=as_of)
        if roster is None:
            return None
        for item in roster["roles"]:
            if item["role_key"] == role and item["wing"] == normalized_wing:
                return item
        return None

    def set_meeting_responsibilities(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        meeting_id: str,
        *,
        facilitator_person_id: str | None = None,
        note_taker_person_id: str | None = None,
        reason: str = "manual",
    ) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        self._require_client_workspace(organization_id, workspace_id)
        self._require_workspace_admin(organization_id, workspace_id, person_id)
        meeting = self.conn.execute(
            "SELECT id FROM meetings WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, meeting_id),
        ).fetchone()
        if meeting is None:
            raise NotFoundError("meeting not found")
        if facilitator_person_id is None and note_taker_person_id is None:
            raise ValidationError("at least one meeting responsibility person is required")
        if facilitator_person_id is not None:
            self._require_active_workspace_person(organization_id, workspace_id, facilitator_person_id)
        if note_taker_person_id is not None:
            self._require_active_workspace_person(organization_id, workspace_id, note_taker_person_id)
        roster = self.get_client_roster(organization_id, workspace_id, person_id)
        created_at = _now()
        event_id = self.new_id("meetingresp")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            event_sequence = int(self.conn.execute(
                "SELECT COALESCE(MAX(event_sequence),0)+1 FROM meeting_responsibility_events WHERE meeting_id=?",
                (meeting_id,),
            ).fetchone()[0])
            self.conn.execute(
                """INSERT INTO meeting_responsibility_events(
                    id,organization_id,workspace_id,meeting_id,event_sequence,roster_id,facilitator_person_id,
                    note_taker_person_id,reason,created_by_person_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id, organization_id, workspace_id, meeting_id, event_sequence,
                    roster["id"] if roster else None, facilitator_person_id, note_taker_person_id,
                    str(reason or "manual").strip() or "manual", person_id, created_at.isoformat(),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        result = self.get_meeting_responsibilities(organization_id, workspace_id, person_id, meeting_id, as_of=created_at)
        return {**result, "event_id": event_id}

    def get_meeting_responsibilities(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        meeting_id: str,
        *,
        as_of: datetime | str | None = None,
    ) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id)
        self._require_client_workspace(organization_id, workspace_id)
        if not self.conn.execute(
            "SELECT id FROM meetings WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, meeting_id),
        ).fetchone():
            raise NotFoundError("meeting not found")
        at = _parse_dt(as_of)
        roster = self.get_client_roster(organization_id, workspace_id, person_id, as_of=at)
        defaults = {"facilitator": None, "note_taker": None}
        if roster is not None:
            for role in roster["roles"]:
                if role["role_key"] == "default_meeting_facilitator":
                    defaults["facilitator"] = role["person_id"]
                if role["role_key"] == "default_meeting_note_taker":
                    defaults["note_taker"] = role["person_id"]
        explicit_facilitator = self.conn.execute(
            """SELECT id,facilitator_person_id FROM meeting_responsibility_events
               WHERE organization_id=? AND workspace_id=? AND meeting_id=?
                 AND facilitator_person_id IS NOT NULL AND created_at<=?
               ORDER BY created_at DESC,event_sequence DESC LIMIT 1""",
            (organization_id, workspace_id, meeting_id, at.isoformat()),
        ).fetchone()
        explicit_note_taker = self.conn.execute(
            """SELECT id,note_taker_person_id FROM meeting_responsibility_events
               WHERE organization_id=? AND workspace_id=? AND meeting_id=?
                 AND note_taker_person_id IS NOT NULL AND created_at<=?
               ORDER BY created_at DESC,event_sequence DESC LIMIT 1""",
            (organization_id, workspace_id, meeting_id, at.isoformat()),
        ).fetchone()
        facilitator = explicit_facilitator["facilitator_person_id"] if explicit_facilitator else defaults["facilitator"]
        note_taker = explicit_note_taker["note_taker_person_id"] if explicit_note_taker else defaults["note_taker"]
        event_ids = {
            "facilitator": explicit_facilitator["id"] if explicit_facilitator else None,
            "note_taker": explicit_note_taker["id"] if explicit_note_taker else None,
        }
        event_id = event_ids["facilitator"] or event_ids["note_taker"]
        return MeetingResponsibilities(
            meeting_id,
            roster["id"] if roster else None,
            facilitator,
            note_taker,
            {
                "facilitator": "explicit" if explicit_facilitator else ("default" if defaults["facilitator"] else None),
                "note_taker": "explicit" if explicit_note_taker else ("default" if defaults["note_taker"] else None),
            },
            event_id,
            event_ids,
        ).to_dict()
