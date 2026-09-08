from __future__ import annotations

import json
import os as environment
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
from auremgrid.api.http_shared import (
    _bool,
    _evaluation_safety_status,
    _float,
    _int,
    _need,
    _number,
    _optional_dt,
    _optional_float,
    _optional_int,
    _optional_str,
    _optional_str_list,
    _optional_string_sequence,
    _provider_import_adapter,
    _require_evaluation_scope,
    _required_dt,
    _required_list,
    _route_capability,
    _what_if_params,
)
from auremgrid.api.http_routes_public import HttpRoutesPublicMixin
from auremgrid.api.http_routes_auth_org import HttpRoutesAuthOrgMixin
from auremgrid.api.http_routes_brain_intelligence import HttpRoutesBrainIntelligenceMixin
from auremgrid.api.http_routes_work_delivery import HttpRoutesWorkDeliveryMixin
from auremgrid.api.http_routes_agency_ops import HttpRoutesAgencyOpsMixin
from auremgrid.api.http_routes_agents_workflows import HttpRoutesAgentsWorkflowsMixin
from auremgrid.api.http_routes_integrations import HttpRoutesIntegrationsMixin
from auremgrid.api.http_routes_client_portal import HttpRoutesClientPortalMixin
from auremgrid.api.http_routes_insights_remainder import HttpRoutesInsightsRemainderMixin


LEGACY_ACTOR_PATHS = {
    "/search", "/entity", "/history", "/neighbors", "/sources", "/recent", "/brief", "/work",
    "/remember", "/work/capture", "/work/capture_work", "/work/assign", "/work/assign_work",
    "/work/start", "/work/start_work", "/work/dod", "/work/mark-dod", "/work/mark_dod",
    "/work/submit-review", "/work/submit_review", "/work/close-review", "/work/close_review",
    "/work/ship", "/work/ship_work",
}
RETIRED_ROUTE_EVIDENCE = {"/memory-proposals/review"}


class CompanyOSRequestHandler(
    HttpRoutesPublicMixin,
    HttpRoutesAuthOrgMixin,
    HttpRoutesBrainIntelligenceMixin,
    HttpRoutesWorkDeliveryMixin,
    HttpRoutesAgencyOpsMixin,
    HttpRoutesAgentsWorkflowsMixin,
    HttpRoutesIntegrationsMixin,
    HttpRoutesClientPortalMixin,
    HttpRoutesInsightsRemainderMixin,
    BaseHTTPRequestHandler,
):
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
            if self._get_public(parsed, params, identity): return
            if self._get_auth_org(parsed, params, identity): return
            if self._get_brain_intelligence(parsed, params, identity): return
            if self._get_work_delivery(parsed, params, identity): return
            if self._get_agency_ops(parsed, params, identity): return
            if self._get_agents_workflows(parsed, params, identity): return
            if self._get_integrations(parsed, params, identity): return
            if self._get_client_portal(parsed, params, identity): return
            if self._get_insights_remainder(parsed, params, identity): return
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
            if self._post_auth_org(parsed, payload, identity): return
            if self._post_brain_intelligence(parsed, payload, identity): return
            if self._post_work_delivery(parsed, payload, identity): return
            if self._post_agency_ops(parsed, payload, identity): return
            if self._post_agents_workflows(parsed, payload, identity): return
            if self._post_integrations(parsed, payload, identity): return
            if self._post_client_portal(parsed, payload, identity): return
            if self._post_insights_remainder(parsed, payload, identity): return
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

        if environment.environ.get("AUREMGRID_WEBHOOK_RECEIPTS_ENABLED") != "1":
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


def serve(os: CompanyOS, host: str = "127.0.0.1", port: int = 8791) -> HTTPServer:
    handler = type(
        "BoundHandler",
        (CompanyOSRequestHandler,),
        {"os": os},
    )
    # A single request loop deliberately serializes the shared SQLite connection.
    # Durable workers use their own connections and leases rather than HTTP threads.
    dashboard_url = environment.environ.get("AUREMGRID_DASHBOARD_URL")
    if dashboard_url:
        configured_port = urlparse(dashboard_url).port
        if configured_port is not None and configured_port != port and port != 0:
            raise ValueError(f"AUREMGRID_DASHBOARD_URL port {configured_port} does not match serve port {port}")
    return HTTPServer((host, port), handler)


def _dashboard_html() -> str:
    path = Path(__file__).with_name("dashboard") / "index.html"
    return path.read_text(encoding="utf-8")
