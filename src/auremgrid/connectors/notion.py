"""Bounded Notion connector for pages and databases."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping
from auremgrid.connectors.http import HttpTransport, ConnectorTransportError, sanitize_content
from auremgrid.connectors.google_auth import ConnectorSourceEvent, RouteLifecycleMutation
from auremgrid.domain.errors import ValidationError

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_MAX_EVENTS = 50
NOTION_MAX_CONTENT_CHARS = 8000
NOTION_MAX_TITLE_CHARS = 320
NOTION_REQUIRED_SCOPES = frozenset({"read_content"})


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}


def _text(value: Any) -> str | None:
    t = str(value).strip() if value is not None else ""
    return t or None


def _bounded(value: Any, limit: int) -> str | None:
    t = _text(value)
    return t[:limit] if t else None


def _digest(*parts: str) -> str:
    return hashlib.sha256(chr(31).join(str(p) for p in parts).encode()).hexdigest()


def _parse_cursor(cursor: str | None) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        value = json.loads(cursor)
    except Exception as exc:
        raise ValidationError("Notion cursor is invalid") from exc
    if not isinstance(value, dict) or value.get("v") != 1:
        raise ValidationError("Notion cursor is invalid")
    return value


def _make_cursor(database_id: str, last_page_id: str | None, has_more: bool) -> str | None:
    if not last_page_id or not has_more:
        return None
    return json.dumps({"v": 1, "db": database_id, "page": last_page_id}, sort_keys=True, separators= (",", ":"))


@dataclass(frozen=True)
class NotionAccountIdentity:
    user_id: str
    name: str | None
    email: str | None
    granted_scopes: frozenset[str]


@dataclass(frozen=True)
class NotionPullResult:
    events: tuple[ConnectorSourceEvent, ...]
    next_cursor: str | None
    has_more: bool = False
    lifecycle_mutations: tuple[RouteLifecycleMutation, ...] = ()


class NotionConnector:
    """Bounded Notion connector for pages and databases.
    Workspace mappings use the pattern database:<id> mapping to a workspace ID."""
    name = "notion"

    def __init__(
        self,
        api_key: str,
        transport: Any | None = None,
        *,
        workspace_mappings: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValidationError("Notion API key is required")
        self.api_key = api_key
        self.transport = transport or HttpTransport()
        self.mappings = dict(workspace_mappings or {})
        if not self.mappings:
            raise ValidationError("Notion requires at least one database:<id> mapping")
        for key in self.mappings:
            if not key.startswith("database:") or not key[9:].strip():
                raise ValidationError("Notion mapping keys must be database:<id>")

    def verify_credentials(self) -> NotionAccountIdentity:
        resp = self.transport.request("GET", f"{NOTION_BASE}/users/me", _headers(self.api_key))
        data = resp.json()
        uid = _text(data.get("id"))
        if not uid:
            raise ConnectorTransportError("Notion credential verification failed", status=401)
        return NotionAccountIdentity(
            user_id=uid,
            name=_bounded(data.get("name"), 240),
            email=_text(data.get("email")),
            granted_scopes=NOTION_REQUIRED_SCOPES,
        )

    def pull(self, cursor: str | None = None) -> NotionPullResult:
        state = _parse_cursor(cursor)
        all_events: list[ConnectorSourceEvent] = []
        all_mutations: list[RouteLifecycleMutation] = []
        for route, workspace_id in self.mappings.items():
            database_id = route[9:]  # strip "database:"
            start_cursor = None
            if state and state.get("db") == database_id:
                start_cursor = state.get("cursor")
            pages, next_db_cursor, has_more = self._query_database(database_id, start_cursor)
            events = self._page_events(pages, route, workspace_id)
            all_events.extend(events)
            last_page = state["page"] if state else None
            for ev in events:
                meta = ev.payload or {}
                pid = meta.get("page_id")
                if pid:
                    last_page = pid
                date = ev.observed_at or ""
                all_mutations.append(
                    RouteLifecycleMutation(ev.external_id, route, workspace_id, "upsert", date, ev.dedupe_key)
                )
            if len(all_events) >= NOTION_MAX_EVENTS:
                break
            if next_db_cursor and has_more:
                return NotionPullResult(
                    tuple(all_events[:NOTION_MAX_EVENTS]),
                    _make_cursor(database_id, last_page, has_more),
                    True,
                    tuple(all_mutations),
                )
        return NotionPullResult(
            tuple(all_events[:NOTION_MAX_EVENTS]),
            None,
            False,
            tuple(all_mutations),
        )

    def _query_database(
        self, database_id: str, start_cursor: str | None
    ) -> tuple[list[dict], str | None, bool]:
        body: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        resp = self.transport.request(
            "POST",
            f"{NOTION_BASE}/databases/{database_id}/query",
            _headers(self.api_key),
            json.dumps(body).encode(),
        )
        data = resp.json()
        if not isinstance(data, dict):
            raise ConnectorTransportError("Notion query response shape is invalid")
        return (
            data.get("results") or [],
            _text(data.get("next_cursor")),
            bool(data.get("has_more", False)),
        )

    def _page_events(
        self, pages: list[dict], route: str, workspace_id: str
    ) -> list[ConnectorSourceEvent]:
        events: list[ConnectorSourceEvent] = []
        seen: set[str] = set()
        for page in pages:
            if len(events) >= NOTION_MAX_EVENTS:
                break
            if not isinstance(page, dict):
                continue
            page_id = _text(page.get("id"))
            if not page_id or page_id in seen:
                continue
            seen.add(page_id)
            props = page.get("properties") or {}
            title = self._extract_title(props)
            extracted = self._extract_properties(props)
            observed = _text(page.get("last_edited_time")) or _text(page.get("created_time")) or ""
            external = f"notion/pages/{page_id}"
            content_obj: dict[str, Any] = {"page_id": page_id, "title": title, **extracted}
            content_obj = {k: v for k, v in content_obj.items() if v is not None and v != "" and v != []}
            content_obj = sanitize_content(content_obj, (self.api_key,))
            content = json.dumps(content_obj, sort_keys=True, separators= (",", ":"), ensure_ascii=False)
            payload = {
                "workspace_ids": [workspace_id],
                "route_keys": [route],
                "page_id": page_id,
                "title": title,
            }
            payload = {k: v for k, v in payload.items() if v is not None}
            dedupe = _digest(external, observed, hashlib.sha256(content.encode()).hexdigest())
            events.append(
                ConnectorSourceEvent(
                    dedupe, external, "notion_page", external, "", content, payload, observed, "application/json"
                )
            )
        return events

    @staticmethod
    def _extract_title(props: dict[str, Any]) -> str | None:
        for value in props.values():
            if not isinstance(value, dict):
                continue
            if value.get("type") == "title":
                texts = value.get("title") or []
                parts = [_text(t.get("plain_text")) for t in texts if isinstance(t, dict) and _text(t.get("plain_text"))]
                title = " ".join(parts).strip()
                return title[:NOTION_MAX_TITLE_CHARS] if title else None
        return None

    @staticmethod
    def _extract_properties(props: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, value in props.items():
            if not isinstance(value, dict):
                continue
            ptype = value.get("type")
            if ptype == "rich_text":
                texts = value.get("rich_text") or []
                parts = [_text(t.get("plain_text")) for t in texts if isinstance(t, dict) and _text(t.get("plain_text"))]
                t = " ".join(parts).strip()
                if t:
                    out[name] = t[:NOTION_MAX_CONTENT_CHARS]
            elif ptype == "select":
                sel = value.get("select")
                if isinstance(sel, dict) and _text(sel.get("name")):
                    out[name] = _text(sel["name"])
            elif ptype == "multi_select":
                items = value.get("multi_select") or []
                names = [_text(i.get("name")) for i in items if isinstance(i, dict) and _text(i.get("name"))]
                if names:
                    out[name] = names[:20]
            elif ptype == "date":
                d = value.get("date")
                if isinstance(d, dict):
                    out[name] = {k: v for k, v in d.items() if v is not None and _text(v)}
            elif ptype == "number":
                n = value.get("number")
                if n is not None:
                    out[name] = n
            elif ptype == "checkbox":
                out[name] = bool(value.get("checkbox", False))
            elif ptype == "url":
                u = _text(value.get("url"))
                if u:
                    out[name] = u[:2048]
        return out
