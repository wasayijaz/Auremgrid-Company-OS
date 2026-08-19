from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity


class _ExistingDashboardService:
    def __init__(self, os: Any) -> None: self.os=os; self.conn=os.store.conn

    def command(self, organization_id: str, person_id: str) -> dict[str, Any]:
        if self.os.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
        workspaces=self.conn.execute("""SELECT w.id,w.name,wo.kind,wm.role FROM workspaces w JOIN workspace_organization wo ON wo.workspace_id=w.id
            JOIN workspace_memberships wm ON wm.workspace_id=w.id WHERE wo.organization_id=? AND wm.person_id=?""",(organization_id,person_id)).fetchall()
        ids=[row["id"] for row in workspaces]; placeholders=",".join("?" for _ in ids) or "NULL"
        def count(table: str, clause: str="1=1") -> int:
            if not ids:return 0
            return int(self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IN ({placeholders}) AND {clause}",ids).fetchone()[0])
        active_clients=sum(row["kind"]=="client" for row in workspaces); open_work=count("work_items","status!='shipped'")
        overdue=count("work_items","status!='shipped' AND needed_by IS NOT NULL AND needed_by < date('now')")
        review=count("reviews","status='open'"); risks=count("risks","status='open'")
        active_workflows=count("workflow_runs","status NOT IN ('completed','cancelled')")
        agents=[dict(r) for r in self.conn.execute("SELECT * FROM agents WHERE organization_id=? ORDER BY name",(organization_id,)).fetchall()]
        automation_count=self.conn.execute("SELECT COUNT(*) FROM automation_runs ar JOIN automations a ON a.id=ar.automation_id WHERE a.organization_id=? AND ar.started_at>=date('now')",(organization_id,)).fetchone()[0]
        finance=self.os.agency_ops.finance_status(organization_id,person_id)
        attention=[{**item,"client":None,"severity":"ranked","evidence":item["reason"],"owner":person_id,"next_action":"Open source record"}
            for item in self.os.agency_ops.attention(organization_id,person_id,10)]
        if ids:
            overdue_rows=self.conn.execute(f"""SELECT wi.id,wi.title,wi.needed_by,wi.assignee_person_id,w.name client
                FROM work_items wi JOIN workspaces w ON w.id=wi.workspace_id
                WHERE wi.workspace_id IN ({placeholders}) AND wi.status!='shipped' AND wi.needed_by IS NOT NULL
                AND wi.needed_by < date('now') ORDER BY wi.needed_by""",ids).fetchall()
            attention.extend({"id":row["id"],"priority":.92,"reason":f"{row['title']} is overdue","source_type":"work_item",
                "source_id":row["id"],"client":row["client"],"severity":"high","evidence":f"Due {row['needed_by']}",
                "owner":row["assignee_person_id"],"next_action":"Replan or complete the work"} for row in overdue_rows)
            risk_rows=self.conn.execute(f"""SELECT r.*,w.name client FROM risks r JOIN workspaces w ON w.id=r.workspace_id
                WHERE r.workspace_id IN ({placeholders}) AND r.status='open' ORDER BY CASE r.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC""",ids).fetchall()
            attention.extend({"id":row["id"],"priority":.98 if row["severity"]=="critical" else .88,"reason":row["impact"],
                "source_type":"risk","source_id":row["id"],"client":row["client"],"severity":row["severity"],
                "evidence":row["evidence"],"owner":row["owner_person_id"],"next_action":row["recommended_action"]} for row in risk_rows)
            stalled=self.conn.execute(f"""SELECT rv.id,rv.opened_at,w.name client FROM reviews rv JOIN workspaces w ON w.id=rv.workspace_id
                WHERE rv.workspace_id IN ({placeholders}) AND rv.status='open' AND rv.opened_at < datetime('now','-48 hours')""",ids).fetchall()
            attention.extend({"id":row["id"],"priority":.84,"reason":"Review has been open over 48 hours","source_type":"review",
                "source_id":row["id"],"client":row["client"],"severity":"medium","evidence":f"Opened {row['opened_at']}",
                "owner":None,"next_action":"Assign a reviewer or close the review"} for row in stalled)
        attention=sorted(attention,key=lambda item:item["priority"],reverse=True)[:3]
        clients=[]
        for ws in workspaces:
            if ws["kind"]!="client":continue
            health=self.conn.execute("SELECT * FROM client_health_snapshots WHERE workspace_id=? ORDER BY calculated_at DESC LIMIT 1",(ws["id"],)).fetchone()
            latest=self.conn.execute("SELECT occurred_at FROM touchpoints WHERE workspace_id=? ORDER BY occurred_at DESC LIMIT 1",(ws["id"],)).fetchone()
            clients.append({"id":ws["id"],"name":ws["name"],"role":ws["role"],"health":health["overall"] if health else None,
                "health_trend":health["trend"] if health else None,"open_work":self.conn.execute("SELECT COUNT(*) FROM work_items WHERE workspace_id=? AND status!='shipped'",(ws["id"],)).fetchone()[0],
                "reviews":self.conn.execute("SELECT COUNT(*) FROM reviews WHERE workspace_id=? AND status='open'",(ws["id"],)).fetchone()[0],
                "risks":self.conn.execute("SELECT COUNT(*) FROM risks WHERE workspace_id=? AND status='open'",(ws["id"],)).fetchone()[0],
                "last_touch":latest[0] if latest else None})
        pulse=[]
        for row in self.conn.execute("SELECT workspace_id,action,target,detail,recorded_at FROM audit_events WHERE workspace_id IN ("+placeholders+") ORDER BY recorded_at DESC LIMIT 12",ids).fetchall() if ids else []:
            pulse.append(dict(row))
        return {"generated_at":datetime.now(timezone.utc).isoformat(),"metrics":{"active_clients":active_clients,"mrr":finance.get("mrr") if finance["status"]=="connected" else None,
            "finance_status":finance["status"],"open_work":open_work,"overdue_work":overdue,"in_review":review,"active_workflows":active_workflows,"agents_running":sum(a["status"]=="running" for a in agents),
            "automations_today":automation_count,"open_risks":risks},"attention":attention,"clients":clients,"agents":agents,"pulse":pulse,
            "workspaces":[dict(row) for row in workspaces]}

    def client_hq(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.os._require_person_access(organization_id,workspace_id,person_id)
        workspace=self.conn.execute("SELECT * FROM workspaces WHERE id=?",(workspace_id,)).fetchone(); health=self.conn.execute("SELECT * FROM client_health_snapshots WHERE workspace_id=? ORDER BY calculated_at DESC LIMIT 1",(workspace_id,)).fetchone()
        return {"workspace":dict(workspace),"health":dict(health) if health else None,
            "projects":[p.to_dict() for p in self.os.company.list_projects(workspace_id)],
            "work":[w.to_dict() for w in self.os.store.list_work_items(workspace_id)],
            "reviews":[r.to_dict() for r in self.os.company.list_reviews(workspace_id)],
            "risks":self.os.client_ops.list_risks(organization_id,workspace_id,person_id),
            "decisions":[d.to_dict() for d in self.os.company.list_decisions(organization_id,workspace_id)],
            "campaigns":[dict(r) for r in self.conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY updated_at DESC",(workspace_id,)).fetchall()],
            "content":[dict(r) for r in self.conn.execute("SELECT * FROM content_items WHERE workspace_id=? ORDER BY updated_at DESC",(workspace_id,)).fetchall()],
            "creative":[dict(r) for r in self.conn.execute("SELECT * FROM creative_assets WHERE workspace_id=? ORDER BY created_at DESC",(workspace_id,)).fetchall()],
            "workflows":[dict(r) for r in self.conn.execute("""SELECT id,definition_key,definition_name,definition_version,status,due_at,updated_at
                FROM workflow_runs WHERE workspace_id=? ORDER BY updated_at DESC""",(workspace_id,)).fetchall()],
            "files":[dict(r) for r in self.conn.execute("""SELECT wf.id,wf.title,wf.url,wf.source,wf.created_at FROM work_files wf JOIN work_items wi ON wi.id=wf.work_item_id WHERE wi.workspace_id=?
                UNION ALL SELECT df.id,df.title,df.url,df.kind,df.created_at FROM deliverable_files df JOIN deliverables d ON d.id=df.deliverable_id WHERE d.workspace_id=?""",(workspace_id,workspace_id)).fetchall()],
            "meetings":[dict(r) for r in self.conn.execute("SELECT * FROM meetings WHERE workspace_id=? ORDER BY occurred_at DESC",(workspace_id,)).fetchall()],
            "messages":[dict(r) for r in self.conn.execute("SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.workspace_id=? ORDER BY sent_at DESC",(workspace_id,)).fetchall()],
            "people":[dict(r) for r in self.conn.execute("""SELECT p.id,p.name,p.title,p.department,wm.role FROM workspace_memberships wm JOIN people p ON p.id=wm.person_id WHERE wm.workspace_id=?""",(workspace_id,)).fetchall()]+[dict(r) for r in self.conn.execute("SELECT id,name,role,influence,decision_power,last_contact_at FROM contacts WHERE workspace_id=?",(workspace_id,)).fetchall()],
            "activity":[dict(r) for r in self.conn.execute("SELECT action,entity_type,entity_id,detail,recorded_at FROM ledger_audit WHERE workspace_id=? ORDER BY recorded_at DESC LIMIT 50",(workspace_id,)).fetchall()],
            "finance":self.os.agency_ops.finance_status(organization_id,person_id,workspace_id),
            "brain":self.os.store.get_client_brain(workspace_id).to_dict() if self.os.store.get_client_brain(workspace_id) else None}

    def module(self, organization_id: str, workspace_id: str, person_id: str, module: str) -> dict[str,Any]:
        self.os._require_person_access(organization_id,workspace_id,person_id)
        queries={
            "Campaigns":("campaigns","SELECT id,name,platform,status,budget,updated_at FROM campaigns WHERE workspace_id=? ORDER BY updated_at DESC"),
            "Content":("content_items","SELECT id,title,stage,objective,publish_at,updated_at FROM content_items WHERE workspace_id=? ORDER BY updated_at DESC"),
            "Creative":("creative_assets","SELECT id,title,format,platform,approval_state,revision_count,created_at FROM creative_assets WHERE workspace_id=? ORDER BY created_at DESC"),
            "Meetings":("meetings","SELECT id,title,occurred_at,summary,sentiment,source FROM meetings WHERE workspace_id=? ORDER BY occurred_at DESC"),
            "Automations":("automations","SELECT id,name,status,approval_policy,created_at FROM automations WHERE organization_id=? ORDER BY created_at DESC"),
            "Reports":("report_runs","SELECT id,type,status,generated_at FROM report_runs WHERE organization_id=? ORDER BY generated_at DESC"),
            "Integrations":("integrations","SELECT id,source,status,last_sync_at,last_error,object_count,health FROM integrations WHERE organization_id=? ORDER BY source"),
            "Workflows":("workflow_runs","SELECT id,definition_name,definition_version,status,due_at,updated_at FROM workflow_runs WHERE workspace_id=? ORDER BY updated_at DESC"),
        }
        if module not in queries:return {"module":module,"items":[]}
        table,sql=queries[module];scope=organization_id if module in {"Automations","Reports","Integrations"} else workspace_id
        return {"module":module,"source_table":table,"items":[dict(row) for row in self.conn.execute(sql,(scope,)).fetchall()]}

_TERMINAL = {"completed", "cancelled"}


class DashboardService(_ExistingDashboardService):
    """Compose dashboard views from canonical scoped state without shadow writes."""

    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn

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

        conflicts = []
        for group_id, alternatives in sorted(conflict_rows.items()):
            live = [item for item in alternatives if item["state"] != "stale"]
            winner = next((item["id"] for item in alternatives if item["state"] == "verified"), None)
            conflicts.append(
                {
                    "id": group_id,
                    "state": "resolved" if winner and len(live) == 1 else "conflicted",
                    "winner_fact_id": winner,
                    "alternatives": sorted(alternatives, key=lambda item: (item["recorded_at"], item["id"])),
                }
            )

        proposals = []
        for proposal in self.os.brain_ops.list_memory_proposals(
            organization_id, workspace_id, person_id, as_of=as_of
        ):
            if proposal.get("source_id") and proposal["source_id"] not in source_ids:
                continue
            item = dict(proposal)
            item["structured_payload"] = _json_object(item.get("structured_payload"))
            proposals.append(item)

        entities = self._entities(organization_id, workspace_id, moment, source_ids)
        workspace = self.os.store.get_workspace(workspace_id)
        graph = self.os.store.graph_generation_state(workspace_id)
        semantic = dict(getattr(self.os, "embedding_health", {}) or {})
        health = {
            "semantic": {
                "status": _health_status(semantic.get("status")),
                "provider": _safe_scalar(semantic.get("provider")),
                "model": _safe_scalar(semantic.get("model")),
                "version": _safe_scalar(semantic.get("version")),
                "fallback_used": bool(semantic.get("fallback_used", False)),
            },
            "graph": {
                "status": "healthy" if graph.get("active_status") == "active" else "degraded",
                "active_generation": graph.get("active_generation"),
                "building": graph.get("building_generation") is not None,
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
            },
            "proposals": proposals,
            "conflicts": conflicts,
            "current_truths": sorted(truth_rows, key=lambda item: (item["subject"], item["predicate"], item["id"])),
            "entities": entities,
            "health": health,
        }

    def workflow_board(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        self._authorize(identity, organization_id, workspace_id, person_id, "workspace_read")
        moment = _moment(as_of)
        cutoff = moment.isoformat()
        historical = as_of is not None
        run_rows = self.conn.execute(
            """SELECT * FROM workflow_runs
               WHERE organization_id=? AND workspace_id=? AND created_at<=?
               ORDER BY COALESCE(due_at,created_at),created_at,id""",
            (organization_id, workspace_id, cutoff),
        ).fetchall()
        if not run_rows:
            return self._workflow_response(moment, historical, [], [])
        run_ids = [str(row["id"]) for row in run_rows]
        marks = ",".join("?" for _ in run_ids)
        stages = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_stage_runs WHERE run_id IN ({marks}) AND created_at<=? ORDER BY run_id,sequence,stage_key",
            (*run_ids, cutoff),
        ).fetchall()]
        stage_ids = [str(row["id"]) for row in stages]
        stage_marks = ",".join("?" for _ in stage_ids) or "NULL"
        history = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_transition_history WHERE run_id IN ({marks}) AND created_at<=? ORDER BY created_at,rowid",
            (*run_ids, cutoff),
        ).fetchall()]
        dependencies = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_stage_dependencies WHERE run_id IN ({marks}) AND created_at<=?",
            (*run_ids, cutoff),
        ).fetchall()]
        evidence = [dict(row) for row in self.conn.execute(
            f"SELECT stage_run_id,kind,COUNT(*) AS count FROM workflow_evidence WHERE stage_run_id IN ({stage_marks}) AND created_at<=? GROUP BY stage_run_id,kind",
            (*stage_ids, cutoff),
        ).fetchall()] if stage_ids else []
        approvals = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_approval_decisions WHERE stage_run_id IN ({stage_marks}) AND created_at<=? ORDER BY created_at,rowid",
            (*stage_ids, cutoff),
        ).fetchall()] if stage_ids else []
        handoffs = [dict(row) for row in self.conn.execute(
            f"SELECT * FROM workflow_handoff_acknowledgements WHERE run_id IN ({marks}) AND created_at<=? ORDER BY created_at,rowid",
            (*run_ids, cutoff),
        ).fetchall()]

        history_by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
        history_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in history:
            history_by_run[str(event["run_id"])].append(event)
            if event["stage_run_id"]:
                history_by_stage[str(event["stage_run_id"])].append(event)
        evidence_by_stage: dict[str, Counter[str]] = defaultdict(Counter)
        for row in evidence:
            evidence_by_stage[str(row["stage_run_id"])][str(row["kind"])] = int(row["count"])
        approval_by_stage = {str(row["stage_run_id"]): row for row in approvals}
        request_id_by_stage: dict[str, str] = {}
        for event in history:
            if event["stage_run_id"] and event["action"] == "request_approval":
                metadata = _json_object(event["metadata"])
                if metadata.get("approval_request_id"):
                    request_id_by_stage[str(event["stage_run_id"])] = str(metadata["approval_request_id"])
        request_ids = sorted(set(request_id_by_stage.values()))
        requests_by_id: dict[str, dict[str, Any]] = {}
        if request_ids:
            request_marks = ",".join("?" for _ in request_ids)
            requests_by_id = {str(row["id"]): dict(row) for row in self.conn.execute(
                f"SELECT id,approver_person_id,status,created_at FROM approval_requests "
                f"WHERE organization_id=? AND workspace_id=? AND id IN ({request_marks}) AND created_at<=?",
                (organization_id, workspace_id, *request_ids, cutoff),
            ).fetchall()}
        ack_by_pair = {(str(row["from_stage_run_id"]), str(row["to_stage_run_id"])): row for row in handoffs}
        dependencies_by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for dependency in dependencies:
            dependencies_by_stage[str(dependency["stage_run_id"])].append(dependency)

        stage_by_id: dict[str, dict[str, Any]] = {}
        for stage in stages:
            events = history_by_stage.get(str(stage["id"]), [])
            if historical:
                stage["status"] = str(events[-1]["to_status"] or "pending") if events else "pending"
                stage["version"] = 1 + sum(event["from_status"] != event["to_status"] for event in events)
                stage["blocked_reason"] = next(
                    (event["reason"] for event in reversed(events) if event["to_status"] == "blocked"), None
                ) if stage["status"] == "blocked" else None
            stage["required_evidence"] = _json_list(stage["required_evidence"])
            stage["requires_approval"] = bool(stage["requires_approval"])
            stage_by_id[str(stage["id"])] = stage

        rendered_stages: list[dict[str, Any]] = []
        for stage in stages:
            stage_id = str(stage["id"])
            deps = dependencies_by_stage.get(stage_id, [])
            dependency_view = []
            dependencies_clear = True
            handoffs_clear = True
            for dependency in deps:
                source = stage_by_id[str(dependency["depends_on_stage_run_id"])]
                completed = source["status"] == "completed"
                requires_handoff = bool(source["handoff_to_wing"] or source["handoff_to_role"] or source["handoff_to_person_id"])
                ack = ack_by_pair.get((str(source["id"]), stage_id))
                acknowledged = bool(ack and int(ack["source_stage_version"]) == int(source["version"]))
                dependencies_clear = dependencies_clear and completed
                handoffs_clear = handoffs_clear and (not requires_handoff or acknowledged)
                dependency_view.append({
                    "stage_run_id": source["id"], "stage_key": source["stage_key"], "kind": dependency["kind"],
                    "status": source["status"], "handoff_required": requires_handoff, "handoff_acknowledged": acknowledged,
                })
            counts = evidence_by_stage.get(stage_id, Counter())
            missing = [kind for kind in stage["required_evidence"] if counts[kind] == 0]
            approval = approval_by_stage.get(stage_id)
            request = requests_by_id.get(request_id_by_stage.get(stage_id, ""))
            approval_view = None if approval is None else {
                "decision": approval["decision"], "approval_request_id": approval["approval_request_id"],
                "approver_person_id": approval["approver_person_id"], "reason": approval["reason"],
                "created_at": approval["created_at"],
            }
            request_view = None if request is None else {
                "id": request["id"], "approver_person_id": request["approver_person_id"],
                "status": (
                    "unknown" if historical and approval_view is None
                    else approval_view["decision"] if historical and approval_view is not None
                    else request["status"]
                ),
            }
            ready = stage["status"] in {"pending", "blocked"} and dependencies_clear and handoffs_clear
            actions = [] if historical else self._stage_actions(
                identity, stage, ready, not missing, approval_view, request_view,
                dependencies_clear, handoffs_clear
            )
            rendered_stages.append({
                "id": stage_id, "run_id": stage["run_id"], "stage_key": stage["stage_key"], "name": stage["name"],
                "sequence": stage["sequence"], "status": stage["status"],
                "assignee": {"wing": stage["assignee_wing"], "role": stage["assignee_role"], "person_id": stage["assignee_person_id"]},
                "readiness": {"ready": ready, "dependencies_clear": dependencies_clear, "handoffs_clear": handoffs_clear},
                "dependencies": dependency_view,
                "evidence": {"total": sum(counts.values()), "by_kind": dict(sorted(counts.items())), "required": stage["required_evidence"], "missing": missing},
                "approval": {"required": stage["requires_approval"], "request": request_view, "latest": approval_view},
                "handoff": {"to_wing": stage["handoff_to_wing"], "to_role": stage["handoff_to_role"], "to_person_id": stage["handoff_to_person_id"]},
                "blocker": stage["blocked_reason"],
                "due": {"at": stage["due_at"], "overdue": bool(stage["due_at"] and stage["due_at"] < cutoff and stage["status"] not in _TERMINAL)},
                "version": stage["version"], "allowed_actions": actions,
            })

        stages_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for stage in rendered_stages:
            stages_by_run[str(stage["run_id"])].append(stage)
        runs = []
        for raw in run_rows:
            run = dict(raw); children = stages_by_run.get(str(run["id"]), [])
            if historical:
                run["status"] = _derived_run_status(children, history_by_run.get(str(run["id"]), []))
            counts = Counter(stage["status"] for stage in children)
            actions = []
            if not historical and identity.can("workflow_run") and run["status"] not in _TERMINAL:
                actions.append("cancel_run")
            runs.append({
                "id": run["id"], "definition_key": run["definition_key"], "definition_name": run["definition_name"],
                "definition_version": run["definition_version"], "status": run["status"],
                "due": {"at": run["due_at"], "escalation_at": run["escalation_at"], "overdue": bool(run["due_at"] and run["due_at"] < cutoff and run["status"] not in _TERMINAL)},
                "blocker": run["blocked_reason"] if not historical else next((stage["blocker"] for stage in children if stage["blocker"]), None),
                "progress": {"completed": counts["completed"], "total": len(children), "status_counts": dict(sorted(counts.items()))},
                "allowed_actions": actions,
            })
        return self._workflow_response(moment, historical, runs, rendered_stages)

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

    @staticmethod
    def _stage_actions(
        identity: AuthenticatedIdentity, stage: dict[str, Any], ready: bool, evidence_clear: bool,
        approval: dict[str, Any] | None, request: dict[str, Any] | None,
        dependencies_clear: bool, handoffs_clear: bool,
    ) -> list[str]:
        actions: list[str] = []
        status = stage["status"]
        if identity.can("workflow_run"):
            if ready: actions.append("start_stage")
            if status not in _TERMINAL: actions.append("submit_evidence")
            if status in {"pending", "in_progress", "waiting_approval"}: actions.append("block_stage")
            approval_clear = not stage["requires_approval"] or bool(approval and approval["decision"] == "approve")
            if status in {"in_progress", "waiting_approval"} and evidence_clear and approval_clear:
                actions.append("complete_stage")
        if identity.can("workflow_gate"):
            if stage["requires_approval"] and status == "in_progress" and evidence_clear:
                actions.append("request_approval")
            if dependencies_clear and not handoffs_clear:
                actions.append("acknowledge_handoff")
        if (
            identity.can("approval_decide") and status == "waiting_approval" and request is not None
            and request["approver_person_id"] == identity.person_id
        ):
            actions.append("decide_approval")
        return actions

    def _authorize(
        self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str,
        person_id: str, capability: str,
    ) -> None:
        if (
            identity.organization_id != organization_id
            or identity.person_id != person_id
            or identity.workspace_id not in {None, workspace_id}
        ):
            raise AuthorizationError("dashboard scope denied")
        identity.require(capability)
        self.os._require_person_access(organization_id, workspace_id, person_id, write=False)

    @staticmethod
    def _workflow_response(moment: datetime, historical: bool, runs: list[dict[str, Any]], stages: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(), "as_of": moment.isoformat(),
            "historical": historical,
            "summary": {"runs": len(runs), "active_runs": sum(run["status"] not in _TERMINAL for run in runs), "stages": len(stages)},
            "runs": runs, "stages": stages,
        }


