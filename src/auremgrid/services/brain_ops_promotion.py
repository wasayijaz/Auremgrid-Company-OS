from __future__ import annotations
from auremgrid.services.brain_ops_shared import *

class BrainOpsPromotionMixin:
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

    def create_proposal(self, organization_id: str, workspace_id: str | None, proposer_type: str, proposer_id: str,
            kind: str, content: str, payload: dict[str, Any], evidence: str, confidence: float, source_id: str | None = None) -> dict[str, Any]:
            if not hasattr(proposer_id,"person_id"): raise AuthorizationError("authenticated identity is required")
            identity=proposer_id
            identity.require("brain_propose")
            if identity.organization_id != organization_id or (workspace_id and identity.workspace_id not in {None,workspace_id}): raise AuthorizationError("identity is outside requested scope")
            if workspace_id and identity.workspace_id is not None: workspace_id = identity.workspace_id
            if source_id and workspace_id is not None and self.os.store.get_source(workspace_id,source_id) is None:
                raise NotFoundError("proposal evidence not found")
            proposer_id = identity.person_id
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
