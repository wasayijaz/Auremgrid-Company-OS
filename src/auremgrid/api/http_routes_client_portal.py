"""Client portal and report delivery route mixin."""
from __future__ import annotations

from typing import Any

from auremgrid.api.http_shared import (
    _need,
    _optional_str,
)
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


class HttpRoutesClientPortalMixin:
    def _get_client_portal(self, parsed: Any, params: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/client-portal/intake":
            organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            items = self.os.client_portal.list_intake_requests(organization_id, workspace_id, person_id, params.get("status"))
            self._json(200, {"intake_requests": items})
            return True
        if parsed.path == "/client-portal/intake/queue":
            organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            items = self.os.client_portal.list_intake_queue(organization_id, workspace_id, person_id)
            self._json(200, {"intake_requests": items})
            return True
        if parsed.path == "/client-portal/reviews":
            organization_id, workspace_id, person_id = _need(params, "organization_id"), _need(params, "workspace_id"), _need(params, "person_id")
            items = self.os.client_portal.list_client_reviews(organization_id, workspace_id, person_id)
            self._json(200, {"reviews": items})
            return True
        if parsed.path == "/client-portal/reports":
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, {"reports": self.os.report_delivery.portal_list(
                scoped, scoped.organization_id, workspace_id
            )})
            return True
        if parsed.path in {"/client-portal/reports/view", "/client-portal/reports/download"}:
            assert identity is not None
            workspace_id = _need(params, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            handler = self.os.report_delivery.portal_download if parsed.path.endswith("/download") else self.os.report_delivery.portal_view
            self._json(200, handler(
                scoped, scoped.organization_id, workspace_id, _need(params, "portal_report_version_id")
            ))
            return True
        if parsed.path == "/reports":
            assert identity is not None
            organization_id, person_id = _need(params, "organization_id"), _need(params, "person_id")
            if identity.organization_id != organization_id or identity.person_id != person_id:
                raise AuthorizationError("identity scope mismatch")
            if self.os.company.org_membership(organization_id, person_id) is None:
                raise AuthorizationError("organization membership required")
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
            rows = self.os.store.conn.execute(
                f"SELECT * FROM report_runs WHERE {where} ORDER BY rowid DESC",
                tuple(values),
            ).fetchall()
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
            self._json(200, {"reports": reports, "allowed_actions": self.os.agent_ops.report_action_descriptors(
                organization_id, person_id, workspace_id, identity.capabilities
            )})
            return True
        return False

    def _post_client_portal(self, parsed: Any, payload: dict[str, Any], identity: Any) -> bool:
        if parsed.path == "/client-portal/intake":
            item = self.os.client_portal.submit_intake_request(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "title"), _need(payload, "request"), _optional_str(payload.get("needed_by")),
            )
            self._json(201, item)
            return True
        if parsed.path == "/client-portal/intake/accept":
            item = self.os.client_portal.accept_intake_request(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "intake_request_id"), _optional_str(payload.get("assignee_id")),
                _optional_str(payload.get("decision_maker")),
            )
            self._json(200, item)
            return True
        if parsed.path == "/client-portal/intake/decline":
            item = self.os.client_portal.decline_intake_request(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "intake_request_id"), str(payload.get("note", "")),
            )
            self._json(200, item)
            return True
        if parsed.path == "/client-portal/reviews/comment":
            item = self.os.client_portal.add_client_review_comment(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "review_id"), _need(payload, "body"),
            )
            self._json(201, item.to_dict())
            return True
        if parsed.path == "/client-portal/reviews/decide":
            item = self.os.client_portal.decide_client_review(
                _need(payload, "organization_id"), _need(payload, "workspace_id"), _need(payload, "person_id"),
                _need(payload, "review_id"), _need(payload, "decision"),
            )
            self._json(200, item.to_dict())
            return True
        if parsed.path == "/reports/generate":
            self._json(201, self.os.agent_ops.generate_report(_need(payload, "organization_id"), _need(payload, "person_id"), _need(payload, "type"), _optional_str(payload.get("workspace_id"))))
            return True
        if parsed.path == "/reports/portal-publish":
            assert identity is not None
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(201, self.os.report_delivery.publish(
                scoped, scoped.organization_id, workspace_id, _need(payload, "report_run_id"),
                _need(payload, "approval_request_id"), _need(payload, "title"),
                str(payload.get("reason", "")),
            ))
            return True
        if parsed.path == "/reports/portal-revoke":
            assert identity is not None
            workspace_id = _need(payload, "workspace_id")
            scoped = self.os.auth.scope_identity(identity, workspace_id)
            self._json(200, self.os.report_delivery.revoke(
                scoped, scoped.organization_id, workspace_id,
                _need(payload, "portal_report_version_id"), _need(payload, "reason"),
            ))
            return True
        return False
