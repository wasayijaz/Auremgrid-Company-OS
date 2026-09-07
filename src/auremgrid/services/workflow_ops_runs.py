from __future__ import annotations
from auremgrid.services.workflow_ops_shared import *

class WorkflowOpsRunsMixin:
    def create_run(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            template: Any,
            due_at: datetime | str | None = None,
            sla_minutes: int | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, person_id, write=True)
            operation = "create_run"
            if idempotency_key:
                cached = self.repo.get_idempotency(organization_id, idempotency_key, operation)
                if cached is not None:
                    return cached["response"]
    
            now = _now()
            now_text = now.isoformat()
            snapshot = self._normalize_template(template)
            # Roster effective timestamps retain microsecond precision; use an
            # untruncated clock for selection so a roster created moments earlier
            # is active for this run.
            roster = self._active_client_roster(organization_id, workspace_id, datetime.now(timezone.utc).isoformat())
            if roster is None:
                raise ValidationError("active client roster is required for workflow runs")
            self._resolve_roster_assignments(snapshot, roster)
            snapshot["client_roster_id"] = roster["id"]
            snapshot["client_roster_version"] = roster["version"]
            for stage in snapshot["stages"]:
                # Internal normalization marker; do not expose it in persisted
                # snapshots.
                stage.pop("handoff_structured", None)
            due_text = _iso(due_at)
            escalation_at = self._escalation_at(now, due_at, sla_minutes)
            with self.conn:
                # Capacity estimates are part of the immutable definition
                # snapshot. Use a new internal version namespace so templates
                # previously stored without estimates are never overwritten.
                definition_version_key = f"{snapshot['version']}@capacity-v1"
                # A roster assignment is part of the immutable definition
                # snapshot. Scope the stored definition version by roster so a
                # later roster can produce a new run without mutating or
                # conflicting with the prior version.
                definition_version_key += f"@client-roster-{roster['id']}"
                definition, definition_version = self.repo.save_definition_version(
                    organization_id,
                    snapshot["key"],
                    snapshot["name"],
                    definition_version_key,
                    snapshot,
                    person_id,
                    now_text,
                )
                run_id = self.new_id("wrun")
                run = {
                    "id": run_id,
                    "organization_id": organization_id,
                    "workspace_id": workspace_id,
                    "definition_id": definition["id"],
                    "definition_version_id": definition_version["id"],
                    "definition_key": snapshot["key"],
                    "definition_version": snapshot["version"],
                    "definition_name": snapshot["name"],
                    "template_snapshot": snapshot,
                    "status": "pending",
                    "created_by_person_id": person_id,
                    "idempotency_key": idempotency_key,
                    "due_at": due_text,
                    "sla_minutes": sla_minutes,
                    "escalation_at": escalation_at,
                    "blocked_reason": None,
                    "created_at": now_text,
                    "updated_at": now_text,
                    "started_at": None,
                    "completed_at": None,
                    "cancelled_at": None,
                    "version": 1,
                }
                stage_ids = {stage["key"]: self.new_id("wstage") for stage in snapshot["stages"]}
                stages = [
                    {
                        "id": stage_ids[stage["key"]],
                        "run_id": run_id,
                        "stage_key": stage["key"],
                        "name": stage["name"],
                        "sequence": stage["sequence"],
                        "status": "pending",
                        "assignee_wing": stage["assignee_wing"],
                        "assignee_role": stage["assignee_role"],
                        "assignee_person_id": stage["assignee_person_id"],
                        "assignee_principal_type": stage.get("assignee_principal_type", "person"),
                        "assignee_principal_id": stage.get("assignee_principal_id") or stage.get("assignee_person_id"),
                        "required_evidence": stage["required_evidence"],
                        "requires_approval": stage["requires_approval"],
                        "handoff_to_wing": stage["handoff_to_wing"],
                        "handoff_to_role": stage["handoff_to_role"],
                        "handoff_to_person_id": stage["handoff_to_person_id"],
                        "handoff_principal_type": stage.get("handoff_principal_type", "person"),
                        "handoff_principal_id": stage.get("handoff_principal_id") or stage.get("handoff_to_person_id"),
                        "on_reject_stage_key": stage["on_reject_stage_key"],
                        "due_at": self._stage_due_at(now, stage),
                        "blocked_reason": None,
                        "created_at": now_text,
                        "updated_at": now_text,
                        "started_at": None,
                        "completed_at": None,
                        "cancelled_at": None,
                        "version": 1,
                    }
                    for stage in snapshot["stages"]
                ]
                dependencies = [
                    {
                        "run_id": run_id,
                        "stage_run_id": stage_ids[edge["to"]],
                        "depends_on_stage_run_id": stage_ids[edge["from"]],
                        "kind": edge["kind"],
                        "created_at": now_text,
                    }
                    for edge in snapshot["edges"]
                ]
                response = self.repo.create_run(
                    run,
                    stages,
                    dependencies,
                    self._history(run_id, None, person_id, "create_run", None, "pending", "run created", {}, None, now_text),
                )
                if idempotency_key:
                    self.repo.save_idempotency(
                        organization_id, idempotency_key, operation, "workflow_run", run_id, response, now_text
                    )
            return response

    def start_stage(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            stage_key: str,
            expected_version: int | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return self._idempotent_transition(
                organization_id,
                idempotency_key,
                f"start_stage:{run_id}:{stage_key}",
                lambda now_text: self._start_stage(
                    organization_id, workspace_id, person_id, run_id, stage_key, expected_version, idempotency_key, now_text
                ),
            )

    def submit_evidence(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            stage_key: str,
            kind: str,
            uri: str | None = None,
            text: str | None = None,
            metadata: dict[str, Any] | None = None,
            object_type: str | None = None,
            object_id: str | None = None,
            locator: str | None = None,
            content_hash: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return self._idempotent_transition(
                organization_id,
                idempotency_key,
                f"submit_evidence:{run_id}:{stage_key}:{kind}",
                lambda now_text: self._submit_evidence(
                    organization_id,
                    workspace_id,
                    person_id,
                    run_id,
                    stage_key,
                    kind,
                    uri,
                    text,
                    metadata,
                    object_type,
                    object_id,
                    locator,
                    content_hash,
                    idempotency_key,
                    now_text,
                ),
            )

    def request_approval(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            stage_key: str,
            reason: str,
            approval_request_id: str | None = None,
            expected_version: int | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return self._idempotent_transition(
                organization_id,
                idempotency_key,
                f"request_approval:{run_id}:{stage_key}",
                lambda now_text: self._request_approval(
                    organization_id,
                    workspace_id,
                    person_id,
                    run_id,
                    stage_key,
                    reason,
                    approval_request_id,
                    expected_version,
                    idempotency_key,
                    now_text,
                ),
            )

    def decide_approval(
            self,
            organization_id: str,
            workspace_id: str | None,
            approver_person_id: str,
            run_id: str,
            stage_key: str,
            decision: str,
            reason: str,
            approval_request_id: str | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return self._idempotent_transition(
                organization_id,
                idempotency_key,
                f"decide_approval:{run_id}:{stage_key}",
                lambda now_text: self._decide_approval(
                    organization_id,
                    workspace_id,
                    approver_person_id,
                    run_id,
                    stage_key,
                    decision,
                    reason,
                    approval_request_id,
                    idempotency_key,
                    now_text,
                ),
            )

    def acknowledge_handoff(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            from_stage_key: str,
            to_stage_key: str,
            artifact_contract: str,
            reason: str = "",
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return self._idempotent_transition(
                organization_id,
                idempotency_key,
                f"ack_handoff:{run_id}:{from_stage_key}:{to_stage_key}",
                lambda now_text: self._acknowledge_handoff(
                    organization_id,
                    workspace_id,
                    person_id,
                    run_id,
                    from_stage_key,
                    to_stage_key,
                    artifact_contract,
                    reason,
                    idempotency_key,
                    now_text,
                ),
            )

    def complete_stage(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            stage_key: str,
            reason: str = "",
            expected_version: int | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            return self._idempotent_transition(
                organization_id,
                idempotency_key,
                f"complete_stage:{run_id}:{stage_key}",
                lambda now_text: self._complete_stage(
                    organization_id,
                    workspace_id,
                    person_id,
                    run_id,
                    stage_key,
                    reason,
                    expected_version,
                    idempotency_key,
                    now_text,
                ),
            )

    def block_stage(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            stage_key: str,
            reason: str,
            expected_version: int | None = None,
        ) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, person_id, write=True)
            reason = _required_text(reason, "blocking reason")
            now_text = _now().isoformat()
            with self.conn:
                run = self._run_in_scope(organization_id, workspace_id, run_id)
                stage = self.repo.get_stage_by_key(run_id, stage_key)
                self._ensure_transition(stage["status"], "blocked")
                updated = self.repo.update_stage_status(
                    stage["id"],
                    stage["status"],
                    "blocked",
                    now_text,
                    expected_version or stage["version"],
                    blocked_reason=reason,
                )
                if run["status"] not in TERMINAL_STATUSES and run["status"] != "blocked":
                    self.repo.update_run_status(
                        run_id, run["status"], "blocked", now_text, run["version"], blocked_reason=reason
                    )
                self.repo.record_history(
                    self._history(run_id, stage["id"], person_id, "block_stage", stage["status"], "blocked", reason, {}, None, now_text)
                )
            return updated

    def cancel_run(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            reason: str,
            expected_version: int | None = None,
        ) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, person_id, write=True)
            reason = _required_text(reason, "cancellation reason")
            now_text = _now().isoformat()
            with self.conn:
                run = self._run_in_scope(organization_id, workspace_id, run_id)
                self._ensure_transition(run["status"], "cancelled")
                updated = self.repo.update_run_status(
                    run_id,
                    run["status"],
                    "cancelled",
                    now_text,
                    expected_version or run["version"],
                    cancelled_at=now_text,
                    blocked_reason=reason,
                )
                for stage in self.repo.list_stages(run_id):
                    if stage["status"] not in TERMINAL_STATUSES:
                        self.repo.update_stage_status(
                            stage["id"],
                            stage["status"],
                            "cancelled",
                            now_text,
                            stage["version"],
                            cancelled_at=now_text,
                        )
                self.repo.record_history(
                    self._history(run_id, None, person_id, "cancel_run", run["status"], "cancelled", reason, {}, None, now_text)
                )
            return updated

    def summary(self, organization_id: str, workspace_id: str | None, person_id: str, run_id: str) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, person_id, write=False)
            run = self._run_in_scope(organization_id, workspace_id, run_id)
            stages = self.repo.list_stages(run_id)
            counts = {status: 0 for status in STATUSES}
            for stage in stages:
                counts[stage["status"]] += 1
            completed = counts["completed"]
            total = len(stages)
            return {
                "run": run,
                "stages": stages,
                "progress": {
                    "completed": completed,
                    "total": total,
                    "percent": 0 if total == 0 else round(completed / total, 4),
                    "status_counts": counts,
                },
                "history": self.repo.history(run_id),
            }

    def overdue_escalations(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            as_of: datetime | str | None = None,
        ) -> dict[str, list[dict[str, Any]]]:
            self._authorize(organization_id, workspace_id, person_id, write=False)
            return self.repo.overdue(organization_id, workspace_id, _iso(as_of) or _now().isoformat())

    def history(self, organization_id: str, workspace_id: str | None, person_id: str, run_id: str) -> list[dict[str, Any]]:
            self._authorize(organization_id, workspace_id, person_id, write=False)
            self._run_in_scope(organization_id, workspace_id, run_id)
            return self.repo.history(run_id)

    def _start_stage(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            stage_key: str,
            expected_version: int | None,
            idempotency_key: str | None,
            now_text: str,
        ) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, person_id, write=True)
            with self.conn:
                run = self._run_in_scope(organization_id, workspace_id, run_id)
                stage = self.repo.get_stage_by_key(run_id, stage_key)
                if run["status"] in TERMINAL_STATUSES:
                    raise ValidationError("terminal workflow run cannot start stages")
                self._ensure_transition(stage["status"], "in_progress")
                self._ensure_stage_has_named_owner(run, stage, organization_id, workspace_id)
                self._ensure_dependencies_clear(stage)
                updated = self.repo.update_stage_status(
                    stage["id"],
                    stage["status"],
                    "in_progress",
                    now_text,
                    expected_version or stage["version"],
                    started_at=stage["started_at"] or now_text,
                    blocked_reason=None,
                )
                latest_run = self.repo.get_run(run_id)
                if latest_run["status"] in {"pending", "blocked", "waiting_approval"}:
                    self.repo.update_run_status(
                        run_id,
                        latest_run["status"],
                        "in_progress",
                        now_text,
                        latest_run["version"],
                        started_at=latest_run["started_at"] or now_text,
                        blocked_reason=None,
                    )
                self.repo.record_history(
                    self._history(
                        run_id, stage["id"], person_id, "start_stage", stage["status"], "in_progress", "stage started", {}, idempotency_key, now_text
                    )
                )
            return updated

    def _submit_evidence(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            stage_key: str,
            kind: str,
            uri: str | None,
            text: str | None,
            metadata: dict[str, Any] | None,
            object_type: str | None,
            object_id: str | None,
            locator: str | None,
            content_hash: str | None,
            idempotency_key: str | None,
            now_text: str,
        ) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, person_id, write=True)
            kind = _required_text(kind, "evidence kind")
            if object_type is not None and object_type not in CANONICAL_EVIDENCE_TYPES:
                raise ValidationError("unsupported canonical evidence object type")
            if object_type and not object_id:
                raise ValidationError("canonical evidence object_id is required")
            if not any([uri, text, object_type]):
                raise ValidationError("evidence requires uri, text, or canonical object reference")
            with self.conn:
                self._run_in_scope(organization_id, workspace_id, run_id)
                stage = self.repo.get_stage_by_key(run_id, stage_key)
                if stage["status"] in TERMINAL_STATUSES:
                    raise ValidationError("terminal workflow stage cannot accept evidence")
                evidence = self.repo.add_evidence(
                    {
                        "id": self.new_id("wevidence"),
                        "run_id": run_id,
                        "stage_run_id": stage["id"],
                        "kind": kind,
                        "uri": uri,
                        "text": text,
                        "metadata": metadata or {},
                        "object_type": object_type,
                        "object_id": object_id,
                        "locator": locator,
                        "content_hash": content_hash,
                        "submitted_by_person_id": person_id,
                        "created_at": now_text,
                    }
                )
                self.repo.record_history(
                    self._history(
                        run_id,
                        stage["id"],
                        person_id,
                        "submit_evidence",
                        stage["status"],
                        stage["status"],
                        kind,
                        {"evidence_id": evidence["id"], "object_type": object_type, "object_id": object_id},
                        idempotency_key,
                        now_text,
                    )
                )
            return evidence

    def _request_approval(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            stage_key: str,
            reason: str,
            approval_request_id: str | None,
            expected_version: int | None,
            idempotency_key: str | None,
            now_text: str,
        ) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, person_id, write=True)
            reason = _required_text(reason, "approval reason")
            with self.conn:
                run = self._run_in_scope(organization_id, workspace_id, run_id)
                stage = self.repo.get_stage_by_key(run_id, stage_key)
                if not stage["requires_approval"]:
                    raise ValidationError("workflow stage does not require approval")
                if not approval_request_id:
                    raise ValidationError("workflow gates require a canonical approval_request_id")
                approval_request = self.repo.get_approval_request(approval_request_id)
                if approval_request["organization_id"] != organization_id or approval_request["workspace_id"] != workspace_id:
                    raise NotFoundError("approval request not found")
                if approval_request["status"] != "pending":
                    raise ValidationError("canonical approval request must be pending")
                self._ensure_required_evidence(stage)
                self._ensure_transition(stage["status"], "waiting_approval")
                updated = self.repo.update_stage_status(
                    stage["id"],
                    stage["status"],
                    "waiting_approval",
                    now_text,
                    expected_version or stage["version"],
                )
                latest_run = self.repo.get_run(run_id)
                if latest_run["status"] not in TERMINAL_STATUSES and latest_run["status"] != "waiting_approval":
                    self.repo.update_run_status(
                        run_id, latest_run["status"], "waiting_approval", now_text, latest_run["version"]
                    )
                self.repo.record_history(
                    self._history(
                        run_id,
                        stage["id"],
                        person_id,
                        "request_approval",
                        stage["status"],
                        "waiting_approval",
                        reason,
                        {"approval_request_id": approval_request_id},
                        idempotency_key,
                        now_text,
                    )
                )
            return updated

    def _decide_approval(
            self,
            organization_id: str,
            workspace_id: str | None,
            approver_person_id: str,
            run_id: str,
            stage_key: str,
            decision: str,
            reason: str,
            approval_request_id: str | None,
            idempotency_key: str | None,
            now_text: str,
        ) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, approver_person_id, write=True)
            if decision not in APPROVAL_DECISIONS:
                raise ValidationError("invalid workflow approval decision")
            reason = _required_text(reason, "approval decision reason")
            with self.conn:
                run = self._run_in_scope(organization_id, workspace_id, run_id)
                stage = self.repo.get_stage_by_key(run_id, stage_key)
                if stage["status"] != "waiting_approval":
                    raise ValidationError("workflow stage is not waiting for approval")
                if not approval_request_id:
                    raise ValidationError("workflow gates require a canonical approval_request_id")
                approval_request = self.repo.get_approval_request(approval_request_id)
                if approval_request["organization_id"] != organization_id or approval_request["workspace_id"] != workspace_id:
                    raise NotFoundError("approval request not found")
                if approval_request["approver_person_id"] != approver_person_id:
                    raise AuthorizationError("canonical approval was assigned to another approver")
                expected_approval_status = "approved" if decision == "approve" else "rejected"
                if approval_request["status"] != expected_approval_status:
                    raise ValidationError(f"canonical approval request must already be {expected_approval_status}")
                approval = self.repo.add_approval_decision(
                    {
                        "id": self.new_id("wapproval"),
                        "run_id": run_id,
                        "stage_run_id": stage["id"],
                        "approval_request_id": approval_request_id,
                        "decision": decision,
                        "approver_person_id": approver_person_id,
                        "reason": reason,
                        "created_at": now_text,
                    }
                )
                to_status = "waiting_approval" if decision == "approve" else (
                    "blocked" if stage.get("on_reject_stage_key") else "in_progress"
                )
                if decision != "approve":
                    self.repo.update_stage_status(
                        stage["id"], stage["status"], to_status, now_text, stage["version"],
                        blocked_reason=(
                            f"Rework required in {stage['on_reject_stage_key']}"
                            if stage.get("on_reject_stage_key") else None
                        ),
                    )
                    if stage.get("on_reject_stage_key"):
                        rework = self.repo.get_stage_by_key(run_id, stage["on_reject_stage_key"])
                        if rework["status"] == "completed":
                            self.repo.update_stage_status(
                                rework["id"], "completed", "in_progress", now_text, rework["version"],
                                completed_at=None, blocked_reason=None,
                            )
                            self.repo.record_history(
                                self._history(
                                    run_id, rework["id"], approver_person_id, "reopen_for_rework",
                                    "completed", "in_progress", reason,
                                    {"rejected_stage_run_id": stage["id"]}, idempotency_key, now_text,
                                )
                            )
                    latest_run = self.repo.get_run(run_id)
                    if latest_run["status"] == "waiting_approval":
                        self.repo.update_run_status(run_id, "waiting_approval", "in_progress", now_text, latest_run["version"])
                self.repo.record_history(
                    self._history(
                        run_id,
                        stage["id"],
                        approver_person_id,
                        f"approval_{decision}",
                        stage["status"],
                        to_status,
                        reason,
                        {"approval_decision_id": approval["id"], "approval_request_id": approval_request_id},
                        idempotency_key,
                        now_text,
                    )
                )
            return approval

    def _acknowledge_handoff(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            from_stage_key: str,
            to_stage_key: str,
            artifact_contract: str,
            reason: str,
            idempotency_key: str | None,
            now_text: str,
        ) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, person_id, write=True)
            artifact_contract = _required_text(artifact_contract, "artifact contract")
            with self.conn:
                self._run_in_scope(organization_id, workspace_id, run_id)
                source = self.repo.get_stage_by_key(run_id, from_stage_key)
                target = self.repo.get_stage_by_key(run_id, to_stage_key)
                if source["status"] != "completed":
                    raise ValidationError("handoff source stage must be completed")
                dependency_ids = {item["depends_on_stage_run_id"] for item in self.repo.dependencies_for_stage(target["id"])}
                if source["id"] not in dependency_ids:
                    raise ValidationError("handoff target must depend on source stage")
                acknowledgement = self.repo.add_handoff_ack(
                    {
                        "id": self.new_id("whandoff"),
                        "run_id": run_id,
                        "from_stage_run_id": source["id"],
                        "to_stage_run_id": target["id"],
                        "acknowledged_by_person_id": person_id,
                        "from_wing": source["assignee_wing"],
                        "from_role": source["assignee_role"],
                        "from_person_id": source.get("assignee_person_id"),
                        "from_principal_type": source.get("assignee_principal_type", "person"),
                        "from_principal_id": source.get("assignee_principal_id") or source.get("assignee_person_id"),
                        "source_stage_version": source["version"],
                        "to_wing": target["assignee_wing"],
                        "to_role": target["assignee_role"],
                        "to_person_id": target.get("assignee_person_id"),
                        "to_principal_type": target.get("assignee_principal_type", "person"),
                        "to_principal_id": target.get("assignee_principal_id") or target.get("assignee_person_id"),
                        "artifact_contract": artifact_contract,
                        "reason": reason,
                        "created_at": now_text,
                    }
                )
                self.repo.record_history(
                    self._history(
                        run_id,
                        target["id"],
                        person_id,
                        "acknowledge_handoff",
                        target["status"],
                        target["status"],
                        artifact_contract,
                        {"from_stage_run_id": source["id"], "handoff_acknowledgement_id": acknowledgement["id"]},
                        idempotency_key,
                        now_text,
                    )
                )
            return acknowledgement

    def _complete_stage(
            self,
            organization_id: str,
            workspace_id: str | None,
            person_id: str,
            run_id: str,
            stage_key: str,
            reason: str,
            expected_version: int | None,
            idempotency_key: str | None,
            now_text: str,
        ) -> dict[str, Any]:
            self._authorize(organization_id, workspace_id, person_id, write=True)
            with self.conn:
                self._run_in_scope(organization_id, workspace_id, run_id)
                stage = self.repo.get_stage_by_key(run_id, stage_key)
                if stage["status"] not in {"in_progress", "waiting_approval"}:
                    raise ValidationError("only active workflow stages can be completed")
                self._ensure_required_evidence(stage)
                if stage["requires_approval"]:
                    approval = self.repo.latest_approval_decision(stage["id"])
                    if approval is None or approval["decision"] != "approve":
                        raise ValidationError("workflow stage requires approval before completion")
                updated = self.repo.update_stage_status(
                    stage["id"],
                    stage["status"],
                    "completed",
                    now_text,
                    expected_version or stage["version"],
                    completed_at=now_text,
                    blocked_reason=None,
                )
                self.repo.record_history(
                    self._history(
                        run_id, stage["id"], person_id, "complete_stage", stage["status"], "completed", reason, {}, idempotency_key, now_text
                    )
                )
                stages = self.repo.list_stages(run_id)
                latest_run = self.repo.get_run(run_id)
                if all(item["status"] == "completed" for item in stages):
                    if latest_run["status"] != "completed":
                        self.repo.update_run_status(
                            run_id,
                            latest_run["status"],
                            "completed",
                            now_text,
                            latest_run["version"],
                            completed_at=now_text,
                        )
                        self.repo.record_history(
                            self._history(
                                run_id, None, person_id, "complete_run", latest_run["status"], "completed", "all stages completed", {}, None, now_text
                            )
                        )
                elif latest_run["status"] in {"waiting_approval", "blocked"}:
                    self.repo.update_run_status(
                        run_id, latest_run["status"], "in_progress", now_text, latest_run["version"], blocked_reason=None
                    )
            return updated
