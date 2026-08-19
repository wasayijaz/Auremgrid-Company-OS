"""Gmail read connector with label routing and gap-free history checkpoints."""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from auremgrid.connectors.google_auth import (
    GMAIL_READ_SCOPES, ConnectorSourceEvent, GoogleApiFailure, GoogleRequestError, HttpResponse, HttpTransport,
    RouteLifecycleMutation, UrllibTransport, classify_google_failure, require_any_scope,
    sanitize_google_payload,
)
from auremgrid.domain.errors import AuthorizationError, ValidationError

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_CURSOR_VERSION = 1


@dataclass(frozen=True)
class GmailAccountIdentity:
    email_address: str
    history_id: str
    granted_scopes: frozenset[str]


@dataclass(frozen=True)
class GmailPullResult:
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


class GmailConnector:
    name = "gmail"

    def __init__(
        self, access_token: str, transport: HttpTransport | None = None, *,
        label_workspace_mappings: Mapping[str, str] | None = None,
        expected_account_id: str | None = None, granted_scopes: Iterable[str] = (),
        route_state: Mapping[str, Iterable[str]] | None = None,
        backfill_page_size: int = 100,
    ) -> None:
        if not access_token:
            raise ValidationError("access_token is required")
        if not 1 <= backfill_page_size <= 500:
            raise ValidationError("backfill_page_size must be between 1 and 500")
        self.access_token = access_token
        self.transport = transport or UrllibTransport()
        raw_mappings = dict(label_workspace_mappings or {})
        invalid = [key for key in raw_mappings if not str(key).startswith("label:")]
        if invalid:
            raise ValidationError("Gmail mappings must use canonical label:<id> keys")
        self.label_workspace_mappings = {str(key).split(":", 1)[1]: value for key, value in raw_mappings.items()}
        self.expected_account_id = expected_account_id
        self.granted_scopes = frozenset(str(scope) for scope in granted_scopes if scope)
        self.route_state = {
            str(key): {str(route).split(":", 1)[1] if str(route).startswith("label:") else str(route) for route in value}
            for key, value in (route_state or {}).items()
        }
        self.backfill_page_size = backfill_page_size

    def verify_credentials(self) -> GmailAccountIdentity:
        scopes = require_any_scope(self.granted_scopes, GMAIL_READ_SCOPES, "Gmail")
        response = self._get(f"{GMAIL_API}/users/me/profile")
        self._raise_failure(response)
        profile = _json(response)
        identity = GmailAccountIdentity(
            str(profile.get("emailAddress") or ""), str(profile.get("historyId") or ""), scopes,
        )
        if not identity.email_address or not identity.history_id:
            raise AuthorizationError("Gmail account identity is incomplete")
        if self.expected_account_id and identity.email_address.casefold() != self.expected_account_id.casefold():
            raise AuthorizationError("Gmail account identity mismatch")
        self.validate_mappings()
        return identity

    def validate_mappings(self) -> None:
        response = self._get(f"{GMAIL_API}/users/me/labels")
        self._raise_failure(response)
        available = {str(item.get("id")) for item in _json(response).get("labels") or [] if isinstance(item, dict) and item.get("id")}
        missing = set(self.label_workspace_mappings).difference(available)
        if missing:
            raise ValidationError("Gmail label mapping is unavailable: " + ", ".join(sorted(missing)))

    def pull(self, cursor: str | None) -> GmailPullResult:
        try:
            return self._pull(cursor)
        except GoogleRequestError as exc:
            return _failure_result(cursor, exc.failure)

    def _pull(self, cursor: str | None) -> GmailPullResult:
        state = _parse_cursor(cursor)
        if state is None:
            response = self._get(f"{GMAIL_API}/users/me/profile")
            failure = classify_google_failure(response, "Gmail")
            if failure:
                return _failure_result(None, failure)
            checkpoint = _json(response).get("historyId")
            if not isinstance(checkpoint, str) or not checkpoint:
                raise ValidationError("Gmail profile response missing historyId")
            if not self.label_workspace_mappings:
                return GmailPullResult([], _encode_cursor(_history_state(checkpoint)))
            state = {"v": 1, "phase": "backfill", "checkpoint": checkpoint, "label_index": 0, "page_token": None}
        return self._pull_backfill(state) if state["phase"] == "backfill" else self._pull_history(state)

    def _pull_backfill(self, state: dict[str, Any]) -> GmailPullResult:
        labels = sorted(self.label_workspace_mappings)
        if state["label_index"] >= len(labels):
            raise ValidationError("Gmail backfill cursor label_index is outside configured mappings")
        label_id = labels[int(state["label_index"])]
        params: list[tuple[str, str]] = [("labelIds", label_id), ("maxResults", str(self.backfill_page_size)), ("includeSpamTrash", "false")]
        if state.get("page_token"):
            params.append(("pageToken", str(state["page_token"])))
        response = self._get(f"{GMAIL_API}/users/me/messages?{urllib.parse.urlencode(params)}")
        failure = classify_google_failure(response, "Gmail")
        if failure:
            return _failure_result(_encode_cursor(state), failure)
        data = _json(response)
        events: list[ConnectorSourceEvent] = []
        mutations: list[RouteLifecycleMutation] = []
        messages = _list_page(data, "messages", "Gmail messages page")
        for stub in messages:
            if not isinstance(stub, dict) or not isinstance(stub.get("id"), str) or not stub["id"]:
                raise ValidationError("Gmail messages page contains an invalid message")
            event, event_mutations = self._message_event(stub["id"], str(state["checkpoint"]), "message_discovered", (label_id,))
            events.append(event)
            mutations.extend(event_mutations)
        state["page_token"] = _optional_token(data, "nextPageToken", "Gmail messages page")
        if not state["page_token"]:
            state["label_index"] = int(state["label_index"]) + 1
        has_more = int(state["label_index"]) < len(labels)
        if not has_more:
            state = _history_state(str(state["checkpoint"]), None)
            has_more = True  # close the no-gap window from the pre-backfill baseline
        return GmailPullResult(events, _encode_cursor(state), has_more=has_more, lifecycle_mutations=tuple(mutations))

    def _pull_history(self, state: dict[str, Any]) -> GmailPullResult:
        params: list[tuple[str, str]] = [("startHistoryId", str(state["checkpoint"]))]
        for history_type in ("messageAdded", "messageDeleted", "labelAdded", "labelRemoved"):
            params.append(("historyTypes", history_type))
        if state.get("page_token"):
            params.append(("pageToken", str(state["page_token"])))
        response = self._get(f"{GMAIL_API}/users/me/history?{urllib.parse.urlencode(params)}")
        if response.status == 404:
            return GmailPullResult([], None, cursor_expired=True, error="Gmail history checkpoint expired",
                                   error_code="cursor_expired")
        failure = classify_google_failure(response, "Gmail")
        if failure:
            return _failure_result(_encode_cursor(state), failure)
        data = _json(response)
        events: list[ConnectorSourceEvent] = []
        mutations: list[RouteLifecycleMutation] = []
        histories = _list_page(data, "history", "Gmail history page")
        for history in histories:
            if not isinstance(history, dict) or not isinstance(history.get("id"), str) or not history["id"]:
                raise ValidationError("Gmail history page contains an invalid history record")
            history_id = history["id"]
            for collection, event_type in (
                ("messagesAdded", "message_added"), ("messagesDeleted", "message_deleted"),
                ("labelsAdded", "labels_added"), ("labelsRemoved", "labels_removed"),
            ):
                collection_value = history.get(collection, [])
                if not isinstance(collection_value, list):
                    raise ValidationError(f"Gmail history record contains invalid {collection}")
                for item in collection_value:
                    if not isinstance(item, dict):
                        raise ValidationError(f"Gmail history record contains invalid {collection} item")
                    message = item.get("message") if isinstance(item.get("message"), dict) else {}
                    message_id = str(message.get("id") or "")
                    if not message_id:
                        raise ValidationError("Gmail history transition missing message id")
                    raw_labels = item.get("labelIds") or message.get("labelIds") or []
                    if not isinstance(raw_labels, list) or any(not isinstance(label, str) or not label for label in raw_labels):
                        raise ValidationError("Gmail history transition contains invalid labels")
                    label_ids = tuple(sorted(set(raw_labels)))
                    relevant = tuple(label for label in label_ids if label in self.label_workspace_mappings)
                    if event_type in {"message_added", "labels_added"}:
                        event, event_mutations = self._message_event(message_id, history_id, event_type, relevant)
                        if event.payload.get("route_keys"):
                            events.append(event)
                            mutations.extend(event_mutations)
                    else:
                        removed_routes = relevant or tuple(sorted(self.route_state.get(message_id) or ()))
                        if removed_routes:
                            event = self._tombstone_event(message_id, history_id, event_type, removed_routes)
                            events.append(event)
                            mutations.extend(self._mutations(
                                message_id, removed_routes, "tombstone", history_id, event.dedupe_key
                            ))
        state["page_token"] = _optional_token(data, "nextPageToken", "Gmail history page")
        terminal_history_id = _optional_token(data, "historyId", "Gmail history page")
        if not state["page_token"]:
            if not terminal_history_id:
                raise ValidationError("Gmail terminal history page missing historyId")
            state["checkpoint"] = terminal_history_id
        return GmailPullResult(events, _encode_cursor(state), has_more=bool(state["page_token"]), lifecycle_mutations=tuple(mutations))

    def _message_event(self, message_id: str, history_id: str, event_type: str,
                       hinted_labels: tuple[str, ...]) -> tuple[ConnectorSourceEvent, list[RouteLifecycleMutation]]:
        query = urllib.parse.urlencode({"format": "metadata", "metadataHeaders": ["Subject", "From", "To", "Date"]}, doseq=True)
        response = self._get(f"{GMAIL_API}/users/me/messages/{urllib.parse.quote(message_id)}?{query}")
        failure = classify_google_failure(response, "Gmail message fetch")
        if failure:
            raise GoogleRequestError(failure)
        data = _json(response)
        if data.get("id") != message_id:
            raise ValidationError("Gmail message response identity mismatch")
        raw_labels = data.get("labelIds") if "labelIds" in data else list(hinted_labels)
        if not isinstance(raw_labels, list) or any(not isinstance(label, str) or not label for label in raw_labels):
            raise ValidationError("Gmail message response labels are invalid")
        payload_node = data.get("payload")
        if not isinstance(payload_node, dict):
            raise ValidationError("Gmail message response payload is invalid")
        header_items = payload_node.get("headers")
        if not isinstance(header_items, list) or any(not isinstance(item, dict) for item in header_items):
            raise ValidationError("Gmail message response headers are invalid")
        labels = tuple(sorted({label for label in raw_labels if label in self.label_workspace_mappings}))
        self._reject_overlap(labels)
        headers = {str(item.get("name")): str(item.get("value") or "") for item in header_items}
        subject = headers.get("Subject") or "(no subject)"
        snippet = str(sanitize_google_payload(str(data.get("snippet") or ""), (self.access_token,)))
        observed_at = _millis_to_iso(int(data["internalDate"])) if str(data.get("internalDate") or "").isdigit() else None
        payload = sanitize_google_payload({
            "message_id": message_id, "thread_id": data.get("threadId"), "label_ids": list(labels),
            "route_keys": [f"label:{label}" for label in labels], "workspace_ids": sorted({self.label_workspace_mappings[label] for label in labels}),
            "history_id": history_id,
        }, (self.access_token,))
        event = ConnectorSourceEvent(
            f"gmail:{message_id}:{history_id}:{event_type}", message_id, event_type,
            f"gmail/messages/{message_id}", f"https://mail.google.com/mail/u/0/#all/{message_id}",
            f"# Gmail message\n\nSubject: {subject}\nFrom: {headers.get('From', '')}\nTo: {headers.get('To', '')}\nDate: {headers.get('Date', '')}\n\n{snippet}",
            payload, observed_at,
        )
        return event, self._mutations(message_id, labels, "upsert", history_id, event.dedupe_key)

    def _tombstone_event(self, message_id: str, history_id: str, event_type: str,
                         labels: tuple[str, ...]) -> ConnectorSourceEvent:
        self._reject_overlap(labels)
        return ConnectorSourceEvent(
            f"gmail:{message_id}:{history_id}:{event_type}", message_id, event_type,
            f"gmail/messages/{message_id}", f"https://mail.google.com/mail/u/0/#all/{message_id}",
            f"# Gmail tombstone\n\nMessage `{message_id}` left the configured route.",
            {"message_id": message_id, "route_keys": [f"label:{label}" for label in labels],
             "workspace_ids": sorted({self.label_workspace_mappings[label] for label in labels}), "history_id": history_id},
        )

    def _mutations(self, message_id: str, labels: Iterable[str], operation: str,
                   history_id: str, event_dedupe_key: str) -> list[RouteLifecycleMutation]:
        return [
            RouteLifecycleMutation(
                message_id, f"label:{label}", self.label_workspace_mappings[label],
                operation, history_id, event_dedupe_key,
            )
            for label in labels
        ]

    def _reject_overlap(self, labels: Iterable[str]) -> None:
        workspaces = {self.label_workspace_mappings[label] for label in labels}
        if len(workspaces) > 1:
            raise ValidationError("Gmail message overlaps mappings for different workspaces")

    def _raise_failure(self, response: HttpResponse) -> None:
        failure = classify_google_failure(response, "Gmail")
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
        raise ValidationError("Gmail cursor is invalid") from exc
    if not isinstance(state, dict) or state.get("v") != _CURSOR_VERSION or state.get("phase") not in {"backfill", "history"}:
        raise ValidationError("Gmail cursor is unsupported")
    expected = (
        {"v", "phase", "checkpoint", "label_index", "page_token"}
        if state["phase"] == "backfill"
        else {"v", "phase", "checkpoint", "page_token"}
    )
    if set(state) != expected:
        raise ValidationError("Gmail cursor fields are invalid")
    if not isinstance(state.get("checkpoint"), str) or not state["checkpoint"]:
        raise ValidationError("Gmail cursor checkpoint is invalid")
    if state["page_token"] is not None and (
        not isinstance(state["page_token"], str) or not state["page_token"]
    ):
        raise ValidationError("Gmail page cursor is invalid")
    if state["phase"] == "backfill" and (
        not isinstance(state["label_index"], int) or isinstance(state["label_index"], bool)
        or state["label_index"] < 0
    ):
        raise ValidationError("Gmail cursor label_index is invalid")
    return state


def _history_state(checkpoint: str, page_token: str | None = None) -> dict[str, Any]:
    return {"v": 1, "phase": "history", "checkpoint": checkpoint, "page_token": page_token}


def _encode_cursor(state: dict[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def _json(response: HttpResponse) -> dict[str, Any]:
    if not isinstance(response.json_body, dict):
        raise ValidationError("Gmail response was not JSON")
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


def _failure_result(cursor: str | None, failure: GoogleApiFailure) -> GmailPullResult:
    return GmailPullResult([], cursor, failure.code in {"quota_exhausted", "rate_limited"},
                           failure.retry_after_seconds, False, failure.message, failure.code, failure.retryable)


def _millis_to_iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(microsecond=0).isoformat()
