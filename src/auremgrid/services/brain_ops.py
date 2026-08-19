from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.models import Citation, Fact


def _now() -> datetime: return datetime.now(timezone.utc).replace(microsecond=0)
def _norm(value: str) -> str: return re.sub(r"[^a-z0-9]+"," ",value.lower()).strip()


class BrainOperations:
    def __init__(self, os: Any) -> None: self.os=os; self.conn=os.store.conn

    def create_entity(self, organization_id: str, workspace_id: str | None, person_id: str,
        canonical_name: str, type: str, aliases: list[str] | None = None) -> dict[str, Any]:
        self._authorize(organization_id,workspace_id,person_id,True)
        item={"id":self.os._new_id("entity") if hasattr(self.os,"_new_id") else self._id("entity"),"organization_id":organization_id,
            "workspace_id":workspace_id,"canonical_name":canonical_name.strip(),"type":type,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO entities VALUES (?,?,?,?,?,?)",tuple(item.values()))
        for alias in [canonical_name,*(aliases or [])]: self._add_alias(item["id"],alias,1.0,"approved",None)
        self.conn.commit(); return item

    def propose_alias(self, organization_id: str, workspace_id: str | None, person_id: str, entity_id: str,
        alias: str, confidence: float, source_id: str | None = None) -> dict[str, Any]:
        self._authorize(organization_id,workspace_id,person_id,True)
        entity=self.conn.execute("SELECT * FROM entities WHERE organization_id=? AND id=?",(organization_id,entity_id)).fetchone()
        if entity is None or entity["workspace_id"]!=workspace_id: raise NotFoundError("entity not found")
        status="approved" if confidence>=0.95 else "proposed"
        return self._add_alias(entity_id,alias,confidence,status,source_id)

    def resolve_entity(self, organization_id: str, workspace_id: str | None, person_id: str, name: str) -> dict[str, Any]:
        self._authorize(organization_id,workspace_id,person_id,False); normalized=_norm(name)
        rows=self.conn.execute("""SELECT e.*,a.alias,a.confidence FROM entity_aliases a JOIN entities e ON e.id=a.entity_id
            WHERE e.organization_id=? AND (e.workspace_id=? OR e.workspace_id IS NULL) AND a.normalized_alias=? AND a.status='approved'""",
            (organization_id,workspace_id,normalized)).fetchall()
        if len(rows)!=1: return {"status":"unknown" if not rows else "ambiguous","candidates":[dict(r) for r in rows]}
        return {"status":"resolved","entity":dict(rows[0])}

    def merge_entities(self, organization_id: str, workspace_id: str | None, person_id: str, source_id: str,
        target_id: str, confidence: float, reason: str) -> dict[str, Any]:
        membership=self.os.company.org_membership(organization_id,person_id)
        if membership is None or membership.role not in {"owner","admin"}: raise AuthorizationError("admin required for entity merge")
        if confidence<0.9: raise ValidationError("low-confidence entities cannot be merged")
        entities=self.conn.execute("SELECT * FROM entities WHERE organization_id=? AND id IN (?,?)",(organization_id,source_id,target_id)).fetchall()
        if len(entities)!=2 or any(e["workspace_id"]!=workspace_id for e in entities): raise NotFoundError("entities not found in scope")
        self.conn.execute("UPDATE entity_aliases SET entity_id=? WHERE entity_id=?",(target_id,source_id)); item={"id":self._id("merge"),"organization_id":organization_id,
            "source_entity_id":source_id,"target_entity_id":target_id,"decided_by_person_id":person_id,"confidence":confidence,"reason":reason,"merged_at":_now().isoformat()}
        self.conn.execute("INSERT INTO entity_merge_history VALUES (?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def create_proposal(self, organization_id: str, workspace_id: str | None, proposer_type: str, proposer_id: str,
        kind: str, content: str, payload: dict[str, Any], evidence: str, confidence: float, source_id: str | None = None) -> dict[str, Any]:
        if kind not in {"memory","fact","decision"} or not evidence.strip(): raise ValidationError("proposal kind and evidence are required")
        item={"id":self._id("proposal"),"organization_id":organization_id,"workspace_id":workspace_id,"kind":kind,
            "proposed_by_type":proposer_type,"proposed_by_id":proposer_id,"content":content,"structured_payload":json.dumps(payload),
            "source_id":source_id,"evidence":evidence,"confidence":confidence,"status":"pending","reviewed_by_person_id":None,
            "reviewed_at":None,"promoted_type":None,"promoted_id":None,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO memory_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item

    def review_proposal(self, organization_id: str, person_id: str, proposal_id: str, action: str,
        edited_payload: dict[str, Any] | None = None) -> dict[str, Any]:
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
        now=_now().isoformat(); status="approved" if action=="approve" else "rejected"
        self.conn.execute("UPDATE memory_proposals SET status=?,structured_payload=?,reviewed_by_person_id=?,reviewed_at=?,promoted_type=?,promoted_id=? WHERE id=?",
            (status,json.dumps(payload),person_id,now,promoted_type,promoted_id,proposal_id)); self.conn.commit(); return dict(self.conn.execute("SELECT * FROM memory_proposals WHERE id=?",(proposal_id,)).fetchone())

    def knowledge_health(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.os._require_person_access(organization_id,workspace_id,person_id)
        issues=[]
        conflicts=self.conn.execute("SELECT conflict_group,COUNT(*) count FROM facts WHERE workspace_id=? AND conflict_group IS NOT NULL AND superseded_by IS NULL GROUP BY conflict_group HAVING COUNT(*)>1",(workspace_id,)).fetchall()
        for row in conflicts: issues.append(("conflicting_facts","high","fact",row["conflict_group"],f"{row['count']} current facts conflict",row["conflict_group"]))
        low=self.conn.execute("SELECT id,confidence FROM facts WHERE workspace_id=? AND confidence<0.6",(workspace_id,)).fetchall()
        for row in low: issues.append(("low_confidence_fact","medium","fact",row["id"],f"Fact confidence is {row['confidence']}",row["id"]))
        decisions=self.conn.execute("SELECT id FROM decisions WHERE workspace_id=? AND source_id IS NULL AND evidence=''",(workspace_id,)).fetchall()
        for row in decisions: issues.append(("unsourced_decision","high","decision",row["id"],"Decision has no source or evidence",row["id"]))
        proposals=self.conn.execute("SELECT id FROM memory_proposals WHERE workspace_id=? AND status='pending'",(workspace_id,)).fetchall()
        for row in proposals: issues.append(("pending_proposal","low","proposal",row["id"],"Proposal is waiting for review",row["id"]))
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
    def _add_alias(self,entity_id:str,alias:str,confidence:float,status:str,source_id:str|None)->dict[str,Any]:
        item={"id":self._id("alias"),"entity_id":entity_id,"alias":alias,"normalized_alias":_norm(alias),"confidence":confidence,"status":status,"source_id":source_id,"created_at":_now().isoformat()}
        self.conn.execute("INSERT INTO entity_aliases VALUES (?,?,?,?,?,?,?,?)",tuple(item.values())); self.conn.commit(); return item
    @staticmethod
    def _id(prefix:str)->str:
        import uuid
        return f"{prefix}_{uuid.uuid4().hex[:16]}"
