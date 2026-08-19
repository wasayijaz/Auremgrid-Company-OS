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


DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_DOC = "application/vnd.google-apps.document"


@dataclass(frozen=True)
class GooglePullResult:
    events: list[ConnectorSourceEvent]
    next_cursor: str | None
    rate_limited: bool = False
    retry_after_seconds: int | None = None
    cursor_expired: bool = False
    error: str | None = None


class GoogleDriveConnector:
    name = "google_drive"

    def __init__(self, access_token: str, transport: HttpTransport | None = None) -> None:
        if not access_token:
            raise ValidationError("access_token is required")
        self.access_token = access_token
        self.transport = transport or UrllibTransport()

    def pull(self, cursor: str | None) -> GooglePullResult:
        if cursor is None:
            response = self._get(f"{DRIVE_API}/changes/startPageToken")
            limited = _rate_limited(response)
            if limited:
                return limited
            data = _json(response)
            token = data.get("startPageToken")
            if not isinstance(token, str) or not token:
                raise ValidationError("Drive startPageToken response missing token")
            return GooglePullResult([], token)

        page_token = cursor
        events: list[ConnectorSourceEvent] = []
        next_cursor: str | None = None
        while page_token:
            query = urllib.parse.urlencode(
                {
                    "pageToken": page_token,
                    "pageSize": "100",
                    "spaces": "drive",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    "fields": (
                        "nextPageToken,newStartPageToken,"
                        "changes(fileId,removed,time,file(id,name,mimeType,modifiedTime,md5Checksum,webViewLink,trashed))"
                    ),
                }
            )
            response = self._get(f"{DRIVE_API}/changes?{query}")
            limited = _rate_limited(response)
            if limited:
                return GooglePullResult([], cursor, True, limited.retry_after_seconds, False, limited.error)
            if response.status == 410:
                return GooglePullResult([], None, cursor_expired=True, error="Drive changes cursor expired")
            data = _json(response)
            for change in data.get("changes") or []:
                events.append(self._event_from_change(change))
            page_token = data.get("nextPageToken")
            next_cursor = data.get("newStartPageToken") or next_cursor
        return GooglePullResult(events, next_cursor or cursor)

    def _event_from_change(self, change: dict[str, Any]) -> ConnectorSourceEvent:
        file_id = str(change.get("fileId") or "")
        if not file_id:
            raise ValidationError("Drive change missing fileId")
        removed = bool(change.get("removed"))
        file = change.get("file") if isinstance(change.get("file"), dict) else {}
        name = str(file.get("name") or file_id)
        modified = str(file.get("modifiedTime") or change.get("time") or "")
        event_type = "file_removed" if removed else "file_changed"
        version_key = "removed" if removed else (file.get("md5Checksum") or modified or change.get("time") or "changed")
        content = self._content_for_file(file_id, file, removed)
        payload = {"change": change}
        return ConnectorSourceEvent(
            dedupe_key=f"drive:{file_id}:{version_key}",
            external_id=file_id,
            event_type=event_type,
            source_key=f"google-drive/{file_id}/{name}",
            locator=str(file.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}"),
            content=content,
            payload=payload,
            observed_at=modified or None,
        )

    def _content_for_file(self, file_id: str, file: dict[str, Any], removed: bool) -> str:
        if removed:
            return f"# Google Drive removal\n\nFile `{file_id}` was removed."
        mime_type = str(file.get("mimeType") or "")
        name = str(file.get("name") or file_id)
        if mime_type == GOOGLE_DOC:
            response = self._get(f"{DRIVE_API}/files/{urllib.parse.quote(file_id)}/export?mimeType=text/plain")
            if 200 <= response.status < 300:
                return response.text
        if mime_type in {"text/plain", "text/markdown"}:
            response = self._get(f"{DRIVE_API}/files/{urllib.parse.quote(file_id)}?alt=media")
            if 200 <= response.status < 300:
                return response.text
        metadata = {
            "id": file_id,
            "name": name,
            "mimeType": mime_type,
            "modifiedTime": file.get("modifiedTime"),
            "webViewLink": file.get("webViewLink"),
        }
        return "# Google Drive file metadata\n\n```json\n" + json.dumps(metadata, sort_keys=True, indent=2) + "\n```"

    def _get(self, url: str) -> HttpResponse:
        return self.transport("GET", url, {"Authorization": f"Bearer {self.access_token}"}, None)


def _json(response: HttpResponse) -> dict[str, Any]:
    if response.status < 200 or response.status >= 300:
        raise ValidationError(f"Google Drive returned HTTP {response.status}")
    if not isinstance(response.json_body, dict):
        raise ValidationError("Google Drive response was not JSON")
    return response.json_body


def _rate_limited(response: HttpResponse) -> GooglePullResult | None:
    if response.status in {403, 429}:
        return GooglePullResult(
            [],
            None,
            rate_limited=True,
            retry_after_seconds=retry_after_seconds(response),
            error=response.text or f"Google Drive returned HTTP {response.status}",
        )
    return None
