from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.security import AuthenticatedIdentity

from .dashboard_shared import _TERMINAL, _action, _derived_run_status, _json_list, _json_object, _moment, _optional_float


class DashboardWorkflowsMixin:
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
        stage_contracts: dict[tuple[str, str], dict[str, Any]] = {}
        for run in run_rows:
            snapshot=_json_object(run["template_snapshot"])
            for item in snapshot.get("stages",[]) if isinstance(snapshot.get("stages"),list) else []:
                if isinstance(item,dict):
                    key = item.get("key") or item.get("id") or item.get("slug") or item.get("stage_key")
                    if key:
                        duration = item.get("expected_duration_hours")
                        if duration is None:
                            duration = item.get("estimate_hours")
                        stage_contracts[(str(run["id"]), str(key))] = {
                            "handoff_contract": str(item.get("handoff_contract") or ""),
                            "expected_duration_hours": _optional_float(duration),
                        }
        assignee_ids = sorted({
            str(stage["assignee_person_id"]) for stage in stages if stage.get("assignee_person_id")
        })
        assignee_agent_ids = sorted({str(stage.get("assignee_principal_id")) for stage in stages if stage.get("assignee_principal_type") == "agent" and stage.get("assignee_principal_id")})
        people_by_id: dict[str, dict[str, Any]] = {}
        if assignee_ids:
            people_marks = ",".join("?" for _ in assignee_ids)
            people_by_id = {
                str(row["id"]): dict(row) for row in self.conn.execute(
                    f"""SELECT p.id,p.name,p.title,p.department,wm.role AS workspace_role
                        FROM people p
                        JOIN workspace_memberships wm ON wm.person_id=p.id
                        WHERE p.organization_id=? AND wm.workspace_id=? AND p.id IN ({people_marks})
                        ORDER BY p.name,p.id""",
                    (organization_id, workspace_id, *assignee_ids),
                ).fetchall()
            }
        agents_by_id = {}
        if assignee_agent_ids:
            marks = ",".join("?" for _ in assignee_agent_ids)
            agents_by_id = {str(row["id"]): dict(row) for row in self.conn.execute(f"SELECT id,name,status FROM agents WHERE organization_id=? AND id IN ({marks})", (organization_id, *assignee_agent_ids)).fetchall()}

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
        request_by_stage: dict[str,dict[str,Any]] = {}
        request_rows=self.conn.execute("""SELECT id,requested_for,approver_person_id,status,created_at
            FROM approval_requests WHERE organization_id=? AND workspace_id=? AND action_type='workflow_stage_approval'
              AND created_at<=? ORDER BY created_at,id""",(organization_id,workspace_id,cutoff)).fetchall()
        stage_id_by_contract_key={
            f"workflow:{stage['run_id']}:{stage['stage_key']}":str(stage["id"]) for stage in stages
        }
        for raw in request_rows:
            stage_id=stage_id_by_contract_key.get(str(raw["requested_for"]))
            if stage_id: request_by_stage[stage_id]=dict(raw)
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
            contract = stage_contracts.get((str(stage["run_id"]), str(stage["stage_key"])), {})
            stage["handoff_contract"] = str(contract.get("handoff_contract") or "")
            stage["expected_duration_hours"] = contract.get("expected_duration_hours")
            stage_by_id[str(stage["id"])] = stage

        rendered_stages: list[dict[str, Any]] = []
        for stage in stages:
            stage_id = str(stage["id"])
            deps = dependencies_by_stage.get(stage_id, [])
            dependency_view = []
            missing_handoffs: list[dict[str,Any]] = []
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
                if requires_handoff and completed and not acknowledged:
                    missing_handoffs.append({
                        "from_stage_id":source["stage_key"],"to_stage_id":stage["stage_key"],
                        "artifact_contract":source["handoff_contract"],
                    })
                dependency_view.append({
                    "stage_run_id": source["id"], "stage_key": source["stage_key"], "kind": dependency["kind"],
                    "status": source["status"], "handoff_required": requires_handoff,
                    "handoff_acknowledged": acknowledged,"handoff_contract":source["handoff_contract"],
                })
            counts = evidence_by_stage.get(stage_id, Counter())
            missing = [kind for kind in stage["required_evidence"] if counts[kind] == 0]
            approval = approval_by_stage.get(stage_id)
            request = request_by_stage.get(stage_id) or requests_by_id.get(request_id_by_stage.get(stage_id, ""))
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
                identity,workspace_id,stage,ready,not missing,approval_view,request_view,
                missing_handoffs
            )
            assignee_person = people_by_id.get(str(stage["assignee_person_id"])) if stage.get("assignee_person_id") else None
            assignee_agent = agents_by_id.get(str(stage.get("assignee_principal_id"))) if stage.get("assignee_principal_type") == "agent" else None
            owner = {
                "wing": stage["assignee_wing"], "role": stage["assignee_role"],
                "person_id": stage["assignee_person_id"], "person": assignee_person,
                "principal_type": stage.get("assignee_principal_type", "person"),
                "principal_id": stage.get("assignee_principal_id") or stage.get("assignee_person_id"),
                "agent_id": assignee_agent["id"] if assignee_agent else None,
                "agent": assignee_agent,
            }
            expected_duration_hours = stage.get("expected_duration_hours")
            rendered_stages.append({
                "id": stage_id, "run_id": stage["run_id"], "stage_key": stage["stage_key"], "name": stage["name"],
                "sequence": stage["sequence"], "status": stage["status"],
                "assignee": owner, "owner": owner,
                "expected_duration_hours": expected_duration_hours,
                "expected_duration": {
                    "hours": expected_duration_hours,
                    "source": "template_snapshot" if expected_duration_hours is not None else "unknown",
                },
                "readiness": {"ready": ready, "dependencies_clear": dependencies_clear, "handoffs_clear": handoffs_clear},
                "dependencies": dependency_view,
                "evidence": {"total": sum(counts.values()), "by_kind": dict(sorted(counts.items())), "required": stage["required_evidence"], "missing": missing},
                "approval": {"required": stage["requires_approval"], "request": request_view, "latest": approval_view},
                "handoff": {"to_wing": stage["handoff_to_wing"], "to_role": stage["handoff_to_role"], "to_person_id": stage["handoff_to_person_id"],"contract":stage["handoff_contract"]},
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
            actions: list[dict[str,Any]] = []
            if not historical and identity.can("workflow_run") and run["status"] not in _TERMINAL:
                actions.append(_action(
                    "cancel_run","/workflows/runs/cancel",
                    {"workspace_id":workspace_id,"run_id":run["id"],"expected_version":run["version"]},
                    ["reason"],
                ))
            expected_total = sum(
                float(stage["expected_duration_hours"])
                for stage in children if stage.get("expected_duration_hours") is not None
            )
            active_expected = sum(
                float(stage["expected_duration_hours"])
                for stage in children
                if stage.get("expected_duration_hours") is not None and stage.get("status") not in _TERMINAL
            )
            runs.append({
                "id": run["id"], "definition_key": run["definition_key"], "definition_name": run["definition_name"],
                "definition_version": run["definition_version"], "status": run["status"],
                "due": {"at": run["due_at"], "escalation_at": run["escalation_at"], "overdue": bool(run["due_at"] and run["due_at"] < cutoff and run["status"] not in _TERMINAL)},
                "blocker": run["blocked_reason"] if not historical else next((stage["blocker"] for stage in children if stage["blocker"]), None),
                "progress": {"completed": counts["completed"], "total": len(children), "status_counts": dict(sorted(counts.items()))},
                "rollups": {
                    "expected_duration_hours": round(expected_total, 4),
                    "active_expected_duration_hours": round(active_expected, 4),
                    "unestimated_stage_count": sum(stage.get("expected_duration_hours") is None for stage in children),
                    "owner_roles": sorted({
                        str((stage.get("owner") or {}).get("role")) for stage in children
                        if (stage.get("owner") or {}).get("role")
                    }),
                },
                "allowed_actions": actions,
            })
        return self._workflow_response(moment, historical, runs, rendered_stages)

    @staticmethod
    def _stage_actions(
        identity: AuthenticatedIdentity, workspace_id: str, stage: dict[str, Any],
        ready: bool, evidence_clear: bool, approval: dict[str, Any] | None,
        request: dict[str, Any] | None, missing_handoffs: list[dict[str,Any]],
    ) -> list[dict[str,Any]]:
        actions: list[dict[str,Any]] = []
        status = stage["status"]
        base={"workspace_id":workspace_id,"run_id":stage["run_id"],"stage_id":stage["stage_key"]}
        if identity.can("workflow_run"):
            if ready:
                actions.append(_action("start_stage","/workflows/stages/start",{**base,"expected_version":stage["version"]}))
            if status not in _TERMINAL:
                actions.append(_action("submit_evidence","/workflows/evidence",base,["kind","one_of:uri,text,object_type"] ))
            if status in {"pending", "in_progress", "waiting_approval"}:
                actions.append(_action("block_stage","/workflows/stages/block",{**base,"expected_version":stage["version"]},["reason"]))
            approval_clear = not stage["requires_approval"] or bool(approval and approval["decision"] == "approve")
            if status in {"in_progress", "waiting_approval"} and evidence_clear and approval_clear:
                actions.append(_action("complete_stage","/workflows/stages/complete",{**base,"expected_version":stage["version"]}))
        if identity.can("workflow_gate"):
            if (
                stage["requires_approval"] and status == "in_progress" and evidence_clear
                and request is not None and request["status"]=="pending"
            ):
                actions.append(_action(
                    "request_approval","/workflows/approvals/request",
                    {**base,"approval_request_id":request["id"],"expected_version":stage["version"]},["reason"],
                ))
            for handoff in missing_handoffs:
                if handoff["artifact_contract"]:
                    actions.append(_action(
                        "acknowledge_handoff","/workflows/handoffs/acknowledge",
                        {"workspace_id":workspace_id,"run_id":stage["run_id"],**handoff},
                    ))
        if (
            identity.can("approval_decide") and status == "waiting_approval" and request is not None
            and request["approver_person_id"] == identity.person_id and request["status"] in {"approved","rejected"}
        ):
            decision="approve" if request["status"]=="approved" else "request_changes"
            actions.append(_action(
                "decide_approval","/workflows/approvals/decide",
                {**base,"approval_request_id":request["id"],"decision":decision},["reason"],
            ))
        return actions
