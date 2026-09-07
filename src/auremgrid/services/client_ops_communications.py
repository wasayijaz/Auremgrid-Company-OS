from __future__ import annotations

import hashlib
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


class ClientOperationsCommunicationsMixin:
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
        kind: str, statement: str, confidence: float, proposed_targets: list[dict[str, str]] | None = None) -> dict[str,Any]:
        self.authorize(organization_id,workspace_id,person_id,write=True)
        if not self.conn.execute("SELECT id FROM meetings WHERE workspace_id=? AND id=?",(workspace_id,meeting_id)).fetchone(): raise NotFoundError("meeting not found")
        if kind not in {"decision","commitment","action_item","request","concern","preference"}: raise ValidationError("invalid meeting output kind")
        item={"id":self.new_id("meetingoutput"),"meeting_id":meeting_id,"kind":kind,"statement":statement,"confidence":confidence,
            "status":"proposed","linked_entity_type":None,"linked_entity_id":None,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO meeting_outputs VALUES (?,?,?,?,?,?,?,?,?)",tuple(item.values()))
        signal_type={"decision":"decision","action_item":"task_candidate","request":"request","concern":"risk"}.get(kind,"information")
        routes = []
        for target in proposed_targets or []:
            target_type = str(target.get("type") or target.get("principal_type") or "person").lower()
            target_id = str(target.get("id") or target.get("person_id") or target.get("agent_id") or "").strip()
            if target_type == "person": self._require_active_workspace_person(organization_id, workspace_id, target_id)
            elif target_type == "agent": self._require_eligible_roster_agent(organization_id, workspace_id, target_id)
            else: raise ValidationError("meeting output route target type must be person or agent")
            route = {"id": self.new_id("meetingroute"), "organization_id": organization_id, "workspace_id": workspace_id, "meeting_id": meeting_id, "meeting_output_id": item["id"], "target_type": target_type, "target_id": target_id, "status": "proposed", "proposed_by_person_id": person_id, "created_at": _now().isoformat()}
            self.conn.execute("INSERT INTO meeting_output_routes VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(route.values())); routes.append(route)
        self.create_signal(organization_id,workspace_id,person_id,signal_type,"meeting",statement,item["id"],confidence); self.conn.commit(); return {**item, "proposed_routes": routes}

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
