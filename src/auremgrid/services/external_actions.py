"""Controlled external actions with outbox, human approval, fencing, and receipts.

Enforces:
1. Intent -> durable outbox record (queued)
2. Explicit human approval (required prior to dispatch)
3. Fenced dispatch (strict provider allowlist + organization scoping)
4. Immutable receipt and outcome tracking
5. Strictly offline/simulated execution (no live network calls)
6. Gmail draft-only invariant (sending is strictly prohibited)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _default_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


EXTERNAL_ACTION_PROVIDERS: frozenset[str] = frozenset({
    "gmail",
    "slack",
    "clickup",
    "google_drive",
    "client_portal",
})

ACTION_TYPE_TO_PROVIDER: dict[str, str] = {
    "gmail.draft": "gmail",
    "slack.reply": "slack",
    "clickup.task": "clickup",
    "drive.report": "google_drive",
    "portal.publish": "client_portal",
}


@dataclass(frozen=True)
class ExternalActionIntent:
    organization_id: str
    action_type: str
    provider: str
    payload: dict[str, Any]
    requested_by_id: str
    workspace_id: str | None = None
    requested_by_type: str = "person"
    reason: str = "External action execution"
    approver_person_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not self.organization_id or not self.organization_id.strip():
            raise ValidationError("organization_id is required")
        if self.action_type not in ACTION_TYPE_TO_PROVIDER:
            raise ValidationError(f"unsupported action_type: {self.action_type}")
        expected_provider = ACTION_TYPE_TO_PROVIDER[self.action_type]
        if self.provider != expected_provider:
            raise ValidationError(
                f"provider mismatch: action_type '{self.action_type}' requires provider '{expected_provider}', got '{self.provider}'"
            )
        if self.provider not in EXTERNAL_ACTION_PROVIDERS:
            raise ValidationError(f"unauthorized provider: {self.provider}")
        if not isinstance(self.payload, dict):
            raise ValidationError("payload must be a dictionary")
        if not self.requested_by_id or not self.requested_by_id.strip():
            raise ValidationError("requested_by_id is required")

        # Provider-specific validations
        validate_action_payload(self.action_type, self.payload)


def validate_action_payload(action_type: str, payload: Mapping[str, Any]) -> None:
    """Validate action-specific payload constraints, including the draft-only invariant for Gmail."""
    if action_type == "gmail.draft":
        # Strict invariant: Gmail must be draft-only, never send
        for banned_key in ("send", "send_immediately", "dispatch_now", "auto_send"):
            if banned_key in payload and bool(payload[banned_key]):
                raise ValidationError("Gmail action must be draft-only; sending is strictly prohibited")
        if str(payload.get("action", "")).lower() == "send" or str(payload.get("mode", "")).lower() == "send":
            raise ValidationError("Gmail action must be draft-only; sending is strictly prohibited")

        recipient = payload.get("recipient") or payload.get("to")
        if not recipient:
            raise ValidationError("gmail.draft requires recipient email")
        subject = payload.get("subject")
        if not subject or not str(subject).strip():
            raise ValidationError("gmail.draft requires non-empty subject")
        body = payload.get("body_html") or payload.get("body_text") or payload.get("body")
        if not body or not str(body).strip():
            raise ValidationError("gmail.draft requires non-empty body content")

    elif action_type == "slack.reply":
        channel_id = payload.get("channel_id") or payload.get("channel")
        if not channel_id or not str(channel_id).strip():
            raise ValidationError("slack.reply requires channel_id")
        text = payload.get("message_text") or payload.get("text")
        if not text or not str(text).strip():
            raise ValidationError("slack.reply requires non-empty message text")

    elif action_type == "clickup.task":
        list_id = payload.get("list_id") or payload.get("space_id")
        if not list_id or not str(list_id).strip():
            raise ValidationError("clickup.task requires list_id")
        name = payload.get("name") or payload.get("title")
        if not name or not str(name).strip():
            raise ValidationError("clickup.task requires non-empty task name")

    elif action_type == "drive.report":
        folder_id = payload.get("folder_id") or payload.get("destination_folder")
        if not folder_id or not str(folder_id).strip():
            raise ValidationError("drive.report requires folder_id")
        title = payload.get("title") or payload.get("name")
        if not title or not str(title).strip():
            raise ValidationError("drive.report requires non-empty report title")

    elif action_type == "portal.publish":
        title = payload.get("title") or payload.get("report_title")
        if not title or not str(title).strip():
            raise ValidationError("portal.publish requires non-empty title")


@dataclass(frozen=True)
class ExternalActionReceipt:
    receipt_id: str
    outbox_event_id: str
    organization_id: str
    workspace_id: str | None
    action_type: str
    provider: str
    external_reference_id: str
    status: str
    dispatched_at: str
    payload_hash: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SimulatedProviderDispatcher:
    """Fenced, offline provider dispatcher.

    Never performs live network calls; simulates external provider interactions
    within strict organization scoping and allowlists.
    """

    def __init__(self, new_id: Callable[[str], str] | None = None) -> None:
        self._new_id = new_id or _default_id
        self._custom_handlers: dict[str, Callable[[str, Mapping[str, Any]], dict[str, Any]]] = {}

    def register_handler(
        self,
        provider: str,
        handler: Callable[[str, Mapping[str, Any]], dict[str, Any]],
    ) -> None:
        if provider not in EXTERNAL_ACTION_PROVIDERS:
            raise ValidationError(f"cannot register handler for unknown provider: {provider}")
        self._custom_handlers[provider] = handler

    def dispatch(
        self,
        provider: str,
        organization_id: str,
        workspace_id: str | None,
        action_type: str,
        payload: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """Dispatch external action to fenced provider.

        Returns (external_reference_id, receipt_details).
        """
        if provider not in EXTERNAL_ACTION_PROVIDERS:
            raise AuthorizationError(f"provider '{provider}' is outside the authorized allowlist")

        # Check custom registered handler first
        if provider in self._custom_handlers:
            result = self._custom_handlers[provider](organization_id, payload)
            ref_id = str(result.get("external_reference_id", self._new_id("extref")))
            return ref_id, result

        # Built-in simulated handlers
        if provider == "gmail":
            return self._dispatch_gmail(organization_id, payload)
        elif provider == "slack":
            return self._dispatch_slack(organization_id, payload)
        elif provider == "clickup":
            return self._dispatch_clickup(organization_id, payload)
        elif provider == "google_drive":
            return self._dispatch_drive(organization_id, payload)
        elif provider == "client_portal":
            return self._dispatch_portal(organization_id, workspace_id, payload)
        else:
            raise ValidationError(f"unhandled provider: {provider}")

    def _dispatch_gmail(self, organization_id: str, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        draft_id = self._new_id("draft")
        details = {
            "provider": "gmail",
            "organization_id": organization_id,
            "draft_id": draft_id,
            "recipient": payload.get("recipient") or payload.get("to"),
            "subject": payload.get("subject"),
            "is_draft": True,
            "sent": False,
            "draft_created_at": _now(),
        }
        return draft_id, details

    def _dispatch_slack(self, organization_id: str, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        msg_id = self._new_id("slack_msg")
        channel_id = payload.get("channel_id") or payload.get("channel")
        thread_ts = payload.get("thread_ts")
        details = {
            "provider": "slack",
            "organization_id": organization_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "message_id": msg_id,
            "posted_at": _now(),
        }
        return msg_id, details

    def _dispatch_clickup(self, organization_id: str, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        task_id = self._new_id("clickup_task")
        details = {
            "provider": "clickup",
            "organization_id": organization_id,
            "task_id": task_id,
            "list_id": payload.get("list_id"),
            "name": payload.get("name") or payload.get("title"),
            "status": payload.get("status", "to_do"),
            "created_at": _now(),
        }
        return task_id, details

    def _dispatch_drive(self, organization_id: str, payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        file_id = self._new_id("drive_file")
        details = {
            "provider": "google_drive",
            "organization_id": organization_id,
            "file_id": file_id,
            "folder_id": payload.get("folder_id"),
            "title": payload.get("title") or payload.get("name"),
            "file_type": "report_document",
            "uploaded_at": _now(),
        }
        return file_id, details

    def _dispatch_portal(
        self,
        organization_id: str,
        workspace_id: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        pub_id = self._new_id("portal_pub")
        details = {
            "provider": "client_portal",
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "publication_id": pub_id,
            "title": payload.get("title"),
            "published_at": _now(),
        }
        return pub_id, details


class ExternalActionService:
    """Manages the full external action lifecycle: intent -> outbox -> approval -> fenced dispatch -> receipt."""

    def __init__(
        self,
        conn: sqlite3.Connection | Any,
        new_id: Callable[[str], str] | None = None,
        dispatcher: SimulatedProviderDispatcher | None = None,
    ) -> None:
        if hasattr(conn, "conn"):
            self.conn = conn.conn
        else:
            self.conn = conn
        self.new_id = new_id or _default_id
        self.dispatcher = dispatcher or SimulatedProviderDispatcher(new_id=self.new_id)

    def queue_action(self, intent: ExternalActionIntent) -> dict[str, Any]:
        """Validate intent, persist outbox_events record, and create matching approval request."""
        # Idempotency check
        if intent.idempotency_key:
            row = self.conn.execute(
                "SELECT * FROM outbox_events WHERE organization_id=? AND idempotency_key=?",
                (intent.organization_id, intent.idempotency_key),
            ).fetchone()
            if row:
                outbox_id = row["id"]
                app_row = self.conn.execute(
                    "SELECT * FROM approval_requests WHERE requested_for=?",
                    (outbox_id,),
                ).fetchone()
                return {
                    "outbox_event": dict(row),
                    "approval_request": dict(app_row) if app_row else None,
                    "reused": True,
                }

        outbox_id = self.new_id("outbox")
        approval_id = self.new_id("approval")
        now = _now()
        payload_hash = _hash(intent.payload)
        payload_json = _json(intent.payload)

        # 1. Insert into outbox_events table
        self.conn.execute(
            """
            INSERT INTO outbox_events(
                id, organization_id, workspace_id, aggregate_type, aggregate_id, event_type,
                payload, payload_hash, idempotency_key, status, attempts, max_attempts, next_attempt_at,
                lease_owner, lease_expires_at, lease_token, published_at, last_error, created_at, updated_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox_id,
                intent.organization_id,
                intent.workspace_id,
                "external_action",
                approval_id,  # aggregate_id references approval request
                intent.action_type,
                payload_json,
                payload_hash,
                intent.idempotency_key,
                "pending",
                0,
                3,
                now,
                None,
                None,
                None,
                None,
                None,
                now,
                now,
                1,
            ),
        )

        # 2. Insert matching approval_requests record
        approval_payload = {
            "outbox_event_id": outbox_id,
            "provider": intent.provider,
            "action_type": intent.action_type,
            "payload_hash": payload_hash,
            "action_payload": intent.payload,
        }
        self.conn.execute(
            """
            INSERT INTO approval_requests (
                id, organization_id, workspace_id, requested_by_type, requested_by_id,
                requested_for, action_type, payload, reason, approver_person_id,
                policy, status, approved_at, rejected_at, comments, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                approval_id,
                intent.organization_id,
                intent.workspace_id,
                intent.requested_by_type,
                intent.requested_by_id,
                outbox_id,  # requested_for links to outbox event
                f"external_action:{intent.action_type}",
                _json(approval_payload),
                intent.reason,
                intent.approver_person_id,
                "human",
                "pending",
                None,
                None,
                "",
                now,
            ),
        )
        self.conn.commit()

        outbox_row = dict(self.conn.execute("SELECT * FROM outbox_events WHERE id=?", (outbox_id,)).fetchone())
        approval_row = dict(self.conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone())

        return {
            "outbox_event": outbox_row,
            "approval_request": approval_row,
            "reused": False,
        }

    def decide_approval(
        self,
        organization_id: str,
        approver_person_id: str,
        approval_id: str,
        approved: bool,
        comments: str = "",
    ) -> dict[str, Any]:
        """Record explicit human approval decision."""
        row = self.conn.execute(
            "SELECT * FROM approval_requests WHERE organization_id=? AND id=?",
            (organization_id, approval_id),
        ).fetchone()
        if not row:
            raise NotFoundError(f"approval request not found: {approval_id}")

        if row["status"] != "pending":
            raise ValidationError(f"approval request already decided: {row['status']}")

        now = _now()
        status = "approved" if approved else "rejected"
        self.conn.execute(
            """
            UPDATE approval_requests
            SET status=?, approved_at=?, rejected_at=?, comments=?, approver_person_id=?
            WHERE id=?
            """,
            (
                status,
                now if approved else None,
                None if approved else now,
                comments,
                approver_person_id,
                approval_id,
            ),
        )
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone())

    def dispatch_action(
        self,
        organization_id: str,
        outbox_event_id: str,
        acting_person_id: str | None = None,
    ) -> ExternalActionReceipt:
        """Execute fenced dispatch after verifying approval, provider allowlist, and organization scoping."""
        # 1. Fetch outbox record
        row = self.conn.execute(
            "SELECT * FROM outbox_events WHERE id=?",
            (outbox_event_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"outbox event not found: {outbox_event_id}")

        # Organization scoping
        if row["organization_id"] != organization_id:
            raise AuthorizationError("outbox event belongs to another organization")

        if row["status"] == "published":
            raise ValidationError("outbox event has already been published")

        # 2. Fetch and verify approval
        app_row = self.conn.execute(
            "SELECT * FROM approval_requests WHERE requested_for=?",
            (outbox_event_id,),
        ).fetchone()
        if not app_row:
            raise ValidationError("no approval request linked to outbox event")

        if app_row["status"] == "pending":
            raise AuthorizationError("cannot dispatch external action: pending human approval")
        if app_row["status"] == "rejected":
            raise AuthorizationError("cannot dispatch external action: approval was rejected")
        if app_row["status"] != "approved":
            raise AuthorizationError(f"invalid approval status: {app_row['status']}")

        action_type = row["event_type"]
        provider = ACTION_TYPE_TO_PROVIDER.get(action_type)
        if not provider or provider not in EXTERNAL_ACTION_PROVIDERS:
            raise AuthorizationError(f"action provider '{provider}' is not allowed")

        payload = json.loads(row["payload"])
        workspace_id = row["workspace_id"]
        now = _now()

        # 3. Fenced dispatch
        try:
            ext_ref_id, details = self.dispatcher.dispatch(
                provider=provider,
                organization_id=organization_id,
                workspace_id=workspace_id,
                action_type=action_type,
                payload=payload,
            )
            receipt_status = "published"
            error_msg = None
        except Exception as exc:
            receipt_status = "failed"
            ext_ref_id = ""
            details = {"error": str(exc)}
            error_msg = str(exc)

        # 4. Record outcome in outbox_events
        receipt_id = self.new_id("rcpt")
        receipt = ExternalActionReceipt(
            receipt_id=receipt_id,
            outbox_event_id=outbox_event_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            action_type=action_type,
            provider=provider,
            external_reference_id=ext_ref_id,
            status=receipt_status,
            dispatched_at=now,
            payload_hash=row["payload_hash"],
            details=details,
        )

        receipt_json = _json(receipt.to_dict())
        self.conn.execute(
            """
            UPDATE outbox_events
            SET status=?, published_at=?, last_error=?, updated_at=?, attempts=attempts+1,
                lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL
            WHERE id=?
            """,
            (
                receipt_status,
                now if receipt_status == "published" else None,
                receipt_json if receipt_status == "published" else error_msg,
                now,
                outbox_event_id,
            ),
        )
        self.conn.commit()

        if receipt_status == "failed":
            raise ValidationError(f"external action dispatch failed: {error_msg}")

        return receipt

    def get_action_receipt(self, organization_id: str, outbox_event_id: str) -> ExternalActionReceipt | None:
        """Retrieve the recorded receipt for a dispatched outbox event."""
        row = self.conn.execute(
            "SELECT * FROM outbox_events WHERE id=? AND organization_id=?",
            (outbox_event_id, organization_id),
        ).fetchone()
        if not row or row["status"] != "published" or not row["last_error"]:
            return None

        try:
            data = json.loads(row["last_error"])
            return ExternalActionReceipt(**data)
        except Exception:
            return None

