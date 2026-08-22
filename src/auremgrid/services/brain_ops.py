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


_DOMAIN_SUFFIXES = frozenset({"com", "net", "org", "io", "co", "ca", "ai", "app", "dev", "uk", "us"})


def _forms(value: str) -> set[str]:
    """Return conservative comparison forms for a name, email, or locator."""
    words = _norm(value).split()
    if not words:
        return set()
    forms = {"".join(words)}
    while words and words[-1] in _DOMAIN_SUFFIXES:
        words.pop()
    if words:
        forms.add("".join(words))
    return {item for item in forms if item}


def _variant_score(left: set[str], right: set[str]) -> float:
    """Score exact and domain/name variants without treating a token as proof."""
    score = 0.0
    for candidate in left:
        for evidence in right:
            if candidate == evidence:
                score = max(score, 1.0)
            elif min(len(candidate), len(evidence)) >= 5 and (candidate in evidence or evidence in candidate):
                score = max(score, 0.85 * min(len(candidate), len(evidence)) / max(len(candidate), len(evidence)))
    return score


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

    def entity_resolution_candidates(self, organization_id: str, workspace_id: str,
        identity: Any, name: str, limit: int = 8) -> list[dict[str, Any]]:
        """Return conservative, evidence-backed candidates without creating proposals."""
        self._identity_person(organization_id, workspace_id, identity, "brain_propose")
        if limit < 1 or limit > 50:
            raise ValidationError("limit must be between 1 and 50")
        allowed = self._allowed_source_ids(identity, workspace_id)
        if not allowed:
            return []
        query_forms = _forms(name)
        if not query_forms:
            raise ValidationError("entity name is required")
        sources = self.os.store.allowed_sources(
            workspace_id,
            self.os._require_actor(workspace_id, self.os.auth.actor_for_identity(identity, workspace_id)),
        )
        source_by_id = {source.id: source for source in sources}
        marks = ",".join("?" for _ in allowed)
        documents = self.conn.execute(
            f"SELECT id,source_id FROM documents WHERE workspace_id=? AND source_id IN ({marks}) ORDER BY recorded_at DESC,id DESC",
            (workspace_id, *sorted(allowed)),
        ).fetchall()
        document_by_source: dict[str, list[str]] = {}
        for row in documents:
            document_by_source.setdefault(str(row["source_id"]), []).append(str(row["id"]))
        facts = self.conn.execute(
            f"SELECT id,source_id,subject,predicate,object FROM facts WHERE workspace_id=? AND source_id IN ({marks})",
            (workspace_id, *sorted(allowed)),
        ).fetchall()
        relations = self.conn.execute(
            f"SELECT id,source_id,from_entity,relation,to_entity FROM relations WHERE workspace_id=? AND source_id IN ({marks})",
            (workspace_id, *sorted(allowed)),
        ).fetchall()
        entities = self.conn.execute(
            "SELECT * FROM entities WHERE organization_id=? AND workspace_id=? AND status='active' ORDER BY id",
            (organization_id, workspace_id),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for entity in entities:
            aliases = self.conn.execute(
                "SELECT * FROM entity_aliases WHERE entity_id=? AND status IN ('approved','proposed') ORDER BY created_at,id",
                (entity["id"],),
            ).fetchall()
            visible_aliases = [row for row in aliases if row["source_id"] is None or str(row["source_id"]) in allowed]
            names = [str(entity["canonical_name"]), *[str(row["alias"]) for row in visible_aliases]]
            entity_forms = set().union(*(_forms(value) for value in names)) if names else set()
            name_score = _variant_score(query_forms, entity_forms)
            if name_score <= 0:
                continue
            refs: dict[str, list[str]] = {"sources": [], "documents": [], "facts": [], "relations": []}
            reasons: list[str] = ["name_variant"]
            for source_id, source in source_by_id.items():
                locator_forms = _forms(f"{source.source_key} {source.locator}")
                if _variant_score(entity_forms, locator_forms) > 0 and _variant_score(query_forms, locator_forms) > 0:
                    refs["sources"].append(source_id)
                    refs["documents"].extend(document_by_source.get(source_id, [])[:1])
                    reasons.append("source_locator")
            for row in facts:
                content_forms = _forms(f"{row['subject']} {row['predicate']} {row['object']}")
                if _variant_score(entity_forms, content_forms) > 0 and _variant_score(query_forms, content_forms) > 0:
                    refs["sources"].append(str(row["source_id"])); refs["facts"].append(str(row["id"]))
                    refs["documents"].extend(document_by_source.get(str(row["source_id"]), [])[:1])
                    reasons.append("fact_evidence")
            for row in relations:
                content_forms = _forms(f"{row['from_entity']} {row['relation']} {row['to_entity']}")
                if _variant_score(entity_forms, content_forms) > 0 and _variant_score(query_forms, content_forms) > 0:
                    refs["sources"].append(str(row["source_id"])); refs["relations"].append(str(row["id"]))
                    refs["documents"].extend(document_by_source.get(str(row["source_id"]), [])[:1])
                    reasons.append("relation_evidence")
            refs = {key: sorted(set(value)) for key, value in refs.items()}
            # Names are only a candidate key.  Returning a row requires
            # independently visible evidence a proposer can cite.
            if not refs["sources"]:
                continue
            evidence_score = min(0.2, 0.1 * len(refs["sources"]))
            score = min(0.99, 0.75 * name_score + evidence_score)
            output.append({
                "entity": {key: entity[key] for key in ("id", "canonical_name", "type")},
                "score": round(score, 6), "reasons": sorted(set(reasons)),
                "evidence_refs": refs,
                "suggested_proposal": {"kind": "alias", "alias": name.strip(), "target_entity_id": entity["id"]},
                "allowed_actions": [{
                    "action": "propose_alias", "label": "Propose alias", "method": "POST", "route": "/brain/propose",
                    "payload": {
                        "workspace_id": workspace_id, "kind": "alias", "candidate_entity_ids": [entity["id"]],
                        "target_id": entity["id"], "alias": name.strip(), "score": round(score, 6),
                        "rationale": "Evidence-backed entity variant", "evidence": "Entity candidate discovery",
                        "source_id": refs["sources"][0], "evidence_refs": refs,
                    },
                    "required_fields": ["rationale"],
                }],
            })
        return sorted(output, key=lambda item: (-item["score"], item["entity"]["id"]))[:limit]

    def _allowed_source_ids(self, identity: Any, workspace_id: str) -> set[str]:
        actor_id = self.os.auth.actor_for_identity(identity, workspace_id)
        actor = self.os._require_actor(workspace_id, actor_id)
        return {source.id for source in self.os.store.allowed_sources(workspace_id, actor)}

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
        identity = person_id
        person_id = self._identity_person(organization_id,workspace_id,identity,"brain_propose")
        if kind not in {"alias","merge"} or not candidate_entity_ids or not evidence.strip(): raise ValidationError("invalid resolution proposal")
        rows=self.conn.execute(f"SELECT id,workspace_id FROM entities WHERE organization_id=? AND id IN ({','.join('?' for _ in candidate_entity_ids)})",(organization_id,*candidate_entity_ids)).fetchall()
        if len(rows)!=len(set(candidate_entity_ids)) or any(row["workspace_id"]!=workspace_id for row in rows): raise NotFoundError("entity candidate not found")
        has_cited_evidence = bool(source_id or any((evidence_refs or {}).values()))
        allowed_source_ids = None
        if workspace_id is not None and hasattr(identity, "person_id") and has_cited_evidence:
            try:
                allowed_source_ids = self._allowed_source_ids(identity, workspace_id)
            except AuthorizationError as exc:
                raise NotFoundError("proposal evidence not found") from exc
        normalized_refs=self._validate_evidence_refs(workspace_id,source_id,evidence_refs,allowed_source_ids)
        proposal={"id":self._id("resolution"),"organization_id":organization_id,"workspace_id":workspace_id,"kind":kind,"alias":alias,"source_entity_id":candidate_entity_ids[0] if kind=="merge" else None,"target_entity_id":target_id or (candidate_entity_ids[1] if kind=="merge" and len(candidate_entity_ids)>1 else candidate_entity_ids[0]),"candidate_entity_ids":json.dumps(candidate_entity_ids),"score":float(score),"rationale":rationale,"status":"pending","proposed_by_person_id":person_id,"reviewed_by_person_id":None,"evidence_source_id":source_id,"evidence":evidence,"evidence_refs":json.dumps(normalized_refs,sort_keys=True),"created_at":_now().isoformat(),"reviewed_at":None}
        self.conn.execute("""INSERT INTO entity_resolution_proposals(
            id,organization_id,workspace_id,kind,alias,source_entity_id,target_entity_id,candidate_entity_ids,
            score,rationale,status,proposed_by_person_id,reviewed_by_person_id,evidence_source_id,evidence,
            evidence_refs,created_at,reviewed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",tuple(proposal.values())); self.conn.commit(); return proposal

    def _validate_evidence_refs(self, workspace_id: str | None, source_id: str | None,
        evidence_refs: dict[str, list[str]] | None, allowed_source_ids: set[str] | None = None) -> dict[str, list[str]]:
        allowed={"sources":"sources","documents":"documents","facts":"facts","relations":"relations"}
        if any(key not in allowed or not isinstance(values,list) for key,values in (evidence_refs or {}).items()):
            raise ValidationError("evidence refs are invalid")
        refs={key: sorted({str(value) for value in values}) for key,values in (evidence_refs or {}).items()}
        if source_id:
            refs.setdefault("sources",[])
            refs["sources"]=sorted(set(refs["sources"]+[source_id]))
        if workspace_id is None and any(refs.values()): raise NotFoundError("proposal evidence not found")
        if allowed_source_ids is not None and any(source_id not in allowed_source_ids for source_id in refs.get("sources", [])):
            raise NotFoundError("proposal evidence not found")
        for key,table in allowed.items():
            ids=refs.get(key,[])
            if not ids: continue
            placeholders=','.join('?' for _ in ids)
            if key == "sources":
                rows = self.conn.execute(
                    f"SELECT id FROM sources WHERE workspace_id=? AND id IN ({placeholders})",
                    (workspace_id,*ids),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT id,source_id FROM {table} WHERE workspace_id=? AND id IN ({placeholders})",
                    (workspace_id,*ids),
                ).fetchall()
                if allowed_source_ids is not None:
                    rows = [row for row in rows if str(row["source_id"]) in allowed_source_ids]
            found={str(row["id"]) for row in rows}
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
        rows=self.conn.execute(
            "SELECT * FROM facts WHERE workspace_id=? AND conflict_group=? AND superseded_by IS NULL",
            (identity.workspace_id,conflict_group),
        ).fetchall()
        if not rows or winner_fact_id not in {row["id"] for row in rows}: raise NotFoundError("conflict not found")
        try:
            actor_id=self.os.auth.actor_for_identity(identity,identity.workspace_id)
            actor=self.os._require_actor(identity.workspace_id,actor_id)
            allowed={source.id for source in self.os.store.allowed_sources(identity.workspace_id,actor)}
        except AuthorizationError as exc:
            membership=self.os.company.workspace_membership(identity.workspace_id,identity.person_id)
            if membership is None or membership.role != "admin":
                raise NotFoundError("conflict not found") from exc
            allowed={str(row["id"]) for row in self.conn.execute("""SELECT DISTINCT s.id FROM sources s
                JOIN source_lifecycle_intervals l ON l.source_id=s.id AND l.workspace_id=s.workspace_id
                WHERE s.workspace_id=? AND l.retired_at IS NULL""",(identity.workspace_id,)).fetchall()}
        if any(row["source_id"] not in allowed for row in rows): raise NotFoundError("conflict not found")
        states={
            row["id"]: self._knowledge_state_row(identity.workspace_id,"fact",row["id"])
            for row in rows
        }
        resolved=[row["id"] for row in rows if states[row["id"]] is not None and states[row["id"]]["state"]=="verified"]
        stale=[row["id"] for row in rows if states[row["id"]] is not None and states[row["id"]]["state"]=="stale"]
        if len(resolved)==1 and len(stale)==len(rows)-1:
            if resolved[0] != winner_fact_id: raise ValidationError("conflict already resolved with a different winner")
            return {"conflict_group":conflict_group,"winner_fact_id":winner_fact_id,"resolved":True,"changed":False}
        with self.os.store.atomic(immediate=True):
            for row in rows:
                self._state_event(identity.organization_id,identity.workspace_id,"fact",row["id"],"verified" if row["id"]==winner_fact_id else "stale","human conflict resolution",row["source_id"],identity.person_id)
        return {"conflict_group":conflict_group,"winner_fact_id":winner_fact_id,"resolved":True,"changed":True}

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

    def create_folder(self, identity: Any, workspace_id: str, name: str,
        parent_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
        name = self._clean_name(name, "folder name")
        self._validate_folder_parent(workspace_id, parent_id)
        payload = {"name": name, "parent_id": parent_id}
        prior = self._idempotent_row("brain_folders", workspace_id, person_id, idempotency_key)
        if prior is not None:
            self._require_same_payload(dict(prior), payload, ("name", "parent_id"))
            return dict(prior)
        now = _now().isoformat()
        item = {
            "id": self._id("brain_folder"), "organization_id": organization_id,
            "workspace_id": workspace_id, "parent_id": parent_id, "name": name,
            "created_by_person_id": person_id, "created_at": now, "updated_at": now,
            "version": 1, "idempotency_key": idempotency_key,
        }
        with self.os.store.atomic(immediate=True):
            self.conn.execute("""INSERT INTO brain_folders(
                id,organization_id,workspace_id,parent_id,name,created_by_person_id,
                created_at,updated_at,version,idempotency_key
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""", tuple(item.values()))
            self._brain_audit(organization_id, workspace_id, "folder", item["id"], "create", person_id, 1, idempotency_key, payload, now)
        return item

    def create_collection(self, identity: Any, workspace_id: str, name: str,
        folder_id: str | None = None, description: str = "", visibility: str = "owner",
        idempotency_key: str | None = None) -> dict[str, Any]:
        organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
        name = self._clean_name(name, "collection name")
        visibility = self._visibility(visibility)
        self._require_folder(workspace_id, folder_id)
        payload = {"name": name, "folder_id": folder_id, "description": description, "visibility": visibility}
        prior = self._idempotent_row("brain_collections", workspace_id, person_id, idempotency_key, "owner_person_id")
        if prior is not None:
            self._require_same_payload(dict(prior), payload, ("name", "folder_id", "description", "visibility"))
            return dict(prior)
        now = _now().isoformat()
        item = {
            "id": self._id("brain_collection"), "organization_id": organization_id,
            "workspace_id": workspace_id, "folder_id": folder_id, "name": name,
            "description": description, "owner_person_id": person_id, "visibility": visibility,
            "created_at": now, "updated_at": now, "version": 1, "idempotency_key": idempotency_key,
        }
        with self.os.store.atomic(immediate=True):
            self.conn.execute("""INSERT INTO brain_collections(
                id,organization_id,workspace_id,folder_id,name,description,owner_person_id,
                visibility,created_at,updated_at,version,idempotency_key
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(item.values()))
            self._brain_audit(organization_id, workspace_id, "collection", item["id"], "create", person_id, 1, idempotency_key, payload, now)
        return item

    def create_tag(self, identity: Any, workspace_id: str, name: str, color: str | None = None,
        idempotency_key: str | None = None) -> dict[str, Any]:
        organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
        name = self._clean_name(name, "tag name")
        normalized = _norm(name)
        if not normalized:
            raise ValidationError("tag name is required")
        payload = {"name": name, "normalized_name": normalized, "color": color}
        prior = self._idempotent_row("brain_tags", workspace_id, person_id, idempotency_key)
        if prior is not None:
            self._require_same_payload(dict(prior), payload, ("name", "normalized_name", "color"))
            return dict(prior)
        existing = self.conn.execute(
            "SELECT * FROM brain_tags WHERE workspace_id=? AND normalized_name=?",
            (workspace_id, normalized),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        now = _now().isoformat()
        item = {
            "id": self._id("brain_tag"), "organization_id": organization_id,
            "workspace_id": workspace_id, "name": name, "normalized_name": normalized,
            "color": color, "created_by_person_id": person_id,
            "created_at": now, "updated_at": now, "version": 1,
            "idempotency_key": idempotency_key,
        }
        with self.os.store.atomic(immediate=True):
            self.conn.execute("""INSERT INTO brain_tags(
                id,organization_id,workspace_id,name,normalized_name,color,created_by_person_id,
                created_at,updated_at,version,idempotency_key
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", tuple(item.values()))
            self._brain_audit(organization_id, workspace_id, "tag", item["id"], "create", person_id, 1, idempotency_key, payload, now)
        return item

    def tag_source(self, identity: Any, workspace_id: str, source_id: str, tag_id: str,
        idempotency_key: str | None = None) -> dict[str, Any]:
        organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
        self._require_tag(workspace_id, tag_id)
        self._require_allowed_source(identity, workspace_id, source_id)
        prior = self._idempotent_row("brain_source_tags", workspace_id, person_id, idempotency_key, "tagged_by_person_id")
        if prior is not None:
            self._require_same_payload(dict(prior), {"source_id": source_id, "tag_id": tag_id}, ("source_id", "tag_id"))
            return dict(prior)
        existing = self.conn.execute(
            "SELECT * FROM brain_source_tags WHERE workspace_id=? AND source_id=? AND tag_id=?",
            (workspace_id, source_id, tag_id),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        now = _now().isoformat()
        item = {
            "organization_id": organization_id, "workspace_id": workspace_id,
            "source_id": source_id, "tag_id": tag_id,
            "tagged_by_person_id": person_id, "created_at": now,
            "idempotency_key": idempotency_key,
        }
        with self.os.store.atomic(immediate=True):
            self.conn.execute("INSERT INTO brain_source_tags VALUES (?,?,?,?,?,?,?)", tuple(item.values()))
            self._brain_audit(organization_id, workspace_id, "source_tag", source_id, "tag", person_id, None, idempotency_key, {"source_id": source_id, "tag_id": tag_id}, now)
        return item

    def tag_document(self, identity: Any, workspace_id: str, document_id: str, tag_id: str,
        idempotency_key: str | None = None) -> dict[str, Any]:
        organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
        self._require_tag(workspace_id, tag_id)
        document = self.os.store.get_document(workspace_id, document_id)
        if document is None:
            raise NotFoundError("document not found")
        self._require_allowed_source(identity, workspace_id, document.source_id)
        prior = self._idempotent_row("brain_document_tags", workspace_id, person_id, idempotency_key, "tagged_by_person_id")
        if prior is not None:
            self._require_same_payload(dict(prior), {"document_id": document_id, "tag_id": tag_id}, ("document_id", "tag_id"))
            return dict(prior)
        existing = self.conn.execute(
            "SELECT * FROM brain_document_tags WHERE workspace_id=? AND document_id=? AND tag_id=?",
            (workspace_id, document_id, tag_id),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        now = _now().isoformat()
        item = {
            "organization_id": organization_id, "workspace_id": workspace_id,
            "document_id": document_id, "tag_id": tag_id,
            "tagged_by_person_id": person_id, "created_at": now,
            "idempotency_key": idempotency_key,
        }
        with self.os.store.atomic(immediate=True):
            self.conn.execute("INSERT INTO brain_document_tags VALUES (?,?,?,?,?,?,?)", tuple(item.values()))
            self._brain_audit(organization_id, workspace_id, "document_tag", document_id, "tag", person_id, None, idempotency_key, {"document_id": document_id, "tag_id": tag_id}, now)
        return item

    def add_collection_item(self, identity: Any, workspace_id: str, collection_id: str,
        item_type: str, item_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
        collection = self._require_collection_for_write(workspace_id, collection_id, person_id)
        if item_type == "source":
            self._require_allowed_source(identity, workspace_id, item_id)
        elif item_type == "document":
            document = self.os.store.get_document(workspace_id, item_id)
            if document is None:
                raise NotFoundError("document not found")
            self._require_allowed_source(identity, workspace_id, document.source_id)
        else:
            raise ValidationError("collection item type is invalid")
        prior = self._idempotent_row("brain_collection_items", workspace_id, person_id, idempotency_key, "added_by_person_id")
        if prior is not None:
            self._require_same_payload(dict(prior), {"collection_id": collection_id, "item_type": item_type, "item_id": item_id}, ("collection_id", "item_type", "item_id"))
            return dict(prior)
        existing = self.conn.execute(
            """SELECT * FROM brain_collection_items
               WHERE workspace_id=? AND collection_id=? AND item_type=? AND item_id=?""",
            (workspace_id, collection_id, item_type, item_id),
        ).fetchone()
        if existing is not None:
            return dict(existing)
        now = _now().isoformat()
        item = {
            "organization_id": organization_id, "workspace_id": workspace_id,
            "collection_id": collection["id"], "item_type": item_type,
            "item_id": item_id, "added_by_person_id": person_id,
            "created_at": now, "idempotency_key": idempotency_key,
        }
        with self.os.store.atomic(immediate=True):
            self.conn.execute("INSERT INTO brain_collection_items VALUES (?,?,?,?,?,?,?,?)", tuple(item.values()))
            self._brain_audit(organization_id, workspace_id, "collection_item", collection_id, "add_item", person_id, None, idempotency_key, {"item_type": item_type, "item_id": item_id}, now)
        return item

    def list_tagged_sources(self, identity: Any, workspace_id: str, tag_id: str) -> list[dict[str, Any]]:
        self._brain_identity(identity, workspace_id, "brain_read", write=False)
        self._require_tag(workspace_id, tag_id)
        allowed = sorted(self._allowed_source_ids(identity, workspace_id))
        if not allowed:
            return []
        marks = ",".join("?" for _ in allowed)
        rows = self.conn.execute(
            f"""SELECT s.*, t.created_at AS tagged_at FROM brain_source_tags t
                JOIN sources s ON s.id=t.source_id AND s.workspace_id=t.workspace_id
                WHERE t.workspace_id=? AND t.tag_id=? AND t.source_id IN ({marks})
                ORDER BY t.created_at ASC, s.id ASC""",
            (workspace_id, tag_id, *allowed),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_tagged_documents(self, identity: Any, workspace_id: str, tag_id: str) -> list[dict[str, Any]]:
        self._brain_identity(identity, workspace_id, "brain_read", write=False)
        self._require_tag(workspace_id, tag_id)
        allowed = sorted(self._allowed_source_ids(identity, workspace_id))
        if not allowed:
            return []
        marks = ",".join("?" for _ in allowed)
        rows = self.conn.execute(
            f"""SELECT d.*, t.created_at AS tagged_at FROM brain_document_tags t
                JOIN documents d ON d.id=t.document_id AND d.workspace_id=t.workspace_id
                WHERE t.workspace_id=? AND t.tag_id=? AND d.source_id IN ({marks})
                ORDER BY t.created_at ASC, d.id ASC""",
            (workspace_id, tag_id, *allowed),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_collection_items(self, identity: Any, workspace_id: str, collection_id: str) -> list[dict[str, Any]]:
        _, person_id = self._brain_identity(identity, workspace_id, "brain_read", write=False)
        self._require_collection_for_read(workspace_id, collection_id, person_id)
        allowed = self._allowed_source_ids(identity, workspace_id)
        rows = self.conn.execute(
            """SELECT * FROM brain_collection_items
               WHERE workspace_id=? AND collection_id=?
               ORDER BY created_at ASC, item_type ASC, item_id ASC""",
            (workspace_id, collection_id),
        ).fetchall()
        output = []
        for row in rows:
            if row["item_type"] == "source":
                if row["item_id"] not in allowed:
                    continue
            else:
                document = self.os.store.get_document(workspace_id, row["item_id"])
                if document is None or document.source_id not in allowed:
                    continue
            output.append(dict(row))
        return output

    def save_view(self, identity: Any, workspace_id: str, name: str, *,
        query: Any, filters: Any | None = None, sort: Any | None = None,
        folder_id: str | None = None, description: str = "", visibility: str = "owner",
        idempotency_key: str | None = None) -> dict[str, Any]:
        organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
        name = self._clean_name(name, "saved view name")
        visibility = self._visibility(visibility)
        self._require_folder(workspace_id, folder_id)
        query_json = self._json_payload(query, "query")
        filters_json = self._json_payload(filters or {}, "filters")
        sort_json = self._json_payload(sort or [], "sort")
        payload = {
            "name": name, "description": description, "visibility": visibility,
            "folder_id": folder_id, "query_json": query_json,
            "filters_json": filters_json, "sort_json": sort_json,
        }
        prior = self._idempotent_row("brain_saved_views", workspace_id, person_id, idempotency_key, "owner_person_id")
        if prior is not None:
            self._require_same_payload(dict(prior), payload, tuple(payload.keys()))
            return self._saved_view_from_row(prior)
        now = _now().isoformat()
        view_id = self._id("brain_view")
        item = {
            "id": view_id, "organization_id": organization_id, "workspace_id": workspace_id,
            "folder_id": folder_id, "name": name, "description": description,
            "owner_person_id": person_id, "visibility": visibility, "query_json": query_json,
            "filters_json": filters_json, "sort_json": sort_json, "created_at": now,
            "updated_at": now, "version": 1, "idempotency_key": idempotency_key,
        }
        with self.os.store.atomic(immediate=True):
            self.conn.execute("""INSERT INTO brain_saved_views(
                id,organization_id,workspace_id,folder_id,name,description,owner_person_id,
                visibility,query_json,filters_json,sort_json,created_at,updated_at,version,idempotency_key
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(item.values()))
            self._insert_saved_view_version(view_id, organization_id, workspace_id, 1, name, description, visibility, query_json, filters_json, sort_json, person_id, "created", now)
            self._brain_audit(organization_id, workspace_id, "saved_view", view_id, "create", person_id, 1, idempotency_key, payload, now)
        return self._saved_view_from_row(item)

    def update_view(self, identity: Any, workspace_id: str, saved_view_id: str, *,
        name: str | None = None, query: Any | None = None, filters: Any | None = None,
        sort: Any | None = None, description: str | None = None, visibility: str | None = None,
        folder_id: str | None = None, idempotency_key: str | None = None,
        reason: str = "updated") -> dict[str, Any]:
        organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
        row = self.os.store.get_brain_saved_view(workspace_id, saved_view_id)
        if row is None:
            raise NotFoundError("saved view not found")
        if row["owner_person_id"] != person_id:
            raise AuthorizationError("saved view owner required")
        prior = self._prior_mutation(workspace_id, "saved_view", saved_view_id, "update", idempotency_key)
        if prior is not None:
            return self._saved_view_from_row(self.os.store.get_brain_saved_view(workspace_id, saved_view_id))
        next_name = self._clean_name(name, "saved view name") if name is not None else str(row["name"])
        next_description = description if description is not None else str(row["description"])
        next_visibility = self._visibility(visibility) if visibility is not None else str(row["visibility"])
        next_folder_id = folder_id if folder_id is not None else row["folder_id"]
        self._require_folder(workspace_id, next_folder_id)
        query_json = self._json_payload(query, "query") if query is not None else str(row["query_json"])
        filters_json = self._json_payload(filters, "filters") if filters is not None else str(row["filters_json"])
        sort_json = self._json_payload(sort, "sort") if sort is not None else str(row["sort_json"])
        payload = {
            "name": next_name, "description": next_description, "visibility": next_visibility,
            "folder_id": next_folder_id, "query_json": query_json,
            "filters_json": filters_json, "sort_json": sort_json,
        }
        old_payload = {key: row[key] for key in payload}
        if payload == old_payload:
            return self._saved_view_from_row(row)
        now = _now().isoformat()
        next_version = int(row["version"]) + 1
        with self.os.store.atomic(immediate=True):
            cursor = self.conn.execute("""UPDATE brain_saved_views
                SET folder_id=?,name=?,description=?,visibility=?,query_json=?,
                    filters_json=?,sort_json=?,updated_at=?,version=?
                WHERE workspace_id=? AND id=? AND version=?""",
                (next_folder_id, next_name, next_description, next_visibility, query_json,
                 filters_json, sort_json, now, next_version, workspace_id, saved_view_id, row["version"]))
            if cursor.rowcount != 1:
                raise ValidationError("saved view version changed")
            self._insert_saved_view_version(saved_view_id, organization_id, workspace_id, next_version, next_name, next_description, next_visibility, query_json, filters_json, sort_json, person_id, reason, now)
            self._brain_audit(organization_id, workspace_id, "saved_view", saved_view_id, "update", person_id, next_version, idempotency_key, payload, now)
        return self._saved_view_from_row(self.os.store.get_brain_saved_view(workspace_id, saved_view_id))

    def get_view(self, identity: Any, workspace_id: str, saved_view_id: str) -> dict[str, Any]:
        _, person_id = self._brain_identity(identity, workspace_id, "brain_read", write=False)
        row = self.os.store.get_brain_saved_view(workspace_id, saved_view_id)
        if row is None:
            raise NotFoundError("saved view not found")
        if row["visibility"] != "shared" and row["owner_person_id"] != person_id:
            raise NotFoundError("saved view not found")
        return self._saved_view_from_row(row)

    def list_views(self, identity: Any, workspace_id: str) -> list[dict[str, Any]]:
        _, person_id = self._brain_identity(identity, workspace_id, "brain_read", write=False)
        rows = self.conn.execute(
            """SELECT * FROM brain_saved_views
               WHERE workspace_id=? AND (visibility='shared' OR owner_person_id=?)
               ORDER BY updated_at DESC, id DESC""",
            (workspace_id, person_id),
        ).fetchall()
        return [self._saved_view_from_row(row) for row in rows]

    def view_versions(self, identity: Any, workspace_id: str, saved_view_id: str) -> list[dict[str, Any]]:
        self.get_view(identity, workspace_id, saved_view_id)
        rows = self.os.store.list_brain_saved_view_versions(workspace_id, saved_view_id)
        return [self._saved_view_version_from_row(row) for row in rows]

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

    def _brain_identity(self, identity: Any, workspace_id: str, capability: str, write: bool) -> tuple[str, str]:
        if not hasattr(identity, "person_id"):
            raise AuthorizationError("authenticated identity is required")
        if identity.workspace_id not in {None, workspace_id}:
            raise AuthorizationError("identity is outside requested scope")
        identity.require(capability)
        self.os._require_person_access(identity.organization_id, workspace_id, identity.person_id, write=write)
        return identity.organization_id, identity.person_id

    @staticmethod
    def _clean_name(value: str, label: str) -> str:
        cleaned = " ".join(str(value).split())
        if not cleaned:
            raise ValidationError(f"{label} is required")
        return cleaned

    @staticmethod
    def _visibility(value: str | None) -> str:
        if value not in {"owner", "shared"}:
            raise ValidationError("visibility must be owner or shared")
        return value

    @staticmethod
    def _json_payload(value: Any, label: str) -> str:
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        except TypeError as exc:
            raise ValidationError(f"{label} must be JSON serializable") from exc

    def _saved_view_from_row(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["query"] = json.loads(str(item.pop("query_json")))
        item["filters"] = json.loads(str(item.pop("filters_json")))
        item["sort"] = json.loads(str(item.pop("sort_json")))
        return item

    def _saved_view_version_from_row(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["query"] = json.loads(str(item.pop("query_json")))
        item["filters"] = json.loads(str(item.pop("filters_json")))
        item["sort"] = json.loads(str(item.pop("sort_json")))
        return item

    def _brain_audit(self, organization_id: str, workspace_id: str, entity_type: str, entity_id: str,
        action: str, actor_person_id: str, version: int | None, idempotency_key: str | None,
        payload: dict[str, object], created_at: str) -> None:
        self.os.store.record_brain_audit(
            audit_id=self._id("brain_audit"), organization_id=organization_id,
            workspace_id=workspace_id, entity_type=entity_type, entity_id=entity_id,
            action=action, actor_person_id=actor_person_id, version=version,
            idempotency_key=idempotency_key, payload=payload, created_at=created_at,
        )

    def _insert_saved_view_version(self, saved_view_id: str, organization_id: str, workspace_id: str,
        version: int, name: str, description: str, visibility: str, query_json: str,
        filters_json: str, sort_json: str, changed_by_person_id: str, reason: str, created_at: str) -> None:
        self.conn.execute("""INSERT INTO brain_saved_view_versions(
            id,saved_view_id,organization_id,workspace_id,version,name,description,visibility,
            query_json,filters_json,sort_json,changed_by_person_id,change_reason,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            self._id("brain_view_ver"), saved_view_id, organization_id, workspace_id, version,
            name, description, visibility, query_json, filters_json, sort_json,
            changed_by_person_id, reason, created_at,
        ))

    def _idempotent_row(self, table: str, workspace_id: str, person_id: str,
        idempotency_key: str | None, person_column: str = "created_by_person_id") -> Any:
        if idempotency_key is None:
            return None
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE workspace_id=? AND {person_column}=? AND idempotency_key=?",
            (workspace_id, person_id, idempotency_key),
        ).fetchone()

    def _prior_mutation(self, workspace_id: str, entity_type: str, entity_id: str,
        action: str, idempotency_key: str | None) -> Any:
        if idempotency_key is None:
            return None
        return self.conn.execute(
            """SELECT * FROM brain_mutation_audit
               WHERE workspace_id=? AND entity_type=? AND entity_id=? AND action=? AND idempotency_key=?
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (workspace_id, entity_type, entity_id, action, idempotency_key),
        ).fetchone()

    @staticmethod
    def _require_same_payload(row: dict[str, Any], payload: dict[str, Any], fields: tuple[str, ...]) -> None:
        if any(row.get(field) != payload.get(field) for field in fields):
            raise ValidationError("idempotency key was already used for a different mutation")

    def _require_folder(self, workspace_id: str, folder_id: str | None) -> None:
        if folder_id is None:
            return
        if self.os.store.get_brain_folder(workspace_id, folder_id) is None:
            raise NotFoundError("folder not found")

    def _validate_folder_parent(self, workspace_id: str, parent_id: str | None) -> None:
        if parent_id is None:
            return
        seen: set[str] = set()
        cursor = parent_id
        while cursor is not None:
            if cursor in seen:
                raise ValidationError("folder hierarchy has a cycle")
            seen.add(cursor)
            folder = self.os.store.get_brain_folder(workspace_id, cursor)
            if folder is None:
                raise NotFoundError("parent folder not found")
            cursor = folder["parent_id"]
            if len(seen) > 25:
                raise ValidationError("folder hierarchy is too deep")

    def _require_tag(self, workspace_id: str, tag_id: str) -> dict[str, object]:
        tag = self.os.store.get_brain_tag(workspace_id, tag_id)
        if tag is None:
            raise NotFoundError("tag not found")
        return tag

    def _require_collection_for_read(self, workspace_id: str, collection_id: str, person_id: str) -> dict[str, object]:
        collection = self.os.store.get_brain_collection(workspace_id, collection_id)
        if collection is None:
            raise NotFoundError("collection not found")
        if collection["visibility"] != "shared" and collection["owner_person_id"] != person_id:
            raise NotFoundError("collection not found")
        return collection

    def _require_collection_for_write(self, workspace_id: str, collection_id: str, person_id: str) -> dict[str, object]:
        collection = self.os.store.get_brain_collection(workspace_id, collection_id)
        if collection is None:
            raise NotFoundError("collection not found")
        if collection["owner_person_id"] != person_id:
            raise AuthorizationError("collection owner required")
        return collection

    def _require_allowed_source(self, identity: Any, workspace_id: str, source_id: str) -> None:
        if source_id not in self._allowed_source_ids(identity, workspace_id):
            raise NotFoundError("source not found")

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
