from __future__ import annotations
from auremgrid.services.brain_ops_shared import *

class BrainOpsResolutionMixin:
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

    def _add_alias(self,entity_id:str,alias:str,confidence:float,status:str,source_id:str|None,commit:bool=True)->dict[str,Any]:
            item={"id":self._id("alias"),"entity_id":entity_id,"alias":alias,"normalized_alias":_norm(alias),"confidence":confidence,"status":status,"source_id":source_id,"created_at":_now().isoformat()}
            self.conn.execute("INSERT INTO entity_aliases(id,entity_id,alias,normalized_alias,confidence,status,source_id,created_at,reviewed_by_person_id,reviewed_at,evidence,retired_at) VALUES (?,?,?,?,?,?,?, ?,NULL,NULL,NULL,NULL)",tuple(item.values()));
            if commit: self.conn.commit()
            return item
