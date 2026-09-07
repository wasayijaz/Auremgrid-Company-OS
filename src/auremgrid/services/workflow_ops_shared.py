from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import role_capabilities
from auremgrid.storage.workflows import TERMINAL_STATUSES, WorkflowRepository

STATUSES = {"pending", "in_progress", "waiting_approval", "blocked", "completed", "cancelled"}
CAN_TRANSITION = {"pending": {"in_progress", "blocked", "cancelled"}, "in_progress": {"waiting_approval", "blocked", "completed", "cancelled"}, "waiting_approval": {"in_progress", "blocked", "completed", "cancelled"}, "blocked": {"in_progress", "cancelled"}, "completed": set(), "cancelled": set()}
APPROVAL_DECISIONS = {"approve", "reject", "request_changes"}
CANONICAL_EVIDENCE_TYPES = {"deliverable", "review", "decision", "source", "document", "signal"}

def _now() -> datetime: return datetime.now(timezone.utc).replace(microsecond=0)
def _iso(value: datetime | str | None) -> str | None:
    if value is None: return None
    if isinstance(value, datetime): return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return str(value)
def _obj(value: Any) -> dict[str, Any]:
    if value is None: return {}
    if isinstance(value, dict): return value
    if hasattr(value, "to_dict") and callable(value.to_dict): return value.to_dict()
    if is_dataclass(value): return asdict(value)
    if hasattr(value, "__dict__"): return dict(value.__dict__)
    raise ValidationError("workflow template must be dict-like or dataclass-like")
def _required_text(value: Any, label: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text: raise ValidationError(f"{label} is required")
    return text

class WorkflowOpsSharedMixin:
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
                if dependency["handoff_to_wing"] or dependency["handoff_to_role"] or dependency["handoff_to_person_id"] or dependency.get("handoff_principal_id"):
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

__all__ = [
    "WorkflowOpsSharedMixin", "_now", "_iso", "_obj", "_required_text", "json", "asdict",
    "is_dataclass", "datetime", "timedelta", "timezone", "Any", "Callable", "AuthorizationError",
    "NotFoundError", "ValidationError", "role_capabilities", "TERMINAL_STATUSES", "WorkflowRepository",
    "STATUSES", "CAN_TRANSITION", "APPROVAL_DECISIONS", "CANONICAL_EVIDENCE_TYPES",
]
