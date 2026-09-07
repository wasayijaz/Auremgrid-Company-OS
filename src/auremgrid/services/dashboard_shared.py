from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity


_TERMINAL = {"completed", "cancelled", "failed"}


class DashboardSharedMixin:
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

def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

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
    return str(value) if value in {"healthy", "configured", "building", "degraded", "unavailable"} else "unavailable"

def _safe_scalar(value: Any) -> str | None:
    return str(value)[:120] if isinstance(value, (str, int, float)) else None

def _action(
    action: str, route: str, payload: dict[str,Any], required_fields: list[str] | None = None,
) -> dict[str,Any]:
    return {
        "action":action,"method":"POST","route":route,"payload":payload,
        "required_fields":required_fields or [],
    }

def _refs_visible(
    refs: dict[str,Any], sources: set[str], documents: set[str], facts: set[str], relations: set[str],
) -> bool:
    allowed={"sources":sources,"documents":documents,"facts":facts,"relations":relations}
    for kind,values in refs.items():
        if kind not in allowed or not isinstance(values,list):
            return False
        if not {str(value) for value in values}.issubset(allowed[kind]):
            return False
    return True
