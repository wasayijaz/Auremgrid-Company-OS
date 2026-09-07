from __future__ import annotations

import json
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


class ClientOperationsRelationshipsMixin:
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
