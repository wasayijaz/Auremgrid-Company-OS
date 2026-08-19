from __future__ import annotations

from datetime import datetime
from typing import Any

from auremgrid.domain.errors import AuremgridError
from auremgrid.services.brain import CompanyOS


class McpToolRouter:
    """Protocol-neutral handlers that can sit behind MCP or any other agent transport."""

    def __init__(self, os: CompanyOS) -> None:
        self.os = os

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {"name": "search", "description": "Retrieve citation-backed evidence for a workspace query."},
            {"name": "entity", "description": "Return facts and relations for one entity."},
            {"name": "history", "description": "Return temporal versions of a subject/predicate claim."},
            {"name": "neighbors", "description": "Return graph neighbors for an entity."},
            {"name": "sources", "description": "List sources visible to the actor."},
            {"name": "recent", "description": "List recently ingested documents visible to the actor."},
            {"name": "remember", "description": "Store an actor-scoped preference or interaction note."},
            {"name": "brief", "description": "Assemble the client brief: brain, playbooks, open work, and last touchpoint."},
            {"name": "engines", "description": "Show what each open-source engine contributed for a query."},
            {"name": "work", "description": "List open or all work items in a workspace."},
            {"name": "capture_work", "description": "Capture a new request at the front door of the work loop."},
            {"name": "assign_work", "description": "Assign a captured work item to an actor in the same workspace."},
            {"name": "start_work", "description": "Move an assigned work item into production."},
            {"name": "mark_dod", "description": "Update definition-of-done checks for a work item."},
            {"name": "submit_review", "description": "Submit a complete work item for internal review."},
            {"name": "close_review", "description": "Approve or return an item from internal review."},
            {"name": "ship_work", "description": "Ship an item after client review is complete."},
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(arguments, dict):
                raise AuremgridError("arguments must be an object")
            workspace_id = _required(arguments, "workspace_id")
            actor_id = _required(arguments, "actor_id")
            if name == "search":
                as_of = _optional_dt(arguments.get("as_of"))
                return self.os.search(
                    workspace_id,
                    actor_id,
                    _required(arguments, "query"),
                    as_of=as_of,
                    limit=int(arguments.get("limit", 8)),
                ).to_dict()
            if name == "entity":
                return self.os.entity(
                    workspace_id,
                    actor_id,
                    _required(arguments, "name"),
                    as_of=_optional_dt(arguments.get("as_of")),
                )
            if name == "history":
                return self.os.history(
                    workspace_id,
                    actor_id,
                    _required(arguments, "subject"),
                    predicate=arguments.get("predicate"),
                )
            if name == "neighbors":
                return self.os.neighbors(
                    workspace_id,
                    actor_id,
                    _required(arguments, "entity"),
                    as_of=_optional_dt(arguments.get("as_of")),
                )
            if name == "sources":
                return self.os.sources(workspace_id, actor_id)
            if name == "recent":
                return self.os.recent(workspace_id, actor_id, limit=int(arguments.get("limit", 5)))
            if name == "remember":
                memory = self.os.remember(
                    workspace_id,
                    actor_id,
                    _required(arguments, "content"),
                    kind=str(arguments.get("kind", "preference")),
                )
                return memory.to_dict()
            if name == "brief":
                return self.os.account_brief(
                    workspace_id,
                    actor_id,
                    query=arguments.get("query"),
                ).to_dict()
            if name == "work":
                items = self.os.list_work(
                    workspace_id,
                    actor_id,
                    open_only=bool(arguments.get("open_only", True)),
                )
                return {"work": [item.to_dict() for item in items]}
            if name == "engines":
                return self.os.engine_status(
                    workspace_id,
                    actor_id,
                    _required(arguments, "query"),
                )
            if name == "capture_work":
                return self.os.capture_work(
                    workspace_id,
                    actor_id,
                    _required(arguments, "title"),
                    _required(arguments, "request"),
                    _required(arguments, "requested_by"),
                    needed_by=_optional_str(arguments.get("needed_by")),
                    playbook_id=_optional_str(arguments.get("playbook_id")),
                    decision_maker=_optional_str(arguments.get("decision_maker")),
                ).to_dict()
            if name == "assign_work":
                return self.os.assign_work(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                    _required(arguments, "assignee_id"),
                    decision_maker=_optional_str(arguments.get("decision_maker")),
                ).to_dict()
            if name == "start_work":
                return self.os.start_work(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                ).to_dict()
            if name == "mark_dod":
                checks = arguments.get("checks")
                if not isinstance(checks, dict):
                    raise AuremgridError("checks must be an object")
                return self.os.mark_dod(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                    {str(key): _bool(value, f"checks.{key}") for key, value in checks.items()},
                ).to_dict()
            if name == "submit_review":
                return self.os.submit_review(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                ).to_dict()
            if name == "close_review":
                return self.os.close_review(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                    _bool(arguments.get("approved"), "approved"),
                    note=str(arguments.get("note", "")),
                ).to_dict()
            if name == "ship_work":
                return self.os.ship_work(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                    note=str(arguments.get("note", "")),
                ).to_dict()
            raise AuremgridError(f"unknown tool: {name}")
        except (AuremgridError, ValueError, TypeError) as exc:
            return {"error": exc.__class__.__name__, "message": str(exc)}


def _required(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not value:
        raise AuremgridError(f"{key} is required")
    return str(value)


def _optional_dt(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise AuremgridError(f"{key} must be a boolean")
