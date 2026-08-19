from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.models import Citation, Fact


def _now() -> datetime: return datetime.now(timezone.utc)
def _transition_now() -> datetime:
    """Return a wall-clock instant strictly after this transition began.

    Some Windows clocks repeat a microsecond value across adjacent calls.  A
    review boundary must still sort after an `as_of` captured immediately
    before the review, so wait for the next representable UTC instant.
    """
    started=datetime.now(timezone.utc); current=started
    while current <= started:
        current=datetime.now(timezone.utc)
    return current
def _norm(value: str) -> str: return re.sub(r"[^a-z0-9]+"," ",value.lower()).strip()


class BrainOperations:
    def __init__(self, os: Any) -> None: self.os=os; self.conn=os.store.conn

    def create_entity(self, organization_id: str, workspace_id: str | None, person_id: str,
        canonical_name: str, type: str, aliases: list[str] | None = None) -> dict[str, Any]:
        self._authorize(organization_id,workspace_id,person_id,True)
        item={"id":self.os._new_id("entity") if hasattr(self.os,"_new_id") else self._id("entity"),"organization_id":organization_id,
            "workspace_id":workspace_id,"canonical_name":canonical_name.strip(),"type":type,"created_at":_now().isoformat()}
        self.conn.execute(
            "INSERT INTO entities(id,organization_id,workspace_id,canonical_name,type,created_at,status,merged_into,updated_at) VALUES (?,?,?,?,?,?, 'active', NULL, ?)",
            (item["id"],item["organization_id"],item["workspace_id"],item["canonical_name"],item["type"],item["created_at"],item["created_at"]),
        )
        self._state_event(organization_id,workspace_id,"canonical",item["id"],"inferred","entity created",None,person_id)
        self._add_alias(item["id"],canonical_name,1.0,"approved",None)
        for alias in (aliases or []): self._add_alias(item["id"],alias,1.0,"proposed",None)
        self.conn.commit(); return item

    def propose_alias(self, organization_id: str, workspace_id: str | None, person_id: str, entity_id: str,
        alias: str, confidence: float, source_id: str | None = None) -> dict[str, Any]:
        self._authorize(organization_id,workspace_id,person_id,True)
        entity=self.conn.execute("SELECT * FROM entities WHERE organization_id=? AND id=?",(organization_id,entity_id)).fetchone()
        if entity is None or entity["workspace_id"]!=workspace_id: raise NotFoundError("entity not found")
        status="proposed"
        return self._add_alias(entity_id,alias,confidence,status,source_id)

    def resolve_entity(self, organization_id: str, workspace_id: str | None, person_id: str, name: str,
        as_of: datetime | None = None) -> dict[str, Any]:
        self._authorize(organization_id,workspace_id,person_id,False); normalized=_norm(name)
        moment=(as_of or datetime.now(timezone.utc)).isoformat()
        candidates=self.conn.execute("""SELECT e.*,a.id AS alias_id,a.alias,a.confidence FROM entity_aliases a JOIN entities e ON e.id=a.entity_id
            WHERE e.organization_id=? AND (e.workspace_id=? OR e.workspace_id IS NULL)
              AND e.created_at<=? AND a.created_at<=? AND a.normalized_alias=? AND a.status='approved'""",
            (organization_id,workspace_id,moment,moment,normalized)).fetchall()
        rows=[]
        for candidate in candidates:
            lifecycle=self.conn.execute(
                "SELECT state FROM entity_alias_state_events WHERE alias_id=? AND organization_id=? "
                "AND workspace_id IS ? AND created_at<=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (candidate["alias_id"],organization_id,candidate["workspace_id"],moment),
            ).fetchone()
            redirect=self._entity_redirect(organization_id,candidate["workspace_id"],candidate["id"],moment)
            # A retired alias remains a valid historical pointer only when the
            # same scoped entity has already redirected by the requested time.
            if lifecycle is not None and lifecycle["state"] == "retired" and redirect is None:
                continue
            item=dict(candidate); seen=set()
            while item["id"] not in seen:
                seen.add(item["id"])
                target_id=self._entity_redirect(organization_id,item["workspace_id"],item["id"],moment)
                if target_id is None: break
                target=self.conn.execute(
                    "SELECT * FROM entities WHERE id=? AND organization_id=? AND workspace_id IS ? AND created_at<=?",
                    (target_id,organization_id,item["workspace_id"],moment),
                ).fetchone()
                if target is None: break
                item=dict(target)
            rows.append(item)
        resolved={row["id"]: row for row in rows}
        if len(resolved)!=1:
            return {"status":"unknown" if not resolved else "ambiguous","candidates":list(resolved.values())}
        return {"status":"resolved","entity":next(iter(resolved.values()))}

    def _entity_redirect(self, organization_id: str, workspace_id: str | None, source_id: str,
        moment: str) -> str | None:
        row=self.conn.execute("""
            SELECT h.target_entity_id FROM entity_merge_history h
            JOIN entities source ON source.id=h.source_entity_id
            JOIN entities target ON target.id=h.target_entity_id
            WHERE h.organization_id=? AND h.source_entity_id=? AND h.merged_at<=?
              AND source.workspace_id IS ? AND target.workspace_id IS ?
            ORDER BY h.merged_at DESC,h.id DESC LIMIT 1
        """,(organization_id,source_id,moment,workspace_id,workspace_id)).fetchone()
        return str(row["target_entity_id"]) if row is not None else None

    def merge_entities(self, organization_id: str, workspace_id: str | None, person_id: str, source_id: str,
        target_id: str, confidence: float, reason: str) -> dict[str, Any]:
        raise ValidationError("direct entity merge is disabled; create and approve a resolution proposal")

    def brain_propose(self, organization_id: str, workspace_id: str | None, person_id: str,
        kind: str, candidate_entity_ids: list[str], score: float, rationale: str,
        evidence: str, alias: str | None = None, source_id: str | None = None,
        target_id: str | None = None, evidence_refs: dict[str, list[str]] | None = None) -> dict[str, Any]:
        person_id = self._identity_person(organization_id,workspace_id,person_id,"brain_propose")
        if kind not in {"alias","merge"} or not candidate_entity_ids or not evidence.strip(): raise ValidationError("invalid resolution proposal")
        rows=self.conn.execute(f"SELECT id,workspace_id FROM entities WHERE organization_id=? AND id IN ({','.join('?' for _ in candidate_entity_ids)})",(organization_id,*candidate_entity_ids)).fetchall()
        if len(rows)!=len(set(candidate_entity_ids)) or any(row["workspace_id"]!=workspace_id for row in rows): raise NotFoundError("entity candidate not found")
        normalized_refs=self._validate_evidence_refs(workspace_id,source_id,evidence_refs)
        proposal={"id":self._id("resolution"),"organization_id":organization_id,"workspace_id":workspace_id,"kind":kind,"alias":alias,"source_entity_id":candidate_entity_ids[0] if kind=="merge" else None,"target_entity_id":target_id or (candidate_entity_ids[1] if kind=="merge" and len(candidate_entity_ids)>1 else candidate_entity_ids[0]),"candidate_entity_ids":json.dumps(candidate_entity_ids),"score":float(score),"rationale":rationale,"status":"pending","proposed_by_person_id":person_id,"reviewed_by_person_id":None,"evidence_source_id":source_id,"evidence":evidence,"evidence_refs":json.dumps(normalized_refs,sort_keys=True),"created_at":_now().isoformat(),"reviewed_at":None}
        self.conn.execute("""INSERT INTO entity_resolution_proposals(
            id,organization_id,workspace_id,kind,alias,source_entity_id,target_entity_id,candidate_entity_ids,
            score,rationale,status,proposed_by_person_id,reviewed_by_person_id,evidence_source_id,evidence,
            evidence_refs,created_at,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",tuple(proposal.values())); self.conn.commit(); return proposal

    def _validate_evidence_refs(self, workspace_id: str | None, source_id: str | None,
        evidence_refs: dict[str, list[str]] | None) -> dict[str, list[str]]:
        allowed={"sources":"sources","documents":"documents","facts":"facts","relations":"relations"}
        if any(key not in allowed or not isinstance(values,list) for key,values in (evidence_refs or {}).items()):
            raise ValidationError("evidence refs are invalid")
        refs={key: sorted({str(value) for value in values}) for key,values in (evidence_refs or {}).items()}
        if source_id:
            refs.setdefault("sources",[])
            refs["sources"]=sorted(set(refs["sources"]+[source_id]))
        if workspace_id is None and any(refs.values()): raise NotFoundError("proposal evidence not found")
        for key,table in allowed.items():
            ids=refs.get(key,[])
            if not ids: continue
            placeholders=','.join('?' for _ in ids)
            found={str(row["id"]) for row in self.conn.execute(
                f"SELECT id FROM {table} WHERE workspace_id=? AND id IN ({placeholders})",
                (workspace_id,*ids),
            ).fetchall()}
            if found != set(ids): raise NotFoundError("proposal evidence not found")
        return refs

    def brain_promote(self, organization_id: str, workspace_id: str | None, person_id: str, proposal_id: str, action: str) -> dict[str, Any]:
        with self.os.store.atomic(immediate=True):
            return self._brain_promote_impl(organization_id, workspace_id, person_id, proposal_id, action)

    def brain_promote_fact(self, identity: Any, proposal_id: str, action: str) -> dict[str, Any]:
        if not hasattr(identity, "person_id"):
            raise AuthorizationError("authenticated identity is required")
        identity.require("brain_promote")
        organization_id, person_id = identity.organization_id, identity.person_id
        row=self.conn.execute("SELECT * FROM memory_proposals WHERE organization_id=? AND id=?",(organization_id,proposal_id)).fetchone()
        if row is None or row["workspace_id"] != identity.workspace_id: raise NotFoundError("proposal not found")
        if row["kind"] != "fact" or action not in {"approve","reject"}: raise ValidationError("fact proposal action is invalid")
        prior=self.conn.execute("SELECT * FROM knowledge_proposal_decisions WHERE proposal_id=?",(proposal_id,)).fetchone()
        if prior is not None:
            if prior["action"] != action: raise ValidationError("proposal already decided")
            result=dict(row); result["status"]="approved" if action=="approve" else "rejected"
            if action == "approve":
                payload=json.loads(row["structured_payload"])
                promoted=self.conn.execute(
                    "SELECT id FROM facts WHERE workspace_id=? AND source_id=? AND subject=? AND predicate=? AND object=? ORDER BY recorded_at,id LIMIT 1",
                    (row["workspace_id"],row["source_id"],str(payload.get("subject")),str(payload.get("predicate")),str(payload.get("object"))),
                ).fetchone()
                if promoted is not None:
                    result.update({"promoted_type":"fact","promoted_id":promoted["id"]})
            return result
        promoted_type = promoted_id = None
        with self.os.store.atomic(immediate=True):
            if action == "approve":
                payload=json.loads(row["structured_payload"]); source=self.os.store.get_source(row["workspace_id"],row["source_id"])
                doc_row=self.conn.execute("SELECT * FROM documents WHERE workspace_id=? AND source_id=? ORDER BY recorded_at DESC LIMIT 1",(row["workspace_id"],row["source_id"])).fetchone()
                if source is None or doc_row is None: raise NotFoundError("proposal evidence not found")
                document=self.os.store._document_from_row(doc_row); now=_now(); subject,predicate,obj=str(payload["subject"]),str(payload["predicate"]),str(payload["object"])
                # Claim identity is normalized in the same way as ingestion;
                # SQLite's default text comparison is case-sensitive and would
                # otherwise let ``Plan`` and ``plan`` escape the shared conflict
                # group.  Scope the read to the workspace, then compare the
                # canonicalized subject/predicate/object in Python.
                conflicts=[item for item in self.conn.execute(
                    "SELECT id,subject,predicate,object,conflict_group FROM facts "
                    "WHERE workspace_id=? AND superseded_by IS NULL",
                    (row["workspace_id"],),
                ).fetchall() if _norm(str(item["subject"])) == _norm(subject)
                    and _norm(str(item["predicate"])) == _norm(predicate)]
                conflict_group=payload.get("conflict_group")
                if any(_norm(str(item["object"])) != _norm(obj) for item in conflicts):
                    conflict_group=conflict_group or f"{_norm(subject)}::{_norm(predicate)}"
                    for item in conflicts:
                        if item["conflict_group"] is None: self.conn.execute("UPDATE facts SET conflict_group=? WHERE id=?",(conflict_group,item["id"]))
                        self._state_event(organization_id,row["workspace_id"],"fact",item["id"],"conflicted","incompatible current fact",None,person_id)
                fact=Fact(self._id("fact"),row["workspace_id"],source.id,document.id,subject,predicate,obj,now,None,source.observed_at,now,float(row["confidence"]),None,conflict_group,Citation(source.id,source.source_key,source.locator,source.content_hash,row["evidence"],source.observed_at,now,None,float(row["confidence"])))
                self.os.store.create_fact(fact)
                self._state_event(organization_id,row["workspace_id"],"fact",fact.id,"conflicted" if conflict_group else "verified","human proposal approval",source.id,person_id)
                promoted_type, promoted_id = "fact", fact.id
            decision_at=_transition_now().isoformat()
            self.conn.execute("INSERT INTO knowledge_proposal_decisions VALUES (?,?,?,?,?,?,?)",(self._id("knowledge_decision"),proposal_id,organization_id,row["workspace_id"],action,person_id,decision_at))
        result=dict(row); result.update({"status":"approved" if action=="approve" else "rejected","promoted_type":promoted_type,"promoted_id":promoted_id,"reviewed_by_person_id":person_id,"reviewed_at":decision_at}); return result

    def _brain_promote_impl(self, organization_id: str, workspace_id: str | None, person_id: str, proposal_id: str, action: str) -> dict[str, Any]:
        person_id = self._identity_person(organization_id,workspace_id,person_id,"brain_promote")
        row=self.conn.execute("SELECT * FROM entity_resolution_proposals WHERE organization_id=? AND id=?",(organization_id,proposal_id)).fetchone()
        if row is None or row["workspace_id"]!=workspace_id: raise NotFoundError("resolution proposal not found")
        prior_decision=self.conn.execute("SELECT * FROM entity_resolution_decisions WHERE proposal_id=?",(proposal_id,)).fetchone()
        if prior_decision is not None:
            if prior_decision["action"] != action: raise ValidationError("resolution already decided")
            result=dict(row); result["status"]="approved" if action=="approve" else "rejected"; result["reviewed_by_person_id"]=prior_decision["reviewer_person_id"]; result["reviewed_at"]=prior_decision["created_at"]; return result
        if action not in {"approve","reject"}: raise ValidationError("pending proposal required")
        now=_transition_now().isoformat()
        if action=="approve" and row["kind"]=="merge":
            source_id,target_id=row["source_entity_id"],row["target_entity_id"]
            if source_id == target_id: raise ValidationError("entity merge cycle")
            source_row=self.conn.execute("SELECT status FROM entities WHERE id=? AND organization_id=? AND workspace_id=?",(source_id,organization_id,workspace_id)).fetchone()
            if source_row is None or source_row["status"] != "active": raise ValidationError("entity merge source is not active")
            target_row=self.conn.execute("SELECT status,merged_into FROM entities WHERE id=? AND organization_id=? AND workspace_id=?",(target_id,organization_id,workspace_id)).fetchone()
            if target_row is None or target_row["status"] != "active": raise ValidationError("entity merge target is not active")
            seen={source_id}; cursor=target_id
            while cursor:
                if cursor in seen: raise ValidationError("entity merge cycle")
                seen.add(cursor)
                nxt=self.conn.execute("SELECT merged_into FROM entities WHERE id=?",(cursor,)).fetchone()
                cursor=nxt["merged_into"] if nxt else None
            self.conn.execute("UPDATE entities SET status='merged',merged_into=?,updated_at=? WHERE id=? AND status='active'",(target_id,now,source_id))
            self.conn.execute("INSERT INTO entity_merge_history VALUES (?,?,?,?,?,?,?,?)",(self._id("merge"),organization_id,source_id,target_id,person_id,row["score"],row["rationale"],now))
            aliases=self.conn.execute("SELECT id FROM entity_aliases WHERE entity_id=?",(source_id,)).fetchall()
            for alias_row in aliases:
                self.conn.execute(
                    "INSERT INTO entity_alias_state_events VALUES (?,?,?,?,?,?,?,?)",
                    (self._id("alias_state"),alias_row["id"],organization_id,workspace_id,"retired","entity merged",person_id,now),
                )
        elif action=="approve" and row["kind"]=="alias": self._add_alias(row["target_entity_id"],row["alias"],row["score"],"approved",row["evidence_source_id"],commit=False)
        self.conn.execute("INSERT INTO entity_resolution_decisions VALUES (?,?,?,?,?,?,?,?)",(self._id("resolution_decision"),proposal_id,organization_id,workspace_id,action,person_id,row["rationale"],now))
        result=dict(row); result["status"]="approved" if action=="approve" else "rejected"; result["reviewed_by_person_id"]=person_id; result["reviewed_at"]=now; return result

    def _state_event(self, organization_id: str, workspace_id: str | None, subject_type: str, subject_id: str, state: str, reason: str, evidence_source_id: str | None, actor_id: str, effective_from: datetime | None = None, effective_until: datetime | None = None) -> str:
        now=effective_from or datetime.now(timezone.utc); event_id=self._id("kstate")
        prior=self.conn.execute(
            "SELECT id,event_sequence FROM knowledge_state_events WHERE organization_id=? AND workspace_id IS ? "
            "AND subject_type=? AND subject_id=? ORDER BY event_sequence DESC LIMIT 1",
            (organization_id,workspace_id,subject_type,subject_id),
        ).fetchone()
        sequence=1 if prior is None else int(prior["event_sequence"])+1
        self.conn.execute("""INSERT INTO knowledge_state_events(
            id,organization_id,workspace_id,subject_type,subject_id,state,reason,evidence_source_id,actor_id,
            effective_from,effective_until,recorded_at,event_sequence,supersedes_event_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            event_id,organization_id,workspace_id,subject_type,subject_id,state,reason,evidence_source_id,actor_id,
            now.isoformat(),effective_until.isoformat() if effective_until else None,datetime.now(timezone.utc).isoformat(),
            sequence,None if prior is None else prior["id"],
        )); return event_id

    def record_knowledge_state(self, organization_id: str, workspace_id: str | None, subject_type: str, subject_id: str, state: str, reason: str, actor_id: str, evidence_source_id: str | None = None, effective_from: datetime | None = None, effective_until: datetime | None = None) -> dict[str, Any]:
        if not hasattr(actor_id,"person_id"):
            raise AuthorizationError("authenticated identity is required")
        actor_id=self._identity_person(organization_id,workspace_id,actor_id,"brain_promote")
        with self.os.store.atomic(immediate=True):
            event_id=self._state_event(organization_id,workspace_id,subject_type,subject_id,state,reason,evidence_source_id,actor_id,effective_from,effective_until)
        return dict(self.conn.execute("SELECT * FROM knowledge_state_events WHERE id=?",(event_id,)).fetchone())

    def knowledge_state(self, organization_id: str, workspace_id: str, person_id: str, subject_type: str, subject_id: str, as_of: datetime | None = None) -> dict[str, Any]:
        self._authorize(organization_id,workspace_id,person_id,False)
        row=self._knowledge_state_row(workspace_id,subject_type,subject_id,as_of,organization_id)
        return dict(row) if row is not None else {"state":"unknown","subject_id":subject_id}

    def _knowledge_state_row(self, workspace_id: str, subject_type: str, subject_id: str,
        as_of: datetime | None = None, organization_id: str | None = None) -> Any:
        moment=(as_of or datetime.now(timezone.utc)).isoformat()
        parameters: list[Any]=[workspace_id,subject_type,subject_id,moment,moment]
        organization_clause=""
        if organization_id is not None:
            organization_clause=" AND organization_id=?"
            parameters.append(organization_id)
        return self.conn.execute(f"""SELECT * FROM knowledge_state_events
            WHERE workspace_id=? AND subject_type=? AND subject_id=?
              AND effective_from<=? AND (effective_until IS NULL OR effective_until>?)
              {organization_clause}
            ORDER BY effective_from DESC,event_sequence DESC,recorded_at DESC,id DESC LIMIT 1""",parameters).fetchone()

    def resolve_fact_conflict(self, identity: Any, conflict_group: str, winner_fact_id: str) -> dict[str, Any]:
        if not hasattr(identity, "person_id"): raise AuthorizationError("authenticated identity is required")
        identity.require("brain_promote")
        rows=self.conn.execute("SELECT * FROM facts WHERE workspace_id=? AND conflict_group=? AND superseded_by IS NULL",(identity.workspace_id,conflict_group)).fetchall()
        if not rows or winner_fact_id not in {row["id"] for row in rows}: raise NotFoundError("conflict not found")
        with self.os.store.atomic(immediate=True):
            for row in rows:
                self._state_event(identity.organization_id,identity.workspace_id,"fact",row["id"],"verified" if row["id"]==winner_fact_id else "stale","human conflict resolution",row["source_id"],identity.person_id)
        return {"conflict_group":conflict_group,"winner_fact_id":winner_fact_id,"resolved":True}

    def create_proposal(self, organization_id: str, workspace_id: str | None, proposer_type: str, proposer_id: str,
        kind: str, content: str, payload: dict[str, Any], evidence: str, confidence: float, source_id: str | None = None) -> dict[str, Any]:
        if not hasattr(proposer_id,"person_id"): raise AuthorizationError("authenticated identity is required")
        proposer_id.require("brain_propose")
        if proposer_id.organization_id != organization_id or (workspace_id and proposer_id.workspace_id not in {None,workspace_id}): raise AuthorizationError("identity is outside requested scope")
        if workspace_id and proposer_id.workspace_id is not None: workspace_id = proposer_id.workspace_id
        proposer_id = proposer_id.person_id
        if source_id and workspace_id is not None and self.os.store.get_source(workspace_id,source_id) is None: raise NotFoundError("proposal evidence not found")
        if kind not in {"memory","fact","decision"} or not evidence.strip(): raise ValidationError("proposal kind and evidence are required")
        item={"id":self._id("proposal"),"organization_id":organization_id,"workspace_id":workspace_id,"kind":kind,
            "proposed_by_type":proposer_type,"proposed_by_id":proposer_id,"content":content,"structured_payload":json.dumps(payload),
            "source_id":source_id,"evidence":evidence,"confidence":confidence,"status":"pending","reviewed_by_person_id":None,
            "reviewed_at":None,"promoted_type":None,"promoted_id":None,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO memory_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def list_memory_proposals(self, organization_id: str, workspace_id: str, person_id: str,
        as_of: datetime | None = None) -> list[dict[str, Any]]:
        self._authorize(organization_id,workspace_id,person_id,False)
        decision_cutoff=(as_of or datetime.now(timezone.utc)).isoformat()
        rows=self.conn.execute("""SELECT p.*,d.action AS decision_action,
                d.reviewer_person_id AS decision_reviewer_person_id,d.created_at AS decision_created_at
            FROM memory_proposals p
            LEFT JOIN knowledge_proposal_decisions d
              ON d.proposal_id=p.id AND d.organization_id=p.organization_id
             AND d.workspace_id IS p.workspace_id AND d.created_at<=?
            WHERE p.organization_id=? AND p.workspace_id=? AND p.created_at<=?
            ORDER BY p.created_at DESC,p.id DESC""",
            (decision_cutoff,organization_id,workspace_id,decision_cutoff),
        ).fetchall()
        result=[]
        for row in rows:
            item=dict(row); action=item.pop("decision_action")
            reviewer=item.pop("decision_reviewer_person_id"); decided_at=item.pop("decision_created_at")
            item["status"]="approved" if action=="approve" else "rejected" if action=="reject" else "pending"
            item["reviewed_by_person_id"]=reviewer; item["reviewed_at"]=decided_at
            result.append(item)
        return result

    def review_proposal(self, organization_id: str, person_id: str, proposal_id: str, action: str,
        edited_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        raise ValidationError("legacy proposal review is disabled; use authenticated brain_promote")
        # Retained below only as historical reference for migration operators.
        membership=self.os.company.org_membership(organization_id,person_id)
        if membership is None: raise AuthorizationError("organization membership required")
        row=self.conn.execute("SELECT * FROM memory_proposals WHERE organization_id=? AND id=?",(organization_id,proposal_id)).fetchone()
        if row is None: raise NotFoundError("proposal not found")
        if row["status"]!="pending" or action not in {"approve","reject"}: raise ValidationError("pending proposal and valid action required")
        promoted_type=promoted_id=None; payload=edited_payload or json.loads(row["structured_payload"])
        if action=="approve":
            if row["kind"]=="decision":
                decision=self.os.create_decision(organization_id,person_id,payload["statement"],payload["rationale"],row["workspace_id"],
                    payload.get("project_id"),row["source_id"],row["evidence"],payload.get("tags",[])); promoted_type,promoted_id="decision",decision.id
            elif row["kind"]=="memory":
                promoted_id=self._id("knowledge");self.conn.execute("INSERT INTO canonical_knowledge VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (promoted_id,organization_id,row["workspace_id"],"memory",row["content"],json.dumps(payload),row["source_id"],row["evidence"],person_id,_now().isoformat()));promoted_type="memory"
            else:
                if not row["workspace_id"] or not row["source_id"]: raise ValidationError("fact promotion requires workspace and source evidence")
                source=self.os.store.get_source(row["workspace_id"],row["source_id"])
                document_row=self.conn.execute("SELECT * FROM documents WHERE workspace_id=? AND source_id=? ORDER BY recorded_at DESC LIMIT 1",(row["workspace_id"],row["source_id"])).fetchone()
                if source is None or document_row is None: raise NotFoundError("proposal source evidence not found")
                document=self.os.store._document_from_row(document_row);now=_now();promoted_id=self._id("fact")
                fact=Fact(promoted_id,row["workspace_id"],source.id,document.id,str(payload["subject"]),str(payload["predicate"]),str(payload["object"]),now,None,source.observed_at,now,float(row["confidence"]),None,payload.get("conflict_group"),Citation(source.id,source.source_key,source.locator,source.content_hash,row["evidence"],source.observed_at,now,None,float(row["confidence"])))
                self.os.store.create_fact(fact);self.os.stack.ingest_fact(fact);promoted_type="fact"
        # memory_proposals is historical input and is immutable.  The decision
        # row above is the durable status; return a derived view without
        # rewriting the proposal (which is protected by append-only triggers).
        status="approved" if action=="approve" else "rejected"
        result=dict(row)
        result.update({"status":status,"promoted_type":promoted_type,"promoted_id":promoted_id,"reviewed_by_person_id":person_id,"reviewed_at":_now().isoformat()})
        return result

    def knowledge_health(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.os._require_person_access(organization_id,workspace_id,person_id)
        issues=[]
        current=[]
        for fact in self.conn.execute("SELECT * FROM facts WHERE workspace_id=? AND superseded_by IS NULL",(workspace_id,)).fetchall():
            state=self._knowledge_state_row(workspace_id,"fact",fact["id"])
            if state is not None and state["state"] == "stale":
                continue
            current.append(fact)
        grouped={}
        for fact in current:
            if fact["conflict_group"]:
                grouped.setdefault(fact["conflict_group"],[]).append(fact)
        for group, rows in grouped.items():
            if len(rows)>1:
                issues.append(("conflicting_facts","high","fact",group,f"{len(rows)} current facts conflict",group))
        low=[row for row in current if row["confidence"]<0.6]
        for row in low: issues.append(("low_confidence_fact","medium","fact",row["id"],f"Fact confidence is {row['confidence']}",row["id"]))
        decisions=self.conn.execute("SELECT id FROM decisions WHERE workspace_id=? AND source_id IS NULL AND evidence=''",(workspace_id,)).fetchall()
        for row in decisions: issues.append(("unsourced_decision","high","decision",row["id"],"Decision has no source or evidence",row["id"]))
        proposals=self.list_memory_proposals(organization_id,workspace_id,person_id)
        for row in proposals:
            if row["status"] == "pending":
                issues.append(("pending_proposal","low","proposal",row["id"],"Proposal is waiting for review",row["id"]))
        self.conn.execute("UPDATE knowledge_health_issues SET status='resolved',resolved_at=? WHERE workspace_id=? AND status='open'",(_now().isoformat(),workspace_id))
        for type,severity,entity_type,entity_id,explanation,evidence in issues:
            self.conn.execute("INSERT INTO knowledge_health_issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(self._id("kh"),organization_id,workspace_id,type,severity,entity_type,entity_id,explanation,evidence,"open",_now().isoformat(),None))
        self.conn.commit(); projection_rows=self.conn.execute("SELECT * FROM projection_state WHERE workspace_id=?",(workspace_id,)).fetchall()
        if not projection_rows:
            self.os.rebuild_projections(workspace_id)
            projection_rows=self.conn.execute("SELECT * FROM projection_state WHERE workspace_id=?",(workspace_id,)).fetchall()
        projections=[dict(r) for r in projection_rows]
        return {"issues":[dict(r) for r in self.conn.execute("SELECT * FROM knowledge_health_issues WHERE workspace_id=? AND status='open' ORDER BY severity",(workspace_id,)).fetchall()],"projections":projections}

    def _authorize(self, organization_id: str, workspace_id: str | None, person_id: str, write: bool) -> None:
        if workspace_id: self.os._require_person_access(organization_id,workspace_id,person_id,write=write)
        elif self.os.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")

    def _identity_person(self, organization_id: str, workspace_id: str | None, identity: Any, capability: str) -> str:
        if hasattr(identity, "person_id"):
            if identity.organization_id != organization_id or (workspace_id and identity.workspace_id not in {None, workspace_id}):
                raise AuthorizationError("identity is outside requested scope")
            identity.require(capability)
            return identity.person_id
        raise AuthorizationError("authenticated identity is required")
    def _add_alias(self,entity_id:str,alias:str,confidence:float,status:str,source_id:str|None,commit:bool=True)->dict[str,Any]:
        item={"id":self._id("alias"),"entity_id":entity_id,"alias":alias,"normalized_alias":_norm(alias),"confidence":confidence,"status":status,"source_id":source_id,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO entity_aliases(id,entity_id,alias,normalized_alias,confidence,status,source_id,created_at,reviewed_by_person_id,reviewed_at,evidence,retired_at) VALUES (?,?,?,?,?,?,?, ?,NULL,NULL,NULL,NULL)",tuple(item.values()));
        if commit: self.conn.commit()
        return item
    @staticmethod
    def _id(prefix:str)->str:
        import uuid
        return f"{prefix}_{uuid.uuid4().hex[:16]}"
