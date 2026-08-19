"""Google Drive read connector with bounded, gap-free backfill and change checkpoints."""
from __future__ import annotations

import json
import hashlib
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from auremgrid.connectors.google_auth import (
    DRIVE_READ_SCOPES, ConnectorSourceEvent, GoogleApiFailure, GoogleRequestError, HttpResponse, HttpTransport,
    RouteLifecycleMutation, UrllibTransport, classify_google_failure, require_any_scope, sanitize_google_payload,
)
from auremgrid.domain.errors import AuthorizationError, ValidationError

DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_FOLDER = "application/vnd.google-apps.folder"
_CURSOR_VERSION = 1


@dataclass(frozen=True)
class DriveAccountIdentity:
    account_id: str
    email_address: str
    display_name: str
    granted_scopes: frozenset[str]


@dataclass(frozen=True)
class DriveBackfillTask:
    route_key: str
    container_id: str
    page_token: str | None = None


@dataclass(frozen=True)
class DriveReconciliationRequest:
    external_id: str
    parent_ids: tuple[str, ...]
    reason: str
    descendants: bool = False


@dataclass(frozen=True)
class GooglePullResult:
    events: list[ConnectorSourceEvent]
    next_cursor: str | None
    rate_limited: bool = False
    retry_after_seconds: int | None = None
    cursor_expired: bool = False
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False
    has_more: bool = False
    lifecycle_mutations: tuple[RouteLifecycleMutation, ...] = ()
    backfill_tasks: tuple[DriveBackfillTask, ...] = ()
    reconciliation_requests: tuple[DriveReconciliationRequest, ...] = ()


