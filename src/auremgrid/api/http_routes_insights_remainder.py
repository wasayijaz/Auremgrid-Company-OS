"""Insights, performance, forecasts, retention, and remaining operational routes mixin."""
from __future__ import annotations

from typing import Any

from auremgrid.api.http_shared import (
    _bool,
    _int,
    _need,
    _number,
    _optional_float,
    _optional_int,
    _optional_str,
)
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.job_types import PUBLIC_JOB_TYPES


class HttpRoutesInsightsRemainderMixin:
    def _get_insights_remainder(self, parsed: Any, params: dict[str, Any], identity: Any) -> bool:
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
            self._json(200,{parsed.path[1:]:result}); return True
        if parsed.path == "/sales/prospects":
            self._json(200, {"prospects": self.os.revenue.list_prospects(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"), params.get("status"))}); return True
        if parsed.path == "/sales/proposals":
            self._json(200, {"proposals": self.os.revenue.list_proposals(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"), params.get("status"))}); return True
        if parsed.path == "/campaigns/budget-pacing":
            self._json(200, {"signals": self.os.revenue.campaign_budget_pacing(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"))}); return True
        if parsed.path == "/client-hq/retainer":
            self._json(200, self.os.revenue.retainer_read_model(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"))); return True
        if parsed.path == "/report-packs":
            self._json(200, {"requests": self.os.revenue.list_report_packs(_need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id"))}); return True
        if parsed.path == "/health/explain":
            self._json(200, self.os.client_ops.explain_health(
                _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
            )); return True
        if parsed.path == "/risks/detail":
            self._json(200, self.os.client_ops.risk_detail(
                _need(params,"organization_id"), _need(params,"workspace_id"),
                _need(params,"person_id"), _need(params,"risk_id")
            )); return True
        if parsed.path == "/opportunities/detail":
            self._json(200, self.os.client_ops.opportunity_detail(
                _need(params,"organization_id"), _need(params,"workspace_id"),
                _need(params,"person_id"), _need(params,"opportunity_id")
            )); return True
        if parsed.path == "/scope/status":
            self._json(200, self.os.client_ops.scope_status(
                _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
            )); return True
        if parsed.path in {"/assets", "/asset-registry"}:
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"assets": self.os.asset_recovery.list_assets(
                scoped, scoped.organization_id, workspace_id,
                _optional_str(params.get("status")),
                _optional_str(params.get("retention_class")),
                _int(params.get("limit", "100"), "limit"),
            )}); return True
        if parsed.path in {"/assets/detail", "/asset-registry/detail"}:
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, self.os.asset_recovery.asset_detail(
                scoped, scoped.organization_id, workspace_id, _need(params, "asset_id")
            )); return True
        if parsed.path == "/assets/backups":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"backups": self.os.asset_recovery.list_asset_backups(scoped, scoped.organization_id, workspace_id, _optional_str(params.get("asset_id")))})
            return True
        if parsed.path == "/dashboard/performance":
            self._json(200, self.os.dashboard.performance_surface(
                _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            )); return True
        if parsed.path == "/jobs":
            items=self.os.jobs.list_jobs(_need(params,"organization_id"),_optional_str(params.get("workspace_id")),params.get("status"))
            self._json(200,{"jobs":items}); return True
        if parsed.path == "/jobs/get":
            organization_id,workspace_id=_need(params,"organization_id"),_optional_str(params.get("workspace_id")); job_id=_need(params,"job_id")
            self._json(200,{"job":self.os.jobs.get_job(organization_id,workspace_id,job_id),
                "events":self.os.jobs.job_events(organization_id,workspace_id,job_id)}); return True
        if parsed.path == "/feedback/patterns":
            assert identity is not None
            org, ws, person_id = identity.organization_id, _need(params, "workspace_id"), identity.person_id
            self._json(200, self.os.feedback.list_patterns(org, ws, person_id, params.get("category"), params.get("status"))); return True
        if parsed.path == "/insights/performance":
            assert identity is not None
            org, ws, person_id = identity.organization_id, _need(params, "workspace_id"), identity.person_id
            self._json(200, self.os.performance.list_insights(org, ws, person_id, params.get("status"), params.get("insight_type"))); return True
        if parsed.path == "/forecasts":
            assert identity is not None
            org, person_id = identity.organization_id, identity.person_id
            self._json(200, self.os.forecasts.list_forecasts(org, person_id, params.get("forecast_type"), params.get("status"))); return True
        if parsed.path == "/retention/policies":
            assert identity is not None
            org, person_id = identity.organization_id, identity.person_id
            self._json(200, self.os.retention.list_policies(org, person_id, params.get("scope"))); return True
        if parsed.path == "/export/workspace":
            assert identity is not None
            org, ws, person_id = identity.organization_id, _need(params, "workspace_id"), identity.person_id
            self._json(200, self.os.retention.export_workspace(org, ws, person_id)); return True
        return False

    def _post_insights_remainder(self, parsed: Any, payload: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/jobs":
            job_type=_need(payload,"type")
            if job_type not in PUBLIC_JOB_TYPES: raise ValidationError("unsupported job type")
            item=self.os.jobs.enqueue_job(identity.organization_id,_optional_str(payload.get("workspace_id")),identity.principal_id,job_type,
                payload.get("payload") or {},int(payload.get("priority",0)),int(payload.get("max_attempts",3)),
                _optional_str(payload.get("available_at")),_optional_str(payload.get("idempotency_key")))
            self._json(201,item); return True
        if parsed.path == "/jobs/cancel":
            item=self.os.jobs.cancel_job(identity.organization_id,_optional_str(payload.get("workspace_id")),_need(payload,"job_id"),
                _need(payload,"reason"),identity.principal_id,_optional_int(payload.get("expected_version")))
            if item["type"]=="connector.sync": self.os.integrations.release_job_stream(item["id"])
            self._json(200,item); return True
        if parsed.path == "/decisions":
            item = self.os.create_decision(
                _need(payload, "organization_id"), _need(payload, "person_id"),
                _need(payload, "statement"), _need(payload, "rationale"),
                _optional_str(payload.get("workspace_id")), _optional_str(payload.get("project_id")),
                _optional_str(payload.get("source_id")), str(payload.get("evidence", "")),
                [str(value) for value in payload.get("tags", [])],
            )
            self._json(201, item.to_dict())
            return True
        if parsed.path == "/signals":
            item=self.os.client_ops.create_signal(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"type"),_need(payload,"source_type"),_need(payload,"evidence"),_optional_str(payload.get("source_id")),float(payload.get("confidence",1)))
            self._json(201,item.to_dict()); return True
        if parsed.path == "/signals/route":
            self._json(200,self.os.client_ops.route_signal(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"signal_id"),_need(payload,"destination"))); return True
        if parsed.path == "/risks":
            item=self.os.client_ops.create_risk(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"type"),_need(payload,"severity"),float(payload.get("probability",0.5)),_need(payload,"impact"),_need(payload,"evidence"),_need(payload,"recommended_action"),_optional_str(payload.get("project_id")))
            self._json(201,item.to_dict()); return True
        if parsed.path == "/risks/resolve":
            self._json(200, self.os.client_ops.resolve_risk(
                _need(payload,"organization_id"), _need(payload,"workspace_id"),
                _need(payload,"person_id"), _need(payload,"risk_id"), _need(payload,"resolution")
            )); return True
        if parsed.path == "/risks/reopen":
            self._json(200, self.os.client_ops.reopen_risk(
                _need(payload,"organization_id"), _need(payload,"workspace_id"),
                _need(payload,"person_id"), _need(payload,"risk_id"), _need(payload,"reason")
            )); return True
        if parsed.path == "/opportunities":
            item=self.os.client_ops.create_opportunity(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"type"),_need(payload,"reason"),_need(payload,"evidence"),_need(payload,"recommendation"),float(payload["estimated_value"]) if payload.get("estimated_value") is not None else None)
            self._json(201,item.to_dict()); return True
        if parsed.path == "/opportunities/advance":
            self._json(200, self.os.client_ops.advance_opportunity(
                _need(payload,"organization_id"), _need(payload,"workspace_id"),
                _need(payload,"person_id"), _need(payload,"opportunity_id"),
                _need(payload,"to_status"), str(payload.get("note", "")),
            )); return True
        if parsed.path == "/opportunities/close":
            self._json(200, self.os.client_ops.close_opportunity(
                _need(payload,"organization_id"), _need(payload,"workspace_id"),
                _need(payload,"person_id"), _need(payload,"opportunity_id"),
                _need(payload,"outcome"), _need(payload,"note"),
            )); return True
        if parsed.path == "/health/calculate":
            item=self.os.client_ops.calculate_health(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id")); self._json(201,item.to_dict()); return True
        if parsed.path == "/sales/prospects":
            self._json(201, self.os.revenue.create_prospect(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"name"), _need(payload,"company_name"), _optional_str(payload.get("contact_email")))); return True
        if parsed.path == "/sales/proposals":
            self._json(201, self.os.revenue.create_proposal(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"prospect_id"), _need(payload,"title"), float(_need(payload,"amount")), str(payload.get("currency","USD")), _optional_str(payload.get("valid_until")))); return True
        if parsed.path == "/sales/convert":
            self._json(201, self.os.revenue.convert_to_client(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"proposal_id"), str(payload.get("client_name","")), str(payload.get("contract_kind","retainer")), str(payload.get("billing_model","monthly")), _optional_str(payload.get("start_date")), _optional_str(payload.get("end_date")), _need(payload,"idempotency_key"))); return True
        if parsed.path == "/report-packs":
            self._json(201, self.os.revenue.request_report_pack(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"note"), _optional_str(payload.get("report_run_id")))); return True
        if parsed.path == "/report-packs/approve":
            self._json(200, self.os.revenue.decide_report_pack(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"request_id"), _bool(payload.get("approved"),"approved"), str(payload.get("note","")))); return True
        if parsed.path == "/report-packs/deliver-internal":
            self._json(200, self.os.revenue.deliver_report_pack_internal(_need(payload,"organization_id"), _need(payload,"workspace_id"), _need(payload,"person_id"), _need(payload,"request_id"), str(payload.get("note","")))); return True
        if parsed.path == "/feedback/record":
            assert identity is not None
            org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
            self._json(200, self.os.feedback.record_feedback(
                org, ws, person_id, _need(payload, "category"), _need(payload, "raw_feedback"),
                _need(payload, "source_type"), _optional_str(payload.get("source_id"))
            )); return True
        if parsed.path == "/feedback/patterns/promote":
            assert identity is not None
            org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
            self._json(200, self.os.feedback.promote_pattern(org, ws, person_id, _need(payload, "pattern_id"))); return True
        if parsed.path == "/feedback/patterns/decide":
            assert identity is not None
            org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
            self._json(200, self.os.feedback.decide_pattern(
                org, ws, person_id, _need(payload, "pattern_id"), _need(payload, "decision")
            )); return True
        if parsed.path == "/insights/performance/generate":
            assert identity is not None
            org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
            self._json(200, self.os.performance.generate_insights(org, ws, person_id, _optional_str(payload.get("insight_type")))); return True
        if parsed.path == "/insights/performance/decide":
            assert identity is not None
            org, ws, person_id = identity.organization_id, _need(payload, "workspace_id"), identity.person_id
            self._json(200, self.os.performance.decide_insight(
                org, ws, person_id, _need(payload, "insight_id"), _need(payload, "decision")
            )); return True
        if parsed.path == "/forecasts/generate":
            assert identity is not None
            org, person_id = identity.organization_id, identity.person_id
            self._json(200, self.os.forecasts.generate_forecasts(org, person_id, _optional_str(payload.get("forecast_type")))); return True
        if parsed.path == "/retention/policies":
            assert identity is not None
            org, person_id = identity.organization_id, identity.person_id
            self._json(200, self.os.retention.create_policy(
                org, person_id, _need(payload, "scope"), _need(payload, "data_category"),
                _int(payload.get("max_age_days"), "max_age_days"), _need(payload, "action"),
                _optional_str(payload.get("scope_id"))
            )); return True
        if parsed.path == "/retention/execute":
            assert identity is not None
            org, person_id = identity.organization_id, identity.person_id
            self._json(200, self.os.retention.execute_deletion(
                org, person_id, _need(payload, "table_name"), [str(item) for item in payload.get("record_ids", [])],
                _need(payload, "reason"), _optional_str(payload.get("policy_id"))
            )); return True
        return False
