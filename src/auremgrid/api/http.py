from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from auremgrid.api.mcp import McpToolRouter
from auremgrid.domain.errors import AuremgridError, AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS
from pathlib import Path


class CompanyOSRequestHandler(BaseHTTPRequestHandler):
    os: CompanyOS
    router: McpToolRouter

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/health":
                self._json(200, {"ok": True})
                return
            if parsed.path in {"/", "/dashboard"}:
                self._html(200, _dashboard_html())
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
            if parsed.path == "/entity":
                self._json(
                    200,
                    self.os.entity(_need(params, "workspace_id"), _need(params, "actor_id"), _need(params, "name")),
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
                    ),
                )
                return
            if parsed.path == "/neighbors":
                self._json(
                    200,
                    self.os.neighbors(_need(params, "workspace_id"), _need(params, "actor_id"), _need(params, "entity")),
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
            self._json(404, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/tools/call":
                result = self.router.call(str(payload.get("name", "")), payload.get("arguments") or {})
                status = 400 if "error" in result else 200
                self._json(status, result)
                return
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

    def _handle_error(self, exc: Exception) -> None:
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


def _int(value: Any, key: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{key} must be an integer") from exc


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValidationError(f"{key} must be a boolean")


def _optional_dt(value: Any) -> Any:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError("as_of must be an ISO datetime") from exc


def serve(os: CompanyOS, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (CompanyOSRequestHandler,),
        {"os": os, "router": McpToolRouter(os)},
    )
    return ThreadingHTTPServer((host, port), handler)


def _dashboard_html() -> str:
    path = Path(__file__).with_name("dashboard.html")
    return path.read_text(encoding="utf-8")
