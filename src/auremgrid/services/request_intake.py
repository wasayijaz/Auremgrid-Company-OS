"""Scoped client-request intake pipeline composed from existing primitives."""
from __future__ import annotations

import json
from typing import Any, Mapping

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


class RequestIntakeService:
    """Connect a request, signal, workflow handoff, owner, and work item."""

    def __init__(self, company_os: Any) -> None:
        self.os = company_os
        self._metadata_by_intake: dict[str, dict[str, Any]] = {}

    def create_request(
        self, os_scope: Any, request_type: str, goal: str, deadline: str,
        *, attachments: list[str] | None = None, references: list[str] | None = None,
        priority: str = "normal",
    ) -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope)
        self._authorize(organization_id, workspace_id, person_id)
        request_type = str(request_type or "").strip()
        goal = str(goal or "").strip()
        deadline = str(deadline or "").strip()
        if not request_type:
            raise ValidationError("request type is required")
        if not goal:
            raise ValidationError("request goal is required")
        if not deadline:
            raise ValidationError("request deadline is required")
        if priority not in {"low", "normal", "high", "urgent"}:
            raise ValidationError("invalid priority")
        metadata = {
            "request_type": request_type, "goal": goal, "deadline": deadline,
            "attachments": [str(item) for item in (attachments or [])],
            "references": [str(item) for item in (references or [])], "priority": priority,
        }
        intake = self.os.client_portal.submit_intake_request(
            organization_id, workspace_id, person_id, request_type, goal, deadline,
        )
        self._metadata_by_intake[intake["id"]] = metadata
        return {"intake": intake, "metadata": metadata, "status": "requested", "signal": None, "work": None, "workflow": None, "owner": None}

    def create_signal(self, os_scope: Any, intake_id: str) -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope)
        self._authorize(organization_id, workspace_id, person_id, write=True)
        row = self._intake(organization_id, workspace_id, intake_id)
        metadata = self._metadata_by_intake.get(intake_id, self._metadata(row))
        signal = self.os.client_ops.create_signal(
            organization_id, workspace_id, person_id, "request", "client_intake", json.dumps({"request": row["request"], "metadata": metadata}, sort_keys=True), intake_id, 1.0,
        )
        self.os.client_ops.route_signal(organization_id, workspace_id, person_id, signal.id, "work")
        return {"signal": signal.to_dict(), "status": "signalled"}

    def create_work(self, os_scope: Any, intake_id: str) -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope)
        self._authorize(organization_id, workspace_id, person_id, write=True)
        row = self._intake(organization_id, workspace_id, intake_id)
        signal = self._signal(organization_id, workspace_id, intake_id)
        if signal is None:
            raise ValidationError("signal must be created before work")
        metadata = json.loads(signal["evidence"])["metadata"]
        item = self.os.work_ops.create(
            organization_id, workspace_id, person_id, row["title"], row["request"], row["submitted_by_person_id"],
            priority=metadata["priority"], tags=["client_request", intake_id], deadline=row["needed_by"],
            brain_context=json.dumps(metadata, sort_keys=True),
        )
        return {"work": item.to_dict(), "workflow": {"status": "linked", "work_item_id": item.id}, "status": "work_created"}

    def assign_owner(self, os_scope: Any, work_item_id: str, owner_person_id: str) -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope)
        self._authorize(organization_id, workspace_id, person_id, write=True)
        self._authorize(organization_id, workspace_id, owner_person_id)
        item = self.os.work_ops.assign(organization_id, workspace_id, person_id, work_item_id, owner_person_id)
        return {"owner": owner_person_id, "work": item.to_dict(), "status": "owned"}

    def transition(self, os_scope: Any, work_item_id: str, status: str, reason: str = "") -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope)
        self._authorize(organization_id, workspace_id, person_id, write=True)
        result = self.os.work_ops.transition(organization_id, workspace_id, person_id, work_item_id, status, reason)
        return {"work": result["work_item"], "status": result["work_item"]["status"]}

    def pipeline_status(self, os_scope: Any, intake_id: str) -> dict[str, Any]:
        organization_id, workspace_id, person_id = self._scope(os_scope)
        self._authorize(organization_id, workspace_id, person_id)
        row = self._intake(organization_id, workspace_id, intake_id)
        signal = self._signal(organization_id, workspace_id, intake_id)
        work_id = self._work_id(workspace_id, intake_id)
        work = self.os.store.get_work_item(workspace_id, work_id) if work_id else None
        return {
            "intake_id": intake_id, "request": "requested" if row else None,
            "signal": dict(signal) if signal else None, "work": work.to_dict() if work else None,
            "workflow": {"status": "linked", "work_item_id": work.id} if work else {"status": "not_started"},
            "status": work.status if work else ("signalled" if signal else "requested"),
        }

    def _intake(self, organization_id: str, workspace_id: str, intake_id: str) -> Any:
        row = self.os.store.conn.execute(
            "SELECT * FROM client_intake_requests WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, intake_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("intake request not found")
        return row

    def _signal(self, organization_id: str, workspace_id: str, intake_id: str) -> Any:
        return self.os.store.conn.execute(
            "SELECT * FROM signals WHERE organization_id=? AND workspace_id=? AND source_id=? ORDER BY created_at DESC LIMIT 1",
            (organization_id, workspace_id, intake_id),
        ).fetchone()

    def _work_id(self, workspace_id: str, intake_id: str) -> str | None:
        row = self.os.store.conn.execute(
            "SELECT id FROM work_items WHERE workspace_id=? AND ? IN (SELECT value FROM json_each(tags)) ORDER BY created_at DESC LIMIT 1",
            (workspace_id, intake_id),
        ).fetchone()
        return str(row["id"]) if row else None

    @staticmethod
    def _metadata(row: Any) -> dict[str, Any]:
        return {"request_type": row["title"], "goal": row["request"], "deadline": row["needed_by"], "priority": "normal", "attachments": [], "references": []}

    def _authorize(self, organization_id: str, workspace_id: str, person_id: str, write: bool = False) -> None:
        if hasattr(self.os, "_require_person_access"):
            self.os._require_person_access(organization_id, workspace_id, person_id, write=write)
        elif self.os.company.org_membership(organization_id, person_id) is None:
            raise AuthorizationError("person is not an organization member")

    @staticmethod
    def _scope(os_scope: Any) -> tuple[str, str, str]:
        def value(key: str) -> Any:
            return os_scope.get(key) if isinstance(os_scope, Mapping) else getattr(os_scope, key, None)
        organization_id, workspace_id, person_id = (str(value(key) or "").strip() for key in ("organization_id", "workspace_id", "person_id"))
        if not organization_id or not workspace_id or not person_id:
            raise ValidationError("organization, workspace, and person are required")
        return organization_id, workspace_id, person_id


RequestIntake = RequestIntakeService
