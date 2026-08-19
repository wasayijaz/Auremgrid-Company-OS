from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Protocol

from auremgrid.domain.errors import NotFoundError, ValidationError
from auremgrid.services.secrets import redact


GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
DRIVE_READ_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive",
    }
)
GMAIL_READ_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.metadata",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://mail.google.com/",
    }
)
EVENT_TERMINAL_STATUSES = {"ingested", "skipped", "quarantined"}
JOB_TERMINAL_STATUSES = {"succeeded", "failed", "dead_letter", "cancelled"}


class HttpTransport(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> "HttpResponse":
        ...


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    json_body: Any = None
    text: str = ""


@dataclass(frozen=True)
class OAuthRefreshResult:
    access_token: str | None
    expires_at: str | None
    scopes: tuple[str, ...]
    rate_limited: bool = False
    retry_after_seconds: int | None = None
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class ConnectorSourceEvent:
    dedupe_key: str
    external_id: str
    event_type: str
    source_key: str
    locator: str
    content: str
    payload: dict[str, Any]
    observed_at: str | None = None
    media_type: str = "text/markdown"


@dataclass(frozen=True)
class GoogleApiFailure:
    """Sanitized, provider-independent classification for a Google API failure."""

    code: str
    status: int
    retryable: bool
    retry_after_seconds: int | None
    message: str


class GoogleRequestError(Exception):
    def __init__(self, failure: GoogleApiFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


@dataclass(frozen=True)
class RouteLifecycleMutation:
    """A durable route-state change the integration layer must commit with a batch."""

    external_id: str
    route_key: str
    workspace_id: str
    operation: str  # upsert | tombstone
    provider_version: str
    event_dedupe_key: str


_QUOTA_REASONS = frozenset(
    {
        "dailylimitexceeded",
        "downloadserviceforbidden",
        "quotaexceeded",
        "ratelimitexceeded",
        "resourcerate-limitexceeded",
        "resourceexhausted",
        "userratelimitexceeded",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "client_secret",
        "credentials",
        "raw",
        "refresh_token",
        "token",
    }
)


def normalize_scopes(scopes: Iterable[str]) -> frozenset[str]:
    return frozenset(str(scope).strip() for scope in scopes if str(scope).strip())


def require_any_scope(granted: Iterable[str], accepted: frozenset[str], provider: str) -> frozenset[str]:
    normalized = normalize_scopes(granted)
    if not normalized.intersection(accepted):
        raise ValidationError(f"{provider} credential lacks the required read-only scope")
    return normalized


def classify_google_failure(response: HttpResponse, provider: str) -> GoogleApiFailure | None:
    """Classify errors without leaking response bodies or credentials.

    Google uses HTTP 403 for both permanent permission failures and transient quota
    exhaustion.  The structured reason, rather than the status alone, decides which
    behavior callers should use.
    """

    if 200 <= response.status < 300:
        return None
    reasons = _google_error_reasons(response)
    reason = sorted(reasons)[0] if reasons else ""
    retry_after = retry_after_seconds(response)
    if response.status == 0:
        code, retryable = "network_error", True
    elif response.status == 401:
        code, retryable = "authorization_required", False
    elif response.status == 408:
        code, retryable = "request_timeout", True
    elif response.status == 425:
        code, retryable = "too_early", True
    elif response.status == 403 and reasons.intersection(_QUOTA_REASONS):
        code, retryable = "quota_exhausted", True
    elif response.status == 403:
        code, retryable = "permission_denied", False
    elif response.status == 429:
        code, retryable = "rate_limited", True
    elif 500 <= response.status <= 599:
        code, retryable = "provider_unavailable", True
    elif response.status == 404:
        code, retryable = "not_found", False
    else:
        code, retryable = "provider_error", False
    suffix = f" ({reason})" if reason else ""
    return GoogleApiFailure(
        code=code,
        status=response.status,
        retryable=retryable,
        retry_after_seconds=retry_after,
        message=f"{provider} request failed with HTTP {response.status}{suffix}",
    )


def sanitize_google_payload(value: Any, known_secrets: Iterable[str] = ()) -> Any:
    """Return JSON-safe provider evidence with secrets and raw message bodies removed."""

    secrets = tuple(str(item) for item in known_secrets if item)
    if isinstance(value, dict):
        return {
            str(key): sanitize_google_payload(item, secrets)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_google_payload(item, secrets) for item in value]
    if isinstance(value, str):
        safe = value
        for secret in secrets:
            safe = safe.replace(secret, "[REDACTED]")
        return redact(safe)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(redact(str(value)))


def _google_error_reasons(response: HttpResponse) -> frozenset[str]:
    payload = response.json_body if isinstance(response.json_body, dict) else {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    reasons: set[str] = set()
    for item in error.get("errors") or []:
        if isinstance(item, dict) and item.get("reason"):
            reasons.add(str(item["reason"]).strip().lower())
    details = error.get("details") or []
    for item in details:
        if isinstance(item, dict):
            reason = item.get("reason")
            if reason:
                reasons.add(str(reason).strip().lower())
    return frozenset(reasons)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return str(value)


def _coerce_now(value: datetime | str | None) -> datetime:
    if value is None:
        return utcnow()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def retry_after_seconds(response: HttpResponse) -> int | None:
    value = None
    for key, header_value in response.headers.items():
        if key.lower() == "retry-after":
            value = header_value
            break
    if not value:
        return None
    try:
        return max(int(value), 0)
    except ValueError:
        return None


class UrllibTransport:
    def __call__(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                text = raw.decode("utf-8")
                return HttpResponse(response.status, dict(response.headers), _json_or_none(text), text)
        except urllib.error.HTTPError as error:
            raw = error.read()
            text = raw.decode("utf-8")
            return HttpResponse(error.code, dict(error.headers), _json_or_none(text), text)
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            return HttpResponse(0, {}, {"error": {"errors": [{"reason": "networkError"}]}}, "")


class GoogleOAuthClient:
    def __init__(self, transport: HttpTransport | None = None) -> None:
        self.transport = transport or UrllibTransport()

    def refresh_access_token(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        scopes: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> OAuthRefreshResult:
        if not client_id or not client_secret or not refresh_token:
            raise ValidationError("client_id, client_secret, and refresh_token are required")
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        body = urllib.parse.urlencode(payload).encode("utf-8")
        response = self.transport(
            "POST",
            GOOGLE_TOKEN_ENDPOINT,
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
        )
        failure = classify_google_failure(response, "Google OAuth")
        if failure is not None:
            error_code = "authorization_required" if response.status == 400 else failure.code
            return OAuthRefreshResult(
                None,
                None,
                (),
                rate_limited=failure.code in {"quota_exhausted", "rate_limited"},
                retry_after_seconds=failure.retry_after_seconds,
                error=failure.message,
                error_code=error_code,
                retryable=failure.retryable if response.status != 400 else False,
            )
        data = response.json_body if isinstance(response.json_body, dict) else {}
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValidationError("Google token response did not include access_token")
        expires_in = int(data.get("expires_in") or 3600)
        issued_at = now or utcnow()
        # The token endpoint's response is authoritative. Requested scopes are
        # never treated as granted scopes when Google omits `scope`.
        scope_text = str(data.get("scope") or "")
        return OAuthRefreshResult(
            access_token=token,
            expires_at=(issued_at + timedelta(seconds=expires_in)).isoformat(),
            scopes=tuple(scope for scope in scope_text.split() if scope),
        )


class ConnectorInboxRepository:
    def __init__(self, conn: sqlite3.Connection, new_id: Callable[[str], str]) -> None:
        self.conn = conn
        self.new_id = new_id

    def get_cursor(
        self,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        cursor_type: str = "sync",
    ) -> str | None:
        return self.get_cursor_record(organization_id, workspace_id, connector, account_key, cursor_type)["cursor_value"]

    def get_cursor_record(
        self,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        cursor_type: str = "sync",
    ) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT * FROM connector_cursors
            WHERE organization_id=? AND workspace_id=? AND connector=? AND account_key=? AND cursor_type=?
            """,
            (organization_id, workspace_id, connector, account_key, cursor_type),
        ).fetchone()
        if row is None:
            return {"cursor_value": None, "version": 0}
        return dict(row)

    def record_pull(
        self,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        cursor_before: str | None,
        cursor_after: str | None,
        events: list[ConnectorSourceEvent],
        cursor_type: str = "sync",
        rate_limit_retry_after_seconds: int | None = None,
        rate_limit_reset_at: str | None = None,
        error: str | None = None,
        stream_lock_id: str | None = None,
        reservation_token: str | None = None,
        credential_binding_id: str | None = None,
        credential_generation: int | None = None,
        lifecycle_mutations: Iterable[RouteLifecycleMutation] = (),
        manage_transaction: bool = True,
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        error = str(redact(error))[:300] if error is not None else None
        status = "rate_limited" if rate_limit_retry_after_seconds is not None else "pending"
        if error and status != "rate_limited":
            status = "failed"
        cursor_version_before = self.get_cursor_record(
            organization_id, workspace_id, connector, account_key, cursor_type
        )["version"]
        batch_id = self.new_id("cbatch")
        with (self.conn if manage_transaction else nullcontext(self.conn)):
            self._assert_stream_fence(stream_lock_id,reservation_token,now)
            self._assert_credential_fence(credential_binding_id,credential_generation)
            self.conn.execute(
                """
                INSERT INTO connector_ingest_batches(
                    id, organization_id, workspace_id, connector, account_key, cursor_type,
                    cursor_before, cursor_version_before, cursor_after, status, event_count,
                    rate_limit_retry_after_seconds, rate_limit_reset_at, error, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    organization_id,
                    workspace_id,
                    connector,
                    account_key,
                    cursor_type,
                    cursor_before,
                    cursor_version_before,
                    cursor_after,
                    status,
                    len(events),
                    rate_limit_retry_after_seconds,
                    rate_limit_reset_at,
                    error,
                    now,
                    None,
                ),
            )
            stored_events = [
                self._store_or_reference_event(batch_id, organization_id, workspace_id, connector, account_key, event, now)
                for event in events
            ]
            self._stage_lifecycle_mutations(
                batch_id, organization_id, workspace_id, connector, account_key,
                stored_events, lifecycle_mutations, now,
            )
        return {**self.get_batch(batch_id), "events": stored_events}

    def claim_event(
        self,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        lease_owner: str,
        lease_seconds: int = 60,
        now: datetime | str | None = None,
        stream_lock_id: str | None = None,
        reservation_token: str | None = None,
        credential_binding_id: str | None = None,
        credential_generation: int | None = None,
    ) -> dict[str, Any] | None:
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")
        now_dt = _coerce_now(now)
        now_text = now_dt.isoformat()
        lease_token = self.new_id("lease")
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self.conn:
            self._assert_stream_fence(stream_lock_id,reservation_token,now_text)
            self._assert_credential_fence(credential_binding_id,credential_generation)
            row = self.conn.execute(
                """
                SELECT * FROM connector_source_events
                WHERE organization_id=? AND workspace_id=? AND connector=? AND account_key=?
                  AND available_at <= ?
                  AND (
                    status IN ('pending','failed')
                    OR (status='leased' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                  )
                ORDER BY received_at ASC, rowid ASC
                LIMIT 1
                """,
                (organization_id, workspace_id, connector, account_key, now_text, now_text),
            ).fetchone()
            if row is None:
                return None
            cursor = self.conn.execute(
                """
                UPDATE connector_source_events
                SET status='leased', attempts=attempts+1, lease_owner=?, lease_token=?,
                    lease_expires_at=?, version=version+1
                WHERE id=? AND version=?
                  AND available_at <= ?
                  AND (
                    status IN ('pending','failed')
                    OR (status='leased' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                  )
                """,
                (lease_owner, lease_token, lease_expires_at, row["id"], row["version"], now_text, now_text),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_event(row["id"])

    def heartbeat_event(
        self,
        event_id: str,
        lease_owner: str,
        lease_token: str,
        lease_seconds: int = 60,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")
        now_dt = _coerce_now(now)
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self.conn:
            event = self._leased_event(event_id, lease_owner, lease_token, now_dt.isoformat())
            self.conn.execute(
                "UPDATE connector_source_events SET lease_expires_at=?, version=version+1 WHERE id=? AND version=?",
                (lease_expires_at, event_id, event["version"]),
            )
        return self.get_event(event_id)

    def complete_event(
        self,
        event_id: str,
        lease_owner: str,
        lease_token: str,
        now: datetime | str | None = None,
        stream_lock_id: str | None = None,
        reservation_token: str | None = None,
        credential_binding_id: str | None = None,
        credential_generation: int | None = None,
    ) -> dict[str, Any]:
        now_text = _coerce_now(now).isoformat()
        if self.conn.in_transaction:
            self._assert_stream_fence(stream_lock_id,reservation_token,now_text)
            self._assert_credential_fence(credential_binding_id,credential_generation)
            event = self._leased_event(event_id, lease_owner, lease_token, now_text)
            self._complete_event_update(event_id, event, now_text)
        else:
            with self.conn:
                self._assert_stream_fence(stream_lock_id,reservation_token,now_text)
                self._assert_credential_fence(credential_binding_id,credential_generation)
                event = self._leased_event(event_id, lease_owner, lease_token, now_text)
                self._complete_event_update(event_id, event, now_text)
        return self.get_event(event_id)

    def _complete_event_update(self, event_id: str, event: dict[str, Any], now_text: str) -> None:
        self.conn.execute(
                """
                UPDATE connector_source_events
                SET status='ingested', ingest_error=NULL, ingested_at=?, lease_owner=NULL,
                    lease_token=NULL, lease_expires_at=NULL, version=version+1
                WHERE id=? AND version=?
                """,
                (now_text, event_id, event["version"]),
            )

    def fail_event(
        self,
        event_id: str,
        lease_owner: str,
        lease_token: str,
        error: str,
        retry_after_seconds: int | None = None,
        now: datetime | str | None = None,
        stream_lock_id: str | None = None,
        reservation_token: str | None = None,
        credential_binding_id: str | None = None,
        credential_generation: int | None = None,
    ) -> dict[str, Any]:
        now_dt = _coerce_now(now)
        now_text = now_dt.isoformat()
        with self.conn:
            self._assert_stream_fence(stream_lock_id,reservation_token,now_text)
            self._assert_credential_fence(credential_binding_id,credential_generation)
            event = self._leased_event(event_id, lease_owner, lease_token, now_text)
            if event["attempts"] >= event["max_attempts"]:
                status = "quarantined"
                quarantine_reason = str(redact(error))[:300]
            else:
                status = "failed"
                quarantine_reason = None
            delay = retry_after_seconds if retry_after_seconds is not None else min(3600, 60 * (2 ** max(event["attempts"] - 1, 0)))
            available_at = (now_dt + timedelta(seconds=max(delay, 0))).isoformat()
            self.conn.execute(
                """
                UPDATE connector_source_events
                SET status=?, ingest_error=?, available_at=?, lease_owner=NULL, lease_token=NULL,
                    lease_expires_at=NULL, quarantine_reason=?, version=version+1
                WHERE id=? AND version=?
                """,
                (status, str(redact(error))[:300], available_at, quarantine_reason, event_id, event["version"]),
            )
        return self.get_event(event_id)

    def quarantine_event(
        self,
        event_id: str,
        lease_owner: str,
        lease_token: str,
        reason: str,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        now_text = _coerce_now(now).isoformat()
        with self.conn:
            event = self._leased_event(event_id, lease_owner, lease_token, now_text)
            safe_reason = str(redact(reason))[:300]
            self.conn.execute(
                """
                UPDATE connector_source_events
                SET status='quarantined', ingest_error=?, quarantine_reason=?, lease_owner=NULL,
                    lease_token=NULL, lease_expires_at=NULL, version=version+1
                WHERE id=? AND version=?
                """,
                (safe_reason, safe_reason, event_id, event["version"]),
            )
        return self.get_event(event_id)

    def mark_event_ingested(self, event_id: str) -> dict[str, Any]:
        now = utcnow().isoformat()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE connector_source_events
                SET status='ingested', ingest_error=NULL, ingested_at=?, lease_owner=NULL,
                    lease_token=NULL, lease_expires_at=NULL, version=version+1
                WHERE id=? AND status IN ('pending','leased','failed')
                """,
                (now, event_id),
            )
            if cursor.rowcount != 1:
                raise ValidationError("connector event cannot be marked ingested")
        return self.get_event(event_id)

    def mark_event_failed(self, event_id: str, error: str) -> dict[str, Any]:
        error = str(redact(error))[:300]
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE connector_source_events
                SET status='failed', ingest_error=?, lease_owner=NULL, lease_token=NULL,
                    lease_expires_at=NULL, version=version+1
                WHERE id=? AND status IN ('pending','leased')
                """,
                (error, event_id),
            )
            if cursor.rowcount != 1:
                raise ValidationError("connector event cannot be marked failed")
        return self.get_event(event_id)

    def complete_batch(
        self,batch_id: str,stream_lock_id: str | None=None,reservation_token: str | None=None,
        credential_binding_id: str | None=None,credential_generation: int | None=None,
        manage_transaction: bool=True,
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        with (self.conn if manage_transaction else nullcontext(self.conn)):
            self._assert_stream_fence(stream_lock_id,reservation_token,now)
            self._assert_credential_fence(credential_binding_id,credential_generation)
            batch = self.get_batch(batch_id)
            if batch["status"] != "pending":
                raise ValidationError("only pending connector batches can be completed")
            unfinished = self.conn.execute(
                """
                SELECT COUNT(*)
                FROM connector_batch_events be
                JOIN connector_source_events e ON e.id=be.event_id
                WHERE be.batch_id=? AND e.status NOT IN ('ingested','skipped','quarantined')
                """,
                (batch_id,),
            ).fetchone()[0]
            if unfinished:
                raise ValidationError("connector batch still has unprocessed events")
            lifecycle_blockers = self.conn.execute(
                """SELECT COUNT(*) FROM provider_route_mutation_staging mutation
                   JOIN connector_source_events event ON event.id=mutation.event_id
                   WHERE mutation.batch_id=?
                     AND (mutation.status!='applied' OR event.status='quarantined')""",
                (batch_id,),
            ).fetchone()[0]
            if lifecycle_blockers:
                raise ValidationError("connector batch has unapplied or quarantined lifecycle state")
            incomplete_tasks = self.conn.execute(
                """SELECT COUNT(*) FROM provider_sync_tasks
                   WHERE workspace_id=? AND connector=? AND account_key=?
                     AND status NOT IN ('completed','cancelled')""",
                (batch["workspace_id"], batch["connector"], batch["account_key"]),
            ).fetchone()[0]
            if incomplete_tasks:
                raise ValidationError("connector batch has incomplete provider sync tasks")
            running_generations = self.conn.execute(
                """SELECT COUNT(*) FROM provider_sync_generations
                   WHERE workspace_id=? AND connector=? AND account_key=? AND status='running'""",
                (batch["workspace_id"], batch["connector"], batch["account_key"]),
            ).fetchone()[0]
            if running_generations:
                raise ValidationError("connector batch has incomplete provider generation coverage")
            if batch["cursor_after"] is not None:
                self._promote_cursor(
                    batch["organization_id"],
                    batch["workspace_id"],
                    batch["connector"],
                    batch["account_key"],
                    batch["cursor_type"],
                    batch["cursor_after"],
                    batch["cursor_version_before"],
                    now,
                )
            self.conn.execute(
                "UPDATE connector_ingest_batches SET status='completed', completed_at=? WHERE id=?",
                (now, batch_id),
            )
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM connector_ingest_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("connector batch not found")
        return dict(row)

    def get_event(self, event_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM connector_source_events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFoundError("connector source event not found")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def list_batch_events(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT e.id
            FROM connector_batch_events be
            JOIN connector_source_events e ON e.id=be.event_id
            WHERE be.batch_id=?
            ORDER BY e.received_at, e.rowid
            """,
            (batch_id,),
        ).fetchall()
        return [self.get_event(row["id"]) for row in rows]

    def _stage_lifecycle_mutations(
        self,
        batch_id: str,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        stored_events: list[dict[str, Any]],
        mutations: Iterable[RouteLifecycleMutation],
        now: str,
    ) -> None:
        events_by_dedupe = {str(event["dedupe_key"]): event for event in stored_events}
        for mutation in mutations:
            event = events_by_dedupe.get(mutation.event_dedupe_key)
            if event is None:
                raise ValidationError("lifecycle mutation does not match an exact pulled event")
            if (
                event["external_id"] != mutation.external_id
                or event["source_key"] != f"google-drive/files/{mutation.external_id}"
                and event["source_key"] != f"gmail/messages/{mutation.external_id}"
            ):
                raise ValidationError("lifecycle mutation identity does not match pulled event")
            if mutation.workspace_id != workspace_id:
                raise ValidationError("lifecycle mutation workspace is outside pulled stream")
            operation = {"upsert": "activate", "tombstone": "retire"}.get(mutation.operation)
            if operation is None:
                raise ValidationError("lifecycle mutation operation is invalid")
            mutation_id = "pmut_" + hashlib.sha256(
                "\x1f".join(
                    (batch_id, mutation.event_dedupe_key, mutation.route_key,
                     mutation.provider_version, operation)
                ).encode("utf-8")
            ).hexdigest()[:32]
            self.conn.execute(
                """INSERT OR IGNORE INTO provider_route_mutation_staging(
                       id,batch_id,event_id,workspace_id,connector,account_key,external_id,
                       route_key,source_key,source_id,provider_version,operation,occurred_at,
                       status,created_at,applied_at,event_dedupe_key
                   ) VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,?,'staged',?,NULL,?)""",
                (mutation_id, batch_id, event["id"], workspace_id, connector, account_key,
                 mutation.external_id, mutation.route_key, event["source_key"],
                 mutation.provider_version, operation, event.get("observed_at") or now,
                 now, mutation.event_dedupe_key),
            )

    def reserve_stream(
        self,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        stream_key: str,
        job_id: str,
        mapping_hash: str,
        lease_owner: str,
        lease_seconds: int = 300,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        self._validate_stream_reservation(connector, account_key, stream_key, job_id, mapping_hash, lease_owner, lease_seconds)
        now_dt = _coerce_now(now)
        lock_id = self.new_id("clock")
        reservation_token = self.new_id("streamtoken")
        self._begin_immediate()
        try:
            self._insert_stream_lock(
                lock_id,
                organization_id,
                workspace_id,
                connector,
                account_key,
                stream_key,
                job_id,
                mapping_hash,
                lease_owner,
                reservation_token,
                now_dt,
                lease_seconds,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_stream_lock(lock_id)

    def reserve_stream_in_transaction(
        self,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        stream_key: str,
        job_id: str,
        mapping_hash: str,
        lease_owner: str,
        lease_seconds: int = 300,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Reserve a connector stream inside a caller-owned transaction.

        This is the transaction seam for services that enqueue a job and reserve
        its connector stream in one SQLite transaction. It performs the same
        single INSERT protected by the partial unique index, so callers avoid a
        check-then-insert race.
        """

        self._validate_stream_reservation(connector, account_key, stream_key, job_id, mapping_hash, lease_owner, lease_seconds)
        now_dt = _coerce_now(now)
        lock_id = self.new_id("clock")
        reservation_token = self.new_id("streamtoken")
        self._insert_stream_lock(
            lock_id,
            organization_id,
            workspace_id,
            connector,
            account_key,
            stream_key,
            job_id,
            mapping_hash,
            lease_owner,
            reservation_token,
            now_dt,
            lease_seconds,
        )
        return self.get_stream_lock(lock_id)

    def get_stream_lock(self, lock_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM connector_stream_locks WHERE id=?", (lock_id,)).fetchone()
        if row is None:
            raise NotFoundError("connector stream lock not found")
        return dict(row)

    def active_stream_lock(
        self,
        organization_id: str,
        workspace_id: str,
        connector: str,
        stream_key: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM connector_stream_locks
            WHERE organization_id=? AND workspace_id=? AND connector=? AND stream_key=? AND status='active'
            """,
            (organization_id, workspace_id, connector, stream_key),
        ).fetchone()
        return dict(row) if row is not None else None

    def heartbeat_stream(
        self,
        lock_id: str,
        reservation_token: str,
        lease_seconds: int = 300,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")
        now_dt = _coerce_now(now)
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        self._begin_immediate()
        try:
            lock = self._active_stream_lock_for_update(lock_id, reservation_token, now_dt.isoformat())
            cursor = self.conn.execute(
                """
                UPDATE connector_stream_locks
                SET lease_expires_at=?, updated_at=?, version=version+1
                WHERE id=? AND status='active' AND reservation_token=? AND version=?
                """,
                (lease_expires_at, now_dt.isoformat(), lock_id, reservation_token, lock["version"]),
            )
            if cursor.rowcount != 1:
                raise ValidationError("connector stream reservation changed concurrently")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_stream_lock(lock_id)

    def release_stream(
        self,
        lock_id: str,
        reservation_token: str,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        now_text = _coerce_now(now).isoformat()
        self._begin_immediate()
        try:
            lock = self._active_stream_lock_for_update(lock_id, reservation_token, now_text)
            self._require_stream_job_terminal(lock)
            cursor = self.conn.execute(
                """
                UPDATE connector_stream_locks
                SET status='released', released_at=?, updated_at=?, version=version+1
                WHERE id=? AND status='active' AND reservation_token=? AND version=?
                """,
                (now_text, now_text, lock_id, reservation_token, lock["version"]),
            )
            if cursor.rowcount != 1:
                raise ValidationError("connector stream reservation changed concurrently")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_stream_lock(lock_id)

    def cancel_stream(
        self,
        lock_id: str,
        reservation_token: str,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        now_text = _coerce_now(now).isoformat()
        self._begin_immediate()
        try:
            lock = self._active_stream_lock_for_update(lock_id, reservation_token, now_text)
            cursor = self.conn.execute(
                """
                UPDATE connector_stream_locks
                SET status='cancelled', cancelled_at=?, updated_at=?, version=version+1
                WHERE id=? AND status='active' AND reservation_token=? AND version=?
                """,
                (now_text, now_text, lock_id, reservation_token, lock["version"]),
            )
            if cursor.rowcount != 1:
                raise ValidationError("connector stream reservation changed concurrently")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_stream_lock(lock_id)

    def replace_stream(
        self,
        lock_id: str,
        reservation_token: str,
        new_job_id: str,
        mapping_hash: str,
        lease_owner: str,
        lease_seconds: int = 300,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        self._validate_stream_reservation("connector", "account", "stream", new_job_id, mapping_hash, lease_owner, lease_seconds)
        now_dt = _coerce_now(now)
        new_lock_id = self.new_id("clock")
        new_token = self.new_id("streamtoken")
        self._begin_immediate()
        try:
            lock = self._active_stream_lock_for_update(lock_id, reservation_token, now_dt.isoformat())
            self._require_stream_job_terminal(lock)
            replaced = self.conn.execute(
                """
                UPDATE connector_stream_locks
                SET status='replaced', updated_at=?, version=version+1
                WHERE id=? AND status='active' AND reservation_token=? AND version=?
                """,
                (now_dt.isoformat(), lock_id, reservation_token, lock["version"]),
            )
            if replaced.rowcount != 1:
                raise ValidationError("connector stream reservation changed concurrently")
            self._insert_stream_lock(
                new_lock_id,
                lock["organization_id"],
                lock["workspace_id"],
                lock["connector"],
                lock["account_key"],
                lock["stream_key"],
                new_job_id,
                mapping_hash,
                lease_owner,
                new_token,
                now_dt,
                lease_seconds,
            )
            self.conn.execute("UPDATE connector_stream_locks SET replaced_by_id=? WHERE id=?", (new_lock_id, lock_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get_stream_lock(new_lock_id)

    def _store_or_reference_event(
        self,
        batch_id: str,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        event: ConnectorSourceEvent,
        now: str,
    ) -> dict[str, Any]:
        existing = self.conn.execute(
            """
            SELECT d.first_event_id, e.status
            FROM connector_dedupe_keys d
            JOIN connector_source_events e ON e.id=d.first_event_id
            WHERE d.organization_id=? AND d.connector=? AND d.account_key=? AND d.dedupe_key=?
            """,
            (organization_id, connector, account_key, event.dedupe_key),
        ).fetchone()
        if existing is not None:
            self._link_batch_event(batch_id, existing["first_event_id"], now)
            item = self.get_event(existing["first_event_id"])
            item["replayed_in_batch_id"] = batch_id
            if item["status"] in EVENT_TERMINAL_STATUSES:
                item["original_status"] = item["status"]
                item["status"] = "skipped"
            return item

        event_id = self.new_id("cevent")
        safe_content = str(redact(event.content))
        safe_payload = redact(event.payload)
        self.conn.execute(
            """
            INSERT INTO connector_source_events(
                id, batch_id, organization_id, workspace_id, connector, account_key,
                dedupe_key, external_id, event_type, source_key, locator, media_type,
                content, content_hash, payload, observed_at, received_at, status, ingest_error,
                ingested_at, attempts, max_attempts, available_at, lease_owner, lease_token,
                lease_expires_at, quarantine_reason, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL,
                NULL, 0, 3, ?, NULL, NULL, NULL, NULL, 1)
            """,
            (
                event_id,
                batch_id,
                organization_id,
                workspace_id,
                connector,
                account_key,
                event.dedupe_key,
                event.external_id,
                event.event_type,
                event.source_key,
                event.locator,
                event.media_type,
                safe_content,
                content_hash(safe_content),
                json.dumps(safe_payload, sort_keys=True),
                event.observed_at,
                now,
                now,
            ),
        )
        self._link_batch_event(batch_id, event_id, now)
        self.conn.execute(
            """
            INSERT INTO connector_dedupe_keys(
                organization_id, connector, account_key, dedupe_key, first_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (organization_id, connector, account_key, event.dedupe_key, event_id, now),
        )
        return self.get_event(event_id)

    def _link_batch_event(self, batch_id: str, event_id: str, now: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO connector_batch_events(batch_id, event_id, created_at) VALUES (?, ?, ?)",
            (batch_id, event_id, now),
        )

    def _begin_immediate(self) -> None:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "cannot start a transaction within a transaction" in str(exc).lower():
                raise ValidationError("connector stream reservation requires a caller-owned transaction seam") from exc
            raise

    @staticmethod
    def _required_text(value: str, label: str) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValidationError(f"{label} is required")
        return text

    def _validate_stream_reservation(
        self,
        connector: str,
        account_key: str,
        stream_key: str,
        job_id: str,
        mapping_hash: str,
        lease_owner: str,
        lease_seconds: int,
    ) -> None:
        self._required_text(connector, "connector")
        self._required_text(account_key, "account key")
        self._required_text(stream_key, "stream key")
        self._required_text(job_id, "job id")
        self._required_text(mapping_hash, "mapping hash")
        self._required_text(lease_owner, "lease owner")
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")

    def _insert_stream_lock(
        self,
        lock_id: str,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        stream_key: str,
        job_id: str,
        mapping_hash: str,
        lease_owner: str,
        reservation_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> None:
        now_text = now.isoformat()
        lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        try:
            self.conn.execute(
                """
                INSERT INTO connector_stream_locks(
                    id, organization_id, workspace_id, connector, account_key, stream_key,
                    job_id, mapping_hash, status, lease_owner, reservation_token, reserved_at,
                    lease_expires_at, updated_at, released_at, cancelled_at, replaced_by_id, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, NULL, NULL, NULL, 1)
                """,
                (
                    lock_id,
                    organization_id,
                    workspace_id,
                    connector,
                    account_key,
                    stream_key,
                    job_id,
                    mapping_hash,
                    lease_owner,
                    reservation_token,
                    now_text,
                    lease_expires_at,
                    now_text,
                ),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "idx_connector_stream_locks_active" in message or "unique" in message:
                raise ValidationError("connector stream already has an active reservation") from exc
            raise

    def _active_stream_lock_for_update(self, lock_id: str, reservation_token: str, now: str) -> dict[str, Any]:
        lock = self.get_stream_lock(lock_id)
        if lock["status"] != "active":
            raise ValidationError("connector stream reservation is not active")
        if lock["reservation_token"] != reservation_token:
            raise ValidationError("connector stream reservation token is stale")
        if lock["lease_expires_at"] <= now:
            raise ValidationError("connector stream reservation lease has expired")
        return lock

    def _require_stream_job_terminal(self, lock: dict[str, Any]) -> None:
        job = self.conn.execute("SELECT status FROM jobs WHERE id=?", (lock["job_id"],)).fetchone()
        if job is None:
            raise ValidationError("linked connector stream job was not found")
        if job["status"] not in JOB_TERMINAL_STATUSES:
            raise ValidationError("connector stream can only release or replace after linked job is terminal")

    def _leased_event(self, event_id: str, lease_owner: str, lease_token: str, now: str) -> dict[str, Any]:
        event = self.get_event(event_id)
        if event["status"] != "leased":
            raise ValidationError("connector event is not leased")
        if event["lease_owner"] != lease_owner or event["lease_token"] != lease_token:
            raise ValidationError("connector event lease token is stale")
        if event["lease_expires_at"] is not None and event["lease_expires_at"] <= now:
            raise ValidationError("connector event lease has expired")
        return event

    def _assert_stream_fence(
        self,lock_id: str | None,reservation_token: str | None,now: str
    ) -> None:
        if lock_id is None and reservation_token is None:
            return
        if not lock_id or not reservation_token:
            raise ValidationError("complete connector stream fence is required")
        row=self.conn.execute(
            """SELECT id FROM connector_stream_locks WHERE id=? AND status='active'
            AND reservation_token=? AND lease_expires_at>?""",(lock_id,reservation_token,now)
        ).fetchone()
        if row is None:
            raise ValidationError("connector stream fence is stale")

    def _assert_credential_fence(self,binding_id: str | None,generation: int | None) -> None:
        if binding_id is None and generation is None:
            return
        if not binding_id or generation is None:
            raise ValidationError("complete connector credential fence is required")
        row=self.conn.execute(
            """SELECT id FROM secret_bindings WHERE id=? AND status='active' AND revoked_at IS NULL
            AND generation=?""",(binding_id,int(generation))
        ).fetchone()
        if row is None:
            raise ValidationError("connector credential fence is stale")

    def _promote_cursor(
        self,
        organization_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        cursor_type: str,
        cursor_value: str,
        expected_version: int,
        now: str,
    ) -> None:
        current = self.get_cursor_record(organization_id, workspace_id, connector, account_key, cursor_type)
        if current["version"] != expected_version:
            raise ValidationError("connector cursor changed concurrently")
        if expected_version == 0:
            self.conn.execute(
                """
                INSERT INTO connector_cursors(
                    id, organization_id, workspace_id, connector, account_key, cursor_type,
                    cursor_value, updated_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (self.new_id("ccursor"), organization_id, workspace_id, connector, account_key, cursor_type, cursor_value, now),
            )
            return
        cursor = self.conn.execute(
            """
            UPDATE connector_cursors
            SET cursor_value=?, updated_at=?, version=version+1
            WHERE organization_id=? AND workspace_id=? AND connector=? AND account_key=?
              AND cursor_type=? AND version=?
            """,
            (cursor_value, now, organization_id, workspace_id, connector, account_key, cursor_type, expected_version),
        )
        if cursor.rowcount != 1:
            raise ValidationError("connector cursor changed concurrently")


def _json_or_none(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _google_error(response: HttpResponse) -> str:
    if isinstance(response.json_body, dict):
        error = response.json_body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        if isinstance(error, str):
            return error
    return response.text or f"Google API returned HTTP {response.status}"
