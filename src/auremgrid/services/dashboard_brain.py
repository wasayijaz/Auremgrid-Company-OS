from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from auremgrid.domain.security import AuthenticatedIdentity

from .dashboard_shared import _action, _health_status, _json_list, _json_object, _moment, _redirect, _refs_visible, _safe_scalar


class DashboardBrainMixin:
    def brain(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        self._authorize(identity, organization_id, workspace_id, person_id, "brain_read")
        actor_id = self.os.auth.actor_for_identity(identity, workspace_id)
        actor = self.os._require_actor(workspace_id, actor_id)
        moment = _moment(as_of)
        sources = self.os.store.allowed_sources(workspace_id, actor, as_of=as_of)
        source_ids = {source.id for source in sources}
        facts = self.os.store.list_facts(workspace_id, source_ids, as_of=moment, include_superseded=True)
        states = self._fact_states(workspace_id, (fact.id for fact in facts), moment)

        truth_rows: list[dict[str, Any]] = []
        conflict_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fact in facts:
            state = states.get(fact.id, "inferred")
            item = fact.to_dict()
            item["state"] = state
            if fact.conflict_group:
                conflict_rows[fact.conflict_group].append(item)
            if state not in {"stale", "conflicted", "proposed"} and not fact.superseded_by:
                truth_rows.append(item)

        all_conflict_ids: dict[str,set[str]] = defaultdict(set)
        if conflict_rows:
            group_ids=sorted(conflict_rows); marks=','.join('?' for _ in group_ids)
            for row in self.conn.execute(
                f"SELECT id,conflict_group FROM facts WHERE workspace_id=? AND conflict_group IN ({marks}) AND superseded_by IS NULL",
                (workspace_id,*group_ids),
            ).fetchall():
                all_conflict_ids[str(row["conflict_group"])].add(str(row["id"]))
        conflicts = []
        for group_id, alternatives in sorted(conflict_rows.items()):
            if all_conflict_ids[group_id] != {str(item["id"]) for item in alternatives}:
                # A partial ACL must not reveal that additional alternatives,
                # or even the conflict group itself, exist.
                continue
            live = [item for item in alternatives if item["state"] != "stale"]
            winner = next((item["id"] for item in alternatives if item["state"] == "verified"), None)
            actions = []
            if as_of is None and identity.can("brain_promote") and winner is None:
                actions = [
                    _action(
                        "resolve_conflict", "/brain/conflicts/resolve",
                        {"workspace_id":workspace_id,"conflict_group":group_id,"winner_fact_id":item["id"]},
                    )
                    for item in alternatives
                ]
            conflicts.append(
                {
                    "id": group_id,
                    "state": "resolved" if winner and len(live) == 1 else "conflicted",
                    "winner_fact_id": winner,
                    "alternatives": sorted(alternatives, key=lambda item: (item["recorded_at"], item["id"])),
                    "allowed_actions": actions,
                }
            )

        entities = self._entities(organization_id, workspace_id, moment, source_ids)
        proposals = self._proposal_rows(
            identity,organization_id,workspace_id,person_id,moment,as_of,source_ids,
            self._visible_proposal_entity_ids(organization_id,workspace_id,moment,source_ids),
        )
        # Decisions are canonical organization records.  A decision may be
        # backed by a source (which must be visible to this actor) or remain a
        # source-less ledger entry; never widen the source ACL while building
        # the dashboard projection.
        decisions = []
        for decision in self.os.company.list_decisions(organization_id, workspace_id):
            if decision.created_at is not None and decision.created_at > moment:
                continue
            if decision.effective_from is not None and decision.effective_from > moment:
                continue
            if decision.effective_until is not None and decision.effective_until <= moment:
                continue
            if decision.source_id and str(decision.source_id) not in source_ids:
                continue
            if decision.superseded_by:
                continue
            item = decision.to_dict()
            item["kind"] = "decision"
            item["state"] = "current"
            decisions.append(item)
        decisions.sort(key=lambda item: (item.get("effective_from") or "", item["id"]), reverse=True)

        # Preferences are durable actor-scoped memories.  They are read only
        # here and deliberately do not pretend to be sourced facts.
        preferences = [
            {**memory.to_dict(), "state": "recorded", "kind": "preference"}
            for memory in self.os.store.list_memories(workspace_id, actor.id)
            if memory.kind == "preference" and memory.recorded_at <= moment
        ]

        # Keep an immutable history collection separate from current truth:
        # include every ACL-visible fact version, including superseded and
        # stale observations, with its effective knowledge state at the read
        # moment.  This preserves provenance without exposing hidden sources.
        history = []
        for fact in self.os.store.list_facts(workspace_id, source_ids, include_superseded=True):
            if fact.recorded_at > moment or fact.observed_at > moment:
                continue
            item = fact.to_dict()
            item["state"] = states.get(fact.id, "inferred")
            item["historical"] = True
            history.append(item)
        history.sort(key=lambda item: (item.get("recorded_at") or "", item["id"]), reverse=True)
        workspace = self.os.store.get_workspace(workspace_id)
        graph = self.os.store.graph_generation_state(workspace_id)
        semantic = dict(getattr(self.os, "embedding_health", {}) or {})
        graph_runtime = dict(getattr(self.os, "graph_health", {}) or {})
        semantic_fallback = bool(semantic.get("fallback_used", False))
        graph_runtime_status = _health_status(graph_runtime.get("status"))
        graph_status = (
            "degraded" if graph_runtime_status == "degraded"
            else "healthy" if graph.get("active_status") == "active"
            else "building" if graph.get("building_generation") is not None
            else "unavailable"
        )
        health = {
            "semantic": {
                "status": _health_status(semantic.get("status")),
                "provider": _safe_scalar(semantic.get("provider")),
                "model": _safe_scalar(semantic.get("model")),
                "version": _safe_scalar(semantic.get("version")),
                "mode": "deterministic_fallback" if semantic_fallback else "configured_provider",
                "fallback_used": semantic_fallback,
            },
            "graph": {
                "status": graph_status,
                "provider": _safe_scalar(getattr(self.os.graph, "name", None)),
                "active_generation": graph.get("active_generation"),
                "building": graph.get("building_generation") is not None,
                "serving_stale_generation": bool(
                    graph_runtime_status == "degraded" and graph.get("active_generation")
                ),
            },
        }
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "as_of": moment.isoformat(),
            "workspace": {"id": workspace_id, "name": workspace.name if workspace else ""},
            "summary": {
                "sources": len(sources),
                "current_truths": len(truth_rows),
                "conflict_groups": len(conflicts),
                "pending_proposals": sum(item["status"] == "pending" for item in proposals),
                "entities": len(entities),
                "decisions": len(decisions),
                "preferences": len(preferences),
                "history": len(history),
            },
            "proposals": proposals,
            "conflicts": conflicts,
            "current_truths": sorted(truth_rows, key=lambda item: (item["subject"], item["predicate"], item["id"])),
            "entities": entities,
            "health": health,
            # Explicit collection names are the stable dashboard read model.
            "collections": {
                "current_truth": sorted(truth_rows, key=lambda item: (item["subject"], item["predicate"], item["id"])),
                "decisions": decisions,
                "preferences": preferences,
                "entities": entities,
                "conflicts": conflicts,
                "proposed": proposals,
                "sources": [source.to_dict() for source in sources],
                "history": history,
            },
        }

    def _proposal_rows(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str,
        person_id: str, moment: datetime, as_of: datetime | None, allowed_source_ids: set[str],
        visible_entity_ids: set[str],
    ) -> list[dict[str, Any]]:
        cutoff=moment.isoformat(); historical=as_of is not None
        visible_documents,visible_facts,visible_relations = self._visible_evidence_ids(
            workspace_id,allowed_source_ids,cutoff
        )
        rows: list[dict[str, Any]]=[]
        for proposal in self.os.brain_ops.list_memory_proposals(
            organization_id,workspace_id,person_id,as_of=as_of
        ):
            if proposal.get("source_id") and str(proposal["source_id"]) not in allowed_source_ids:
                continue
            kind=str(proposal["kind"]); status=str(proposal["status"])
            actions=[]
            if not historical and status=="pending" and kind=="fact" and identity.can("brain_promote"):
                actions=[
                    _action("approve","/brain/promote",{"workspace_id":workspace_id,"proposal_id":proposal["id"],"action":"approve"}),
                    _action("reject","/brain/promote",{"workspace_id":workspace_id,"proposal_id":proposal["id"],"action":"reject"}),
                ]
            rows.append({
                "id":proposal["id"],"family":"knowledge","kind":kind,"status":status,
                "content":proposal["content"],"structured_payload":_json_object(proposal.get("structured_payload")),
                "confidence":proposal["confidence"],"evidence":proposal["evidence"],
                "created_at":proposal["created_at"],"reviewed_at":proposal.get("reviewed_at"),
                "allowed_actions":actions,
            })

        resolution_rows=self.conn.execute("""SELECT p.*,d.action AS decision_action,
                d.reviewer_person_id AS decision_reviewer,d.created_at AS decision_at
            FROM entity_resolution_proposals p
            LEFT JOIN entity_resolution_decisions d ON d.proposal_id=p.id AND d.created_at<=?
            WHERE p.organization_id=? AND p.workspace_id=? AND p.created_at<=?
            ORDER BY p.created_at DESC,p.id DESC""",(cutoff,organization_id,workspace_id,cutoff)).fetchall()
        for raw in resolution_rows:
            proposal=dict(raw); candidates={str(item) for item in _json_list(proposal["candidate_entity_ids"])}
            if not candidates or not candidates.issubset(visible_entity_ids):
                continue
            if proposal.get("evidence_source_id") and str(proposal["evidence_source_id"]) not in allowed_source_ids:
                continue
            refs=_json_object(proposal.get("evidence_refs"))
            if not _refs_visible(refs,allowed_source_ids,visible_documents,visible_facts,visible_relations):
                continue
            action=proposal.get("decision_action")
            status="approved" if action=="approve" else "rejected" if action=="reject" else "pending"
            actions=[]
            if not historical and status=="pending" and identity.can("brain_promote"):
                actions=[
                    _action("approve","/brain/promote",{"workspace_id":workspace_id,"proposal_id":proposal["id"],"action":"approve"}),
                    _action("reject","/brain/promote",{"workspace_id":workspace_id,"proposal_id":proposal["id"],"action":"reject"}),
                ]
            rows.append({
                "id":proposal["id"],"family":"entity_resolution","kind":proposal["kind"],"status":status,
                "content":proposal["alias"] or proposal["rationale"],
                "structured_payload":{
                    "alias":proposal["alias"],"source_entity_id":proposal["source_entity_id"],
                    "target_entity_id":proposal["target_entity_id"],"candidate_entity_ids":sorted(candidates),
                },
                "confidence":proposal["score"],"evidence":proposal["evidence"],
                "created_at":proposal["created_at"],"reviewed_at":proposal.get("decision_at"),
                "allowed_actions":actions,
            })
        return sorted(rows,key=lambda item:(item["created_at"],item["id"]),reverse=True)

    def _visible_proposal_entity_ids(
        self, organization_id: str, workspace_id: str, moment: datetime,
        allowed_source_ids: set[str],
    ) -> set[str]:
        rows=self.conn.execute("""SELECT e.id,a.source_id FROM entities e
            JOIN entity_aliases a ON a.entity_id=e.id
            WHERE e.organization_id=? AND e.workspace_id=? AND e.created_at<=? AND a.created_at<=?
              AND a.status='approved'""",
            (organization_id,workspace_id,moment.isoformat(),moment.isoformat()),
        ).fetchall()
        return {
            str(row["id"]) for row in rows
            if row["source_id"] is None or str(row["source_id"]) in allowed_source_ids
        }

    def _visible_evidence_ids(
        self, workspace_id: str, allowed_source_ids: set[str], cutoff: str,
    ) -> tuple[set[str],set[str],set[str]]:
        if not allowed_source_ids:
            return set(),set(),set()
        marks=','.join('?' for _ in allowed_source_ids); values=(workspace_id,*sorted(allowed_source_ids),cutoff)
        documents={str(row["id"]) for row in self.conn.execute(
            f"SELECT id FROM documents WHERE workspace_id=? AND source_id IN ({marks}) AND observed_at<=?",values
        ).fetchall()}
        facts={str(row["id"]) for row in self.conn.execute(
            f"SELECT id FROM facts WHERE workspace_id=? AND source_id IN ({marks}) AND observed_at<=?",values
        ).fetchall()}
        relations={str(row["id"]) for row in self.conn.execute(
            f"SELECT id FROM relations WHERE workspace_id=? AND source_id IN ({marks}) AND observed_at<=?",values
        ).fetchall()}
        return documents,facts,relations

    def _fact_states(self, workspace_id: str, fact_ids: Iterable[str], moment: datetime) -> dict[str, str]:
        ids = list(fact_ids)
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        cutoff = moment.isoformat()
        rows = self.conn.execute(
            f"""SELECT subject_id,state FROM knowledge_state_events
                WHERE workspace_id=? AND subject_type='fact' AND subject_id IN ({marks})
                  AND effective_from<=? AND (effective_until IS NULL OR effective_until>?)
                ORDER BY effective_from DESC,event_sequence DESC,recorded_at DESC,id DESC""",
            (workspace_id, *ids, cutoff, cutoff),
        ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            result.setdefault(str(row["subject_id"]), str(row["state"]))
        return result

    def _entities(
        self, organization_id: str, workspace_id: str, moment: datetime,
        allowed_source_ids: set[str],
    ) -> list[dict[str, Any]]:
        cutoff = moment.isoformat()
        entities = [dict(row) for row in self.conn.execute(
            "SELECT * FROM entities WHERE organization_id=? AND workspace_id=? AND created_at<=? ORDER BY canonical_name,id",
            (organization_id, workspace_id, cutoff),
        ).fetchall()]
        if not entities:
            return []
        ids = [str(row["id"]) for row in entities]; marks = ",".join("?" for _ in ids)
        merges = self.conn.execute(
            f"SELECT source_entity_id,target_entity_id FROM entity_merge_history WHERE organization_id=? AND source_entity_id IN ({marks}) AND merged_at<=? ORDER BY merged_at,id",
            (organization_id, *ids, cutoff),
        ).fetchall()
        redirect = {str(row["source_entity_id"]): str(row["target_entity_id"]) for row in merges}
        aliases = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM entity_aliases WHERE entity_id IN ({marks}) AND created_at<=? AND status='approved' ORDER BY created_at,id",
            (*ids, cutoff),
        ).fetchall()]
        alias_ids = [str(row["id"]) for row in aliases]
        lifecycle: dict[str, str] = {}
        if alias_ids:
            alias_marks = ",".join("?" for _ in alias_ids)
            rows = self.conn.execute(
                f"SELECT alias_id,state FROM entity_alias_state_events WHERE organization_id=? AND workspace_id=? AND alias_id IN ({alias_marks}) AND created_at<=? ORDER BY created_at DESC,id DESC",
                (organization_id, workspace_id, *alias_ids, cutoff),
            ).fetchall()
            for row in rows: lifecycle.setdefault(str(row["alias_id"]), str(row["state"]))
        by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for alias in aliases:
            if alias["source_id"] is not None and str(alias["source_id"]) not in allowed_source_ids:
                continue
            owner = str(alias["entity_id"])
            target = _redirect(owner, redirect)
            state = lifecycle.get(str(alias["id"]), "active")
            if state == "retired" and target == owner:
                continue
            by_entity[target].append({
                "id": alias["id"], "alias": alias["alias"], "confidence": alias["confidence"],
                "state": state, "origin_entity_id": owner,
            })
        result = []
        for entity in entities:
            if str(entity["id"]) in redirect:
                continue
            if not by_entity.get(str(entity["id"])):
                continue
            result.append({
                "id": entity["id"], "canonical_name": entity["canonical_name"], "type": entity["type"],
                "aliases": by_entity.get(str(entity["id"]), []),
            })
        return result
