from __future__ import annotations

from typing import Any

from auremgrid.domain.errors import NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS


def _provider_import_adapter(provider: str, transport: Any | None) -> Any | None:
    if transport is None:
        return None
    from auremgrid.connectors.financial import (
        CRMReadOnlyAdapter,
        GoogleAdsReadOnlyAdapter,
        MetaAdsReadOnlyAdapter,
        StripeReadOnlyAdapter,
    )

    return {
        "crm": CRMReadOnlyAdapter(transport),
        "google_ads": GoogleAdsReadOnlyAdapter(transport),
        "meta_ads": MetaAdsReadOnlyAdapter(transport),
        "stripe_accounting": StripeReadOnlyAdapter(transport),
    }.get(provider)


def _route_capability(path: str, method: str) -> str:
    if method == "GET":
        if path.startswith("/sales/") or path in {"/campaigns/budget-pacing","/client-hq/retainer","/report-packs"}: return "workspace_read"
        if path.startswith("/jobs"): return "job_manage"
        if path == "/client-portal/intake/queue": return "people_manage"
        if path in {"/client-portal/reports", "/client-portal/reports/view", "/client-portal/reports/download"}: return "client_portal"
        if path == "/auth/me": return "workspace_read"
        if path in {"/auth/invites", "/auth/sessions"}: return "auth_manage"
        if path == "/reports": return "workspace_read"
        if path == "/finance": return "finance_read"
        if path == "/capacity": return "workspace_read"
        if path in {"/agents", "/agents/detail"} or path.startswith("/agents/runs"): return "agent_run"
        if path in {"/integrations"}: return "integration_configure"
        if path == "/webhooks/provider/status": return "integration_sync"
        if path == "/connectors/catalog": return "workspace_read"
        if path in {"/assets", "/assets/detail", "/assets/backups", "/asset-registry", "/asset-registry/detail"}: return "workspace_read"
        if path == "/operator/health": return "workspace_read"
        if path in {"/operator/pause", "/operator/resume"}: return "job_manage"
        if path == "/onboarding/templates" or path.startswith("/onboarding/imports"): return "workspace_read"
        if path.startswith("/oauth/install/") and path.endswith("/health"): return "integration_sync"
        if path.startswith("/workflows"): return "workspace_read"
        if path == "/entity/candidates": return "brain_propose"
        if path in {"/knowledge-health", "/memory-proposals", "/search", "/entity", "/history", "/neighbors", "/sources", "/recent", "/brief"}: return "brain_read"
        if path == "/dashboard/brain" or path.startswith("/dashboard/intelligence"): return "brain_read"
        if path == "/dashboard/settings": return "workspace_read"
        if path == "/brain/customizations/active": return "brain_read"
        if path == "/reviews/annotations": return "workspace_read"
        return "workspace_read"
    if path in {"/approvals/decide", "/workflows/approvals/decide"}: return "approval_decide"
    if path.startswith("/sales/") or path.startswith("/report-packs"):
        return "workspace_write"
    if path.startswith("/jobs"): return "job_manage"
    if path in {"/auth/sessions/rotate", "/auth/revoke"}: return "workspace_read"
    if path in {"/dashboard/intelligence/refresh", "/dashboard/intelligence/orchestrator/run"}: return "brain_read"
    if path in {"/dashboard/intelligence/hypotheses", "/dashboard/intelligence/recommendations", "/dashboard/intelligence/recommendations/handoff", "/dashboard/intelligence/evaluation/start"}:
        return "brain_propose"
    if path in {"/dashboard/intelligence/recommendations/lifecycle", "/dashboard/intelligence/evaluation/complete"}:
        return "brain_promote"
    if path.startswith("/auth/"): return "auth_manage"
    if path.startswith("/workflows/stages") or path == "/workflows/evidence": return "workflow_run"
    if path.startswith("/workflows/approvals/request") or path.startswith("/workflows/handoffs"): return "workflow_gate"
    if path.startswith("/workflows/approvals/decide"): return "approval_decide"
    if path.startswith("/workflows"): return "workflow_run"
    if path == "/integrations/credentials": return "secret_bind"
    if path == "/connectors/catalog": return "workspace_read"
    if path == "/provider-imports/preview": return "integration_sync"
    if path == "/provider-imports/sync": return "integration_sync"
    if path in {"/oauth/begin", "/oauth/callback", "/oauth/revoke"}: return "integration_configure"
    if path.startswith("/oauth/install/") and path.endswith("/health"): return "integration_sync"
    if path in {"/integrations/verify","/integrations/sync"}: return "integration_sync"
    if path == "/integrations": return "integration_configure"
    if path.startswith("/onboarding/imports"): return "workspace_write"
    if path == "/agents/runs/request-review": return "brain_read"
    if path.startswith("/agents/runs") or path == "/agents/tasks": return "agent_run"
    if path == "/reports/generate": return "workspace_write"
    if path.startswith("/agents"): return "agent_configure"
    if path.startswith("/automations/execute") or path == "/automations/trigger": return "automation_execute"
    if path.startswith("/automations"): return "automation_manage"
    if path == "/memory-proposals/review": return "brain_promote"
    if path in {"/brain/promote", "/brain/conflicts/resolve"}: return "brain_promote"
    if path == "/brain/propose": return "brain_propose"
    if path.startswith("/brain/customizations"): return "brain_configure"
    if path in {"/memory-proposals", "/remember"}: return "brain_propose"
    if path in {"/people", "/workspace-memberships"}: return "people_manage"
    if path in {"/clients/roster", "/meetings/responsibilities"} and method == "POST": return "people_manage"
    if path in {"/client-portal/intake/accept", "/client-portal/intake/decline"}: return "people_manage"
    if path.startswith("/reports/portal"): return "workspace_write"
    if path in {"/client-portal/intake", "/client-portal/reviews/comment", "/client-portal/reviews/decide"} and method == "POST": return "client_portal"
    if path in {"/reviews/annotations", "/reviews/annotations/resolve", "/reviews/annotations/supersede", "/reviews/media", "/assets/backups", "/assets/backups/status"}: return "workspace_write"
    if path in {"/organizations", "/workspaces"}: return "organization_manage"
    return "workspace_write"