class GoogleDriveConnector:
    name = "google_drive"

    def __init__(
        self, access_token: str, transport: HttpTransport | None = None, *,
        folder_workspace_mappings: Mapping[str, str] | None = None,
        shared_drive_workspace_mappings: Mapping[str, str] | None = None,
        expected_account_id: str | None = None, granted_scopes: Iterable[str] = (),
        route_state: Mapping[str, Iterable[str]] | None = None,
        ancestry_state: Mapping[str, Mapping[str, Any] | Iterable[str]] | None = None,
        backfill_task: DriveBackfillTask | None = None, backfill_page_size: int = 100,
    ) -> None:
        if not access_token:
            raise ValidationError("access_token is required")
        if not 1 <= backfill_page_size <= 1000:
            raise ValidationError("backfill_page_size must be positive and bounded")
        self.access_token = access_token
        self.transport = transport or UrllibTransport()
        self.folder_workspace_mappings = dict(folder_workspace_mappings or {})
        self.shared_drive_workspace_mappings = dict(shared_drive_workspace_mappings or {})
        self.expected_account_id = expected_account_id
        self.granted_scopes = frozenset(str(scope) for scope in granted_scopes if scope)
        self.route_state = {str(key): set(value) for key, value in (route_state or {}).items()}
        self.ancestry_state = dict(ancestry_state or {})
        self.backfill_task = backfill_task
        self.backfill_page_size = backfill_page_size

    def verify_credentials(self) -> DriveAccountIdentity:
        scopes = require_any_scope(self.granted_scopes, DRIVE_READ_SCOPES, "Google Drive")
        query = urllib.parse.urlencode({"fields": "user(displayName,emailAddress,permissionId)"})
        response = self._get(f"{DRIVE_API}/about?{query}")
        self._raise_failure(response)
        user = _json(response).get("user")
        if not isinstance(user, dict):
            raise AuthorizationError("Google Drive account identity is unavailable")
        identity = DriveAccountIdentity(
            str(user.get("permissionId") or ""), str(user.get("emailAddress") or ""),
            str(user.get("displayName") or ""), scopes,
        )
        if not identity.account_id or not identity.email_address:
            raise AuthorizationError("Google Drive account identity is incomplete")
        if self.expected_account_id and self.expected_account_id != identity.account_id:
            raise AuthorizationError("Google Drive account identity mismatch")
        self.validate_mappings()
        return identity

    def validate_mappings(self) -> None:
        for folder_id in self.folder_workspace_mappings:
            query = urllib.parse.urlencode({"fields": "id,name,mimeType,trashed,driveId"})
            response = self._get(f"{DRIVE_API}/files/{urllib.parse.quote(folder_id)}?{query}")
            self._raise_failure(response)
            folder = _json(response)
            if folder.get("mimeType") != GOOGLE_FOLDER or folder.get("trashed") is True:
                raise ValidationError(f"Google Drive mapping is not an active folder: {folder_id}")
        for drive_id in self.shared_drive_workspace_mappings:
            query = urllib.parse.urlencode({"fields": "id,name"})
            response = self._get(f"{DRIVE_API}/drives/{urllib.parse.quote(drive_id)}?{query}")
            self._raise_failure(response)
            if str(_json(response).get("id") or "") != drive_id:
                raise AuthorizationError("Google shared-drive identity mismatch")

    def pull(self, cursor: str | None) -> GooglePullResult:
        try:
            return self._pull(cursor)
        except GoogleRequestError as exc:
            return _failure_result(cursor, exc.failure)

    def _pull(self, cursor: str | None) -> GooglePullResult:
        state = _parse_cursor(cursor)
        if state is None:
            response = self._get(f"{DRIVE_API}/changes/startPageToken")
            failure = classify_google_failure(response, "Google Drive")
            if failure:
                return _failure_result(None, failure)
            checkpoint = _json(response).get("startPageToken")
            if not isinstance(checkpoint, str) or not checkpoint:
                raise ValidationError("Drive startPageToken response missing token")
            if not self._routes():
                return GooglePullResult([], _encode_cursor(_changes_state(checkpoint)))
            state = {"v": 1, "phase": "backfill", "checkpoint": checkpoint}
        return self._pull_backfill(state) if state["phase"] == "backfill" else self._pull_changes(state)

    def _pull_backfill(self, state: dict[str, Any]) -> GooglePullResult:
        initial = tuple(DriveBackfillTask(route, route.split(":", 1)[1]) for route, _ in self._routes())
        if self.backfill_task is None and not initial:
            raise ValidationError("Drive backfill cursor requires a configured route or durable task")
        task = self.backfill_task or initial[0]
        if task.route_key not in {route for route, _ in self._routes()} or not task.container_id:
            raise ValidationError("Drive backfill task is outside configured routes")
        remaining = list(initial[1:]) if self.backfill_task is None else []
        response = self._get(
            f"{DRIVE_API}/files?{urllib.parse.urlencode(self._backfill_params(task.route_key, task.container_id, task.page_token))}"
        )
        failure = classify_google_failure(response, "Google Drive")
        if failure:
            return _failure_result(_encode_cursor(state), failure)
        data = _json(response)
        events: list[ConnectorSourceEvent] = []
        mutations: list[RouteLifecycleMutation] = []
        files = _list_page(data, "files", "Drive files page")
        for file in files:
            if not isinstance(file, dict) or not isinstance(file.get("id"), str) or not file["id"]:
                raise ValidationError("Drive files page contains an invalid file")
            file_id = file["id"]
            event = self._event_for_file(file, "file_discovered", (task.route_key,), "backfill")
            events.append(event)
            mutations.extend(self._mutations(
                file_id, (task.route_key,), "upsert",
                str(file.get("modifiedTime") or "backfill"), event.dedupe_key,
            ))
            if task.route_key.startswith("folder:") and file.get("mimeType") == GOOGLE_FOLDER:
                remaining.append(DriveBackfillTask(task.route_key, file_id))
        next_page = _optional_token(data, "nextPageToken", "Drive files page")
        if next_page:
            remaining.insert(0, DriveBackfillTask(task.route_key, task.container_id, next_page))
        # The task queue is an explicit durable output. It is intentionally not
        # embedded in the cursor, so large folder trees cannot make cursors unbounded.
        next_state = state if remaining else _changes_state(str(state["checkpoint"]))
        return GooglePullResult(events, _encode_cursor(next_state), has_more=True,
                                lifecycle_mutations=tuple(mutations), backfill_tasks=tuple(remaining))

    def _backfill_params(self, route_key: str, container_id: str, page_token: Any) -> dict[str, str]:
        params = {
            "pageSize": str(self.backfill_page_size), "spaces": "drive", "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,webViewLink,trashed,parents,driveId)",
        }
        kind, identifier = route_key.split(":", 1)
        if kind == "folder":
            params["q"] = f"'{container_id}' in parents and trashed = false"
        else:
            params.update({"corpora": "drive", "driveId": identifier, "q": "trashed = false"})
        if page_token:
            params["pageToken"] = str(page_token)
        return params

    def _pull_changes(self, state: dict[str, Any]) -> GooglePullResult:
        events: list[ConnectorSourceEvent] = []
        mutations: list[RouteLifecycleMutation] = []
        page_token = str(state.get("page_token") or state["checkpoint"])
        params = {
                "pageToken": page_token, "pageSize": "100", "spaces": "drive", "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "fields": "nextPageToken,newStartPageToken,changes(fileId,removed,time,driveId,file(id,name,mimeType,modifiedTime,md5Checksum,webViewLink,trashed,parents,driveId))",
            }
        response = self._get(f"{DRIVE_API}/changes?{urllib.parse.urlencode(params)}")
        if response.status == 410:
            return GooglePullResult([], None, cursor_expired=True, error="Google Drive changes checkpoint expired",
                                    error_code="cursor_expired")
        failure = classify_google_failure(response, "Google Drive")
        if failure:
            return _failure_result(_encode_cursor(state), failure)
        data = _json(response)
        changes = _list_page(data, "changes", "Drive changes page")
        next_page = _optional_token(data, "nextPageToken", "Drive changes page")
        new_checkpoint = _optional_token(data, "newStartPageToken", "Drive changes page")
        if not next_page and not new_checkpoint:
            raise ValidationError("Drive terminal changes page missing newStartPageToken")
        reconciliations: list[DriveReconciliationRequest] = []
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                raise ValidationError("Drive changes page contains an invalid change")
            transition_id = _transition_id(page_token, index, change)
            event, event_mutations, requests = self._event_from_change(change, transition_id)
            if event:
                events.append(event)
                mutations.extend(event_mutations)
            reconciliations.extend(requests)
        state["page_token"] = next_page
        if not next_page:
            state["checkpoint"] = new_checkpoint
        return GooglePullResult(events, _encode_cursor(state), has_more=bool(state["page_token"]),
                                lifecycle_mutations=tuple(mutations),
                                reconciliation_requests=tuple(reconciliations))

    def _event_from_change(
        self,
        change: dict[str, Any],
        transition_id: str | None = None,
    ) -> tuple[ConnectorSourceEvent | None, list[RouteLifecycleMutation], list[DriveReconciliationRequest]]:
        file_id = str(change.get("fileId") or "")
        if not file_id:
            raise ValidationError("Drive change missing fileId")
        previous = tuple(sorted(self.route_state.get(file_id) or ()))
        file = change.get("file") if isinstance(change.get("file"), dict) else {}
        removed = bool(change.get("removed")) or file.get("trashed") is True
        current, unknown_parents = ((), ()) if removed else self._routes_for_file(file, change)
        requests: list[DriveReconciliationRequest] = []
        if unknown_parents:
            requests.append(DriveReconciliationRequest(file_id, unknown_parents, "unknown_ancestry"))
            # Preserve known durable membership. An incomplete ancestry projection
            # is never evidence that the object left a configured route.
            current = previous
        routed = tuple(sorted(set(previous).union(current)))
        if not routed:
            return None, [], requests
        self._reject_overlap(routed)
        if removed:
            event_type = "file_removed"
        elif previous and not current:
            event_type = "file_moved_out"
        elif set(current) != set(previous):
            event_type = "file_moved"
        else:
            event_type = "file_changed"
        version = str(file.get("md5Checksum") or file.get("modifiedTime") or change.get("time") or transition_id or event_type)
        if removed:
            version = transition_id or version
        event = self._event_for_file(
            file, event_type, routed, "changes", change=change,
            file_id=file_id, transition_id=transition_id,
        )
        mutations = self._mutations(file_id, current, "upsert", version, event.dedupe_key)
        if not unknown_parents:
            mutations.extend(self._mutations(
                file_id, set(previous).difference(current), "tombstone", version, event.dedupe_key
            ))
        if file.get("mimeType") == GOOGLE_FOLDER and set(current) != set(previous):
            requests.append(DriveReconciliationRequest(file_id, tuple(str(item) for item in file.get("parents") or ()), "folder_moved", True))
        return (
            event,
            mutations,
            requests,
        )

    def _event_for_file(self, file: dict[str, Any], event_type: str, route_keys: tuple[str, ...], phase: str,
                        *, change: dict[str, Any] | None = None, file_id: str | None = None,
                        transition_id: str | None = None) -> ConnectorSourceEvent:
        file_id = file_id or str(file.get("id") or "")
        name = str(file.get("name") or file_id)
        modified = str(file.get("modifiedTime") or (change or {}).get("time") or "")
        version = transition_id if event_type in {"file_removed", "file_moved_out"} and transition_id else (
            file.get("md5Checksum") or modified or (change or {}).get("time") or event_type)
        removed = event_type in {"file_removed", "file_moved_out"}
        payload = sanitize_google_payload({
            "file": {k: file.get(k) for k in ("id", "name", "mimeType", "modifiedTime", "md5Checksum", "webViewLink", "trashed", "parents", "driveId")},
            "change": {k: (change or {}).get(k) for k in ("fileId", "removed", "time", "driveId")},
            "route_keys": list(route_keys),
            "workspace_ids": sorted({self._workspace_for_route(route) for route in route_keys}),
            "phase": phase,
        }, (self.access_token,))
        return ConnectorSourceEvent(
            f"drive:{file_id}:{version}:{event_type}", file_id, event_type,
            f"google-drive/files/{file_id}", str(file.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}"),
            self._content_for_file(file_id, file, removed), payload, modified or None,
        )

    def _routes(self) -> list[tuple[str, str]]:
        return ([(f"folder:{k}", v) for k, v in sorted(self.folder_workspace_mappings.items())] +
                [(f"drive:{k}", v) for k, v in sorted(self.shared_drive_workspace_mappings.items())])

    def _routes_for_file(
        self, file: dict[str, Any], change: dict[str, Any]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        raw_parents = file.get("parents") or []
        if not isinstance(raw_parents, list) or any(not isinstance(item, str) or not item for item in raw_parents):
            raise ValidationError("Drive file parents are invalid")
        parents = set(raw_parents)
        drive_id = str(file.get("driveId") or change.get("driveId") or "")
        routes = {f"folder:{key}" for key in self.folder_workspace_mappings if key in parents}
        unknown: set[str] = set()
        direct_parent_ids = set(self.folder_workspace_mappings)
        for parent_id in parents.difference(direct_parent_ids):
            ancestry = self.ancestry_state.get(parent_id)
            if ancestry is None:
                unknown.add(parent_id)
                continue
            if isinstance(ancestry, Mapping):
                if ancestry.get("reconciliation_required") or ancestry.get("reconciliation_status") == "required":
                    unknown.add(parent_id)
                    continue
                inherited = ancestry.get("route_keys") or ancestry.get("root_route_keys") or ()
                if isinstance(inherited, str):
                    try:
                        inherited = json.loads(inherited)
                    except json.JSONDecodeError as exc:
                        raise ValidationError("Drive ancestry root routes are invalid") from exc
            else:
                inherited = ancestry
            if not isinstance(inherited, Iterable) or isinstance(inherited, (str, bytes)):
                raise ValidationError("Drive ancestry root routes are invalid")
            inherited_routes = {str(route) for route in inherited if str(route)}
            configured = {route for route, _ in self._routes()}
            if not inherited_routes or not inherited_routes.issubset(configured):
                unknown.add(parent_id)
                continue
            routes.update(inherited_routes)
        if drive_id in self.shared_drive_workspace_mappings:
            routes.add(f"drive:{drive_id}")
        return tuple(sorted(routes)), tuple(sorted(unknown))

    def _workspace_for_route(self, route_key: str) -> str:
        kind, identifier = route_key.split(":", 1)
        return (self.folder_workspace_mappings if kind == "folder" else self.shared_drive_workspace_mappings)[identifier]

    def _mutations(self, file_id: str, routes: Iterable[str], operation: str,
                   version: str, event_dedupe_key: str) -> list[RouteLifecycleMutation]:
        return [
            RouteLifecycleMutation(
                file_id, route, self._workspace_for_route(route), operation, version, event_dedupe_key
            )
            for route in routes
        ]

    def _reject_overlap(self, routes: Iterable[str]) -> None:
        workspaces = {self._workspace_for_route(route) for route in routes}
        if len(workspaces) > 1:
            raise ValidationError("Google Drive file overlaps mappings for different workspaces")

    def _content_for_file(self, file_id: str, file: dict[str, Any], removed: bool) -> str:
        if removed:
            return f"# Google Drive tombstone\n\nFile `{file_id}` is no longer available in the configured route."
        mime_type = str(file.get("mimeType") or "")
        if mime_type == GOOGLE_DOC:
            response = self._get(f"{DRIVE_API}/files/{urllib.parse.quote(file_id)}/export?mimeType=text/plain")
            failure = classify_google_failure(response, "Google Drive content export")
            if failure:
                raise GoogleRequestError(failure)
            return str(sanitize_google_payload(response.text, (self.access_token,)))
        if mime_type in {"text/plain", "text/markdown"}:
            response = self._get(f"{DRIVE_API}/files/{urllib.parse.quote(file_id)}?alt=media")
            failure = classify_google_failure(response, "Google Drive content download")
            if failure:
                raise GoogleRequestError(failure)
            return str(sanitize_google_payload(response.text, (self.access_token,)))
        metadata = {k: file.get(k) for k in ("id", "name", "mimeType", "modifiedTime", "webViewLink")}
        return "# Google Drive file metadata\n\n```json\n" + json.dumps(sanitize_google_payload(metadata, (self.access_token,)), sort_keys=True, indent=2) + "\n```"

    def _raise_failure(self, response: HttpResponse) -> None:
        failure = classify_google_failure(response, "Google Drive")
        if failure:
            raise GoogleRequestError(failure)

    def _get(self, url: str) -> HttpResponse:
        return self.transport("GET", url, {"Authorization": f"Bearer {self.access_token}"}, None)


def _parse_cursor(cursor: str | None) -> dict[str, Any] | None:
    if cursor is None:
        return None
    try:
        state = json.loads(cursor)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("Google Drive cursor is invalid") from exc
    if not isinstance(state, dict) or state.get("v") != _CURSOR_VERSION or state.get("phase") not in {"backfill", "changes"}:
        raise ValidationError("Google Drive cursor is unsupported")
    expected = {"v", "phase", "checkpoint"} if state["phase"] == "backfill" else {
        "v", "phase", "checkpoint", "page_token"
    }
    if set(state) != expected:
        raise ValidationError("Google Drive cursor fields are invalid")
    if not isinstance(state.get("checkpoint"), str) or not state["checkpoint"]:
        raise ValidationError("Google Drive cursor checkpoint is invalid")
    if state["phase"] == "changes" and state["page_token"] is not None and (
        not isinstance(state["page_token"], str) or not state["page_token"]
    ):
        raise ValidationError("Google Drive page cursor is invalid")
    return state


def _changes_state(checkpoint: str) -> dict[str, Any]:
    return {"v": 1, "phase": "changes", "checkpoint": checkpoint, "page_token": None}


def _encode_cursor(state: dict[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def _json(response: HttpResponse) -> dict[str, Any]:
    if not isinstance(response.json_body, dict):
        raise ValidationError("Google Drive response was not JSON")
    return response.json_body


def _list_page(data: dict[str, Any], field: str, page_name: str) -> list[Any]:
    value = data.get(field)
    if not isinstance(value, list):
        raise ValidationError(f"{page_name} missing valid {field}")
    return value


def _optional_token(data: dict[str, Any], field: str, page_name: str) -> str | None:
    value = data.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{page_name} contains invalid {field}")
    return value


def _transition_id(page_token: str, index: int, change: dict[str, Any]) -> str:
    canonical = json.dumps(change, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(f"{page_token}\x1f{index}\x1f{canonical}".encode("utf-8")).hexdigest()[:24]


def _failure_result(cursor: str | None, failure: GoogleApiFailure) -> GooglePullResult:
    return GooglePullResult([], cursor, failure.code in {"quota_exhausted", "rate_limited"},
                            failure.retry_after_seconds, False, failure.message, failure.code, failure.retryable)
