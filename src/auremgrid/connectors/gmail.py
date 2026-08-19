from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Any

from auremgrid.connectors.google_auth import (
    ConnectorSourceEvent,
    HttpResponse,
    HttpTransport,
    UrllibTransport,
    retry_after_seconds,
)
from auremgrid.domain.errors import ValidationError


GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


@dataclass(frozen=True)
class GmailPullResult:
    events: list[ConnectorSourceEvent]
    next_cursor: str | None
    rate_limited: bool = False
    retry_after_seconds: int | None = None
    cursor_expired: bool = False
    error: str | None = None


class GmailConnector:
    name = "gmail"

    def __init__(self, access_token: str, transport: HttpTransport | None = None) -> None:
        if not access_token:
            raise ValidationError("access_token is required")
        self.access_token = access_token
        self.transport = transport or UrllibTransport()

    def pull(self, cursor: str | None) -> GmailPullResult:
        if cursor is None:
            response = self._get(f"{GMAIL_API}/users/me/profile")
            limited = _rate_limited(response)
            if limited:
                return limited
            data = _json(response)
            history_id = data.get("historyId")
            if not isinstance(history_id, str) or not history_id:
                raise ValidationError("Gmail profile response missing historyId")
            return GmailPullResult([], history_id)

        page_token: str | None = None
        latest_history_id = cursor
        events: list[ConnectorSourceEvent] = []
        while True:
            params = {
                "startHistoryId": cursor,
                "historyTypes": "messageAdded",
            }
            if page_token:
                params["pageToken"] = page_token
            response = self._get(f"{GMAIL_API}/users/me/history?{urllib.parse.urlencode(params)}")
            limited = _rate_limited(response)
            if limited:
                return GmailPullResult([], cursor, True, limited.retry_after_seconds, False, limited.error)
            if response.status == 404:
                return GmailPullResult([], None, cursor_expired=True, error="Gmail history cursor expired")
            data = _json(response)
            if isinstance(data.get("historyId"), str):
                latest_history_id = data["historyId"]
            for history in data.get("history") or []:
                history_id = str(history.get("id") or latest_history_id)
                for item in history.get("messagesAdded") or []:
                    message = item.get("message") if isinstance(item, dict) else None
                    if isinstance(message, dict) and message.get("id"):
                        events.append(self._event_from_message_stub(str(message["id"]), history_id))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return GmailPullResult(events, latest_history_id)

    def _event_from_message_stub(self, message_id: str, history_id: str) -> ConnectorSourceEvent:
        query = urllib.parse.urlencode(
            {
                "format": "metadata",
                "metadataHeaders": ["Subject", "From", "To", "Date"],
            },
            doseq=True,
        )
        response = self._get(f"{GMAIL_API}/users/me/messages/{urllib.parse.quote(message_id)}?{query}")
        data = _json(response)
        headers = {
            item.get("name"): item.get("value")
            for item in (data.get("payload") or {}).get("headers", [])
            if isinstance(item, dict)
        }
        subject = str(headers.get("Subject") or "(no subject)")
        snippet = str(data.get("snippet") or "")
        internal_date = data.get("internalDate")
        observed_at = None
        if isinstance(internal_date, str) and internal_date.isdigit():
            observed_at = _millis_to_iso(int(internal_date))
        content = (
            "# Gmail message\n\n"
            f"Subject: {subject}\n"
            f"From: {headers.get('From') or ''}\n"
            f"To: {headers.get('To') or ''}\n"
            f"Date: {headers.get('Date') or ''}\n\n"
            f"{snippet}"
        )
        return ConnectorSourceEvent(
            dedupe_key=f"gmail:{message_id}:{history_id}",
            external_id=message_id,
            event_type="message_added",
            source_key=f"gmail/messages/{message_id}",
            locator=f"https://mail.google.com/mail/u/0/#all/{message_id}",
            content=content,
            payload={"message": data},
            observed_at=observed_at,
        )

    def _get(self, url: str) -> HttpResponse:
        return self.transport("GET", url, {"Authorization": f"Bearer {self.access_token}"}, None)


def _json(response: HttpResponse) -> dict[str, Any]:
    if response.status < 200 or response.status >= 300:
        raise ValidationError(f"Gmail returned HTTP {response.status}")
    if not isinstance(response.json_body, dict):
        raise ValidationError("Gmail response was not JSON")
    return response.json_body


def _rate_limited(response: HttpResponse) -> GmailPullResult | None:
    if response.status in {403, 429}:
        return GmailPullResult(
            [],
            None,
            rate_limited=True,
            retry_after_seconds=retry_after_seconds(response),
            error=response.text or f"Gmail returned HTTP {response.status}",
        )
    return None


def _millis_to_iso(value: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()
