from __future__ import annotations

from typing import Any

from auremgrid.api.http_shared import (
    _bool, _int, _need, _number, _optional_dt, _optional_float, _optional_int, _optional_str, _required_dt,
)
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


class HttpRoutesWorkDeliveryMixin:
    def _get_work_delivery(self, parsed: Any, params: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/work":
            items = self.os.list_work(
                _need(params, "workspace_id"),
                _need(params, "actor_id"),
                open_only=params.get("open_only", "1") != "0",
            )
            self._json(200, {"work": [item.to_dict() for item in items]})
            return True
        if parsed.path == "/capacity":
            assert identity is not None
            self._json(200, self.os.capacity.weekly_board(
                identity.organization_id,
                identity.person_id,
                _need(params, "week_start"),
                _optional_str(params.get("workspace_id")),
                as_of=_optional_dt(params.get("as_of")),
            ))
            return True
        if parsed.path == "/projects":
            items = self.os.list_projects(
                _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            )
            self._json(200, {"projects": [item.to_dict() for item in items]})
            return True
        if parsed.path == "/projects/get":
            organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
            self.os._require_person_access(organization_id,workspace_id,person_id);item=self.os.company.get_project(workspace_id,_need(params,"project_id"))
            if item is None:raise NotFoundError("project not found")
            self._json(200,item.to_dict());return True
        if parsed.path == "/deliverables":
            organization_id,workspace_id,person_id=_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id")
            self.os._require_person_access(organization_id,workspace_id,person_id);items=self.os.company.list_deliverables(workspace_id,params.get("project_id"))
            self._json(200,{"deliverables":[item.to_dict() for item in items]});return True
        if parsed.path == "/reviews":
            organization_id, workspace_id, person_id = (
                _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            )
            self.os._require_person_access(organization_id, workspace_id, person_id)
            items = self.os.company.list_reviews(workspace_id, params.get("status"))
            self._json(200, {"reviews": [item.to_dict() for item in items]})
            return True
        if parsed.path == "/reviews/annotations":
            organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            self._json(200, {"annotations": self.os.list_review_annotations(organization_id, workspace_id, person_id, params.get("review_id"), params.get("include_closed", "1") != "0")})
            return True
        if parsed.path == "/reviews/media":
            organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            self._json(200, {"media": self.os.list_review_media_contracts(organization_id, workspace_id, person_id, _need(params, "review_id"))})
            return True
        if parsed.path == "/clients/roster":
            organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            self.os._require_person_access(organization_id, workspace_id, person_id)
            result = self.os.client_ops.get_client_roster(
                organization_id, workspace_id, person_id, _optional_str(params.get("roster_id")),
                as_of=_optional_dt(params.get("as_of")),
            )
            if result is None:
                raise NotFoundError("client roster not found")
            self._json(200, result); return True
        if parsed.path == "/meetings/responsibilities":
            organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            self.os._require_person_access(organization_id, workspace_id, person_id)
            self._json(200, self.os.client_ops.get_meeting_responsibilities(
                organization_id, workspace_id, person_id, _need(params, "meeting_id"),
                as_of=_optional_dt(params.get("as_of")),
            )); return True
        if parsed.path == "/decisions":
            organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
            workspace_id = params.get("workspace_id")
            if workspace_id:
                self.os._require_person_access(organization_id, workspace_id, person_id)
            elif self.os.company.org_membership(organization_id, person_id) is None:
                raise AuthorizationError("person is not an organization member")
            items = self.os.company.list_decisions(organization_id, workspace_id)
            self._json(200, {"decisions": [item.to_dict() for item in items]})
            return True
        if parsed.path == "/dashboard/data":
            self._json(200, self.os.dashboard.command(_need(params,"organization_id"),_need(params,"person_id")))
            return True
        if parsed.path == "/dashboard/settings":
            assert identity is not None
            self._json(200, self.os.dashboard.settings(
                identity, _need(params, "organization_id"), _optional_str(params.get("workspace_id"))
            )); return True
        if parsed.path == "/dashboard/review-center":
            self._json(200, self.os.dashboard.review_center(_need(params,"organization_id"),_need(params,"person_id")))
            return True
        if parsed.path == "/dashboard/client":
            assert identity is not None
            self._json(200, self.os.dashboard.client_hq(
                identity, _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
            ))
            return True
        if parsed.path == "/dashboard/module":
            self._json(200,self.os.dashboard.module(_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id"),_need(params,"module")));return True
        if parsed.path == "/dashboard/workflows":
            assert identity is not None
            organization_id, workspace_id, person_id = _need(params,"organization_id"), _need(params,"workspace_id"), _need(params,"person_id")
            self._json(200, self.os.dashboard.workflow_board(identity, organization_id, workspace_id, person_id, _optional_dt(params.get("as_of")))); return True
        if parsed.path == "/work/detail":
            self._json(200,self.os.work_ops.detail(_need(params,"organization_id"),_need(params,"workspace_id"),_need(params,"person_id"),_need(params,"work_item_id"))); return True
        return False

    def _post_work_delivery(self, parsed: Any, payload: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/projects":
            item = self.os.create_project(
                _need(payload, "organization_id"), _need(payload, "workspace_id"),
                _need(payload, "person_id"), _need(payload, "name"), str(payload.get("description", "")),
                str(payload.get("priority", "normal")), _optional_str(payload.get("due_date")),
                float(payload["budget"]) if payload.get("budget") is not None else None,
                [str(value) for value in payload.get("tags", [])],
            )
            self._json(201, item.to_dict())
            return True
        if parsed.path == "/contracts":
            item = self.os.client_ops.create_contract(
                _need(payload, "organization_id"), _need(payload, "workspace_id"),
                _need(payload, "person_id"), _need(payload, "kind"),
                _need(payload, "billing_model"), _need(payload, "start_date"),
                _optional_float(payload.get("value")), str(payload.get("currency", "USD")),
                _optional_str(payload.get("end_date")), _optional_str(payload.get("renewal_date")),
            )
            self._json(201, item)
            return True
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
            return True
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
            return True
        if parsed.path == "/deliverables":
            item = self.os.create_deliverable(
                _need(payload, "organization_id"), _need(payload, "workspace_id"),
                _need(payload, "person_id"), _need(payload, "project_id"),
                _need(payload, "title"), _need(payload, "type"), _optional_str(payload.get("work_item_id")),
            )
            self._json(201, item.to_dict())
            return True
        if parsed.path == "/reviews":
            item = self.os.open_review(
                _need(payload, "organization_id"), _need(payload, "workspace_id"),
                _need(payload, "person_id"), _need(payload, "deliverable_id"),
                str(payload.get("kind", "internal")), _optional_str(payload.get("reviewer_person_id")),
            )
            self._json(201, item.to_dict())
            return True
        if parsed.path == "/clients/roster":
            result = self.os.client_ops.create_client_roster(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                payload.get("roles") or [], _optional_str(payload.get("effective_at")), str(payload.get("note", "")),
            )
            self._json(201, result); return True
        if parsed.path == "/meetings/responsibilities":
            result = self.os.client_ops.set_meeting_responsibilities(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "meeting_id"), facilitator_person_id=_optional_str(payload.get("facilitator_person_id")),
                note_taker_person_id=_optional_str(payload.get("note_taker_person_id")), reason=str(payload.get("reason", "manual")),
            )
            self._json(200, result); return True
        if parsed.path == "/reviews/decide":
            item = self.os.decide_review(
                _need(payload, "organization_id"), _need(payload, "workspace_id"),
                _need(payload, "person_id"), _need(payload, "review_id"), _need(payload, "decision"),
            )
            self._json(200, item.to_dict())
            return True
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
        if parsed.path == "/initiatives":
            self._json(201,self.os.create_initiative(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"project_id"),_need(payload,"name"),str(payload.get("description","")))); return True
        if parsed.path == "/deliverables/version":
            item=self.os.add_deliverable_version(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"deliverable_id"),str(payload.get("notes","")),_optional_str(payload.get("file_url")));self._json(201,item.to_dict());return True
        if parsed.path == "/reviews/comment":
            item=self.os.add_review_comment(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"review_id"),_need(payload,"body"),_optional_float(payload.get("timestamp_seconds")));self._json(201,item.to_dict());return True
        if parsed.path == "/reviews/annotations":
            item = self.os.create_review_annotation(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "review_id"), _need(payload, "annotation_type"), str(payload.get("body", "")),
                _optional_str(payload.get("source_locator")), payload.get("coordinates") or {},
                _optional_int(payload.get("page_number")), _optional_float(payload.get("start_seconds")),
                _optional_float(payload.get("end_seconds")), _optional_str(payload.get("idempotency_key")), payload.get("metadata") or {},
            )
            self._json(201, item); return True
        if parsed.path == "/reviews/media":
            item = self.os.register_review_media_contract(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "review_id"), _need(payload, "source_locator"), _need(payload, "media_kind"),
                payload.get("metadata") or {}, _optional_int(payload.get("width_px")), _optional_int(payload.get("height_px")),
                _optional_float(payload.get("duration_seconds")), _optional_float(payload.get("frame_rate")), _optional_int(payload.get("page_count")),
            )
            self._json(201, item); return True
        if parsed.path == "/assets/backups":
            assert identity is not None
            workspace_id = _optional_str(payload.get("workspace_id"))
            scoped = self.os.auth.scope_identity(identity, workspace_id) if workspace_id else identity
            item = self.os.asset_recovery.register_asset_backup(
                scoped, _need(payload, "organization_id"), workspace_id, _need(payload, "asset_id"),
                _need(payload, "backup_manifest_id"), _optional_str(payload.get("target_locator")), payload.get("detail") or {},
            )
            self._json(201, item); return True
        if parsed.path == "/assets/backups/status":
            assert identity is not None
            item = self.os.asset_recovery.update_asset_backup_status(
                identity, _need(payload, "organization_id"), _need(payload, "manifest_id"), _need(payload, "status"), payload.get("detail") or {},
            )
            self._json(200, item); return True
        if parsed.path == "/reviews/annotations/resolve":
            item = self.os.resolve_review_annotation(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "annotation_id"), _optional_str(payload.get("idempotency_key")), str(payload.get("note", "")),
            )
            self._json(200, item); return True
        if parsed.path == "/reviews/annotations/supersede":
            item = self.os.supersede_review_annotation(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "annotation_id"), _optional_str(payload.get("replacement_annotation_id")), _optional_str(payload.get("idempotency_key")),
            )
            self._json(200, item); return True
        if parsed.path == "/work/items":
            item=self.os.work_ops.create(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"title"),_need(payload,"request"),_need(payload,"requested_by"),_optional_str(payload.get("project_id")),_optional_str(payload.get("campaign_id")),_optional_str(payload.get("parent_id")),str(payload.get("priority","normal")),[str(x) for x in payload.get("tags",[])],_optional_float(payload.get("estimate_hours")),_optional_str(payload.get("deadline")),str(payload.get("brief","")),str(payload.get("brain_context","")),_optional_float(payload.get("financial_value")));self._json(201,item.to_dict());return True
        if parsed.path == "/work/items/update":
            item=self.os.work_ops.update(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),payload.get("changes") or {});self._json(200,item.to_dict());return True
        if parsed.path == "/work/items/transition":
            item=self.os.work_ops.transition(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_need(payload,"to_status"),str(payload.get("reason","")),_optional_int(payload.get("expected_version")),_optional_str(payload.get("idempotency_key")));self._json(200,item);return True
        if parsed.path == "/work/items/assign":
            item=self.os.work_ops.assign(
                _need(payload,"organization_id"), _need(payload,"workspace_id"),
                _need(payload,"person_id"), _need(payload,"work_item_id"),
                _need(payload,"assignee_person_id"),
            ); self._json(200,item.to_dict()); return True
        if parsed.path == "/work/dependencies":
            self._json(201,self.os.work_ops.add_dependency(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_need(payload,"depends_on_id"),str(payload.get("kind","blocks"))));return True
        if parsed.path == "/work/comments":
            self._json(201,self.os.work_ops.add_comment(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_need(payload,"body")));return True
        if parsed.path == "/work/time":
            self._json(201,self.os.work_ops.log_time(_need(payload,"organization_id"),_need(payload,"workspace_id"),_need(payload,"person_id"),_need(payload,"work_item_id"),_required_dt(payload.get("started_at"),"started_at"),_required_dt(payload.get("ended_at"),"ended_at"),str(payload.get("notes","")),bool(payload.get("billable",True))));return True
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
            return True
        return False
