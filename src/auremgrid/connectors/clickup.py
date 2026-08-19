"""Credential-backed ClickUp task connector."""

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
class ClickUpTeamIdentity:
    team_id: str
    team_name: str


class ClickUpConnector:
    name = "clickup"

    def __init__(
        self,
        token: str,
        list_workspace_mappings: Mapping[str, str],
        list_ids: tuple[str, ...] | list[str] | None = None,
        transport: HttpTransport | None = None,
        cursor: str | None = None,
        expected_team_id: str | None = None,
    ) -> None:
        if not token:
            raise ValueError("ClickUp token is required")
        self._token = token
        self.list_workspace_mappings = dict(list_workspace_mappings)
        self.list_ids = tuple(list_ids or self.list_workspace_mappings.keys())
        self.transport = transport or HttpTransport()
        self.cursor = cursor
        self.next_cursor = cursor
        self.expected_team_id = expected_team_id
        self.has_more = cursor not in (None, "0")

    def pull(self) -> list[ConnectorEvent]:
        events: list[ConnectorEvent] = []
        cursor = self.cursor
        for list_id in self.list_workspace_mappings:
            workspace_id = self.list_workspace_mappings.get(list_id)
            if workspace_id is None:
                raise ValueError(f"ClickUp list is not mapped: {list_id}")
            query = {"include_closed": "true", "subtasks": "true"}
            if cursor:
                query["page"] = cursor
            response = self.transport.request(
                "GET",
                f"https://api.clickup.com/api/v2/list/{list_id}/task?{urlencode(query)}",
                {"Authorization": self._token, "Accept": "application/json"},
            )
            payload = _json_payload(response.body)
            for task in payload.get("tasks", []):
                if isinstance(task, dict):
                    events.append(_event(workspace_id, list_id, task, (self._token,)))
            # ClickUp's page cursor is numeric and last_page marks completion.
            if payload.get("last_page", True):
                cursor = "0"
            else:
                current = int(cursor or 0)
                cursor = str(current + 1)
        self.cursor = cursor
        self.next_cursor = cursor
        self.has_more = cursor != "0"
        return events

    def verify_credentials(self) -> tuple[ClickUpTeamIdentity, ...]:
        response = self.transport.request(
            "GET",
            "https://api.clickup.com/api/v2/team",
            {"Authorization": self._token, "Accept": "application/json"},
        )
        payload = _json_payload(response.body)
        teams: list[ClickUpTeamIdentity] = []
        for team in payload.get("teams", []):
            if isinstance(team, dict):
                team_id = str(team.get("id", ""))
                if team_id:
                    teams.append(ClickUpTeamIdentity(team_id=team_id, team_name=str(team.get("name", ""))))
        if not teams:
            raise ConnectorTransportError("ClickUp credential verification failed", retryable=False)
        if self.expected_team_id and self.expected_team_id not in {team.team_id for team in teams}:
            raise AuthorizationError("ClickUp account identity mismatch")
        if self.expected_team_id:
            self._validate_mapped_lists(self.expected_team_id)
        return tuple(teams)

    def _validate_mapped_lists(self, expected_team_id: str) -> None:
        """Confirm every configured list belongs to a space in the expected team."""

        list_space_ids: list[str] = []
        for list_id in self.list_ids:
            response = self.transport.request(
                "GET",
                f"https://api.clickup.com/api/v2/list/{list_id}",
                {"Authorization": self._token, "Accept": "application/json"},
            )
            payload = _json_payload(response.body)
            space = payload.get("space")
            space_id = str(space.get("id", "")) if isinstance(space, dict) else ""
            if not space_id:
                raise AuthorizationError("ClickUp list hierarchy is incomplete")
            list_space_ids.append(space_id)
        response = self.transport.request(
            "GET",
            f"https://api.clickup.com/api/v2/team/{expected_team_id}/space?archived=false",
            {"Authorization": self._token, "Accept": "application/json"},
        )
        payload = _json_payload(response.body)
        team_space_ids = {
            str(space.get("id")) for space in payload.get("spaces", []) if isinstance(space, dict) and space.get("id")
        }
        if any(space_id not in team_space_ids for space_id in list_space_ids):
            raise AuthorizationError("ClickUp list is outside expected team")


def _json_payload(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorTransportError("connector returned invalid JSON", retryable=False) from exc
    if not isinstance(payload, dict):
        raise ConnectorTransportError("connector returned an invalid payload", retryable=False)
    return payload


def _event(workspace_id: str, list_id: str, task: Mapping[str, object], known_secrets: tuple[str, ...] = ()) -> ConnectorEvent:
    task_id = str(task.get("id", "unknown"))
    name = str(task.get("name", "Untitled task"))
    description = sanitize_content(str(task.get("description", "")), known_secrets)
    status = task.get("status")
    status_name = str(status.get("status", "")) if isinstance(status, dict) else str(status or "")
    updated = task.get("date_updated")
    observed_at = None
    if updated:
        try:
            observed_at = datetime.fromtimestamp(float(updated) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            observed_at = None
    content = f"# ClickUp task: {name}\n\nStatus: {status_name}\n\n{description}".rstrip()
    return ConnectorEvent(
        workspace_id=workspace_id,
        source_key=f"clickup:{list_id}:{task_id}",
        locator=str(task.get("url") or f"clickup://task/{task_id}"),
        content=content,
        connector="clickup",
        observed_at=observed_at,
        media_type="text/markdown",
        trust_level="external",
    )
