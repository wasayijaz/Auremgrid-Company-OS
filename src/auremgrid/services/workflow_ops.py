from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import role_capabilities
from auremgrid.storage.workflows import TERMINAL_STATUSES, WorkflowRepository


STATUSES = {"pending", "in_progress", "waiting_approval", "blocked", "completed", "cancelled"}
CAN_TRANSITION = {
    "pending": {"in_progress", "blocked", "cancelled"},
    "in_progress": {"waiting_approval", "blocked", "completed", "cancelled"},
    "waiting_approval": {"in_progress", "blocked", "completed", "cancelled"},
    "blocked": {"in_progress", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
APPROVAL_DECISIONS = {"approve", "reject", "request_changes"}
CANONICAL_EVIDENCE_TYPES = {"deliverable", "review", "decision", "source", "document", "signal"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return str(value)


def _obj(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise ValidationError("workflow template must be dict-like or dataclass-like")


def _required_text(value: Any, label: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValidationError(f"{label} is required")
    return text


class WorkflowOperations:
    def __init__(
        self,
        conn: Any,
        new_id: Callable[[str], str],
        authorize: Callable[..., Any] | None = None,
    ) -> None:
        self.conn = conn
        self.new_id = new_id
        self.authorize = authorize
        self.repo = WorkflowRepository(conn, new_id)

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
                    "required_evidence": stage["required_evidence"],
                    "requires_approval": stage["requires_approval"],
                    "handoff_to_wing": stage["handoff_to_wing"],
                    "handoff_to_role": stage["handoff_to_role"],
                    "handoff_to_person_id": stage["handoff_to_person_id"],
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
                    "from_person_id": source["assignee_person_id"],
                    "source_stage_version": source["version"],
                    "to_wing": target["assignee_wing"],
                    "to_role": target["assignee_role"],
                    "to_person_id": target["assignee_person_id"],
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

    def _idempotent_transition(
        self,
        organization_id: str,
        idempotency_key: str | None,
        operation: str,
        callback: Callable[[str], dict[str, Any]],
    ) -> dict[str, Any]:
        if idempotency_key:
            cached = self.repo.get_idempotency(organization_id, idempotency_key, operation)
            if cached is not None:
                return cached["response"]
        now_text = _now().isoformat()
        with self.conn:
            response = callback(now_text)
            if idempotency_key:
                result_id = str(response.get("id") or response.get("run_id") or "")
                self.repo.save_idempotency(organization_id, idempotency_key, operation, "workflow_transition", result_id, response, now_text)
        return response

    def _run_in_scope(self, organization_id: str, workspace_id: str | None, run_id: str) -> dict[str, Any]:
        run = self.repo.get_run(run_id)
        if run["organization_id"] != organization_id or run["workspace_id"] != workspace_id:
            raise NotFoundError("workflow run not found")
        return run

    def _authorize(self, organization_id: str, workspace_id: str | None, person_id: str, write: bool) -> None:
        if self.authorize is None:
            return
        if workspace_id is None:
            try:
                self.authorize(organization_id, workspace_id, person_id, write=write)
            except TypeError:
                if write:
                    raise AuthorizationError("workspace-scoped authorization is required for workflow writes")
            return
        self.authorize(organization_id, workspace_id, person_id, write=write)

    def _ensure_transition(self, from_status: str, to_status: str) -> None:
        if from_status not in STATUSES or to_status not in STATUSES or to_status not in CAN_TRANSITION[from_status]:
            raise ValidationError(f"cannot move workflow from {from_status} to {to_status}")

    def _ensure_dependencies_clear(self, stage: dict[str, Any]) -> None:
        for dependency in self.repo.dependencies_for_stage(stage["id"]):
            if dependency["dependency_status"] != "completed":
                raise ValidationError("workflow stage dependencies are not complete")
            if dependency["handoff_to_wing"] or dependency["handoff_to_role"] or dependency["handoff_to_person_id"]:
                if not self.repo.has_handoff_ack(dependency["depends_on_stage_run_id"], stage["id"]):
                    raise ValidationError("workflow stage requires handoff acknowledgement")

    def _ensure_required_evidence(self, stage: dict[str, Any]) -> None:
        required = [str(item) for item in stage["required_evidence"]]
        if not required:
            return
        submitted = {item["kind"] for item in self.repo.list_evidence(stage["id"])}
        missing = [item for item in required if item not in submitted]
        if missing:
            raise ValidationError(f"workflow stage is missing required evidence: {', '.join(missing)}")

    def _normalize_template(self, template: Any) -> dict[str, Any]:
        raw = _obj(template)
        key = _required_text(raw.get("key") or raw.get("id") or raw.get("slug"), "workflow key")
        name = _required_text(raw.get("name") or raw.get("title"), "workflow name")
        version = _required_text(raw.get("version") or "1", "workflow version")
        raw_stages = raw.get("stages") or raw.get("steps")
        if not isinstance(raw_stages, (list, tuple)) or not raw_stages:
            raise ValidationError("workflow template requires at least one stage")

        stages: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw_stage in enumerate(raw_stages):
            stage = _obj(raw_stage)
            assignee = _obj(stage.get("assignee"))
            handoff_value = stage.get("handoff_to")
            handoff = _obj(handoff_value) if isinstance(handoff_value, dict) or hasattr(handoff_value, "to_dict") or is_dataclass(handoff_value) else {}
            handoff_target = stage.get("handoff_target")
            # Legacy handoff_target is opaque free text. Keep it for existing
            # behavior, but do not use it to infer a roster assignee.
            structured_handoff_wing = handoff.get("wing") or stage.get("handoff_to_wing")
            structured_handoff_role = handoff.get("role") or stage.get("handoff_to_role")
            stage_key = _required_text(stage.get("key") or stage.get("id") or stage.get("slug"), "stage key")
            if stage_key in seen:
                raise ValidationError("workflow stage keys must be unique")
            seen.add(stage_key)
            required_evidence = stage.get("required_evidence") or stage.get("evidence_required") or []
            if not isinstance(required_evidence, (list, tuple)):
                raise ValidationError("required evidence must be a list")
            normalized_evidence = [_required_text(item, "required evidence kind") for item in required_evidence]
            dependencies = stage.get("depends_on") or stage.get("dependencies") or stage.get("after") or []
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            if not isinstance(dependencies, (list, tuple)):
                raise ValidationError("stage dependencies must be a list")
            handoff_contract = str(
                stage.get("handoff_contract")
                or stage.get("artifact_contract")
                or handoff.get("artifact_contract")
                or stage.get("completion_outcome")
                or ", ".join(normalized_evidence)
                or ""
            )
            handoff_to_wing = handoff.get("wing") or stage.get("handoff_to_wing") or handoff_target
            handoff_to_role = handoff.get("role") or stage.get("handoff_to_role")
            handoff_to_person_id = handoff.get("person_id") or handoff.get("person") or stage.get("handoff_to_person_id")
            if (handoff_to_wing or handoff_to_role or handoff_to_person_id) and not handoff_contract.strip():
                raise ValidationError("handoff stages require an artifact contract")
            on_reject_stage_key = stage.get("on_reject_stage_key") or stage.get("on_reject_stage_id")
            approval_gate = stage.get("approval_gate")
            requires_approval = stage.get("requires_approval", stage.get("approval_required", False))
            if approval_gate is not None:
                requires_approval = approval_gate != "none"
            sla_hours = stage.get("sla_hours")
            if sla_hours is not None and (not isinstance(sla_hours, (int, float)) or isinstance(sla_hours, bool) or sla_hours <= 0):
                raise ValidationError("stage sla_hours must be positive")
            expected_duration_hours = stage.get("expected_duration_hours")
            if expected_duration_hours is not None and (
                not isinstance(expected_duration_hours, (int, float))
                or isinstance(expected_duration_hours, bool)
                or expected_duration_hours <= 0
            ):
                raise ValidationError("stage expected_duration_hours must be positive")
            stages.append(
                {
                    "key": stage_key,
                    "name": _required_text(stage.get("name") or stage.get("title"), "stage name"),
                    "sequence": int(stage.get("sequence", stage.get("order", index + 1))),
                    "assignee_wing": _required_text(
                        stage.get("assignee_wing") or stage.get("wing") or stage.get("owner_wing") or assignee.get("wing"),
                        "stage assignee wing",
                    ),
                    "assignee_role": _required_text(
                        stage.get("assignee_role") or stage.get("role") or stage.get("owner_role") or assignee.get("role"),
                        "stage assignee role",
                    ),
                    "assignee_person_id": stage.get("assignee_person_id") or assignee.get("person_id") or assignee.get("person"),
                    "required_evidence": normalized_evidence,
                    "requires_approval": bool(requires_approval),
                    "dependencies": [_required_text(item, "dependency stage key") for item in dependencies],
                    "handoff_to_wing": handoff_to_wing,
                    "handoff_to_role": handoff_to_role,
                    "handoff_to_person_id": handoff_to_person_id,
                    "handoff_structured": bool(structured_handoff_wing and structured_handoff_role),
                    "handoff_contract": handoff_contract,
                    "on_reject_stage_key": on_reject_stage_key,
                    "due_at": _iso(stage.get("due_at") or stage.get("deadline")),
                    "sla_hours": sla_hours,
                    "expected_duration_hours": expected_duration_hours,
                }
            )
        stages.sort(key=lambda item: (item["sequence"], item["key"]))
        self._validate_dependencies(stages)
        edges = [
            {"from": dependency, "to": stage["key"], "kind": "depends_on"}
            for stage in stages
            for dependency in stage["dependencies"]
        ]
        return {"key": key, "name": name, "version": version, "stages": stages, "edges": edges}

    def _active_client_roster(
        self, organization_id: str, workspace_id: str | None, as_of: str
    ) -> dict[str, Any] | None:
        """Return the latest effective roster for a client workspace, if any."""
        if not workspace_id:
            return None
        row = self.conn.execute(
            """
            SELECT * FROM client_account_rosters
            WHERE organization_id=? AND workspace_id=? AND effective_at<=?
            ORDER BY effective_at DESC, created_at DESC, id DESC LIMIT 1
            """,
            (organization_id, workspace_id, as_of),
        ).fetchone()
        if row is None:
            return None
        row_dict = dict(row)
        version = row_dict["version"]
        roles = [
            dict(item)
            for item in self.conn.execute(
                """
                SELECT id, roster_id, organization_id, workspace_id, role_key, wing, person_id
                FROM client_account_roster_roles
                WHERE roster_id=? AND organization_id=? AND workspace_id=?
                ORDER BY role_key, wing, id
                """,
                (row_dict["id"], organization_id, workspace_id),
            ).fetchall()
        ]
        return {
            "id": row_dict["id"],
            "organization_id": row_dict["organization_id"],
            "workspace_id": row_dict["workspace_id"],
            "version": version,
            "roles": roles,
        }

    @staticmethod
    def _roster_role_key(label: Any) -> str:
        text = "" if label is None else str(label).strip().casefold()
        if "account" in text:
            return "account_lead" if "lead" in text else "account_executive"
        if "lead" in text:
            return "wing_lead"
        return "wing_executive"

    @staticmethod
    def _wing_key(value: Any) -> str:
        return "" if value is None else str(value).strip().casefold()

    def _matching_roster_rows(
        self, roster: dict[str, Any], role_label: Any, wing: Any
    ) -> list[dict[str, Any]]:
        role_key = self._roster_role_key(role_label)
        rows = [row for row in roster["roles"] if row.get("role_key") == role_key]
        # Account roles are account-wide and intentionally have no wing.
        if role_key not in {"account_lead", "account_executive"}:
            wing_key = self._wing_key(wing)
            rows = [row for row in rows if self._wing_key(row.get("wing")) == wing_key]
        return rows

    def _require_eligible_owner(self, organization_id: str, workspace_id: str | None, person_id: Any, label: str) -> str:
        resolved_person_id = _required_text(person_id, label)
        if workspace_id is None:
            raise ValidationError(f"{label} requires a workspace-scoped active roster owner")
        row = self.conn.execute(
            """SELECT om.role AS organization_role, wm.role AS workspace_role
               FROM people p
               JOIN organization_memberships om
                 ON om.person_id=p.id AND om.organization_id=p.organization_id
               JOIN workspace_memberships wm ON wm.person_id=p.id
              WHERE p.organization_id=? AND p.id=? AND p.status='active' AND wm.workspace_id=?""",
            (organization_id, resolved_person_id, workspace_id),
        ).fetchone()
        if row is None:
            raise ValidationError(f"{label} must be an active workspace member")
        capabilities = role_capabilities(str(row["organization_role"]), str(row["workspace_role"]))
        if "workflow_run" not in capabilities:
            raise ValidationError(f"{label} must have workflow_run capability")
        return resolved_person_id

    def _ensure_stage_has_named_owner(
        self, run: dict[str, Any], stage: dict[str, Any], organization_id: str, workspace_id: str | None
    ) -> None:
        snapshot = run.get("template_snapshot") or {}
        if not snapshot.get("client_roster_id"):
            raise ValidationError("workflow stage cannot start without an active client roster owner")
        self._require_eligible_owner(
            organization_id,
            workspace_id,
            stage.get("assignee_person_id"),
            f"stage {stage['stage_key']} owner",
        )

    def _resolve_roster_assignments(self, snapshot: dict[str, Any], roster: dict[str, Any]) -> None:
        """Resolve missing stage/handoff people against one immutable roster.

        Every failure occurs before definition/run persistence, preserving the
        all-or-nothing create_run contract.
        """
        for stage in snapshot["stages"]:
            matches = self._matching_roster_rows(roster, stage["assignee_role"], stage["assignee_wing"])
            explicit = stage.get("assignee_person_id")
            if explicit:
                if len(matches) != 1 or matches[0]["person_id"] != explicit:
                    raise ValidationError(
                        f"explicit assignee for stage {stage['key']} does not match active client roster"
                    )
            else:
                if len(matches) != 1:
                    raise ValidationError(
                        f"active client roster has {len(matches)} matches for stage {stage['key']}"
                    )
                stage["assignee_person_id"] = matches[0]["person_id"]
            stage["assignee_person_id"] = self._require_eligible_owner(
                roster["organization_id"],
                roster["workspace_id"],
                stage["assignee_person_id"],
                f"stage {stage['key']} owner",
            )

            # Only structured handoffs can be roster-resolved. Opaque legacy
            # handoff_target values remain untouched and never drive guessing.
            if stage.get("handoff_structured") and not stage.get("handoff_to_person_id"):
                handoff_matches = self._matching_roster_rows(
                    roster, stage.get("handoff_to_role"), stage.get("handoff_to_wing")
                )
                if len(handoff_matches) != 1:
                    raise ValidationError(
                        f"active client roster has {len(handoff_matches)} matches for handoff from stage {stage['key']}"
                    )
                stage["handoff_to_person_id"] = handoff_matches[0]["person_id"]
            elif stage.get("handoff_structured") and stage.get("handoff_to_person_id"):
                handoff_matches = self._matching_roster_rows(
                    roster, stage.get("handoff_to_role"), stage.get("handoff_to_wing")
                )
                if len(handoff_matches) != 1 or handoff_matches[0]["person_id"] != stage["handoff_to_person_id"]:
                    raise ValidationError(
                        f"explicit handoff person from stage {stage['key']} does not match active client roster"
                    )
            if stage.get("handoff_structured"):
                stage["handoff_to_person_id"] = self._require_eligible_owner(
                    roster["organization_id"],
                    roster["workspace_id"],
                    stage["handoff_to_person_id"],
                    f"handoff owner from stage {stage['key']}",
                )

    def _validate_dependencies(self, stages: list[dict[str, Any]]) -> None:
        stage_keys = {stage["key"] for stage in stages}
        by_sequence = {stage["key"]: stage["sequence"] for stage in stages}
        for stage in stages:
            unknown = [item for item in stage["dependencies"] if item not in stage_keys]
            if unknown:
                raise ValidationError(f"unknown workflow stage dependency: {', '.join(unknown)}")
            target = stage.get("on_reject_stage_key")
            if target is not None:
                if target not in stage_keys:
                    raise ValidationError(f"unknown workflow rejection target: {target}")
                if by_sequence[target] >= stage["sequence"]:
                    raise ValidationError("workflow rejection targets must be earlier stages")
        visiting: set[str] = set()
        visited: set[str] = set()
        by_key = {stage["key"]: stage for stage in stages}

        def visit(key: str) -> None:
            if key in visited:
                return
            if key in visiting:
                raise ValidationError("workflow stage dependencies cannot form a cycle")
            visiting.add(key)
            for dependency in by_key[key]["dependencies"]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in stage_keys:
            visit(key)

    def _escalation_at(self, now: datetime, due_at: datetime | str | None, sla_minutes: int | None) -> str | None:
        if sla_minutes is not None:
            if sla_minutes <= 0:
                raise ValidationError("sla_minutes must be positive")
            return (now + timedelta(minutes=sla_minutes)).isoformat()
        return _iso(due_at)

    def _stage_due_at(self, now: datetime, stage: dict[str, Any]) -> str | None:
        if stage["due_at"] is not None:
            return stage["due_at"]
        if stage.get("sla_hours") is None:
            return None
        return (now + timedelta(hours=float(stage["sla_hours"]))).isoformat()

    def _history(
        self,
        run_id: str,
        stage_run_id: str | None,
        actor_person_id: str,
        action: str,
        from_status: str | None,
        to_status: str | None,
        reason: str,
        metadata: dict[str, Any],
        idempotency_key: str | None,
        now_text: str,
    ) -> dict[str, Any]:
        return {
            "id": self.new_id("whistory"),
            "run_id": run_id,
            "stage_run_id": stage_run_id,
            "actor_person_id": actor_person_id,
            "action": action,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "metadata": metadata,
            "idempotency_key": idempotency_key,
            "created_at": now_text,
        }
