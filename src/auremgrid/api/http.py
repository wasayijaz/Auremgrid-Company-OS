from __future__ import annotations

import json
import os
import mimetypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from auremgrid.api.mcp import McpToolRouter, _mcp_capability
from auremgrid.domain.errors import AuthenticationError, AuremgridError, AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS
from auremgrid.connectors.catalog import connector_catalog
from pathlib import Path


LEGACY_ACTOR_PATHS = {
    "/search", "/entity", "/history", "/neighbors", "/sources", "/recent", "/brief", "/work",
    "/remember", "/work/capture", "/work/capture_work", "/work/assign", "/work/assign_work",
    "/work/start", "/work/start_work", "/work/dod", "/work/mark-dod", "/work/mark_dod",
    "/work/submit-review", "/work/submit_review", "/work/close-review", "/work/close_review",
    "/work/ship", "/work/ship_work",
}
JOB_TYPES = {"report.generate", "projection.rebuild", "agent.run", "automation.execute", "outbox.dispatch", "backup.create", "proactive_intelligence.refresh"}


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


class CompanyOSRequestHandler(BaseHTTPRequestHandler):
    os: CompanyOS

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            identity = None
            if parsed.path not in {"/", "/dashboard", "/health", "/health/detailed", "/oauth/callback"} and not parsed.path.startswith("/dashboard-assets/"):
                identity = self._authenticate_request(parsed.path, "GET", params)
            if parsed.path == "/health":
                self._json(200, {"ok": True, "schema_version": self.os.store.schema_version})
                return
            if parsed.path == "/metrics":
                from auremgrid.observability import get_metrics
                self._json(200, get_metrics().snapshot())
                return
            if parsed.path == "/health/detailed":
                from auremgrid.lifecycle import startup_health
                from auremgrid.observability import get_metrics
                health_warnings = startup_health(self.os.store.raw_connection, getattr(self.os.store, "path", ":memory:"))
                self._json(200, {
                    "ok": len(health_warnings) == 0,
                    "schema_version": self.os.store.schema_version,
                    "warnings": health_warnings,
                    "metrics": get_metrics().snapshot(),
                })
                return
            if parsed.path == "/auth/me":
                assert identity is not None
                self._json(200, identity.to_dict()); return
            if parsed.path == "/auth/invites":
                assert identity is not None
                self._json(200, {"invites": self.os.auth.list_invites(identity, params.get("include_inactive") == "true")}); return
            if parsed.path == "/auth/sessions":
                assert identity is not None
                self._json(200, {"sessions": self.os.auth.list_sessions(identity, params.get("include_revoked") == "true")}); return
            if parsed.path == "/onboarding/templates":
                self._json(200, self.os.onboarding.templates()); return
            if parsed.path in {"/", "/dashboard"}:
                self._html(200, _dashboard_html())
                return
            if parsed.path.startswith("/dashboard-assets/"):
                relative_path = parsed.path.removeprefix("/dashboard-assets/")
                self._dashboard_asset(relative_path)
                return
            if parsed.path == "/search":
                bundle = self.os.search(
                    _need(params, "workspace_id"),
                    _need(params, "actor_id"),
                    _need(params, "query"),
                    as_of=_optional_dt(params.get("as_of")),
                    limit=_int(params.get("limit", "8"), "limit"),
                )
                self._json(200, bundle.to_dict())
                return
            if parsed.path == "/entity/candidates":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, {"candidates": self.os.brain_ops.entity_resolution_candidates(
                    scoped.organization_id, workspace_id, scoped, _need(params, "name"), _int(params.get("limit", "8"), "limit")
                )}); return
            if parsed.path == "/entity":
                self._json(
                    200,
                    self.os.entity(_need(params, "workspace_id"), _need(params, "actor_id"), _need(params, "name"),
                                   as_of=_optional_dt(params.get("as_of"))),
                )
                return
            if parsed.path == "/history":
                self._json(
                    200,
                    self.os.history(
                        _need(params, "workspace_id"),
                        _need(params, "actor_id"),
                        _need(params, "subject"),
                        predicate=params.get("predicate"),
                        as_of=_optional_dt(params.get("as_of")),
                    ),
                )
                return
            if parsed.path == "/neighbors":
                self._json(
                    200,
                    self.os.neighbors(
                        _need(params, "workspace_id"), _need(params, "actor_id"), _need(params, "entity"),
                        as_of=_optional_dt(params.get("as_of")),
                    ),
                )
                return
            if parsed.path == "/sources":
                self._json(200, self.os.sources(_need(params, "workspace_id"), _need(params, "actor_id")))
                return
            if parsed.path == "/recent":
                self._json(
                    200,
                    self.os.recent(
                        _need(params, "workspace_id"),
                        _need(params, "actor_id"),
                        limit=int(params.get("limit", "5")),
                    ),
                )
                return
            if parsed.path == "/brief":
                self._json(
                    200,
                    self.os.account_brief(
                        _need(params, "workspace_id"),
                        _need(params, "actor_id"),
                        query=params.get("query"),
                    ).to_dict(),
                )
                return
            if parsed.path == "/work":
                items = self.os.list_work(
                    _need(params, "workspace_id"),
                    _need(params, "actor_id"),
                    open_only=params.get("open_only", "1") != "0",
                )
                self._json(200, {"work": [item.to_dict() for item in items]})
                return
            if parsed.path == "/organizations/workspaces":
                items = self.os.company.list_workspaces(_need(params, "organization_id"))
                self._json(200, {"workspaces": items})
                return
            if parsed.path == "/onboarding/imports":
                assert identity is not None
                self._json(200, self.os.onboarding.list_import_batches(
                    identity.organization_id,
                    _optional_str(params.get("workspace_id")),
                    identity.person_id,
                    _int(params.get("limit", "10"), "limit"),
                )); return
            if parsed.path == "/people":
                organization_id,person_id=_need(params,"organization_id"),_need(params,"person_id")
                membership = self.os.company.org_membership(organization_id, person_id)
                if membership is None: raise AuthorizationError("organization membership required")
                if membership.role == "client": raise AuthorizationError("people directory requires agency membership")
                items = self.os.company.list_people(organization_id)
                workspace_id = _optional_str(params.get("workspace_id"))
                if workspace_id:
                    self.os._require_person_access(organization_id, workspace_id, person_id)
                    items = [item for item in items if self.os.company.workspace_membership(workspace_id, item.id) is not None]
                self._json(200, {"people": [item.to_dict() for item in items]})
                return
            if parsed.path == "/people/detail":
                self._json(200, self.os.dashboard.person_detail(
                    _need(params, "organization_id"), _need(params, "person_id"), _need(params, "target_person_id"), params.get("workspace_id"), params.get("week_start")
                ))
                return
            if parsed.path == "/capacity":
                assert identity is not None
                self._json(200, self.os.capacity.weekly_board(
                    identity.organization_id,
                    identity.person_id,
                    _need(params, "week_start"),
                    _optional_str(params.get("workspace_id")),
                    as_of=_optional_dt(params.get("as_of")),
                ))
                return
            if parsed.path == "/projects":
                items = self.os.list_projects(
                    _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
                )
                self._json(200, {"projects": [item.to_dict() for item in items]})
                return
            if parsed.path == "/projects/get":
                organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                self.os._require_person_access(organization_id,workspace_id,person_id);item=self.os.company.get_project(workspace_id,_need(params,"project_id"))
                if item is None:raise NotFoundError("project not found")
                self._json(200,item.to_dict());return
            if parsed.path == "/deliverables":
                organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                self.os._require_person_access(organization_id,workspace_id,person_id);items=self.os.company.list_deliverables(workspace_id,params.get("project_id"))
                self._json(200,{"deliverables":[item.to_dict() for item in items]});return
            if parsed.path == "/reviews":
                organization_id, workspace_id, person_id = (
                    _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
                )
                self.os._require_person_access(organization_id, workspace_id, person_id)
                items = self.os.company.list_reviews(workspace_id, params.get("status"))
                self._json(200, {"reviews": [item.to_dict() for item in items]})
                return
            if parsed.path == "/reviews/annotations":
                organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
                self._json(200, {"annotations": self.os.list_review_annotations(organization_id, workspace_id, person_id, params.get("review_id"), params.get("include_closed", "1") != "0")})
                return
            if parsed.path == "/reviews/media":
                organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
                self._json(200, {"media": self.os.list_review_media_contracts(organization_id, workspace_id, person_id, _need(params, "review_id"))})
                return
            if parsed.path == "/clients/roster":
                organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
                self.os._require_person_access(organization_id, workspace_id, person_id)
                result = self.os.client_ops.get_client_roster(
                    organization_id, workspace_id, person_id, _optional_str(params.get("roster_id")),
                    as_of=_optional_dt(params.get("as_of")),
                )
                if result is None:
                    raise NotFoundError("client roster not found")
                self._json(200, result); return
            if parsed.path == "/meetings/responsibilities":
                organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
                self.os._require_person_access(organization_id, workspace_id, person_id)
                self._json(200, self.os.client_ops.get_meeting_responsibilities(
                    organization_id, workspace_id, person_id, _need(params, "meeting_id"),
                    as_of=_optional_dt(params.get("as_of")),
                )); return
            if parsed.path == "/decisions":
                organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
                workspace_id = params.get("workspace_id")
                if workspace_id:
                    self.os._require_person_access(organization_id, workspace_id, person_id)
                elif self.os.company.org_membership(organization_id, person_id) is None:
                    raise AuthorizationError("person is not an organization member")
                items = self.os.company.list_decisions(organization_id, workspace_id)
                self._json(200, {"decisions": [item.to_dict() for item in items]})
                return
            if parsed.path == "/dashboard/data":
                self._json(200, self.os.dashboard.command(_need(params,"organization_id"),_need(params,"person_id")))
                return
            if parsed.path == "/dashboard/settings":
                assert identity is not None
                self._json(200, self.os.dashboard.settings(
                    identity, _need(params, "organization_id"), _optional_str(params.get("workspace_id"))
                )); return
            if parsed.path == "/dashboard/review-center":
                self._json(200, self.os.dashboard.review_center(_need(params,"organization_id"),_need(params,"person_id")))
                return
            if parsed.path == "/dashboard/client":
                assert identity is not None
                self._json(200, self.os.dashboard.client_hq(
                    identity, _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
                ))
                return
            if parsed.path == "/dashboard/module":
                self._json(200,self.os.dashboard.module(_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id"),_need(params,"module")));return
            if parsed.path == "/dashboard/brain":
                assert identity is not None
                organization_id, workspace_id, person_id = _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
                self._json(200, self.os.dashboard.brain(identity, organization_id, workspace_id, person_id, _optional_dt(params.get("as_of")))); return
            if parsed.path == "/brain/customizations/active":
                assert identity is not None
                scoped_workspace = _optional_str(params.get("workspace_id"))
                scoped = self.os.auth.scope_identity(identity, scoped_workspace) if scoped_workspace else identity
                self._json(200, self.os.brain_customizations.active(
                    scoped, _need(params, "organization_id"), scoped_workspace,
                    _optional_str(params.get("kind")), _optional_dt(params.get("as_of")),
                )); return
            if parsed.path == "/dashboard/intelligence":
                assert identity is not None
                organization_id, workspace_id, person_id = _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                if scoped.organization_id != organization_id or scoped.person_id != person_id:
                    raise AuthorizationError("identity scope mismatch")
                actor_id = None
                try:
                    actor_id = self.os.auth.actor_for_identity(scoped, workspace_id)
                except AuthorizationError:
                    # Canonical operational findings remain available when a
                    # principal has not yet been bound to a brain actor.
                    actor_id = None
                self._json(200, self.os.intelligence.workspace(
                    organization_id, workspace_id, person_id, actor_id,
                    _optional_dt(params.get("as_of")), params.get("query"),
                    what_if=_what_if_params(params),
                    context_type=_optional_str(params.get("context_type")),
                    context_id=_optional_str(params.get("context_id")),
                    capabilities=identity.capabilities,
                )); return
            if parsed.path in {"/dashboard/intelligence/portfolio", "/dashboard/intelligence/executive"}:
                assert identity is not None
                organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
                if identity.organization_id != organization_id or identity.person_id != person_id:
                    raise AuthorizationError("identity scope mismatch")
                method = self.os.intelligence.executive_brief if parsed.path.endswith("/executive") else self.os.intelligence.portfolio
                self._json(200, method(
                    organization_id, person_id, as_of=_optional_dt(params.get("as_of")),
                )); return
            if parsed.path == "/dashboard/intelligence/snapshots":
                assert identity is not None
                organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
                workspace_id = _optional_str(params.get("workspace_id"))
                self.os.proactive_intelligence.authorize_read(identity, organization_id, person_id, workspace_id)
                snapshot = self.os.proactive_intelligence.require_latest_snapshot(
                    organization_id,
                    person_id,
                    str(params.get("snapshot_type") or ("workspace" if workspace_id else "executive")),
                    workspace_id,
                )
                self._json(200, {"snapshot": snapshot}); return
            if parsed.path == "/dashboard/intelligence/attention":
                assert identity is not None
                organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
                workspace_id = _optional_str(params.get("workspace_id"))
                self.os.proactive_intelligence.authorize_read(identity, organization_id, person_id, workspace_id)
                self._json(200, {"attention": self.os.proactive_intelligence.attention_queue(
                    organization_id, person_id, workspace_id, _int(params.get("limit", "20"), "limit")
                )}); return
            if parsed.path == "/dashboard/intelligence/refresh-status":
                assert identity is not None
                self._json(200, self.os.proactive_intelligence.refresh_status(
                    identity,
                    str(params.get("snapshot_type") or ("workspace" if params.get("workspace_id") else "executive")),
                    _optional_str(params.get("workspace_id")),
                )); return
            if parsed.path == "/dashboard/intelligence/profiles":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, {"profiles": list(self.os.intelligence_contracts.list_profiles(
                    scoped.organization_id, workspace_id, scoped.person_id,
                    domain=_optional_str(params.get("domain")),
                    capability_level=_optional_str(params.get("capability_level")),
                    capabilities=scoped.capabilities,
                ))}); return
            if parsed.path == "/dashboard/intelligence/profiles/get":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, {"profile": self.os.intelligence_contracts.get_profile(
                    scoped.organization_id, workspace_id, scoped.person_id, _need(params, "profile_id"),
                    version=_optional_int(params.get("version")),
                    capabilities=scoped.capabilities,
                )}); return
            if parsed.path == "/dashboard/intelligence/runbooks":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, {"runbooks": list(self.os.intelligence_contracts.list_runbooks(
                    scoped.organization_id, workspace_id, scoped.person_id,
                    domain=_optional_str(params.get("domain")),
                    profile_id=_optional_str(params.get("profile_id")),
                    capabilities=scoped.capabilities,
                ))}); return
            if parsed.path == "/dashboard/intelligence/runbooks/get":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, {"runbook": self.os.intelligence_contracts.get_runbook(
                    scoped.organization_id, workspace_id, scoped.person_id, _need(params, "runbook_id"),
                    version=_optional_int(params.get("version")),
                    capabilities=scoped.capabilities,
                )}); return
            if parsed.path == "/dashboard/intelligence/orchestrator/result":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                result = self.os.intelligence_orchestrator.get_run(
                    _need(params, "trace_id"), scoped.organization_id, workspace_id, scoped.person_id,
                )
                if result is None:
                    raise NotFoundError("orchestrator result not found")
                self._json(200, {"result": result}); return
            if parsed.path == "/dashboard/intelligence/orchestrator/latest":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                result = self.os.intelligence_orchestrator.latest_run(
                    scoped.organization_id, workspace_id, scoped.person_id,
                )
                self._json(200, {"result": result}); return
            if parsed.path == "/dashboard/intelligence/learning":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, self.os.intelligence_learning.workspace_learning(
                    scoped.organization_id, workspace_id, scoped.person_id,
                )); return
            if parsed.path in {"/dashboard/intelligence/recommendation-quality", "/dashboard/intelligence/recommendations/quality"}:
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, self.os.intelligence_learning.recommendation_quality(
                    scoped.organization_id, workspace_id, scoped.person_id,
                    as_of=_optional_str(params.get("as_of")),
                )); return
            if parsed.path == "/dashboard/intelligence/evaluation-safety":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, _evaluation_safety_status(
                    self.os, scoped.organization_id, workspace_id, scoped.person_id,
                    _optional_str(params.get("task_class")) or "reasoning",
                )); return
            if parsed.path == "/dashboard/workflows":
                assert identity is not None
                organization_id, workspace_id, person_id = _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
                self._json(200, self.os.dashboard.workflow_board(identity, organization_id, workspace_id, person_id, _optional_dt(params.get("as_of")))); return
            if parsed.path in {"/signals","/risks","/opportunities","/meetings","/campaigns","/creative","/content"}:
                organization_id, workspace_id, person_id = _need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                self.os._require_person_access(organization_id,workspace_id,person_id)
                if parsed.path == "/signals": result=self.os.client_ops.list_signals(organization_id,workspace_id,person_id,params.get("status"))
                elif parsed.path == "/risks": result=self.os.client_ops.list_risks(organization_id,workspace_id,person_id,params.get("open_only","1")!="0")
                elif parsed.path == "/opportunities": result=[dict(r) for r in self.os.store.conn.execute("SELECT * FROM opportunities WHERE workspace_id=? ORDER BY created_at DESC",(workspace_id,)).fetchall()]
                elif parsed.path == "/meetings": result=[dict(r) for r in self.os.store.conn.execute("SELECT * FROM meetings WHERE workspace_id=? ORDER BY occurred_at DESC",(workspace_id,)).fetchall()]
                elif parsed.path == "/campaigns": result=[dict(r) for r in self.os.store.conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY updated_at DESC",(workspace_id,)).fetchall()]
                elif parsed.path == "/creative": result=self.os.agency_ops.search_creative(organization_id,workspace_id,person_id,params.get("query",""),params.get("approval_state"),params.get("campaign_id"))
                else: result=[dict(r) for r in self.os.store.conn.execute("SELECT * FROM content_items WHERE workspace_id=? ORDER BY updated_at DESC",(workspace_id,)).fetchall()]
                self._json(200,{parsed.path[1:]:result}); return
            if parsed.path == "/sales/prospects":
                self._json(200, {"prospects": self.os.revenue.list_prospects(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"), params.get("status"))}); return
            if parsed.path == "/sales/proposals":
                self._json(200, {"proposals": self.os.revenue.list_proposals(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"), params.get("status"))}); return
            if parsed.path == "/campaigns/budget-pacing":
                self._json(200, {"signals": self.os.revenue.campaign_budget_pacing(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"))}); return
            if parsed.path == "/client-hq/retainer":
                self._json(200, self.os.revenue.retainer_read_model(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"))); return
            if parsed.path == "/report-packs":
                self._json(200, {"requests": self.os.revenue.list_report_packs(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"))}); return
            if parsed.path == "/finance":
                self._json(200,self.os.agency_ops.finance_status(_need(params,"organization_id"),_need(params,"person_id"),params.get("workspace_id"))); return
            if parsed.path == "/health/explain":
                self._json(200, self.os.client_ops.explain_health(
                    _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
                )); return
            if parsed.path == "/risks/detail":
                self._json(200, self.os.client_ops.risk_detail(
                    _need(params,"organization_id"), _need(params,"workspace_id"),
                    _need(params,"person_id"), _need(params,"risk_id")
                )); return
            if parsed.path == "/opportunities/detail":
                self._json(200, self.os.client_ops.opportunity_detail(
                    _need(params,"organization_id"), _need(params,"workspace_id"),
                    _need(params,"person_id"), _need(params,"opportunity_id")
                )); return
            if parsed.path == "/scope/status":
                self._json(200, self.os.client_ops.scope_status(
                    _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
                )); return
            if parsed.path == "/campaigns/detail":
                self._json(200, self.os.agency_ops.campaign_detail(
                    _need(params,"organization_id"), _need(params,"workspace_id"),
                    _need(params,"person_id"), _need(params,"campaign_id")
                )); return
            if parsed.path == "/creative/detail":
                self._json(200, self.os.agency_ops.creative_detail(
                    _need(params,"organization_id"), _need(params,"workspace_id"),
                    _need(params,"person_id"), _need(params,"asset_id")
                )); return
            if parsed.path in {"/assets", "/asset-registry"}:
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, {"assets": self.os.asset_recovery.list_assets(
                    scoped, scoped.organization_id, workspace_id,
                    _optional_str(params.get("status")),
                    _optional_str(params.get("retention_class")),
                    _int(params.get("limit", "100"), "limit"),
                )}); return
            if parsed.path in {"/assets/detail", "/asset-registry/detail"}:
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, self.os.asset_recovery.asset_detail(
                    scoped, scoped.organization_id, workspace_id, _need(params, "asset_id")
                )); return
            if parsed.path == "/assets/backups":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, {"backups": self.os.asset_recovery.list_asset_backups(scoped, scoped.organization_id, workspace_id, _optional_str(params.get("asset_id")))})
                return
            if parsed.path == "/notifications":
                self._json(200,{"notifications":self.os.agency_ops.attention(_need(params,"organization_id"),_need(params,"person_id"),_int(params.get("limit",20),"limit"))}); return
            if parsed.path == "/agents":
                self._json(200,self.os.agent_ops.command_center(_need(params,"organization_id"),_need(params,"person_id"))); return
            if parsed.path == "/agents/detail":
                assert identity is not None
                self._json(200, self.os.dashboard.agent_detail(
                    _need(params, "organization_id"), _need(params, "person_id"), _need(params, "agent_id"),
                    identity.capabilities,
                )); return
            if parsed.path == "/agents/runs":
                self._json(200, {"runs": self.os.agent_ops.list_runs(
                    _need(params,"organization_id"), _need(params,"person_id"),
                    _optional_str(params.get("workspace_id")), _optional_str(params.get("agent_id")),
                )}); return
            if parsed.path == "/agents/runs/detail":
                self._json(200, self.os.agent_ops.run_detail(
                    _need(params,"organization_id"), _need(params,"person_id"), _need(params,"run_id")
                )); return
            if parsed.path == "/dashboard/performance":
                self._json(200, self.os.dashboard.performance_surface(
                    _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
                )); return
            if parsed.path in {"/approvals","/automations","/reports"}:
                assert identity is not None
                organization_id,person_id=_need(params,"organization_id"),_need(params,"person_id")
                if identity.organization_id != organization_id or identity.person_id != person_id:
                    raise AuthorizationError("identity scope mismatch")
                if self.os.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
                table={"/approvals":"approval_requests","/automations":"automations","/reports":"report_runs"}[parsed.path]
                if parsed.path == "/reports":
                    workspace_id = _optional_str(params.get("workspace_id"))
                    visible = self.os.agent_ops.visible_workspace_ids(organization_id, person_id)
                    if workspace_id and workspace_id not in visible:
                        raise AuthorizationError("report workspace is not visible to caller")
                    values: list[Any] = [organization_id]
                    if workspace_id:
                        where = "organization_id=? AND workspace_id=?"
                        values.append(workspace_id)
                    elif visible:
                        marks = ",".join("?" for _ in visible)
                        where = f"organization_id=? AND (workspace_id IS NULL OR workspace_id IN ({marks}))"
                        values.extend(sorted(visible))
                    else:
                        where = "organization_id=? AND workspace_id IS NULL"
                    rows=self.os.store.conn.execute(f"SELECT * FROM report_runs WHERE {where} ORDER BY rowid DESC",values).fetchall()
                    reports = []
                    for row in rows:
                        item = dict(row)
                        item["allowed_actions"] = [{
                            "id": "view-report",
                            "action": "view_report",
                            "label": "View report",
                            "kind": "report.view",
                            "route": "",
                            "method": "GET",
                            "payload": {"report_id": item["id"]},
                            "required_fields": [],
                            "safe": True,
                            "one_way": False,
                            "requires_approval": False,
                            "status": "available",
                        }]
                        reports.append(item)
                    self._json(200,{"reports":reports,"allowed_actions":self.os.agent_ops.report_action_descriptors(
                        organization_id, person_id, workspace_id, identity.capabilities
                    )});return
                rows=self.os.store.conn.execute(f"SELECT * FROM {table} WHERE organization_id=? ORDER BY rowid DESC",(organization_id,)).fetchall()
                self._json(200,{parsed.path[1:]:[dict(r) for r in rows]});return
            if parsed.path == "/integrations":
                assert identity is not None
                self._json(200,{"integrations":self.os.integrations.list(identity)}); return
            if parsed.path == "/connectors/catalog":
                self._json(200, {"connectors": connector_catalog()}); return
            if parsed.path == "/operator/health":
                assert identity is not None
                worker_id = params.get("worker_id", "default")
                self._json(200, self.os.scheduler(identity.organization_id, params.get("workspace_id"), worker_id).health()); return
            if parsed.path == "/provider-imports/status":
                assert identity is not None
                rows = self.os.store.conn.execute(
                    "SELECT * FROM provider_import_cursors WHERE organization_id=? ORDER BY updated_at DESC",
                    (identity.organization_id,),
                ).fetchall()
                quarantines = self.os.store.conn.execute("SELECT provider,object_type,external_id,reason,evidence_digest,created_at FROM provider_import_quarantines WHERE organization_id=? ORDER BY created_at DESC LIMIT 50", (identity.organization_id,)).fetchall()
                self._json(200, {"imports": [dict(row) for row in rows], "quarantines": [dict(row) for row in quarantines]}); return
            if parsed.path == "/webhooks/provider/status":
                assert identity is not None
                from auremgrid.services.integration_security import WebhookIntakeService
                self._json(200, WebhookIntakeService(self.os.store.conn, self.os.jobs.new_id).status(identity)); return
            if parsed.path == "/oauth/callback":
                if "code_verifier" in params:
                    raise ValidationError("code_verifier is not accepted on OAuth callback")
                item = self.os.oauth_service().complete(_need(params,"state"), _need(params,"code"),
                    None, _need(params,"redirect_uri"), _need(params,"provider"))
                self._json(200, item); return
            if parsed.path.startswith("/oauth/install/") and parsed.path.endswith("/health"):
                assert identity is not None
                installation_id = parsed.path.split("/")[3]
                self._json(200, self.os.oauth_service().health(identity, installation_id)); return
            if parsed.path == "/memory-proposals":
                assert identity is not None
                workspace_id = params.get("workspace_id")
                if not workspace_id: raise NotFoundError("proposal scope not found")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self.os._require_person_access(scoped.organization_id, workspace_id, scoped.person_id)
                self._json(200,{"proposals":self.os.brain_ops.list_memory_proposals(
                    scoped.organization_id, workspace_id, scoped.person_id, _optional_dt(params.get("as_of"))
                )}); return
            if parsed.path == "/knowledge-health":
                assert identity is not None
                view = self.os.dashboard.brain(
                    identity, _need(params,"organization_id"), _need(params,"workspace_id"),
                    _need(params,"person_id"), _optional_dt(params.get("as_of")),
                )
                self._json(200,{
                    "generated_at":view["generated_at"],"as_of":view["as_of"],
                    "workspace":view["workspace"],"summary":view["summary"],"health":view["health"],
                }); return
            if parsed.path == "/work/detail":
                self._json(200,self.os.work_ops.detail(_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id"),_need(params,"work_item_id"))); return
            if parsed.path == "/workflows/templates":
                organization_id,person_id=_need(params,"organization_id"),_need(params,"person_id")
                if self.os.company.org_membership(organization_id,person_id) is None: raise AuthorizationError("organization membership required")
                templates=self.os.workflow_catalog.for_wing(params["wing"]) if params.get("wing") else self.os.workflow_catalog.all()
                self._json(200,{"templates":[item.to_dict() for item in templates]}); return
            if parsed.path == "/workflows/runs":
                organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                self.os._require_person_access(organization_id,workspace_id,person_id)
                rows=self.os.store.conn.execute("""SELECT id,definition_key,definition_name,definition_version,status,due_at,
                    escalation_at,created_at,updated_at FROM workflow_runs WHERE organization_id=? AND workspace_id=? ORDER BY updated_at DESC""",
                    (organization_id,workspace_id)).fetchall()
                self._json(200,{"runs":[dict(row) for row in rows]}); return
            if parsed.path == "/workflows/runs/get":
                self._json(200,self.os.workflow_ops.summary(_need(params,"organization_id"),_need(params,"workspace_id"),
                    _need(params,"person_id"),_need(params,"run_id"))); return
            if parsed.path == "/workflows/escalations":
                self._json(200,self.os.workflow_ops.overdue_escalations(_need(params,"organization_id"),_need(params,"workspace_id"),
                    _need(params,"person_id"),params.get("as_of"))); return
            if parsed.path == "/jobs":
                items=self.os.jobs.list_jobs(_need(params,"organization_id"),_optional_str(params.get("workspace_id")),params.get("status"))
                self._json(200,{"jobs":items}); return
            if parsed.path == "/jobs/get":
                organization_id,workspace_id=_need(params,"organization_id"),_optional_str(params.get("workspace_id")); job_id=_need(params,"job_id")
                self._json(200,{"job":self.os.jobs.get_job(organization_id,workspace_id,job_id),
                    "events":self.os.jobs.job_events(organization_id,workspace_id,job_id)}); return
            if parsed.path == "/client-portal/intake":
                organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                items=self.os.client_portal.list_intake_requests(organization_id,workspace_id,person_id,params.get("status"))
                self._json(200,{"intake_requests":items}); return
            if parsed.path == "/client-portal/intake/queue":
                organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                items=self.os.client_portal.list_intake_queue(organization_id,workspace_id,person_id)
                self._json(200,{"intake_requests":items}); return
            if parsed.path == "/client-portal/reviews":
                organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
                items=self.os.client_portal.list_client_reviews(organization_id,workspace_id,person_id)
                self._json(200,{"reviews":items}); return
            if parsed.path == "/client-portal/reports":
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, {"reports": self.os.report_delivery.portal_list(
                    scoped, scoped.organization_id, workspace_id
                )}); return
            if parsed.path in {"/client-portal/reports/view", "/client-portal/reports/download"}:
                assert identity is not None
                workspace_id = _need(params, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                handler = self.os.report_delivery.portal_download if parsed.path.endswith("/download") else self.os.report_delivery.portal_view
                self._json(200, handler(
                    scoped, scoped.organization_id, workspace_id, _need(params, "portal_report_version_id")
                )); return
            if parsed.path == "/feedback/patterns":
                assert identity is not None
                org, ws, person_id = identity.organization_id, _need(params, "workspace_id"), identity.person_id
                self._json(200, self.os.feedback.list_patterns(org, ws, person_id, params.get("category"), params.get("status"))); return
            if parsed.path == "/insights/performance":
                assert identity is not None
                org, ws, person_id = identity.organization_id, _need(params, "workspace_id"), identity.person_id
                self._json(200, self.os.performance.list_insights(org, ws, person_id, params.get("status"), params.get("insight_type"))); return
            if parsed.path == "/forecasts":
                assert identity is not None
                org, person_id = identity.organization_id, identity.person_id
                self._json(200, self.os.forecasts.list_forecasts(org, person_id, params.get("forecast_type"), params.get("status"))); return
            if parsed.path == "/retention/policies":
                assert identity is not None
                org, person_id = identity.organization_id, identity.person_id
                self._json(200, self.os.retention.list_policies(org, person_id, params.get("scope"))); return
            if parsed.path == "/export/workspace":
                assert identity is not None
                org, ws, person_id = identity.organization_id, _need(params, "workspace_id"), identity.person_id
                self._json(200, self.os.retention.export_workspace(org, ws, person_id)); return
            self._json(404, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/webhooks/provider/"):
                self._receive_provider_webhook(parsed.path)
                return
            payload = self._read_json()
            if parsed.path == "/oauth/callback":
                if "code_verifier" in payload:
                    raise ValidationError("code_verifier is not accepted on OAuth callback")
                item = self.os.oauth_service().complete(_need(payload,"state"), _need(payload,"code"),
                    None, _need(payload,"redirect_uri"), _need(payload,"provider"))
                self._json(200, item); return
            if parsed.path == "/tools/call":
                arguments = payload.get("arguments") or {}
                if not isinstance(arguments, dict): raise ValidationError("arguments must be an object")
                tool_name=str(payload.get("name", ""))
                identity = self._authenticate_request(parsed.path, "POST", arguments, _mcp_capability(tool_name))
                result = McpToolRouter(self.os, identity).call(tool_name, arguments)
                status = 400 if "error" in result else 200
                self._json(status, result)
                return
            identity = self._authenticate_request(parsed.path, "POST", payload)
            if parsed.path == "/auth/api-tokens":
                item=self.os.auth.create_api_token(identity.principal_id,_need(payload,"name"),
                    [str(value) for value in payload.get("scopes",[])])
                self._json(201,{"id":item["id"],"name":item["name"],"token":item["token"],"scopes":item["scopes"],"expires_at":item["expires_at"]}); return
            if parsed.path == "/auth/invites":
                from datetime import timedelta
                item=self.os.auth.create_invite(identity,_need(payload,"target_person_id"),_need(payload,"email"),
                    _optional_str(payload.get("workspace_id")),_optional_str(payload.get("actor_id")),
                    timedelta(seconds=int(payload.get("expires_in_seconds", 604800))))
                self._json(201,item); return
            if parsed.path == "/auth/invites/revoke":
                self._json(200,self.os.auth.revoke_invite(identity,_need(payload,"invite_id"))); return
            if parsed.path == "/auth/invites/consume":
                self._json(200,self.os.auth.consume_invite(identity,_need(payload,"token"))); return
            if parsed.path == "/auth/sessions/rotate":
                token=self.headers.get("Authorization","")[7:].strip(); item=self.os.auth.rotate_session(token)
                self._json(200,{"id":item["id"],"token":item["token"],"expires_at":item["expires_at"]}); return
            if parsed.path == "/auth/sessions/revoke":
                self._json(200,self.os.auth.revoke_session_by_id(identity,_need(payload,"session_id"))); return
            if parsed.path == "/auth/revoke":
                token=self.headers.get("Authorization","")[7:].strip()
                if identity.is_api_token:self.os.auth.revoke_api_token(token)
                else:self.os.auth.revoke_session(token)
                self._json(200,{"revoked":True}); return
            if parsed.path == "/auth/actor-bindings":
                self._json(201,self.os.auth.bind_actor(identity,_need(payload,"workspace_id"),_need(payload,"actor_id"))); return
            if parsed.path == "/jobs":
                job_type=_need(payload,"type")
                if job_type not in JOB_TYPES: raise ValidationError("unsupported job type")
                item=self.os.jobs.enqueue_job(identity.organization_id,_optional_str(payload.get("workspace_id")),identity.principal_id,job_type,
                    payload.get("payload") or {},int(payload.get("priority",0)),int(payload.get("max_attempts",3)),
                    _optional_str(payload.get("available_at")),_optional_str(payload.get("idempotency_key")))
                self._json(201,item); return
            if parsed.path == "/jobs/cancel":
                item=self.os.jobs.cancel_job(identity.organization_id,_optional_str(payload.get("workspace_id")),_need(payload,"job_id"),
                    _need(payload,"reason"),identity.principal_id,_optional_int(payload.get("expected_version")))
                if item["type"]=="connector.sync": self.os.integrations.release_job_stream(item["id"])
                self._json(200,item); return
            if parsed.path == "/dashboard/intelligence/refresh":
                item = self.os.proactive_intelligence.enqueue_refresh(
                    identity,
                    str(payload.get("snapshot_type") or ("workspace" if payload.get("workspace_id") else "executive")),
                    _optional_str(payload.get("workspace_id")),
                    _optional_str(payload.get("idempotency_key")),
                    int(payload.get("priority", 0)),
                )
                self._json(202, {"job": item}); return
            if parsed.path == "/dashboard/intelligence/orchestrator/run":
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                actor_id = None
                try:
                    actor_id = self.os.auth.actor_for_identity(scoped, workspace_id)
                except AuthorizationError:
                    actor_id = None
                result = self.os.intelligence_orchestrator.run(
                    scoped.organization_id,
                    workspace_id,
                    scoped.person_id,
                    actor_id=actor_id,
                    runbook_id=_optional_str(payload.get("runbook_id")),
                    profile_ids=_optional_string_sequence(payload.get("profile_ids"), "profile_ids"),
                    query=_optional_str(payload.get("query")),
                    as_of=_optional_dt(payload.get("as_of")),
                    capabilities=scoped.capabilities,
                    iterations=_int(payload.get("iterations", 1), "iterations"),
                )
                self._json(200, {"result": result}); return
            if parsed.path == "/dashboard/intelligence/hypotheses":
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(201, {"hypothesis": self.os.intelligence_learning.record_hypothesis(
                    scoped.organization_id, workspace_id, scoped.person_id, _need(payload, "text"),
                    subject=_optional_str(payload.get("subject")),
                    evidence_for_refs=payload.get("evidence_for_refs"),
                    evidence_against_refs=payload.get("evidence_against_refs"),
                    status=str(payload.get("status") or "proposed"),
                    confidence=float(payload.get("confidence", 0.5)),
                    assumptions=payload.get("assumptions"),
                    generated_by=payload.get("generated_by"),
                    resolution=_optional_str(payload.get("resolution")),
                    outcome=payload.get("outcome"),
                    supersedes_hypothesis_id=_optional_str(payload.get("supersedes_hypothesis_id")),
                    idempotency_key=_optional_str(payload.get("idempotency_key")),
                )}); return
            if parsed.path == "/dashboard/intelligence/recommendations":
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(201, {"recommendation": self.os.intelligence_learning.record_recommendation(
                    scoped.organization_id, workspace_id, scoped.person_id, _need(payload, "summary"),
                    runbook_id=_need(payload, "runbook_id"),
                    runbook_version=_int(payload.get("runbook_version"), "runbook_version"),
                    profile_contributors=_required_list(payload.get("profile_contributors"), "profile_contributors"),
                    confidence=float(payload.get("confidence", 0.5)),
                    options=_required_list(payload.get("options"), "options"),
                    recommended_option_id=_optional_str(payload.get("recommended_option_id")),
                    evidence_refs=_required_list(payload.get("evidence_refs"), "evidence_refs"),
                    evaluation_window_start=_need(payload, "evaluation_window_start"),
                    evaluation_window_end=_need(payload, "evaluation_window_end"),
                    generated_by=payload.get("generated_by"),
                    idempotency_key=_optional_str(payload.get("idempotency_key")),
                )}); return
            if parsed.path == "/dashboard/intelligence/recommendations/lifecycle":
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(201, {"event": self.os.intelligence_learning.append_recommendation_event(
                    scoped.organization_id, workspace_id, scoped.person_id,
                    _need(payload, "recommendation_id"), _need(payload, "event_type"),
                    chosen_option_id=_optional_str(payload.get("chosen_option_id")),
                    measured_outcomes=payload.get("measured_outcomes"),
                    score=None if payload.get("score") is None else float(payload.get("score")),
                    lessons=str(payload.get("lessons") or ""),
                    evidence_refs=payload.get("evidence_refs"),
                    evaluation_window_start=_optional_str(payload.get("evaluation_window_start")),
                    evaluation_window_end=_optional_str(payload.get("evaluation_window_end")),
                    idempotency_key=_optional_str(payload.get("idempotency_key")),
                )}); return
            if parsed.path == "/dashboard/intelligence/recommendations/handoff":
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(201, self.os.intelligence_learning.handoff_recommendation(
                    scoped.organization_id, workspace_id, scoped.person_id, _need(payload, "trace_id"),
                    recommendation_id=_optional_str(payload.get("recommendation_id")),
                    summary=_optional_str(payload.get("summary")),
                    runbook_id=_optional_str(payload.get("runbook_id")),
                    runbook_version=_optional_int(payload.get("runbook_version")),
                    profile_contributors=payload.get("profile_contributors"), confidence=float(payload.get("confidence", 0.5)),
                    options=payload.get("options"), recommended_option_id=_optional_str(payload.get("recommended_option_id")),
                    evidence_refs=payload.get("evidence_refs"),
                    evaluation_window_start=_optional_str(payload.get("evaluation_window_start")),
                    evaluation_window_end=_optional_str(payload.get("evaluation_window_end")), generated_by=payload.get("generated_by"),
                    review_status=str(payload.get("review_status") or "reviewed"), decision_id=_optional_str(payload.get("decision_id")),
                    approval_request_id=_optional_str(payload.get("approval_request_id")), work_item_id=_optional_str(payload.get("work_item_id")),
                    action_descriptor=payload.get("action_descriptor"), outcome_refs=payload.get("outcome_refs"),
                    notes=str(payload.get("notes") or ""), idempotency_key=_optional_str(payload.get("idempotency_key")),
                )); return
            if parsed.path == "/dashboard/intelligence/evaluation/start":
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(201, {"evaluation": self.os.intelligence_evaluation_safety.start(
                    scoped.organization_id, scoped.person_id, _need(payload, "task_class"),
                    workspace_id=workspace_id,
                    provider=_optional_str(payload.get("provider")),
                    model=_optional_str(payload.get("model")),
                    specialist_profile_id=_optional_str(payload.get("specialist_profile_id")),
                    runbook_id=_optional_str(payload.get("runbook_id")),
                    runbook_version=_optional_int(payload.get("runbook_version")),
                    trace_id=_optional_str(payload.get("trace_id")),
                    agent_run_id=_optional_str(payload.get("agent_run_id")),
                )}); return
            if parsed.path == "/dashboard/intelligence/evaluation/complete":
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                _require_evaluation_scope(self.os, scoped.organization_id, workspace_id, _need(payload, "evaluation_id"))
                self._json(200, {"evaluation": self.os.intelligence_evaluation_safety.complete(
                    scoped.organization_id, scoped.person_id, _need(payload, "evaluation_id"),
                    workspace_id=workspace_id,
                    input_tokens=_optional_int(payload.get("input_tokens")),
                    output_tokens=_optional_int(payload.get("output_tokens")),
                    cost_amount=None if payload.get("cost_amount") is None else float(payload.get("cost_amount")),
                    cost_currency=_optional_str(payload.get("cost_currency")),
                    evidence_completeness=None if payload.get("evidence_completeness") is None else float(payload.get("evidence_completeness")),
                    evaluator_score=None if payload.get("evaluator_score") is None else float(payload.get("evaluator_score")),
                    human_acceptance=payload.get("human_acceptance") if isinstance(payload.get("human_acceptance"), bool) else None,
                    revision_count=_int(payload.get("revision_count", 0), "revision_count"),
                    downstream_outcome_score=None if payload.get("downstream_outcome_score") is None else float(payload.get("downstream_outcome_score")),
                    metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
                )}); return
            if parsed.path == "/search":
                bundle = self.os.search(
                    _need(payload, "workspace_id"),
                    _need(payload, "actor_id"),
                    _need(payload, "query"),
                    as_of=_optional_dt(payload.get("as_of")),
                    limit=_int(payload.get("limit", 8), "limit"),
                )
                self._json(200, bundle.to_dict())
                return
            if parsed.path == "/remember":
                memory = self.os.remember(
                    _need(payload, "workspace_id"),
                    _need(payload, "actor_id"),
                    _need(payload, "content"),
                    kind=str(payload.get("kind", "preference")),
                )
                self._json(200, memory.to_dict())
                return
            if parsed.path == "/organizations":
                item = self.os.create_organization(_need(payload, "name"), _optional_str(payload.get("id")))
                self._json(201, item.to_dict())
                return
            if parsed.path == "/workspaces":
                item = self.os.create_organization_workspace(
                    _need(payload, "organization_id"), _need(payload, "name"),
                    str(payload.get("kind", "client")), _optional_str(payload.get("id")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/people":
                item = self.os.create_person(
                    _need(payload, "organization_id"), _need(payload, "name"),
                    _optional_str(payload.get("email")), _optional_str(payload.get("title")),
                    _optional_str(payload.get("department")), _optional_str(payload.get("manager_id")),
                    str(payload.get("role", "member")), _optional_str(payload.get("id")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/workspace-memberships":
                item = self.os.add_person_to_workspace(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), str(payload.get("role", "operator")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/onboarding/imports/preview":
                result = self.os.onboarding.preview_csv_import(
                    _need(payload, "organization_id"),
                    _optional_str(payload.get("workspace_id")),
                    _need(payload, "person_id"),
                    _need(payload, "import_type"),
                    _need(payload, "csv_text"),
                    _need(payload, "idempotency_key"),
                )
                self._json(201, result)
                return
            if parsed.path == "/onboarding/imports/commit":
                result = self.os.onboarding.commit_csv_import(
                    _need(payload, "organization_id"),
                    _need(payload, "batch_id"),
                    _need(payload, "person_id"),
                    _need(payload, "idempotency_key"),
                )
                self._json(200, result)
                return
            if parsed.path == "/projects":
                item = self.os.create_project(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "name"), str(payload.get("description", "")),
                    str(payload.get("priority", "normal")), _optional_str(payload.get("due_date")),
                    float(payload["budget"]) if payload.get("budget") is not None else None,
                    [str(value) for value in payload.get("tags", [])],
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/contracts":
                item = self.os.client_ops.create_contract(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "kind"),
                    _need(payload, "billing_model"), _need(payload, "start_date"),
                    _optional_float(payload.get("value")), str(payload.get("currency", "USD")),
                    _optional_str(payload.get("end_date")), _optional_str(payload.get("renewal_date")),
                )
                self._json(201, item)
                return
            if parsed.path == "/scope/allowances":
                item = self.os.client_ops.add_scope_allowance(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "contract_id"),
                    _need(payload, "service_category"), _need(payload, "period"),
                    _optional_float(payload.get("included_quantity")),
                    _optional_float(payload.get("included_hours")),
                    _optional_int(payload.get("revision_limit")),
                )
                self._json(201, item)
                return
            if parsed.path == "/scope/usage":
                item = self.os.client_ops.record_scope_usage(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "contract_id"),
                    _need(payload, "allowance_id"), _need(payload, "period_start"),
                    _number(payload, "delivered"),
                    _optional_float(payload.get("in_review")) or 0.0,
                    _optional_float(payload.get("requested")) or 0.0,
                    _optional_float(payload.get("used_hours")) or 0.0,
                )
                self._json(201, item)
                return
            if parsed.path == "/deliverables":
                item = self.os.create_deliverable(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "project_id"),
                    _need(payload, "title"), _need(payload, "type"), _optional_str(payload.get("work_item_id")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/reviews":
                item = self.os.open_review(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "deliverable_id"),
                    str(payload.get("kind", "internal")), _optional_str(payload.get("reviewer_person_id")),
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/clients/roster":
                result = self.os.client_ops.create_client_roster(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    payload.get("roles") or [], _optional_str(payload.get("effective_at")), str(payload.get("note", "")),
                )
                self._json(201, result); return
            if parsed.path == "/meetings/responsibilities":
                result = self.os.client_ops.set_meeting_responsibilities(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "meeting_id"), facilitator_person_id=_optional_str(payload.get("facilitator_person_id")),
                    note_taker_person_id=_optional_str(payload.get("note_taker_person_id")), reason=str(payload.get("reason", "manual")),
                )
                self._json(200, result); return
            if parsed.path == "/reviews/decide":
                item = self.os.decide_review(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"),
                    _need(payload, "person_id"), _need(payload, "review_id"), _need(payload, "decision"),
                )
                self._json(200, item.to_dict())
                return
            if parsed.path == "/client-portal/intake":
                item = self.os.client_portal.submit_intake_request(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "title"), _need(payload, "request"), _optional_str(payload.get("needed_by")),
                )
                self._json(201, item); return
            if parsed.path == "/client-portal/intake/accept":
                item = self.os.client_portal.accept_intake_request(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "intake_request_id"), _optional_str(payload.get("assignee_id")),
                    _optional_str(payload.get("decision_maker")),
                )
                self._json(200, item); return
            if parsed.path == "/client-portal/intake/decline":
                item = self.os.client_portal.decline_intake_request(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "intake_request_id"), str(payload.get("note", "")),
                )
                self._json(200, item); return
            if parsed.path == "/client-portal/reviews/comment":
                item = self.os.client_portal.add_client_review_comment(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "review_id"), _need(payload, "body"),
                )
                self._json(201, item.to_dict()); return
            if parsed.path == "/client-portal/reviews/decide":
                item = self.os.client_portal.decide_client_review(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "review_id"), _need(payload, "decision"),
                )
                self._json(200, item.to_dict()); return
            if parsed.path == "/decisions":
                item = self.os.create_decision(
                    _need(payload, "organization_id"), _need(payload, "person_id"),
                    _need(payload, "statement"), _need(payload, "rationale"),
                    _optional_str(payload.get("workspace_id")), _optional_str(payload.get("project_id")),
                    _optional_str(payload.get("source_id")), str(payload.get("evidence", "")),
                    [str(value) for value in payload.get("tags", [])],
                )
                self._json(201, item.to_dict())
                return
            if parsed.path == "/signals":
                item=self.os.client_ops.create_signal(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"type"),_need(payload,"source_type"),_need(payload,"evidence"),_optional_str(payload.get("source_id")),float(payload.get("confidence",1)))
                self._json(201,item.to_dict()); return
            if parsed.path == "/signals/route":
                self._json(200,self.os.client_ops.route_signal(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"signal_id"),_need(payload,"destination"))); return
            if parsed.path == "/risks":
                item=self.os.client_ops.create_risk(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"type"),_need(payload,"severity"),float(payload.get("probability",0.5)),_need(payload,"impact"),_need(payload,"evidence"),_need(payload,"recommended_action"),_optional_str(payload.get("project_id")))
                self._json(201,item.to_dict()); return
            if parsed.path == "/risks/resolve":
                self._json(200, self.os.client_ops.resolve_risk(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"),
                    _need(payload,"person_id"), _need(payload,"risk_id"), _need(payload,"resolution")
                )); return
            if parsed.path == "/risks/reopen":
                self._json(200, self.os.client_ops.reopen_risk(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"),
                    _need(payload,"person_id"), _need(payload,"risk_id"), _need(payload,"reason")
                )); return
            if parsed.path == "/opportunities":
                item=self.os.client_ops.create_opportunity(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"type"),_need(payload,"reason"),_need(payload,"evidence"),_need(payload,"recommendation"),float(payload["estimated_value"]) if payload.get("estimated_value") is not None else None)
                self._json(201,item.to_dict()); return
            if parsed.path == "/opportunities/advance":
                self._json(200, self.os.client_ops.advance_opportunity(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"),
                    _need(payload,"person_id"), _need(payload,"opportunity_id"),
                    _need(payload,"to_status"), str(payload.get("note", "")),
                )); return
            if parsed.path == "/opportunities/close":
                self._json(200, self.os.client_ops.close_opportunity(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"),
                    _need(payload,"person_id"), _need(payload,"opportunity_id"),
                    _need(payload,"outcome"), _need(payload,"note"),
                )); return
            if parsed.path == "/health/calculate":
                item=self.os.client_ops.calculate_health(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id")); self._json(201,item.to_dict()); return
            if parsed.path == "/campaigns":
                item=self.os.agency_ops.create_campaign(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"name"),_need(payload,"objective"),_need(payload,"platform"),_optional_str(payload.get("project_id")),float(payload["budget"]) if payload.get("budget") is not None else None,str(payload.get("currency","USD")),_optional_str(payload.get("start_date")),_optional_str(payload.get("end_date")))
                self._json(201,item); return
            if parsed.path == "/sales/prospects":
                self._json(201, self.os.revenue.create_prospect(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"name"), _need(payload,"company_name"), _optional_str(payload.get("contact_email")))); return
            if parsed.path == "/sales/proposals":
                self._json(201, self.os.revenue.create_proposal(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"prospect_id"), _need(payload,"title"), float(_need(payload,"amount")), str(payload.get("currency","USD")), _optional_str(payload.get("valid_until")))); return
            if parsed.path == "/sales/convert":
                self._json(201, self.os.revenue.convert_to_client(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"proposal_id"), str(payload.get("client_name","")), str(payload.get("contract_kind","retainer")), str(payload.get("billing_model","monthly")), _optional_str(payload.get("start_date")), _optional_str(payload.get("end_date")), _need(payload,"idempotency_key"))); return
            if parsed.path == "/report-packs":
                self._json(201, self.os.revenue.request_report_pack(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"note"), _optional_str(payload.get("report_run_id")))); return
            if parsed.path == "/report-packs/approve":
                self._json(200, self.os.revenue.decide_report_pack(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"request_id"), _bool(payload.get("approved"),"approved"), str(payload.get("note","")))); return
            if parsed.path == "/report-packs/deliver-internal":
                self._json(200, self.os.revenue.deliver_report_pack_internal(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"request_id"), str(payload.get("note","")))); return
            if parsed.path == "/campaigns/metrics":
                item=self.os.agency_ops.record_campaign_metrics(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"campaign_id"),_need(payload,"source"),*[_optional_float(payload.get(k)) for k in ("spend","revenue","leads","impressions","clicks")]); self._json(201,item); return
            if parsed.path == "/campaigns/transition":
                self._json(200, self.os.agency_ops.transition_campaign(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"),
                    _need(payload,"person_id"), _need(payload,"campaign_id"),
                    _need(payload,"to_status"), str(payload.get("note", "")),
                )); return
            if parsed.path == "/creative":
                item=self.os.agency_ops.create_creative(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"title"),_need(payload,"format"),_optional_str(payload.get("project_id")),_optional_str(payload.get("campaign_id")),_optional_str(payload.get("platform")),_optional_str(payload.get("dimensions")),[str(x) for x in payload.get("style_tags",[])],_optional_str(payload.get("source_url"))); self._json(201,item); return
            if parsed.path == "/creative/versions":
                self._json(201, self.os.agency_ops.create_creative_version(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"),
                    _need(payload,"person_id"), _need(payload,"asset_id"),
                    _optional_str(payload.get("file_url")), _need(payload,"notes"),
                )); return
            if parsed.path == "/creative/transition":
                self._json(200, self.os.agency_ops.transition_creative(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"),
                    _need(payload,"person_id"), _need(payload,"asset_id"),
                    _need(payload,"to_state"), _need(payload,"note"),
                    _optional_str(payload.get("reviewer_person_id")),
                )); return
            if parsed.path == "/content":
                item=self.os.agency_ops.create_content(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"title"),_need(payload,"objective"),_need(payload,"audience"),str(payload.get("hook","")),str(payload.get("copy","")),_optional_str(payload.get("project_id")),_optional_str(payload.get("channel_id")),[str(x) for x in payload.get("references",[])],str(payload.get("brain_context",""))); self._json(201,item); return
            if parsed.path == "/content/advance":
                self._json(200,self.os.agency_ops.advance_content(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"content_id"),_need(payload,"to_stage"))); return
            if parsed.path == "/finance/costs":
                self._json(201, self.os.agency_ops.record_cost(
                    _need(payload,"organization_id"), _optional_str(payload.get("workspace_id")),
                    _need(payload,"person_id"), float(_need(payload,"amount")), _need(payload,"category"),
                    _need(payload,"incurred_at"), _need(payload,"source"), str(payload.get("currency", "USD")),
                )); return
            if parsed.path == "/finance/revenue":
                self._json(201, self.os.agency_ops.record_revenue(
                    _need(payload,"organization_id"), _optional_str(payload.get("workspace_id")),
                    _need(payload,"person_id"), _number(payload,"amount"), _need(payload,"recognized_at"),
                    _need(payload,"source"), str(payload.get("kind", "retainer")),
                    str(payload.get("currency", "USD")), _optional_str(payload.get("project_id")),
                )); return
            if parsed.path == "/finance/connect":
                self._json(200, self.os.agency_ops.connect_finance(
                    _need(payload,"organization_id"), _need(payload,"person_id"), _need(payload,"provider"),
                )); return
            if parsed.path == "/finance/invoices":
                self._json(201, self.os.agency_ops.record_invoice(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"),
                    _number(payload,"amount"), _need(payload,"issued_at"), _need(payload,"due_at"),
                    _need(payload,"source"), str(payload.get("currency", "USD")),
                    _optional_str(payload.get("external_id")), str(payload.get("status", "issued")),
                )); return
            if parsed.path == "/finance/budgets":
                self._json(201, self.os.agency_ops.record_budget(
                    _need(payload,"organization_id"), _optional_str(payload.get("workspace_id")),
                    _need(payload,"person_id"), float(_need(payload,"amount")), _need(payload,"period_start"),
                    _need(payload,"period_end"), str(payload.get("currency", "USD")),
                    _optional_str(payload.get("project_id")),
                )); return
            if parsed.path == "/finance/software-costs":
                self._json(201, self.os.agency_ops.record_software_cost(
                    _need(payload,"organization_id"), _optional_str(payload.get("workspace_id")),
                    _need(payload,"person_id"), _need(payload,"vendor"), float(_need(payload,"amount")),
                    _need(payload,"period_start"), _need(payload,"source"), str(payload.get("currency", "USD")),
                )); return
            if parsed.path == "/finance/ai-usage-costs":
                self._json(201, self.os.agency_ops.record_ai_usage_cost(
                    _need(payload,"organization_id"), _optional_str(payload.get("workspace_id")),
                    _need(payload,"person_id"), _need(payload,"provider"), _need(payload,"model"),
                    int(_need(payload,"tokens")), float(_need(payload,"amount")), _need(payload,"occurred_at"),
                    _need(payload,"source"), str(payload.get("currency", "USD")),
                    _optional_str(payload.get("agent_id")),
                )); return
            if parsed.path == "/finance/economics/calculate":
                self._json(201, self.os.agency_ops.calculate_client_economics(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"),
                    _need(payload,"person_id"), _need(payload,"period_start"), _need(payload,"period_end"),
                )); return
            if parsed.path == "/approvals":
                item=self.os.agency_ops.request_approval(_need(payload,"organization_id"),_need(payload,"requested_by_type"),_need(payload,"requested_by_id"),_need(payload,"requested_for"),_need(payload,"action_type"),payload.get("payload") or {},_need(payload,"reason"),str(payload.get("policy","human")),_optional_str(payload.get("workspace_id")),_optional_str(payload.get("approver_person_id"))); self._json(201,item); return
            if parsed.path == "/approvals/decide":
                self._json(200,self.os.agency_ops.decide_approval(_need(payload,"organization_id"),_need(payload,"approver_person_id"),_need(payload,"approval_id"),_bool(payload.get("approved"),"approved"),str(payload.get("comments","")))); return
            if parsed.path == "/integrations":
                item=self.os.integrations.configure(identity,_need(payload,"source"),_need(payload,"expected_account_id"),payload.get("workspace_mappings") or {},
                    [str(x) for x in payload.get("permissions",[])])
                self._json(201,item); return
            if parsed.path == "/oauth/begin":
                item = self.os.oauth_service().begin(identity, _need(payload,"organization_id"),
                    _optional_str(payload.get("workspace_id")), _need(payload,"provider"),
                    _need(payload,"client_id"), _need(payload,"redirect_uri"), _need(payload,"scope"),
                    _optional_str(payload.get("installation_id")))
                item.pop("code_verifier", None)
                self._json(200, item); return
            if parsed.path == "/oauth/callback":
                if "code_verifier" in payload:
                    raise ValidationError("code_verifier is not accepted on OAuth callback")
                item = self.os.oauth_service().complete(_need(payload,"state"), _need(payload,"code"),
                    None, _need(payload,"redirect_uri"), _need(payload,"provider"))
                self._json(200, item); return
            if parsed.path == "/oauth/revoke":
                item = self.os.oauth_service().revoke(identity, _need(payload,"installation_id"))
                self._json(200, item); return
            if parsed.path in {"/provider-imports/preview", "/provider-imports/sync"}:
                mappings = payload.get("workspace_mappings") or {}
                provider = _need(payload, "provider")
                adapter = _provider_import_adapter(provider, payload.get("_transport"))
                if parsed.path.endswith("preview"):
                    result = self.os.provider_imports.preview(identity, provider, _need(payload,"account_id"), mappings,
                        _need(payload,"resource"), _optional_str(payload.get("cursor")), adapter)
                else:
                    result = self.os.provider_imports.pull(identity, provider, _need(payload,"account_id"), mappings,
                        _need(payload,"resource"), _optional_str(payload.get("cursor")), adapter)
                self._json(200, result); return
            if parsed.path in {"/operator/pause", "/operator/resume"}:
                scheduler = self.os.scheduler(identity.organization_id, _optional_str(payload.get("workspace_id")), _need(payload, "worker_id"))
                self._json(200, scheduler.set_paused(parsed.path.endswith("pause"))); return
            if parsed.path == "/integrations/credentials":
                item=self.os.integrations.bind_credential(identity,_need(payload,"integration_id"),_need(payload,"name"),
                    _need(payload,"reference"),[str(x) for x in payload.get("scopes",[])])
                self._json(201,item); return
            if parsed.path == "/integrations/verify":
                self._json(200,self.os.integrations.verify(identity,_need(payload,"integration_id"))); return
            if parsed.path == "/integrations/sync":
                integration_id=_need(payload,"integration_id")
                items=self.os.integrations.enqueue_sync(identity,integration_id,int(payload.get("priority",0)),
                    int(payload.get("max_attempts",5)),_optional_str(payload.get("idempotency_key")))
                self._json(202,{"jobs":items}); return
            if parsed.path == "/reports/generate":
                self._json(201,self.os.agent_ops.generate_report(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"type"),_optional_str(payload.get("workspace_id")))); return
            if parsed.path == "/reports/portal-publish":
                assert identity is not None
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(201, self.os.report_delivery.publish(
                    scoped, scoped.organization_id, workspace_id, _need(payload, "report_run_id"),
                    _need(payload, "approval_request_id"), _need(payload, "title"),
                    str(payload.get("reason", "")),
                )); return
            if parsed.path == "/reports/portal-revoke":
                assert identity is not None
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, self.os.report_delivery.revoke(
                    scoped, scoped.organization_id, workspace_id,
                    _need(payload, "portal_report_version_id"), _need(payload, "reason"),
                )); return
            if parsed.path == "/agents/seed":
                self._json(201,{"agents":self.os.agent_ops.seed_primary_agents(_need(payload,"organization_id"),_need(payload,"person_id"))});return
            if parsed.path == "/agents/tasks":
                self._json(201,self.os.agent_ops.enqueue_task(
                    _need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"agent_id"),
                    _need(payload,"title"),_need(payload,"instructions"),_optional_str(payload.get("workspace_id")),
                    int(payload.get("priority",50)),_optional_str_list(payload.get("intent_tags")),
                    _optional_str(payload.get("selected_level")),_optional_str(payload.get("override_reason")) or "",
                ));return
            if parsed.path == "/agents/runs/start":
                self._json(201,self.os.agent_ops.start_run(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"agent_id"),_need(payload,"task_id")));return
            if parsed.path == "/agents/runs/claim":
                item = self.os.agent_ops.claim_next_task(
                    _need(payload,"organization_id"), _need(payload,"person_id"), _need(payload,"agent_id")
                )
                self._json(200, {"run": item}); return
            if parsed.path == "/agents/runs/trace":
                self._json(201, self.os.agent_ops.record_trace(
                    _need(payload,"organization_id"), _need(payload,"agent_id"), _need(payload,"run_id"),
                    _need(payload,"kind"), _need(payload,"message"), payload.get("metadata") or {},
                )); return
            if parsed.path == "/agents/runs/request-review":
                scoped = self.os.auth.scope_identity(identity, _need(payload, "workspace_id"))
                self._json(201, self.os.agent_ops.request_review(
                    scoped.organization_id, scoped.person_id, _need(payload, "agent_id"), _need(payload, "run_id"),
                    str(payload.get("query") or ""), _optional_str(payload.get("runbook_id")),
                    _optional_string_sequence(payload.get("profile_ids"), "profile_ids"), scoped.capabilities,
                )); return
            if parsed.path == "/agents/runs/tool-call":
                self._json(201, self.os.agent_ops.record_tool_call(
                    _need(payload,"organization_id"), _need(payload,"agent_id"), _need(payload,"run_id"),
                    _need(payload,"tool_name"), payload.get("arguments") or {},
                    str(payload.get("result_preview", "")), _optional_str(payload.get("error")),
                )); return
            if parsed.path == "/agents/runs/complete":
                self._json(200,self.os.agent_ops.complete_run(_need(payload,"organization_id"),_need(payload,"agent_id"),_need(payload,"run_id"),str(payload.get("content","")),int(payload.get("input_tokens",0)),int(payload.get("output_tokens",0)),_optional_float(payload.get("cost")),[str(x) for x in payload.get("source_refs",[])]));return
            if parsed.path == "/automations":
                self._json(201,self.os.agent_ops.create_automation(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"name"),_need(payload,"trigger_type"),payload.get("conditions") or [],payload.get("actions") or [],str(payload.get("approval_policy","human"))));return
            if parsed.path == "/automations/trigger":
                self._json(200,{"runs":self.os.agent_ops.trigger_automations(_need(payload,"organization_id"),_need(payload,"trigger_type"),payload.get("payload") or {})});return
            if parsed.path == "/automations/execute-approved":
                self._json(200,self.os.agent_ops.execute_approved_automation_run(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"run_id")));return
            if parsed.path == "/automations/activate":
                self._json(200,self.os.agent_ops.activate_automation(_need(payload,"organization_id"),_need(payload,"person_id"),_need(payload,"automation_id")));return
            if parsed.path == "/memory-proposals":
                assert identity is not None
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(201,self.os.brain_ops.create_proposal(scoped.organization_id,workspace_id,"person",scoped,_need(payload,"kind"),_need(payload,"content"),payload.get("payload") or {},_need(payload,"evidence"),float(payload.get("confidence",0.5)),_optional_str(payload.get("source_id")))); return
            if parsed.path == "/memory-proposals/review":
                # The legacy review endpoint is intentionally retired. Use the
                # authenticated brain promotion route, which enforces workspace
                # scope and append-only proposal decisions.
                raise NotFoundError("legacy proposal review route retired; use brain.promote")
            if parsed.path == "/brain/promote":
                assert identity is not None
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                proposal_id, action = _need(payload, "proposal_id"), _need(payload, "action")
                resolution = self.os.store.conn.execute("SELECT 1 FROM entity_resolution_proposals WHERE organization_id=? AND workspace_id=? AND id=?", (scoped.organization_id,workspace_id,proposal_id)).fetchone()
                if resolution is not None:
                    result = self.os.brain_ops.brain_promote(scoped.organization_id, workspace_id, scoped, proposal_id, action)
                else:
                    result = self.os.brain_ops.brain_promote_fact(scoped, proposal_id, action)
                self._json(200, result); return
            if parsed.path == "/brain/propose":
                assert identity is not None
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                result = self.os.brain_ops.brain_propose(
                    scoped.organization_id, workspace_id, scoped, _need(payload, "kind"),
                    [str(item) for item in payload.get("candidate_entity_ids", [])],
                    float(payload.get("score", 0.0)), _need(payload, "rationale"),
                    _need(payload, "evidence"), _optional_str(payload.get("alias")),
                    _optional_str(payload.get("source_id")), _optional_str(payload.get("target_id")),
                    payload.get("evidence_refs") or {},
                )
                self._json(201, result); return
            if parsed.path == "/brain/conflicts/resolve":
                assert identity is not None
                workspace_id = _need(payload, "workspace_id")
                scoped = self.os.auth.scope_identity(identity, workspace_id)
                self._json(200, self.os.brain_ops.resolve_fact_conflict(scoped, _need(payload,"conflict_group"), _need(payload,"winner_fact_id"))); return
            if parsed.path == "/brain/customizations":
                assert identity is not None
                workspace_id = _optional_str(payload.get("workspace_id"))
                scoped = self.os.auth.scope_identity(identity, workspace_id) if workspace_id else identity
                self._json(201, self.os.brain_customizations.create_version(
                    scoped, _need(payload, "organization_id"), _need(payload, "scope_type"),
                    _need(payload, "kind"), _need(payload, "name"), _need(payload, "body"),
                    payload.get("payload") or {}, workspace_id, str(payload.get("reason", "")),
                )); return
            if parsed.path == "/brain/customizations/activate":
                assert identity is not None
                self._json(200, self.os.brain_customizations.activate_version(
                    identity, _need(payload, "organization_id"), _need(payload, "version_id"),
                    _need(payload, "reason"),
                )); return
            if parsed.path == "/brain/customizations/rollback":
                assert identity is not None
                self._json(200, self.os.brain_customizations.rollback(
                    identity, _need(payload, "organization_id"), _need(payload, "target_version_id"),
                    _need(payload, "reason"),
                )); return
            if parsed.path == "/initiatives":
                self._json(201,self.os.create_initiative(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"project_id"),_need(payload,"name"),str(payload.get("description","")))); return
            if parsed.path == "/deliverables/version":
                item=self.os.add_deliverable_version(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"deliverable_id"),str(payload.get("notes","")),_optional_str(payload.get("file_url")));self._json(201,item.to_dict());return
            if parsed.path == "/reviews/comment":
                item=self.os.add_review_comment(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"review_id"),_need(payload,"body"),_optional_float(payload.get("timestamp_seconds")));self._json(201,item.to_dict());return
            if parsed.path == "/reviews/annotations":
                item = self.os.create_review_annotation(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "review_id"), _need(payload, "annotation_type"), str(payload.get("body", "")),
                    _optional_str(payload.get("source_locator")), payload.get("coordinates") or {},
                    _optional_int(payload.get("page_number")), _optional_float(payload.get("start_seconds")),
                    _optional_float(payload.get("end_seconds")), _optional_str(payload.get("idempotency_key")), payload.get("metadata") or {},
                )
                self._json(201, item); return
            if parsed.path == "/reviews/media":
                item = self.os.register_review_media_contract(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "review_id"), _need(payload, "source_locator"), _need(payload, "media_kind"),
                    payload.get("metadata") or {}, _optional_int(payload.get("width_px")), _optional_int(payload.get("height_px")),
                    _optional_float(payload.get("duration_seconds")), _optional_float(payload.get("frame_rate")), _optional_int(payload.get("page_count")),
                )
                self._json(201, item); return
            if parsed.path == "/assets/backups":
                assert identity is not None
                workspace_id = _optional_str(payload.get("workspace_id"))
                scoped = self.os.auth.scope_identity(identity, workspace_id) if workspace_id else identity
                item = self.os.asset_recovery.register_asset_backup(
                    scoped, _need(payload, "organization_id"), workspace_id, _need(payload, "asset_id"),
                    _need(payload, "backup_manifest_id"), _optional_str(payload.get("target_locator")), payload.get("detail") or {},
                )
                self._json(201, item); return
            if parsed.path == "/assets/backups/status":
                assert identity is not None
                item = self.os.asset_recovery.update_asset_backup_status(
                    identity, _need(payload, "organization_id"), _need(payload, "manifest_id"), _need(payload, "status"), payload.get("detail") or {},
                )
                self._json(200, item); return
            if parsed.path == "/reviews/annotations/resolve":
                item = self.os.resolve_review_annotation(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "annotation_id"), _optional_str(payload.get("idempotency_key")), str(payload.get("note", "")),
                )
                self._json(200, item); return
            if parsed.path == "/reviews/annotations/supersede":
                item = self.os.supersede_review_annotation(
                    _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                    _need(payload, "annotation_id"), _optional_str(payload.get("replacement_annotation_id")), _optional_str(payload.get("idempotency_key")),
                )
                self._json(200, item); return
            if parsed.path == "/work/items":
                item=self.os.work_ops.create(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"title"),_need(payload,"request"),_need(payload,"requested_by"),_optional_str(payload.get("project_id")),_optional_str(payload.get("campaign_id")),_optional_str(payload.get("parent_id")),str(payload.get("priority","normal")),[str(x) for x in payload.get("tags",[])],_optional_float(payload.get("estimate_hours")),_optional_str(payload.get("deadline")),str(payload.get("brief","")),str(payload.get("brain_context","")),_optional_float(payload.get("financial_value")));self._json(201,item.to_dict());return
            if parsed.path == "/work/items/update":
                item=self.os.work_ops.update(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),payload.get("changes") or {});self._json(200,item.to_dict());return
            if parsed.path == "/work/items/transition":
                item=self.os.work_ops.transition(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_need(payload,"to_status"),str(payload.get("reason","")),_optional_int(payload.get("expected_version")),_optional_str(payload.get("idempotency_key")));self._json(200,item);return
            if parsed.path == "/work/items/assign":
                item=self.os.work_ops.assign(
                    _need(payload,"organization_id"), _need(payload,"workspace_id"),
                    _need(payload,"person_id"), _need(payload,"work_item_id"),
                    _need(payload,"assignee_person_id"),
                ); self._json(200,item.to_dict()); return
            if parsed.path == "/work/dependencies":
                self._json(201,self.os.work_ops.add_dependency(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_need(payload,"depends_on_id"),str(payload.get("kind","blocks"))));return
            if parsed.path == "/work/comments":
                self._json(201,self.os.work_ops.add_comment(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_need(payload,"body")));return
            if parsed.path == "/work/time":
                self._json(201,self.os.work_ops.log_time(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_required_dt(payload.get("started_at"),"started_at"),_required_dt(payload.get("ended_at"),"ended_at"),str(payload.get("notes","")),bool(payload.get("billable",True))));return
            if parsed.path == "/workflows/runs":
                template=self.os.workflow_catalog.get(_need(payload,"template_id"))
                item=self.os.workflow_ops.create_run(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),
                    template,_optional_str(payload.get("due_at")),_optional_int(payload.get("sla_minutes")),_optional_str(payload.get("idempotency_key")))
                self._json(201,item); return
            if parsed.path == "/workflows/stages/start":
                item=self.os.workflow_ops.start_stage(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),
                    _need(payload,"run_id"),_need(payload,"stage_id"),_optional_int(payload.get("expected_version")),_optional_str(payload.get("idempotency_key")))
                self._json(200,item); return
            if parsed.path == "/workflows/evidence":
                item=self.os.workflow_ops.submit_evidence(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),
                    _need(payload,"run_id"),_need(payload,"stage_id"),_need(payload,"kind"),_optional_str(payload.get("uri")),
                    _optional_str(payload.get("text")),payload.get("metadata") or {},_optional_str(payload.get("object_type")),
                    _optional_str(payload.get("object_id")),_optional_str(payload.get("locator")),_optional_str(payload.get("content_hash")),
                    _optional_str(payload.get("idempotency_key")))
                self._json(201,item); return
            if parsed.path == "/workflows/approvals/request":
                item=self.os.workflow_ops.request_approval(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),
                    _need(payload,"run_id"),_need(payload,"stage_id"),_need(payload,"reason"),_optional_str(payload.get("approval_request_id")),
                    _optional_int(payload.get("expected_version")),_optional_str(payload.get("idempotency_key")))
                self._json(200,item); return
            if parsed.path == "/workflows/approvals/decide":
                item=self.os.workflow_ops.decide_approval(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),
                    _need(payload,"run_id"),_need(payload,"stage_id"),_need(payload,"decision"),_need(payload,"reason"),
                    _optional_str(payload.get("approval_request_id")),_optional_str(payload.get("idempotency_key")))
                self._json(200,item); return
            if parsed.path == "/workflows/handoffs/acknowledge":
                item=self.os.workflow_ops.acknowledge_handoff(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),
                    _need(payload,"run_id"),_need(payload,"from_stage_id"),_need(payload,"to_stage_id"),_need(payload,"artifact_contract"),
                    str(payload.get("reason","")),_optional_str(payload.get("idempotency_key")))
                self._json(201,item); return
            if parsed.path == "/workflows/stages/complete":
                item=self.os.workflow_ops.complete_stage(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),
                    _need(payload,"run_id"),_need(payload,"stage_id"),str(payload.get("reason","")),
                    _optional_int(payload.get("expected_version")),_optional_str(payload.get("idempotency_key")))
                self._json(200,item); return
            if parsed.path == "/workflows/stages/block":
                item=self.os.workflow_ops.block_stage(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),
                    _need(payload,"run_id"),_need(payload,"stage_id"),_need(payload,"reason"),_optional_int(payload.get("expected_version")))
                self._json(200,item); return
            if parsed.path == "/workflows/runs/cancel":
                item=self.os.workflow_ops.cancel_run(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),
                    _need(payload,"run_id"),_need(payload,"reason"),_optional_int(payload.get("expected_version")))
                self._json(200,item); return
            if parsed.path == "/feedback/record":
                assert identity is not None
                org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
                self._json(200, self.os.feedback.record_feedback(
                    org, ws, person_id, _need(payload, "category"), _need(payload, "raw_feedback"),
                    _need(payload, "source_type"), _optional_str(payload.get("source_id"))
                )); return
            if parsed.path == "/feedback/patterns/promote":
                assert identity is not None
                org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
                self._json(200, self.os.feedback.promote_pattern(org, ws, person_id, _need(payload, "pattern_id"))); return
            if parsed.path == "/feedback/patterns/decide":
                assert identity is not None
                org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
                self._json(200, self.os.feedback.decide_pattern(
                    org, ws, person_id, _need(payload, "pattern_id"), _need(payload, "decision")
                )); return
            if parsed.path == "/insights/performance/generate":
                assert identity is not None
                org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
                self._json(200, self.os.performance.generate_insights(org, ws, person_id, _optional_str(payload.get("insight_type")))); return
            if parsed.path == "/insights/performance/decide":
                assert identity is not None
                org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
                self._json(200, self.os.performance.decide_insight(
                    org, ws, person_id, _need(payload, "insight_id"), _need(payload, "decision")
                )); return
            if parsed.path == "/forecasts/generate":
                assert identity is not None
                org, person_id = identity.organization_id, identity.person_id
                self._json(200, self.os.forecasts.generate_forecasts(org, person_id, _optional_str(payload.get("forecast_type")))); return
            if parsed.path == "/retention/policies":
                assert identity is not None
                org, person_id = identity.organization_id, identity.person_id
                self._json(200, self.os.retention.create_policy(
                    org, person_id, _need(payload, "scope"), _need(payload, "data_category"),
                    _int(payload.get("max_age_days"), "max_age_days"), _need(payload, "action"),
                    _optional_str(payload.get("scope_id"))
                )); return
            if parsed.path == "/retention/execute":
                assert identity is not None
                org, person_id = identity.organization_id, identity.person_id
                self._json(200, self.os.retention.execute_deletion(
                    org, person_id, _need(payload, "table_name"), [str(item) for item in payload.get("record_ids", [])],
                    _need(payload, "reason"), _optional_str(payload.get("policy_id"))
                )); return
            work_action = {
                "/work/capture": "capture_work",
                "/work/capture_work": "capture_work",
                "/work/assign": "assign_work",
                "/work/assign_work": "assign_work",
                "/work/start": "start_work",
                "/work/start_work": "start_work",
                "/work/dod": "mark_dod",
                "/work/mark-dod": "mark_dod",
                "/work/mark_dod": "mark_dod",
                "/work/submit-review": "submit_review",
                "/work/submit_review": "submit_review",
                "/work/close-review": "close_review",
                "/work/close_review": "close_review",
                "/work/ship": "ship_work",
                "/work/ship_work": "ship_work",
            }.get(parsed.path)
            if work_action:
                self._json(200, self._call_work_action(work_action, payload))
                return
            self._json(404, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)

    def _receive_provider_webhook(self, path: str) -> None:
        """Receive a bounded, HMAC-authenticated provider event without bearer auth."""
        from auremgrid.domain.security import AuthenticatedIdentity
        from auremgrid.observability import get_metrics
        from auremgrid.services.integration_security import WebhookIntakeService

        if os.environ.get("AUREMGRID_WEBHOOK_RECEIPTS_ENABLED") != "1":
            get_metrics().inc("webhook.receipt.disabled")
            self._json(404, {"error": "webhook_receipts_disabled"})
            return
        installation_id = path.removeprefix("/webhooks/provider/").strip()
        webhooks = WebhookIntakeService(self.os.store.conn, self.os.jobs.new_id)
        if not installation_id or "/" in installation_id or len(installation_id) > 128:
            get_metrics().inc("webhook.receipt.rejected")
            webhooks.quarantine(installation_id, b"", self.headers.get("X-Webhook-Signature", ""), "invalid_path",
                                self.headers.get("X-Provider-Event-ID"))
            self._json(404, {"error": "webhook_not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1_048_576:
            get_metrics().inc("webhook.receipt.rejected")
            webhooks.quarantine(
                installation_id, b"", self.headers.get("X-Webhook-Signature", ""),
                "payload_too_large", self.headers.get("X-Provider-Event-ID"),
                {"content_length": length},
            )
            self._json(413, {"error": "webhook_payload_too_large"})
            return
        body = self.rfile.read(length) if length else b""
        row = self.os.store.conn.execute(
            "SELECT organization_id,workspace_id,status FROM provider_installations WHERE id=?",
            (installation_id,),
        ).fetchone()
        if row is None:
            get_metrics().inc("webhook.receipt.rejected")
            webhooks.quarantine(
                installation_id, body, self.headers.get("X-Webhook-Signature", ""),
                "unknown_installation", self.headers.get("X-Provider-Event-ID"),
            )
            self._json(404, {"error": "webhook_not_found"})
            return
        if row["status"] != "active":
            get_metrics().inc("webhook.receipt.rejected")
            webhooks.quarantine(
                installation_id, body, self.headers.get("X-Webhook-Signature", ""),
                "inactive_installation", self.headers.get("X-Provider-Event-ID"),
                {"status": row["status"]},
            )
            self._json(404, {"error": "webhook_not_found"})
            return
        identity = AuthenticatedIdentity(
            f"webhook:{installation_id}", row["organization_id"], f"webhook:{installation_id}",
            "webhook", frozenset({"integration_sync"}), workspace_id=row["workspace_id"],
        )
        try:
            result = webhooks.receive(
                identity,
                installation_id,
                body,
                self.headers.get("X-Webhook-Signature", ""),
                provider_event_id=self.headers.get("X-Provider-Event-ID"),
                timestamp=self.headers.get("X-Webhook-Timestamp"),
            )
        except Exception as exc:
            get_metrics().inc("webhook.receipt.rejected")
            if isinstance(exc, (AuthorizationError, NotFoundError, ValidationError)):
                self._json(401 if isinstance(exc, AuthorizationError) else 400, {"error": "webhook_rejected"})
                return
            raise
        if result.get("duplicate"):
            get_metrics().inc("webhook.receipt.duplicate")
            self._json(200, {"status": "duplicate", "event_digest": result["event_digest"]})
            return
        get_metrics().inc("webhook.receipt.accepted")
        self._json(202, {"status": "accepted", "event_digest": result["event_digest"]})

    def _call_work_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        workspace_id = _need(payload, "workspace_id")
        actor_id = _need(payload, "actor_id")
        if action == "capture_work":
            item = self.os.capture_work(
                workspace_id,
                actor_id,
                _need(payload, "title"),
                _need(payload, "request"),
                _need(payload, "requested_by"),
                needed_by=_optional_str(payload.get("needed_by")),
                playbook_id=_optional_str(payload.get("playbook_id")),
                decision_maker=_optional_str(payload.get("decision_maker")),
            )
        elif action == "assign_work":
            item = self.os.assign_work(
                workspace_id,
                actor_id,
                _need(payload, "work_item_id"),
                _need(payload, "assignee_id"),
                decision_maker=_optional_str(payload.get("decision_maker")),
            )
        elif action == "start_work":
            item = self.os.start_work(workspace_id, actor_id, _need(payload, "work_item_id"))
        elif action == "mark_dod":
            checks = payload.get("checks")
            if not isinstance(checks, dict):
                raise ValidationError("checks must be an object")
            item = self.os.mark_dod(
                workspace_id,
                actor_id,
                _need(payload, "work_item_id"),
                {str(key): _bool(value, f"checks.{key}") for key, value in checks.items()},
            )
        elif action == "submit_review":
            item = self.os.submit_review(workspace_id, actor_id, _need(payload, "work_item_id"))
        elif action == "close_review":
            item = self.os.close_review(
                workspace_id,
                actor_id,
                _need(payload, "work_item_id"),
                _bool(payload.get("approved"), "approved"),
                note=str(payload.get("note", "")),
            )
        elif action == "ship_work":
            item = self.os.ship_work(
                workspace_id,
                actor_id,
                _need(payload, "work_item_id"),
                note=str(payload.get("note", "")),
            )
        else:
            raise ValidationError(f"unknown work action: {action}")
        return item.to_dict()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("request body must be a JSON object")
        return payload

    def _authenticate_request(
        self, path: str, method: str, payload: dict[str, Any], required_capability: str | None = None
    ) -> AuthenticatedIdentity:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise AuthenticationError("authentication required")
        token = header[7:].strip()
        organization_id = _optional_str(payload.get("organization_id"))
        workspace_id = _optional_str(payload.get("workspace_id"))
        identity = self.os.auth.authenticate_bearer(token, organization_id)
        if workspace_id:
            identity = self.os.auth.scope_identity(identity, workspace_id)
        capability = required_capability or _route_capability(path, method)
        identity.require(capability)
        supplied_person = _optional_str(payload.get("person_id"))
        if supplied_person and supplied_person != identity.person_id:
            raise AuthorizationError("caller identity does not match person_id")
        payload["organization_id"] = identity.organization_id
        payload["person_id"] = identity.person_id
        if path in LEGACY_ACTOR_PATHS:
            if not workspace_id:
                raise ValidationError("workspace_id is required")
            actor_id = self.os.auth.actor_for_identity(identity, workspace_id)
            supplied_actor = _optional_str(payload.get("actor_id"))
            if supplied_actor and supplied_actor != actor_id:
                raise AuthorizationError("caller identity does not match actor_id")
            payload["actor_id"] = actor_id
        return identity

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _dashboard_asset(self, relative_path: str) -> None:
        root = Path(__file__).with_name("dashboard").resolve()
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise NotFoundError("dashboard asset not found") from exc
        if not candidate.is_file():
            raise NotFoundError("dashboard asset not found")
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        payload = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith(("text/", "application/javascript")) else content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, AuthenticationError):
            self._json(401, {"error": "authentication_error", "message": "authentication failed"})
            return
        if isinstance(exc, ValidationError):
            self._json(400, {"error": "validation_error", "message": str(exc)})
            return
        if isinstance(exc, AuthorizationError):
            self._json(403, {"error": "authorization_error", "message": str(exc)})
            return
        if isinstance(exc, NotFoundError):
            self._json(404, {"error": "not_found", "message": str(exc)})
            return
        if isinstance(exc, AuremgridError):
            self._json(400, {"error": "auremgrid_error", "message": str(exc)})
            return
        self._json(500, {"error": "internal_error", "message": str(exc)})


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


def serve(os: CompanyOS, host: str = "127.0.0.1", port: int = 8787) -> HTTPServer:
    handler = type(
        "BoundHandler",
        (CompanyOSRequestHandler,),
        {"os": os},
    )
    # A single request loop deliberately serializes the shared SQLite connection.
    # Durable workers use their own connections and leases rather than HTTP threads.
    return HTTPServer((host, port), handler)


def _dashboard_html() -> str:
    path = Path(__file__).with_name("dashboard") / "index.html"
    return path.read_text(encoding="utf-8")
