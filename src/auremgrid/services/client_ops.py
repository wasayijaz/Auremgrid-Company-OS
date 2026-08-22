from __future__ import annotations

import hashlib
import json
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


class ClientOperations:
    """Signal-first client operations over the canonical ledger."""

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
            target_person_id = str(role.get("person_id") or "").strip()
            if not target_person_id:
                raise ValidationError("client account roster role person_id is required")
            key = (role_key, wing or "")
            if key in seen:
                raise ValidationError("client account roster roles must be singletons per role and wing")
            seen.add(key)
            self._require_active_workspace_person(organization_id, workspace_id, target_person_id)
            normalized.append({"role_key": role_key, "wing": wing, "person_id": target_person_id})
        dri = [role for role in normalized if role["role_key"] == "client_success_dri"]
        backup = [role for role in normalized if role["role_key"] == "client_success_backup"]
        if len(dri) != 1 or len(backup) != 1:
            raise ValidationError("exactly one client success DRI and one backup are required")
        if dri[0]["person_id"] == backup[0]["person_id"]:
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
                        id,roster_id,organization_id,workspace_id,role_key,wing,person_id,created_at
                    ) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        self.new_id("rosterrole"), roster_id, organization_id, workspace_id,
                        role["role_key"], role["wing"], role["person_id"], created_at.isoformat(),
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

    def create_signal(self, organization_id: str, workspace_id: str, person_id: str, type: str,
        source_type: str, evidence: str, source_id: str | None = None, confidence: float = 1.0) -> Signal:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        allowed = {"information","request","decision","feedback","risk","update","approval","financial_event","campaign_anomaly","task_candidate"}
        if type not in allowed or not evidence.strip() or not 0 <= confidence <= 1:
            raise ValidationError("valid signal type, evidence, and confidence are required")
        item = Signal(self.new_id("signal"),organization_id,workspace_id,type,source_type,source_id,evidence.strip(),confidence,None,"new",None,_now())
        self.conn.execute("INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            item.id,item.organization_id,item.workspace_id,item.type,item.source_type,item.source_id,item.evidence,
            item.confidence,item.classification,item.status,item.routed_to,item.created_at.isoformat(),None))
        self.conn.commit()
        return item

    def create_contact(self, organization_id: str, workspace_id: str, person_id: str, name: str,
        company: str, role: str, influence: str = "medium", decision_power: str = "medium",
        communication_frequency: str | None = None, preferences: list[str] | None = None) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if influence not in {"low","medium","high"} or decision_power not in {"low","medium","high","final"}: raise ValidationError("invalid influence or decision power")
        item={"id":self.new_id("contact"),"organization_id":organization_id,"workspace_id":workspace_id,"name":name,
            "company":company,"role":role,"influence":influence,"decision_power":decision_power,
            "communication_frequency":communication_frequency,"preferences":json.dumps(preferences or []),"last_contact_at":None,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def link_contacts(self, organization_id: str, workspace_id: str, person_id: str, from_contact_id: str,
        to_contact_id: str, kind: str, strength: float, evidence: str) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        rows=self.conn.execute("SELECT id FROM contacts WHERE workspace_id=? AND id IN (?,?)",(workspace_id,from_contact_id,to_contact_id)).fetchall()
        if len(rows)!=2: raise NotFoundError("contacts not found in workspace")
        item={"id":self.new_id("relationship"),"organization_id":organization_id,"workspace_id":workspace_id,
            "from_contact_id":from_contact_id,"to_contact_id":to_contact_id,"kind":kind,"strength":strength,"evidence":evidence,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO relationships VALUES (?,?,?,?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def record_sentiment(self, organization_id: str, workspace_id: str, person_id: str, score: float,
        label: str, evidence: str, contact_id: str | None = None) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if not -1<=score<=1: raise ValidationError("sentiment score must be between -1 and 1")
        if contact_id and not self.conn.execute("SELECT id FROM contacts WHERE workspace_id=? AND id=?",(workspace_id,contact_id)).fetchone(): raise NotFoundError("contact not found")
        item={"id":self.new_id("sentiment"),"organization_id":organization_id,"workspace_id":workspace_id,"contact_id":contact_id,
            "score":score,"label":label,"evidence":evidence,"calculated_at":_now().isoformat()}
        self.conn.execute("INSERT INTO sentiment_snapshots VALUES (?,?,?,?,?,?,?,?)",tuple(item.values()));self.conn.commit();return item

    def relationship_graph(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id)
        contacts=[dict(r) for r in self.conn.execute("SELECT * FROM contacts WHERE workspace_id=? ORDER BY decision_power DESC,influence DESC",(workspace_id,)).fetchall()]
        relationships=[dict(r) for r in self.conn.execute("SELECT * FROM relationships WHERE workspace_id=?",(workspace_id,)).fetchall()]
        for contact in contacts:
            latest=self.conn.execute("SELECT score,label,calculated_at FROM sentiment_snapshots WHERE contact_id=? ORDER BY calculated_at DESC,rowid DESC LIMIT 2",(contact["id"],)).fetchall()
            contact["sentiment"]=dict(latest[0]) if latest else None
            contact["sentiment_trend"]="down" if len(latest)>1 and latest[0]["score"]<latest[1]["score"] else "stable"
        approvers=[c for c in contacts if c["decision_power"] in {"high","final"}]
        return {"contacts":contacts,"relationships":relationships,"approvers":approvers,"declining":[c for c in contacts if c["sentiment_trend"]=="down"]}

    def route_signal(self, organization_id: str, workspace_id: str, person_id: str, signal_id: str,
        destination: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        row = self.conn.execute("SELECT * FROM signals WHERE workspace_id=? AND id=?", (workspace_id,signal_id)).fetchone()
        if row is None:
            raise NotFoundError("signal not found")
        if row["status"] != "new":
            raise ValidationError("signal has already been routed")
        allowed = {"brain","work","risk","decision","notification","approval","proposal"}
        if destination not in allowed:
            raise ValidationError("unsupported signal destination")
        linked: dict[str, Any] = {}
        if destination == "risk":
            risk = self.create_risk(organization_id,workspace_id,person_id,"relationship","medium",0.5,
                "Signal requires attention",row["evidence"],"Account lead should assess and resolve the signal")
            linked = {"risk_id": risk.id}
        self.conn.execute("UPDATE signals SET classification=?,status='routed',routed_to=?,resolved_at=? WHERE id=?",
            (row["type"],destination,_now().isoformat(),signal_id))
        self.conn.commit()
        return {"signal_id": signal_id, "routed_to": destination, **linked}

    def list_signals(self, organization_id: str, workspace_id: str, person_id: str, status: str | None = None) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id)
        sql, values = "SELECT * FROM signals WHERE workspace_id=?", [workspace_id]
        if status:
            sql, values = sql+" AND status=?", [workspace_id,status]
        return [dict(row) for row in self.conn.execute(sql+" ORDER BY created_at DESC",values).fetchall()]

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
        self.conn.commit(); return item

    def list_risks(self, organization_id: str, workspace_id: str, person_id: str, open_only: bool = True) -> list[dict[str, Any]]:
        self.authorize(organization_id,workspace_id,person_id)
        sql="SELECT * FROM risks WHERE workspace_id=?" + (" AND status='open'" if open_only else "")
        return [dict(row) for row in self.conn.execute(sql+" ORDER BY detected_at DESC",(workspace_id,)).fetchall()]

    def create_opportunity(self, organization_id: str, workspace_id: str, person_id: str, type: str,
        reason: str, evidence: str, recommendation: str, estimated_value: float | None = None) -> Opportunity:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if type not in {"upsell","cross_sell","campaign_optimization","workflow_improvement","cost_saving","retention","automation","scope_expansion"}:
            raise ValidationError("unsupported opportunity type")
        item=Opportunity(self.new_id("opportunity"),organization_id,workspace_id,type,estimated_value,reason,evidence,recommendation,person_id,"open",_now())
        self.conn.execute("INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?)",(
            item.id,item.organization_id,item.workspace_id,item.type,item.estimated_value,item.reason,item.evidence,
            item.recommendation,item.owner_person_id,item.status,item.created_at.isoformat()))
        self.conn.commit(); return item

    def create_contract(self, organization_id: str, workspace_id: str, person_id: str, kind: str,
        billing_model: str, start_date: str, value: float | None = None, currency: str = "USD",
        end_date: str | None = None, renewal_date: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        contract={"id":self.new_id("contract"),"organization_id":organization_id,"workspace_id":workspace_id,"kind":kind,
            "billing_model":billing_model,"value":value,"currency":currency,"start_date":start_date,"end_date":end_date,
            "renewal_date":renewal_date,"status":"active","created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",tuple(contract.values())); self.conn.commit()
        return contract

    def add_scope_allowance(self, organization_id: str, workspace_id: str, person_id: str, contract_id: str,
        service_category: str, period: str, included_quantity: float | None = None,
        included_hours: float | None = None, revision_limit: int | None = None) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        contract=self.conn.execute("SELECT id FROM contracts WHERE organization_id=? AND workspace_id=? AND id=?",(organization_id,workspace_id,contract_id)).fetchone()
        if not contract: raise NotFoundError("contract not found")
        item={"id":self.new_id("allowance"),"contract_id":contract_id,"service_category":service_category,"period":period,
            "included_quantity":included_quantity,"included_hours":included_hours,"revision_limit":revision_limit}
        self.conn.execute("INSERT INTO scope_allowances VALUES (?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def record_scope_usage(self, organization_id: str, workspace_id: str, person_id: str, contract_id: str,
        allowance_id: str, period_start: str, delivered: float, in_review: float = 0, requested: float = 0,
        used_hours: float = 0) -> dict[str, Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        allowance=self.conn.execute("""SELECT a.*,c.organization_id,c.workspace_id FROM scope_allowances a
            JOIN contracts c ON c.id=a.contract_id
            WHERE a.contract_id=? AND a.id=? AND c.organization_id=? AND c.workspace_id=?""",(contract_id,allowance_id,organization_id,workspace_id)).fetchone()
        if allowance is None: raise NotFoundError("scope allowance not found")
        item={"id":self.new_id("usage"),"organization_id":organization_id,"workspace_id":workspace_id,"contract_id":contract_id,
            "allowance_id":allowance_id,"period_start":period_start,"delivered_quantity":delivered,"in_review_quantity":in_review,
            "requested_quantity":requested,"used_hours":used_hours,"calculated_at":_now().isoformat()}
        self.conn.execute("INSERT INTO scope_usage VALUES (?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit()
        included=allowance["included_quantity"] or 0; total=delivered+in_review+requested
        percentage=(total/included*100) if included else None
        if percentage is not None and percentage>100:
            self.create_risk(organization_id,workspace_id,person_id,"scope","high",min(percentage/200,1),
                f"Scope usage is {percentage:.0f}%",json.dumps(item),"Review change order or retainer expansion")
            self.create_opportunity(organization_id,workspace_id,person_id,"scope_expansion",f"Scope usage is {percentage:.0f}%",
                json.dumps(item),"Propose expanded allowance")
        return {**item,"usage_percent":percentage}

    def create_meeting(self, organization_id: str, workspace_id: str, person_id: str, title: str,
        occurred_at: datetime, summary: str = "", source: str = "manual", transcript: str | None = None,
        recording_url: str | None = None, sentiment: float | None = None) -> Meeting:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        item=Meeting(self.new_id("meeting"),organization_id,workspace_id,title,occurred_at,summary,sentiment,source,recording_url,_now())
        self.conn.execute("INSERT INTO meetings VALUES (?,?,?,?,?,?,?,?,?,?)",(
            item.id,item.organization_id,item.workspace_id,item.title,item.occurred_at.isoformat(),item.summary,item.sentiment,item.source,item.recording_url,item.created_at.isoformat()))
        if transcript:
            self.conn.execute("INSERT INTO transcripts VALUES (?,?,?,?,?,?)",(self.new_id("transcript"),item.id,transcript,recording_url,
                hashlib.sha256(transcript.encode()).hexdigest(),_now().isoformat()))
            self.create_signal(organization_id,workspace_id,person_id,"information","meeting",transcript,item.id,0.7)
        self.conn.commit(); return item

    def add_meeting_output(self, organization_id: str, workspace_id: str, person_id: str, meeting_id: str,
        kind: str, statement: str, confidence: float) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if not self.conn.execute("SELECT id FROM meetings WHERE workspace_id=? AND id=?",(workspace_id,meeting_id)).fetchone(): raise NotFoundError("meeting not found")
        if kind not in {"decision","commitment","action_item","request","concern","preference"}: raise ValidationError("invalid meeting output kind")
        item={"id":self.new_id("meetingoutput"),"meeting_id":meeting_id,"kind":kind,"statement":statement,"confidence":confidence,
            "status":"proposed","linked_entity_type":None,"linked_entity_id":None,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO meeting_outputs VALUES (?,?,?,?,?,?,?,?,?)",tuple(item.values()))
        signal_type={"decision":"decision","action_item":"task_candidate","request":"request","concern":"risk"}.get(kind,"information")
        self.create_signal(organization_id,workspace_id,person_id,signal_type,"meeting",statement,item["id"],confidence);self.conn.commit();return item

    def add_meeting_participant(self, organization_id: str, workspace_id: str, person_id: str,
        meeting_id: str, participant_type: str, participant_id: str) -> None:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if not self.conn.execute("SELECT id FROM meetings WHERE workspace_id=? AND id=?",(workspace_id,meeting_id)).fetchone(): raise NotFoundError("meeting not found")
        if participant_type not in {"person","contact"}: raise ValidationError("participant type must be person or contact")
        table="people" if participant_type=="person" else "contacts"
        if participant_type == "person":
            participant = self.conn.execute("""SELECT p.id FROM people p JOIN workspace_memberships wm ON wm.person_id=p.id
                WHERE p.organization_id=? AND p.id=? AND wm.workspace_id=?""", (organization_id, participant_id, workspace_id)).fetchone()
        else:
            participant = self.conn.execute("SELECT id FROM contacts WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, participant_id)).fetchone()
        if participant is None: raise NotFoundError("participant not found in workspace")
        self.conn.execute("INSERT OR IGNORE INTO meeting_participants VALUES (?,?,?)",(meeting_id,participant_type,participant_id));self.conn.commit()

    def create_conversation(self, organization_id: str, workspace_id: str, person_id: str, source: str,
        channel: str, subject: str | None = None, external_thread_id: str | None = None) -> Conversation:
        self.authorize(organization_id,workspace_id,person_id,write=True); now=_now()
        item=Conversation(self.new_id("conversation"),organization_id,workspace_id,source,channel,external_thread_id,subject,None,None,None,now)
        self.conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?)",(
            item.id,item.organization_id,item.workspace_id,item.source,item.channel,item.external_thread_id,item.subject,None,None,None,item.created_at.isoformat()))
        self.conn.commit(); return item

    def add_message(self, organization_id: str, workspace_id: str, person_id: str, conversation_id: str,
        sender_type: str, sender_id: str, body: str, sent_at: datetime, requires_reply: bool = False,
        important: bool = False, sentiment: float | None = None, source_locator: str | None = None) -> Message:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        conversation=self.conn.execute("SELECT id FROM conversations WHERE workspace_id=? AND id=?",(workspace_id,conversation_id)).fetchone()
        if conversation is None: raise NotFoundError("conversation not found")
        item=Message(self.new_id("message"),conversation_id,sender_type,sender_id,body,sent_at,None,sentiment,requires_reply,None,important,source_locator)
        self.conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(
            item.id,item.conversation_id,item.sender_type,item.sender_id,item.body,item.sent_at.isoformat(),None,item.sentiment,
            int(item.requires_reply),None,int(item.important),item.source_locator))
        if requires_reply or important:
            self.create_signal(organization_id,workspace_id,person_id,"request" if requires_reply else "information","message",body,item.id,0.9)
        self.conn.commit(); return item

    def add_conversation_participant(self, organization_id: str, workspace_id: str, person_id: str,
        conversation_id: str, participant_type: str, participant_id: str, role: str) -> None:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if not self.conn.execute("SELECT id FROM conversations WHERE workspace_id=? AND id=?",(workspace_id,conversation_id)).fetchone(): raise NotFoundError("conversation not found")
        if participant_type == "person":
            exists = self.conn.execute("""SELECT p.id FROM people p JOIN workspace_memberships wm ON wm.person_id=p.id
                WHERE p.organization_id=? AND p.id=? AND wm.workspace_id=?""", (organization_id, participant_id, workspace_id)).fetchone()
        elif participant_type == "contact":
            exists = self.conn.execute("SELECT id FROM contacts WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, participant_id)).fetchone()
        else:
            raise ValidationError("participant type must be person or contact")
        if exists is None: raise NotFoundError("participant not found in workspace")
        self.conn.execute("INSERT OR IGNORE INTO communication_participants VALUES (?,?,?,?)",(conversation_id,participant_type,participant_id,role));self.conn.commit()

    def reply_to_message(self, organization_id: str, workspace_id: str, person_id: str, message_id: str,
        body: str, sent_at: datetime) -> Message:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        original=self.conn.execute("""SELECT m.*,c.workspace_id FROM messages m JOIN conversations c ON c.id=m.conversation_id
            WHERE c.workspace_id=? AND m.id=?""",(workspace_id,message_id)).fetchone()
        if original is None: raise NotFoundError("message not found")
        item=Message(self.new_id("message"),original["conversation_id"],"person",person_id,body,sent_at,message_id,None,False,None,False,None)
        self.conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(item.id,item.conversation_id,item.sender_type,item.sender_id,item.body,item.sent_at.isoformat(),item.reply_to_id,None,0,None,0,None))
        self.conn.execute("UPDATE messages SET replied_at=? WHERE id=?",(sent_at.isoformat(),message_id));self.conn.commit();return item

    def unanswered_messages(self, organization_id: str, workspace_id: str, person_id: str) -> list[dict[str, Any]]:
        self.authorize(organization_id,workspace_id,person_id)
        rows=self.conn.execute("""SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id
            WHERE c.workspace_id=? AND m.requires_reply=1 AND m.replied_at IS NULL ORDER BY m.sent_at""",(workspace_id,)).fetchall()
        return [dict(row) for row in rows]

    def calculate_health(self, organization_id: str, workspace_id: str, person_id: str) -> ClientHealthSnapshot:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        reasons=[]; delivery=100.0; communication=100.0; scope_score=100.0; relationship=100.0
        overdue=self.conn.execute("SELECT COUNT(*) FROM work_items WHERE workspace_id=? AND status!='shipped' AND needed_by IS NOT NULL AND needed_by < date('now')",(workspace_id,)).fetchone()[0]
        unanswered=len(self.unanswered_messages(organization_id,workspace_id,person_id))
        open_risks=self.conn.execute("SELECT COUNT(*) FROM risks WHERE workspace_id=? AND status='open'",(workspace_id,)).fetchone()[0]
        if overdue: delivery=max(0,100-overdue*12); reasons.append(f"{overdue} overdue work items")
        if unanswered: communication=max(0,100-unanswered*10); reasons.append(f"{unanswered} unanswered client messages")
        if open_risks: relationship=max(0,100-open_risks*8); reasons.append(f"{open_risks} open risks")
        usage=self.conn.execute("""SELECT u.*,a.included_quantity FROM scope_usage u JOIN scope_allowances a ON a.id=u.allowance_id
            WHERE u.workspace_id=? ORDER BY u.calculated_at DESC LIMIT 1""",(workspace_id,)).fetchone()
        if usage and usage["included_quantity"]:
            pct=(usage["delivered_quantity"]+usage["in_review_quantity"]+usage["requested_quantity"])/usage["included_quantity"]*100
            if pct>100: scope_score=max(0,100-(pct-100)); reasons.append(f"scope usage {pct:.0f}%")
        overall=round((delivery+communication+scope_score+relationship)/4,1)
        prev=self.conn.execute("SELECT overall FROM client_health_snapshots WHERE workspace_id=? ORDER BY calculated_at DESC LIMIT 1",(workspace_id,)).fetchone()
        previous=float(prev[0]) if prev else None; trend="stable" if previous is None or previous==overall else ("up" if overall>previous else "down")
        item=ClientHealthSnapshot(self.new_id("health"),organization_id,workspace_id,overall,relationship,delivery,None,None,
            communication,scope_score,None,tuple(reasons),"; ".join(reasons) or "No negative operational signals",previous,trend,_now())
        self.conn.execute("INSERT INTO client_health_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
            item.id,item.organization_id,item.workspace_id,item.overall,item.relationship,item.delivery,item.performance,item.finance,
            item.communication,item.scope,item.sentiment,json.dumps(item.contributing_signals),item.explanation,item.previous_score,item.trend,item.calculated_at.isoformat()))
        self.conn.commit(); return item