def _moment(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("as_of must include a timezone")
    return result.astimezone(timezone.utc)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict): return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list): return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _redirect(entity_id: str, redirects: dict[str, str]) -> str:
    seen: set[str] = set(); current = entity_id
    while current in redirects and current not in seen:
        seen.add(current); current = redirects[current]
    return current


def _derived_run_status(stages: list[dict[str, Any]], history: list[dict[str, Any]]) -> str:
    explicit = [event for event in history if event["stage_run_id"] is None and event["to_status"] in _TERMINAL]
    if explicit: return str(explicit[-1]["to_status"])
    statuses = [stage["status"] for stage in stages]
    if statuses and all(status == "completed" for status in statuses): return "completed"
    if statuses and all(status == "cancelled" for status in statuses): return "cancelled"
    if "waiting_approval" in statuses: return "waiting_approval"
    if "blocked" in statuses and not any(status == "in_progress" for status in statuses): return "blocked"
    if any(status in {"in_progress", "completed", "blocked"} for status in statuses): return "in_progress"
    return "pending"


def _health_status(value: Any) -> str:
    return str(value) if value in {"healthy", "degraded", "unavailable"} else "unavailable"


def _safe_scalar(value: Any) -> str | None:
    return str(value)[:120] if isinstance(value, (str, int, float)) else None
