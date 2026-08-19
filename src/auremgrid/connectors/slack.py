"""Credential-backed Slack history connector."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlencode

from auremgrid.connectors.bus import ConnectorEvent
from auremgrid.connectors.http import ConnectorTransportError, HttpTransport, sanitize_content
from auremgrid.domain.errors import AuthorizationError


@dataclass(frozen=True)
class SlackAccountIdentity:
    team_id: str
    team_name: str
    user_id: str
    user_name: str
    granted_scopes: frozenset[str] = frozenset()


class SlackConnector:
    name = "slack"

    def __init__(
        self,
        token: str,
        channel_workspace_mappings: Mapping[str, str],
        channel_ids: tuple[str, ...] | list[str] | None = None,
        transport: HttpTransport | None = None,
        cursor: str | None = None,
        expected_team_id: str | None = None,
    ) -> None:
        if not token:
            raise ValueError("Slack token is required")
        self._token = token
        self.channel_workspace_mappings = dict(channel_workspace_mappings)
        self.channel_ids = tuple(channel_ids or self.channel_workspace_mappings.keys())
        self.transport = transport or HttpTransport()
        self.cursor = cursor
        self.expected_team_id = expected_team_id
        self._cursor_state = self._parse_cursor_state(cursor)
        self.next_cursor = self._serialized_cursor()
        self.has_more = any(bool(item.get("page_cursor")) for item in self._cursor_state.values())

    def _parse_cursor_state(self, cursor: str | None) -> dict[str, dict[str, str | None]]:
        empty = {channel: {"page_cursor": None, "oldest": None, "cycle_oldest": None, "candidate_oldest": None} for channel in self.channel_ids}
        if not cursor:
            return empty
        try:
            value = json.loads(cursor)
            if len(self.channel_ids) == 1 and isinstance(value, dict) and ("page_cursor" in value or "oldest" in value):
                oldest = value.get("oldest", value.get("cycle_oldest"))
                return {self.channel_ids[0]: {"page_cursor": value.get("page_cursor"), "oldest": oldest, "cycle_oldest": oldest, "candidate_oldest": value.get("candidate_oldest")}}
            if isinstance(value, dict):
                for channel in self.channel_ids:
                    item = value.get(channel)
                    if isinstance(item, dict):
                        oldest = item.get("oldest", item.get("cycle_oldest"))
                        empty[channel] = {"page_cursor": item.get("page_cursor"), "oldest": oldest, "cycle_oldest": oldest, "candidate_oldest": item.get("candidate_oldest")}
                return empty
        except (TypeError, ValueError):
            pass
        # A raw cursor is accepted as an initial page cursor for one legacy checkpoint.
        return {channel: {"page_cursor": cursor, "oldest": None, "cycle_oldest": None, "candidate_oldest": None} for channel in self.channel_ids}

    def _serialized_cursor(self) -> str:
        if len(self.channel_ids) == 1:
            return json.dumps(self._cursor_state[self.channel_ids[0]], sort_keys=True, separators=(",", ":"))
        return json.dumps(self._cursor_state, sort_keys=True, separators=(",", ":"))

    def pull(self) -> list[ConnectorEvent]:
        events: list[ConnectorEvent] = []
        for channel_id in self.channel_ids:
            workspace_id = self.channel_workspace_mappings.get(channel_id)
            if workspace_id is None:
                raise ValueError(f"Slack channel is not mapped: {channel_id}")
            query = {"channel": channel_id, "limit": "15", "inclusive": "false"}
            state = self._cursor_state[channel_id]
            if state["page_cursor"]:
                query["cursor"] = state["page_cursor"]
            if state["oldest"]:
                query["oldest"] = state["oldest"]
            response = self.transport.request(
                "GET",
                "https://slack.com/api/conversations.history?" + urlencode(query),
                {"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            )
            payload = _json_payload(response.body)
            if not payload.get("ok", False):
                error = str(payload.get("error", "Slack API error"))
                retry_after = payload.get("retry_after")
                if error in {"ratelimited", "rate_limited"}:
                    raise ConnectorTransportError("Slack rate limit", status=429, retryable=True, retry_after=float(retry_after) if retry_after else None)
                if error in {"invalid_auth", "not_authed", "account_inactive", "token_revoked"}:
                    raise ConnectorTransportError("Slack credential is no longer authorized", status=401, retryable=False)
                if error in {"missing_scope","no_permission","access_denied"}:
                    raise ConnectorTransportError("Slack credential lacks required permission",status=403,retryable=False)
                raise ConnectorTransportError("Slack API request failed", status=401 if error == "invalid_auth" else None, retryable=False)
            for message in payload.get("messages", []):
                    events.append(_event(workspace_id, channel_id, message, (self._token,)))
            max_ts = max((_message_ts(message) for message in payload.get("messages", []) if isinstance(message, dict)), default=None)
            candidate = state.get("candidate_oldest") or state.get("oldest")
            if max_ts is not None and (candidate is None or float(max_ts) > float(candidate)):
                candidate = max_ts
            state["page_cursor"] = ((payload.get("response_metadata") or {}).get("next_cursor") or None)
            if state["page_cursor"]:
                state["candidate_oldest"] = candidate
            else:
                state["oldest"] = candidate
                state["cycle_oldest"] = candidate
                state["candidate_oldest"] = None
            self.next_cursor = self._serialized_cursor()
        self.next_cursor = self._serialized_cursor()
        self.has_more = any(bool(item.get("page_cursor")) for item in self._cursor_state.values())
        return events

    def verify_credentials(self) -> SlackAccountIdentity:
        response = self.transport.request(
            "GET",
            "https://slack.com/api/auth.test",
            {"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        payload = _json_payload(response.body)
        if not payload.get("ok", False):
            raise ConnectorTransportError(
                "Slack credential verification failed",
                status=401 if payload.get("error") == "invalid_auth" else None,
                retryable=False,
            )
        identity = SlackAccountIdentity(
            team_id=str(payload.get("team_id", "")),
            team_name=str(payload.get("team", "")),
            user_id=str(payload.get("user_id", "")),
            user_name=str(payload.get("user", "")),
            granted_scopes=frozenset(
                scope.strip() for scope in _header(response.headers, "X-OAuth-Scopes").split(",") if scope.strip()
            ),
        )
        if not identity.team_id or (self.expected_team_id and identity.team_id != self.expected_team_id):
            raise AuthorizationError("Slack account identity mismatch")
        return identity


def _json_payload(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorTransportError("connector returned invalid JSON", retryable=False) from exc
    if not isinstance(payload, dict):
        raise ConnectorTransportError("connector returned an invalid payload", retryable=False)
    return payload


def _event(workspace_id: str, channel_id: str, message: Mapping[str, object], known_secrets: tuple[str, ...] = ()) -> ConnectorEvent:
    ts = str(message.get("ts", ""))
    observed_at = None
    if ts:
        try:
            observed_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            observed_at = None
    text = sanitize_content(str(message.get("text", "")), known_secrets)
    user = str(message.get("user", "unknown"))
    return ConnectorEvent(
        workspace_id=workspace_id,
        source_key=f"slack:{channel_id}:{ts or text[:32]}",
        locator=f"slack://{channel_id}/{ts or 'message'}",
        content=f"# Slack message\n\nUser: {user}\n\n{text}",
        connector="slack",
        observed_at=observed_at,
        media_type="text/markdown",
        trust_level="external",
    )


def _message_ts(message: Mapping[str, object]) -> str | None:
    value = message.get("ts")
    if value is None:
        return None
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return None


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return str(value)
    return ""
