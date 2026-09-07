"""Agency operations, creative, campaigns, finance, and approvals route mixin."""
from __future__ import annotations

from typing import Any

from auremgrid.api.http_shared import (
    _bool,
    _float,
    _int,
    _need,
    _number,
    _optional_dt,
    _optional_float,
    _optional_int,
    _optional_str,
    _required_dt,
)
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


class HttpRoutesAgencyOpsMixin:
    def _get_agency_ops(self, parsed: Any, params: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/finance":
            self._json(200, self.os.agency_ops.finance_status(_need(params, "organization_id"), _need(params, "person_id"), params.get("workspace_id")))
            return True
        if parsed.path == "/campaigns/detail":
            self._json(200, self.os.agency_ops.campaign_detail(_need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id"), _need(params, "campaign_id")))
            return True
        if parsed.path == "/creative/detail":
            self._json(200, self.os.agency_ops.creative_detail(_need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id"), _need(params, "asset_id")))
            return True
        if parsed.path == "/notifications":
            self._json(200, {"notifications": self.os.agency_ops.attention(_need(params, "organization_id"), _need(params, "person_id"), _int(params.get("limit", 20), "limit"))})
            return True
        return False

    def _post_agency_ops(self, parsed: Any, payload: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/campaigns":
            item = self.os.agency_ops.create_campaign(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "name"), _need(payload, "objective"), _need(payload, "platform"),
                _optional_str(payload.get("project_id")), float(payload["budget"]) if payload.get("budget") is not None else None,
                str(payload.get("currency", "USD")), _optional_str(payload.get("start_date")), _optional_str(payload.get("end_date")),
            )
            self._json(201, item)
            return True
        if parsed.path == "/campaigns/metrics":
            item = self.os.agency_ops.record_campaign_metrics(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "campaign_id"), _need(payload, "source"),
                *[_optional_float(payload.get(k)) for k in ("spend", "revenue", "leads", "impressions", "clicks")],
            )
            self._json(201, item)
            return True
        if parsed.path == "/campaigns/transition":
            self._json(200, self.os.agency_ops.transition_campaign(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "campaign_id"), _need(payload, "to_status"), str(payload.get("note", "")),
            ))
            return True
        if parsed.path == "/creative":
            item = self.os.agency_ops.create_creative(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "title"), _need(payload, "format"), _optional_str(payload.get("project_id")),
                _optional_str(payload.get("campaign_id")), _optional_str(payload.get("platform")),
                _optional_str(payload.get("dimensions")), [str(x) for x in payload.get("style_tags", [])],
                _optional_str(payload.get("source_url")),
            )
            self._json(201, item)
            return True
        if parsed.path == "/creative/versions":
            self._json(201, self.os.agency_ops.create_creative_version(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "asset_id"), _optional_str(payload.get("file_url")), _need(payload, "notes"),
            ))
            return True
        if parsed.path == "/creative/transition":
            self._json(200, self.os.agency_ops.transition_creative(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "asset_id"), _need(payload, "to_state"), _need(payload, "note"),
                _optional_str(payload.get("reviewer_person_id")),
            ))
            return True
        if parsed.path == "/content":
            item = self.os.agency_ops.create_content(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "title"), _need(payload, "objective"), _need(payload, "audience"),
                str(payload.get("hook", "")), str(payload.get("copy", "")), _optional_str(payload.get("project_id")),
                _optional_str(payload.get("channel_id")), [str(x) for x in payload.get("references", [])],
                str(payload.get("brain_context", "")),
            )
            self._json(201, item)
            return True
        if parsed.path == "/content/advance":
            self._json(200, self.os.agency_ops.advance_content(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "content_id"), _need(payload, "to_stage"),
            ))
            return True
        if parsed.path == "/finance/costs":
            self._json(201, self.os.agency_ops.record_cost(
                _need(payload, "organization_id"), _optional_str(payload.get("workspace_id")),
                _need(payload, "person_id"), float(_need(payload, "amount")), _need(payload, "category"),
                _need(payload, "incurred_at"), _need(payload, "source"), str(payload.get("currency", "USD")),
            ))
            return True
        if parsed.path == "/finance/revenue":
            self._json(201, self.os.agency_ops.record_revenue(
                _need(payload, "organization_id"), _optional_str(payload.get("workspace_id")),
                _need(payload, "person_id"), _number(payload, "amount"), _need(payload, "recognized_at"),
                _need(payload, "source"), str(payload.get("kind", "retainer")),
                str(payload.get("currency", "USD")), _optional_str(payload.get("project_id")),
            ))
            return True
        if parsed.path == "/finance/connect":
            self._json(200, self.os.agency_ops.connect_finance(
                _need(payload, "organization_id"), _need(payload, "person_id"), _need(payload, "provider"),
            ))
            return True
        if parsed.path == "/finance/invoices":
            self._json(201, self.os.agency_ops.record_invoice(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _number(payload, "amount"), _need(payload, "issued_at"), _need(payload, "due_at"),
                _need(payload, "source"), str(payload.get("currency", "USD")),
                _optional_str(payload.get("external_id")), str(payload.get("status", "issued")),
            ))
            return True
        if parsed.path == "/finance/budgets":
            self._json(201, self.os.agency_ops.record_budget(
                _need(payload, "organization_id"), _optional_str(payload.get("workspace_id")),
                _need(payload, "person_id"), float(_need(payload, "amount")), _need(payload, "period_start"),
                _need(payload, "period_end"), str(payload.get("currency", "USD")),
                _optional_str(payload.get("project_id")),
            ))
            return True
        if parsed.path == "/finance/software-costs":
            self._json(201, self.os.agency_ops.record_software_cost(
                _need(payload, "organization_id"), _optional_str(payload.get("workspace_id")),
                _need(payload, "person_id"), _need(payload, "vendor"), float(_need(payload, "amount")),
                _need(payload, "period_start"), _need(payload, "source"), str(payload.get("currency", "USD")),
            ))
            return True
        if parsed.path == "/finance/ai-usage-costs":
            self._json(201, self.os.agency_ops.record_ai_usage_cost(
                _need(payload, "organization_id"), _optional_str(payload.get("workspace_id")),
                _need(payload, "person_id"), _need(payload, "provider"), _need(payload, "model"),
                int(_need(payload, "tokens")), float(_need(payload, "amount")), _need(payload, "occurred_at"),
                _need(payload, "source"), str(payload.get("currency", "USD")),
                _optional_str(payload.get("agent_id")),
            ))
            return True
        if parsed.path == "/finance/economics/calculate":
            self._json(201, self.os.agency_ops.calculate_client_economics(
                _need(payload, "organization_id"), _need(payload, "workspace_id"),
                _need(payload, "person_id"), _need(payload, "period_start"), _need(payload, "period_end"),
            ))
            return True
        if parsed.path == "/approvals":
            item = self.os.agency_ops.request_approval(
                _need(payload, "organization_id"), _need(payload, "requested_by_type"), _need(payload, "requested_by_id"),
                _need(payload, "requested_for"), _need(payload, "action_type"), payload.get("payload") or {},
                _need(payload, "reason"), str(payload.get("policy", "human")),
                _optional_str(payload.get("workspace_id")), _optional_str(payload.get("approver_person_id")),
            )
            self._json(201, item)
            return True
        if parsed.path == "/approvals/decide":
            self._json(200, self.os.agency_ops.decide_approval(
                _need(payload, "organization_id"), _need(payload, "approver_person_id"),
                _need(payload, "approval_id"), _bool(payload.get("approved"), "approved"), str(payload.get("comments", "")),
            ))
            return True
        return False