def _need(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not value:
        raise ValidationError(f"{key} is required")
    return str(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError("intent_tags must be a list")
    return [str(item) for item in value]


def _optional_string_sequence(value: Any, key: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError(f"{key} must be a list")
    return [str(item) for item in value]


def _required_list(value: Any, key: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{key} must be a list")
    return value


def _int(value: Any, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{key} must be an integer") from exc

def _float(value: Any, key: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{key} must be a number") from exc

def _number(payload: dict[str, Any], key: str) -> float:
    if key not in payload or payload.get(key) is None:
        raise ValidationError(f"{key} is required")
    return _float(payload.get(key), key)

def _optional_float(value: Any) -> float | None:
    return _float(value, "value") if value is not None else None


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValidationError(f"{key} must be a boolean")


def _optional_dt(value: Any) -> Any:
    if not value:
        return None
    from datetime import datetime

    try:
        result = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError("as_of must be an ISO datetime") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValidationError("as_of must include a timezone")
    return result

def _what_if_params(params: dict[str, str]) -> dict[str, float | str] | None:
    numeric = {
        "capacity_hours_delta",
        "work_hours_delta",
        "scope_usage_delta",
        "finance_amount_delta",
        "client_health_delta",
        "deadline_days_delta",
        "additional_clients",
        "hours_per_new_client",
        "leave_hours_delta",
        "hiring_hours_delta",
        "client_revenue_delta",
        "client_cost_delta",
        "client_hours_delta",
    }
    result: dict[str, float | str] = {}
    for key in numeric:
        raw = params.get(f"what_if_{key}")
        if raw is None:
            continue
        try:
            result[key] = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"what_if_{key} must be numeric") from exc
    client_action = params.get("what_if_client_action")
    if client_action is not None:
        normalized = client_action.strip().lower()
        if normalized not in {"keep", "drop"}:
            raise ValidationError("what_if_client_action must be keep or drop")
        result["client_action"] = normalized
    return result or None


def _evaluation_safety_status(
    os: CompanyOS, organization_id: str, workspace_id: str, person_id: str, task_class: str
) -> dict[str, Any]:
    decision = os.intelligence_evaluation_safety.can_start(organization_id, person_id, task_class)
    rows = [
        dict(row)
        for row in os.store.conn.execute(
            """SELECT * FROM intelligence_evaluation_runs
               WHERE organization_id=? AND (workspace_id IS NULL OR workspace_id=?)
               ORDER BY created_at DESC,id DESC LIMIT 20""",
            (organization_id, workspace_id),
        ).fetchall()
    ]
    events = [
        dict(row)
        for row in os.store.conn.execute(
            """SELECT * FROM intelligence_evaluation_circuit_events
               WHERE organization_id=? AND task_class=?
               ORDER BY created_at DESC,id DESC LIMIT 20""",
            (organization_id, task_class),
        ).fetchall()
    ]
    return {
        "scope": {"organization_id": organization_id, "workspace_id": workspace_id, "person_id": person_id},
        "task_class": task_class,
        "circuit": decision,
        "evaluations": rows,
        "circuit_events": events,
    }


def _require_evaluation_scope(os: CompanyOS, organization_id: str, workspace_id: str, evaluation_id: str) -> None:
    row = os.store.conn.execute(
        "SELECT workspace_id FROM intelligence_evaluation_runs WHERE organization_id=? AND id=?",
        (organization_id, evaluation_id),
    ).fetchone()
    if row is None or row["workspace_id"] != workspace_id:
        raise NotFoundError("evaluation not found")

def _required_dt(value: Any, key: str) -> Any:
    if not value: raise ValidationError(f"{key} is required")
    result=_optional_dt(value)
    return result

