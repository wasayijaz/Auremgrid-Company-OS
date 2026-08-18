from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from auremgrid.api.mcp import McpToolRouter
from auremgrid.domain.errors import AuremgridError, AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS


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
            if parsed.path == "/search":
                bundle = self.os.search(
                    _need(params, "workspace_id"),
                    _need(params, "actor_id"),
                    _need(params, "query"),
                    limit=int(params.get("limit", "8")),
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
        payload = self._read_json()
        try:
            if parsed.path == "/tools/call":
                result = self.router.call(str(payload.get("name", "")), payload.get("arguments") or {})
                status = 400 if "error" in result else 200
                self._json(status, result)
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
            self._json(404, {"error": "not_found"})
        except Exception as exc:
            self._handle_error(exc)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


def serve(os: CompanyOS, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (CompanyOSRequestHandler,),
        {"os": os, "router": McpToolRouter(os)},
    )
    return ThreadingHTTPServer((host, port), handler)
