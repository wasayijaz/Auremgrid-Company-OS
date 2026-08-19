from __future__ import annotations

import json
import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from auremgrid.domain.errors import NotFoundError, ValidationError
from auremgrid.storage.jobs import JobRepository
from auremgrid.services.secrets import redact


JOB_STATUSES = {"queued", "leased", "running", "succeeded", "failed", "retry_wait", "dead_letter", "cancelled"}
ACTIVE_JOB_STATUSES = {"leased", "running"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return str(value)


def _jsonable(value: Any, label: str, reject_secrets: bool = False) -> Any:
    try:
        json.dumps(value, sort_keys=True)
    except TypeError as exc:
        raise ValidationError(f"{label} must be JSON serializable") from exc
    sanitized = redact(value)
    if reject_secrets and sanitized != value:
        raise ValidationError(f"{label} must contain secret binding IDs, not credential material")
    return sanitized


def _required_text(value: Any, label: str) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValidationError(f"{label} is required")
    return text


class JobOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str]) -> None:
        self.conn = conn
        self.new_id = new_id
        self.repo = JobRepository(conn, new_id)

    def enqueue_job(
        self,
        organization_id: str,
        workspace_id: str | None,
        principal_id: str,
        type: str,
        payload: Any,
        priority: int = 0,
        max_attempts: int = 3,
        available_at: datetime | str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        type = _required_text(type, "job type")
        principal = self.conn.execute(
            "SELECT id FROM auth_principals WHERE id=? AND organization_id=? AND status='active'",
            (principal_id, organization_id),
        ).fetchone()
        if principal is None:
            raise ValidationError("active job principal is required")
        payload = _jsonable(payload, "job payload", reject_secrets=True)
        payload_hash = self._payload_hash(payload)
        if max_attempts < 1:
            raise ValidationError("max_attempts must be positive")
        if idempotency_key:
            existing = self.repo.find_job_by_idempotency(organization_id, idempotency_key)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise ValidationError("idempotency key was already used with a different job payload")
                return existing
        now_text = _now().isoformat()
        available = _iso(available_at) or now_text
        with self._tx_immediate():
            job = self.repo.insert_job(
                {
                    "id": self.new_id("job"),
                    "organization_id": organization_id,
                    "workspace_id": workspace_id,
                    "principal_id": principal_id,
                    "type": type,
                    "payload": payload,
                    "status": "queued",
                    "priority": int(priority),
                    "attempts": 0,
                    "max_attempts": int(max_attempts),
                    "available_at": available,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "progress": 0,
                    "idempotency_key": idempotency_key,
                    "payload_hash": payload_hash,
                    "result": None,
                    "error": None,
                    "created_at": now_text,
                    "updated_at": now_text,
                    "started_at": None,
                    "completed_at": None,
                    "cancelled_at": None,
                    "lease_token": None,
                    "version": 1,
                }
            )
            self._event(job, "system", "enqueue", None, "queued", {"idempotency_key": idempotency_key}, now_text)
        return job

    def claim_job(
        self,
        organization_id: str,
        workspace_id: str | None,
        lease_owner: str,
        lease_seconds: int = 60,
        now: datetime | str | None = None,
    ) -> dict[str, Any] | None:
        lease_owner = _required_text(lease_owner, "lease owner")
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")
        now_dt = self._coerce_now(now)
        now_text = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        lease_token = self.new_id("lease")
        with self._tx_immediate():
            candidate = self.repo.find_claimable_job(organization_id, workspace_id, now_text)
            if candidate is None:
                return None
            claimed = self.repo.claim_job(candidate, lease_owner, lease_token, lease_expires_at, now_text)
            if claimed is None:
                return None
            self._event(
                claimed,
                lease_owner,
                "claim",
                candidate["status"],
                "leased",
                {"lease_expires_at": lease_expires_at, "lease_token": lease_token, "attempt": claimed["attempts"]},
                now_text,
            )
        return claimed

    def heartbeat_job(
        self,
        organization_id: str,
        workspace_id: str | None,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        progress: float | None = None,
        lease_seconds: int = 60,
        expected_version: int | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        lease_owner = _required_text(lease_owner, "lease owner")
        if progress is not None and not 0 <= progress <= 1:
            raise ValidationError("progress must be between 0 and 1")
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")
        now_dt = self._coerce_now(now)
        now_text = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self._tx_immediate():
            job = self._leased_job(organization_id, workspace_id, job_id, lease_owner, lease_token, now_text)
            updated = self.repo.update_job_status(
                job_id,
                ACTIVE_JOB_STATUSES,
                expected_version or job["version"],
                {
                    "status": "running",
                    "progress": job["progress"] if progress is None else float(progress),
                    "lease_expires_at": lease_expires_at,
                    "updated_at": now_text,
                },
            )
            self._event(
                updated,
                lease_owner,
                "heartbeat",
                job["status"],
                "running",
                {"progress": updated["progress"], "lease_expires_at": lease_expires_at},
                now_text,
            )
        return updated

    def succeed_job(
        self,
        organization_id: str,
        workspace_id: str | None,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        result: Any,
        expected_version: int | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        result = _jsonable(result, "job result")
        lease_owner = _required_text(lease_owner, "lease owner")
        now_text = self._coerce_now(now).isoformat()
        with self._tx_immediate():
            job = self._leased_job(organization_id, workspace_id, job_id, lease_owner, lease_token, now_text)
            updated = self.repo.update_job_status(
                job_id,
                ACTIVE_JOB_STATUSES,
                expected_version or job["version"],
                {
                    "status": "succeeded",
                    "progress": 1,
                    "result": result,
                    "error": None,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "completed_at": now_text,
                    "updated_at": now_text,
                },
            )
            self._event(updated, lease_owner, "succeed", job["status"], "succeeded", {"result": result}, now_text)
        return updated

    def fail_job(
        self,
        organization_id: str,
        workspace_id: str | None,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        error: Any,
        retry: bool = True,
        expected_version: int | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        error = _jsonable(error, "job error")
        lease_owner = _required_text(lease_owner, "lease owner")
        now_dt = self._coerce_now(now)
        now_text = now_dt.isoformat()
        with self._tx_immediate():
            job = self._leased_job(organization_id, workspace_id, job_id, lease_owner, lease_token, now_text)
            if retry and job["attempts"] < job["max_attempts"]:
                status = "retry_wait"
                available_at = (now_dt + self._backoff(job["attempts"])).isoformat()
                completed_at = None
            elif retry:
                status = "dead_letter"
                available_at = job["available_at"]
                completed_at = now_text
            else:
                status = "failed"
                available_at = job["available_at"]
                completed_at = now_text
            updated = self.repo.update_job_status(
                job_id,
                ACTIVE_JOB_STATUSES,
                expected_version or job["version"],
                {
                    "status": status,
                    "available_at": available_at,
                    "error": error,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "completed_at": completed_at,
                    "updated_at": now_text,
                },
            )
            self._event(
                updated,
                lease_owner,
                "fail",
                job["status"],
                status,
                {"error": error, "retry": retry, "available_at": available_at},
                now_text,
            )
        return updated

    def cancel_job(
        self,
        organization_id: str,
        workspace_id: str | None,
        job_id: str,
        reason: str,
        actor: str = "system",
        expected_version: int | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        reason = _required_text(reason, "cancellation reason")
        now_text = self._coerce_now(now).isoformat()
        with self._tx_immediate():
            job = self._job_in_scope(organization_id, workspace_id, job_id)
            updated = self.repo.update_job_status(
                job_id,
                {"queued", "retry_wait"},
                expected_version or job["version"],
                {
                    "status": "cancelled",
                    "error": {"reason": reason},
                    "cancelled_at": now_text,
                    "completed_at": now_text,
                    "updated_at": now_text,
                },
            )
            self._event(updated, actor, "cancel", job["status"], "cancelled", {"reason": reason}, now_text)
        return updated

    def get_job(self, organization_id: str, workspace_id: str | None, job_id: str) -> dict[str, Any]:
        return self._job_in_scope(organization_id, workspace_id, job_id)

    def list_jobs(
        self,
        organization_id: str,
        workspace_id: str | None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in JOB_STATUSES:
            raise ValidationError("invalid job status")
        return self.repo.list_jobs(organization_id, workspace_id, status)

    def job_events(self, organization_id: str, workspace_id: str | None, job_id: str) -> list[dict[str, Any]]:
        self._job_in_scope(organization_id, workspace_id, job_id)
        return self.repo.list_job_events(job_id)

    def add_outbox_event(
        self,
        organization_id: str,
        workspace_id: str | None,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: Any,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
        next_attempt_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        aggregate_type = _required_text(aggregate_type, "aggregate type")
        aggregate_id = _required_text(aggregate_id, "aggregate id")
        event_type = _required_text(event_type, "event type")
        payload = _jsonable(payload, "outbox payload", reject_secrets=True)
        payload_hash = self._payload_hash(payload)
        if max_attempts < 1:
            raise ValidationError("max_attempts must be positive")
        if idempotency_key:
            existing = self.repo.find_outbox_by_idempotency(organization_id, idempotency_key)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise ValidationError("idempotency key was already used with a different outbox payload")
                return existing
        now_text = _now().isoformat()
        with self._tx_immediate():
            return self.repo.insert_outbox(
                {
                    "id": self.new_id("outbox"),
                    "organization_id": organization_id,
                    "workspace_id": workspace_id,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "event_type": event_type,
                    "payload": payload,
                    "payload_hash": payload_hash,
                    "idempotency_key": idempotency_key,
                    "status": "pending",
                    "attempts": 0,
                    "max_attempts": int(max_attempts),
                    "next_attempt_at": _iso(next_attempt_at) or now_text,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "lease_token": None,
                    "published_at": None,
                    "last_error": None,
                    "created_at": now_text,
                    "updated_at": now_text,
                    "version": 1,
                }
            )

    def claim_outbox_events(
        self,
        organization_id: str,
        workspace_id: str | None,
        lease_owner: str,
        limit: int = 10,
        lease_seconds: int = 60,
        now: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        controls = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                "SELECT key,value FROM system_state WHERE key IN ('recovery_mode','outbound_dispatch')"
            ).fetchall()
        }
        if controls.get("recovery_mode") == "1" or controls.get("outbound_dispatch") == "disabled":
            return []
        lease_owner = _required_text(lease_owner, "lease owner")
        if limit < 1:
            raise ValidationError("limit must be positive")
        if lease_seconds <= 0:
            raise ValidationError("lease_seconds must be positive")
        now_dt = self._coerce_now(now)
        now_text = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        claimed: list[dict[str, Any]] = []
        with self._tx_immediate():
            for event in self.repo.find_claimable_outbox(organization_id, workspace_id, now_text, limit):
                item = self.repo.claim_outbox(event, lease_owner, self.new_id("lease"), lease_expires_at, now_text)
                if item is not None:
                    claimed.append(item)
        return claimed

    def publish_outbox_event(
        self,
        organization_id: str,
        workspace_id: str | None,
        event_id: str,
        lease_owner: str,
        lease_token: str,
        expected_version: int | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        lease_owner = _required_text(lease_owner, "lease owner")
        now_text = self._coerce_now(now).isoformat()
        with self._tx_immediate():
            event = self._outbox_in_scope(organization_id, workspace_id, event_id)
            if event["status"] == "published":
                return event
            self._outbox_leased(event, lease_owner, lease_token, now_text)
            return self.repo.update_outbox(
                event_id,
                lease_owner,
                expected_version or event["version"],
                {
                    "status": "published",
                    "published_at": now_text,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "updated_at": now_text,
                },
            )

    def fail_outbox_event(
        self,
        organization_id: str,
        workspace_id: str | None,
        event_id: str,
        lease_owner: str,
        lease_token: str,
        error: str,
        retry: bool = True,
        expected_version: int | None = None,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        lease_owner = _required_text(lease_owner, "lease owner")
        error = _required_text(error, "outbox error")
        now_dt = self._coerce_now(now)
        now_text = now_dt.isoformat()
        with self._tx_immediate():
            event = self._outbox_in_scope(organization_id, workspace_id, event_id)
            self._outbox_leased(event, lease_owner, lease_token, now_text)
            next_attempt = event["next_attempt_at"]
            if retry and event["attempts"] < event["max_attempts"]:
                next_attempt = (now_dt + self._backoff(event["attempts"])).isoformat()
            return self.repo.update_outbox(
                event_id,
                lease_owner,
                expected_version or event["version"],
                {
                    "status": "failed",
                    "last_error": error,
                    "next_attempt_at": next_attempt,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "updated_at": now_text,
                },
            )

    def get_outbox_event(self, organization_id: str, workspace_id: str | None, event_id: str) -> dict[str, Any]:
        return self._outbox_in_scope(organization_id, workspace_id, event_id)

    def _job_in_scope(self, organization_id: str, workspace_id: str | None, job_id: str) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        if job["organization_id"] != organization_id or job["workspace_id"] != workspace_id:
            raise NotFoundError("job not found")
        return job

    def _leased_job(
        self,
        organization_id: str,
        workspace_id: str | None,
        job_id: str,
        lease_owner: str,
        lease_token: str,
        now: str,
    ) -> dict[str, Any]:
        job = self._job_in_scope(organization_id, workspace_id, job_id)
        if job["status"] not in ACTIVE_JOB_STATUSES:
            raise ValidationError("job is not leased or running")
        if job["lease_owner"] != lease_owner:
            raise ValidationError("job is leased by another worker")
        if job["lease_token"] != lease_token:
            raise ValidationError("job lease token is stale")
        if job["lease_expires_at"] is not None and job["lease_expires_at"] <= now:
            raise ValidationError("job lease has expired")
        return job

    def _outbox_in_scope(self, organization_id: str, workspace_id: str | None, event_id: str) -> dict[str, Any]:
        event = self.repo.get_outbox(event_id)
        if event["organization_id"] != organization_id or event["workspace_id"] != workspace_id:
            raise NotFoundError("outbox event not found")
        return event

    def _outbox_leased(self, event: dict[str, Any], lease_owner: str, lease_token: str, now: str) -> None:
        if event["status"] == "published":
            raise ValidationError("outbox event is already published")
        if event["lease_owner"] != lease_owner:
            raise ValidationError("outbox event is leased by another worker")
        if event["lease_token"] != lease_token:
            raise ValidationError("outbox lease token is stale")
        if event["lease_expires_at"] is not None and event["lease_expires_at"] <= now:
            raise ValidationError("outbox event lease has expired")

    def _event(
        self,
        job: dict[str, Any],
        actor: str,
        event_type: str,
        from_status: str | None,
        to_status: str | None,
        detail: dict[str, Any],
        now: str,
    ) -> None:
        self.repo.insert_job_event(
            {
                "id": self.new_id("jobevent"),
                "job_id": job["id"],
                "organization_id": job["organization_id"],
                "workspace_id": job["workspace_id"],
                "actor": actor,
                "event_type": event_type,
                "from_status": from_status,
                "to_status": to_status,
                "detail": detail,
                "created_at": now,
            }
        )

    def _coerce_now(self, value: datetime | str | None) -> datetime:
        if value is None:
            return _now()
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).replace(microsecond=0)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(microsecond=0)

    def _backoff(self, attempts: int) -> timedelta:
        delay_seconds = min(3600, 60 * (2 ** max(attempts - 1, 0)))
        return timedelta(seconds=delay_seconds)

    def _payload_hash(self, payload: Any) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @contextmanager
    def _tx_immediate(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
