"""Credential-backed, workspace-scoped connector synchronization."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from auremgrid.connectors.clickup import ClickUpConnector
from auremgrid.connectors.gmail import GmailConnector
from auremgrid.connectors.google_auth import (
    ConnectorInboxRepository,
    ConnectorSourceEvent,
    GoogleRequestError,
    GoogleOAuthClient,
    OAuthRefreshResult,
    RouteLifecycleMutation,
)
from auremgrid.connectors.google_drive import DriveBackfillTask, GoogleDriveConnector
from auremgrid.connectors.http import ConnectorTransportError, sanitize_content
from auremgrid.connectors.slack import SlackConnector
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.secrets import redact
from auremgrid.storage.sqlite import ProviderSyncFence


GOOGLE_DRIVE_READ_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Google sources are configurable so an operator can establish and inspect the
# account/mapping contract, but they are not advertised as live until their
# adapters pass the same routing, backfill, identity, and retry gates as the
# existing providers.
CONFIGURABLE_SOURCES = frozenset({"slack", "clickup", "google_drive", "gmail"})
LIVE_SOURCES = frozenset({"slack", "clickup"})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class IntegrationOperations:
    """Own connection truth and process connector pages through the durable inbox."""

    def __init__(self, os: Any, connector_factory: Callable[..., Any] | None = None) -> None:
        self.os = os
        self.conn = os.store.conn
        self.inbox = ConnectorInboxRepository(self.conn, os.jobs.new_id)
        self.connector_factory = connector_factory

    def configure(
        self,
        identity: AuthenticatedIdentity,
        source: str,
        expected_account_id: str,
        workspace_mappings: dict[str, str],
        permissions: list[str],
    ) -> dict[str, Any]:
        identity.require("integration_configure")
        source = source.strip().lower()
        if source not in CONFIGURABLE_SOURCES:
            raise ValidationError("unsupported connector")
        expected_account_id = self._normalize_expected_account_id(source, expected_account_id)
        if not expected_account_id:
            raise ValidationError("expected provider account ID is required")
        workspace_mappings = self._canonicalize_mappings(workspace_mappings)
        requested_permissions = self._canonicalize_permissions(permissions)
        self._validate_permissions(source, requested_permissions)
        self._validate_mapping_keys(source, workspace_mappings)
        for workspace_id in workspace_mappings.values():
            scope = self.os.company.workspace_scope(workspace_id)
            if scope is None or scope["organization_id"] != identity.organization_id:
                raise AuthorizationError("workspace mapping is outside the organization")
            if identity.workspace_id not in {None, workspace_id}:
                raise AuthorizationError("workspace mapping is outside the credential scope")
        existing = self.conn.execute(
            "SELECT id FROM integrations WHERE organization_id=? AND source=?",
            (identity.organization_id, source),
        ).fetchone()
        integration_id = existing["id"] if existing else self.os.jobs.new_id("integration")
        if existing and self._has_active_sync(integration_id):
            raise ValidationError("integration cannot be reconfigured while a sync job is active")
        now = _now()
        values = (
            integration_id,
            identity.organization_id,
            source,
            "not_connected",
            json.dumps(workspace_mappings, sort_keys=True, separators=(",", ":")),
            json.dumps(sorted(requested_permissions), separators=(",", ":")),
            None,
            None,
            None,
            0,
            "never_synced",
            now,
        )
        self.conn.execute(
            """INSERT INTO integrations(
              id,organization_id,source,status,workspace_mappings,permissions,sync_cursor,
              last_sync_at,last_error,object_count,health,created_at,expected_account_id,
              provider_account_id,provider_account_name,granted_permissions,credential_verified_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(organization_id,source) DO UPDATE SET
              status='not_connected',workspace_mappings=excluded.workspace_mappings,
              permissions=excluded.permissions,sync_cursor=NULL,last_sync_at=NULL,
              last_error=NULL,object_count=0,health='never_synced',
              expected_account_id=excluded.expected_account_id,provider_account_id=NULL,
              provider_account_name=NULL,granted_permissions='[]',credential_verified_at=NULL""",
            values + (expected_account_id,None,None,"[]",None),
        )
        self.conn.commit()
        return self.get(identity, integration_id)

    def get(self, identity: AuthenticatedIdentity, integration_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM integrations WHERE id=? AND organization_id=?",
            (integration_id, identity.organization_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("integration not found")
        item = dict(row)
        item["workspace_mappings"] = json.loads(item["workspace_mappings"])
        item["permissions"] = json.loads(item["permissions"])
        item["granted_permissions"] = json.loads(item.get("granted_permissions") or "[]")
        item["live_enabled"] = item["source"] in LIVE_SOURCES
        if identity.workspace_id is not None and set(item["workspace_mappings"].values()) != {identity.workspace_id}:
            raise AuthorizationError("integration is outside the credential workspace scope")
        item["credential"] = self._credential_metadata(integration_id)
        return item

    def list(self, identity: AuthenticatedIdentity) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self.conn.execute(
            "SELECT id FROM integrations WHERE organization_id=? ORDER BY source", (identity.organization_id,)
        ).fetchall():
            try:
                result.append(self.get(identity, row["id"]))
            except AuthorizationError:
                continue
        return result

    def bind_credential(
        self,
        identity: AuthenticatedIdentity,
        integration_id: str,
        name: str,
        reference: str,
        scopes: list[str],
    ) -> dict[str, Any]:
        integration = self.get(identity, integration_id)
        existing = self.conn.execute(
            "SELECT id FROM secret_bindings WHERE integration_id=? AND revoked_at IS NULL", (integration_id,)
        ).fetchone()
        if existing:
            raise ValidationError("integration already has an active credential binding; rotate or revoke it")
        return self.os.secrets.create(
            identity, identity.organization_id, name, integration["source"], reference,
            scopes, integration_id=integration_id,
        )

    def verify(self, identity: AuthenticatedIdentity, integration_id: str) -> dict[str, Any]:
        identity.require("integration_sync")
        integration = self.get(identity, integration_id)
        if integration["source"] not in LIVE_SOURCES:
            raise ValidationError("connector provider verification is not enabled")
        secret = self._resolve(identity, integration)
        if self._is_google(integration["source"]):
            secret, granted = self._refresh_google_access(integration, secret)
            integration = {**integration, "_runtime_granted_permissions": granted}
        verified = self._provider_verify(integration,secret)
        account_id, account_name, granted = self._verified_account(integration["source"], verified)
        if not self._account_ids_match(integration["source"], account_id, integration["expected_account_id"]):
            raise AuthorizationError("provider account identity mismatch")
        missing = self._missing_permissions(integration["source"], set(integration["permissions"]), set(granted))
        if missing:
            raise AuthorizationError("provider credential is missing required permissions")
        now = _now()
        self.conn.execute(
            """UPDATE integrations SET status='authorized',health='never_synced',last_error=NULL,
            provider_account_id=?,provider_account_name=?,granted_permissions=?,credential_verified_at=? WHERE id=?""",
            (account_id,account_name,json.dumps(sorted(set(granted)),separators=(",",":")),now,integration_id),
        )
        self.conn.commit()
        binding=self.conn.execute(
            "SELECT id FROM secret_bindings WHERE integration_id=? AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (integration_id,),
        ).fetchone()
        if binding is not None:
            self.os.secrets.mark_verified(identity,binding["id"])
        return {"integration": self.get(identity, integration_id), "provider_identity": self._public_identity(verified)}

    def enqueue_sync(
        self, identity: AuthenticatedIdentity, integration_id: str, priority: int = 0,
        max_attempts: int = 5, idempotency_key: str | None = None,
    ) -> list[dict[str, Any]]:
        identity.require("integration_sync")
        integration = self.get(identity, integration_id)
        if integration["source"] not in LIVE_SOURCES:
            raise ValidationError("connector adapter is not enabled for live synchronization")
        if integration["status"] not in {"authorized","connected"}:
            raise ValidationError("integration credentials must be verified before enqueueing sync")
        jobs: list[dict[str, Any]] = []
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for external_key, workspace_id in integration["workspace_mappings"].items():
                if self._has_active_sync(integration_id, external_key):
                    raise ValidationError("a sync job is already active for this integration stream")
                mapping_hash = self._mapping_hash(integration["source"], external_key, workspace_id)
                stream_key = f"{idempotency_key}:{external_key}" if idempotency_key else None
                account_key=f"{integration_id}:{mapping_hash}"
                durable_stream_key=f"managed:{account_key}"
                jobs.append(self.os.jobs.enqueue_job(
                    identity.organization_id, workspace_id, identity.principal_id, "connector.sync",
                    {"integration_id": integration_id, "external_key": external_key,
                     "workspace_id": workspace_id, "mapping_hash": mapping_hash},
                    priority, max_attempts, None, stream_key,
                    lambda job,organization_id=identity.organization_id,workspace_id=workspace_id,
                    source=integration["source"],account_key=account_key,durable_stream_key=durable_stream_key,
                    mapping_hash=mapping_hash,principal_id=identity.principal_id:
                        self.inbox.reserve_stream_in_transaction(
                            organization_id,workspace_id,source,account_key,durable_stream_key,
                            job["id"],mapping_hash,principal_id,lease_seconds=604800,
                        ),
                    False,
                ))
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ValidationError("a sync job is already active for this integration stream") from exc
        except Exception:
            self.conn.rollback()
            raise
        return jobs

    def sync(
        self, identity: AuthenticatedIdentity, integration_id: str,
        external_key: str | None = None, expected_workspace_id: str | None = None,
        expected_mapping_hash: str | None = None,
        event_lease_owner: str = "connector-sync",
        progress_callback: Callable[[float], None] | None = None,
        stream_lock_id: str | None = None,
        stream_reservation_token: str | None = None,
    ) -> dict[str, Any]:
        identity.require("integration_sync")
        if external_key is None:
            integration = self.get(identity, integration_id)
            selected_mappings = integration["workspace_mappings"]
        else:
            integration = self._get_for_job(identity, integration_id)
            workspace_id = integration["workspace_mappings"].get(external_key)
            if workspace_id is None or workspace_id != expected_workspace_id:
                raise AuthorizationError("integration stream mapping changed after enqueue")
            actual_hash = self._mapping_hash(integration["source"], external_key, workspace_id)
            if actual_hash != expected_mapping_hash:
                raise AuthorizationError("integration stream mapping changed after enqueue")
            selected_mappings = {external_key: workspace_id}
        if integration["status"] not in {"authorized", "connected"}:
            raise ValidationError("integration credentials must be verified before syncing")
        if integration["source"] not in LIVE_SOURCES:
            raise ValidationError("connector adapter is not enabled for live synchronization")
        binding = self._verified_binding(identity, integration)
        secret = self.os.secrets.resolve_for_use(
            identity, binding["id"], f"connector:{integration['source']}"
        )
        if self._is_google(integration["source"]):
            try:
                secret, granted = self._refresh_google_access(integration, secret)
            except ConnectorTransportError as exc:
                health, connection_state = self._transport_failure_state(exc)
                if self._owns_sync_fences(
                    stream_lock_id, stream_reservation_token, binding["id"], binding["generation"]
                ):
                    self._record_failure(
                        integration_id, health, self._stable_error(exc), connection_state
                    )
                raise
            integration = {**integration, "_runtime_granted_permissions": granted}
        total_created = total_seen = total_quarantined = 0
        backfill_remaining=False
        batches: list[str] = []
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.inbox._assert_stream_fence(stream_lock_id,stream_reservation_token,_now())
            self.inbox._assert_credential_fence(binding["id"],binding["generation"])
            self.conn.execute("UPDATE integrations SET health='syncing',last_error=NULL WHERE id=?", (integration_id,))
            self.conn.commit()
        except Exception:
            self.conn.rollback();raise
        try:
            current_identity=self._provider_verify({**integration,"workspace_mappings":selected_mappings},secret)
            current_account_id,_,current_granted=self._verified_account(integration["source"],current_identity)
            if not self._account_ids_match(integration["source"], current_account_id, integration["provider_account_id"]) or not self._account_ids_match(
                integration["source"], current_account_id, integration["expected_account_id"]
            ):
                raise ConnectorTransportError("provider account identity changed",status=401,retryable=False)
            if self._missing_permissions(integration["source"], set(integration["permissions"]), set(current_granted)):
                raise ConnectorTransportError("provider permission grant changed",status=403,retryable=False)
            for external_key, workspace_id in selected_mappings.items():
                if identity.workspace_id not in {None, workspace_id}:
                    raise AuthorizationError("job principal cannot access a mapped workspace")
                actor_id = self.os.auth.actor_for_identity(identity, workspace_id)
                account_key = f"{integration_id}:{self._mapping_hash(integration['source'],external_key,workspace_id)}"
                cursor = self.inbox.get_cursor(identity.organization_id, workspace_id, integration["source"], account_key)
                provider_stream_key = f"managed:{account_key}"
                provider_fence = self._provider_fence(
                    stream_lock_id, stream_reservation_token, binding
                )
                page_count=0
                while True:
                    provider_task = None
                    page_integration = integration
                    if self._is_google(integration["source"]):
                        running_generation = self.os.store.get_running_generation(
                            workspace_id, integration["source"], account_key,
                            provider_stream_key, external_key,
                        )
                        if cursor is None and running_generation is not None:
                            cursor = running_generation["baseline_cursor"]
                        next_task_type = self.conn.execute(
                            """SELECT task_type FROM provider_sync_tasks
                            WHERE workspace_id=? AND connector=? AND account_key=? AND stream_key=?
                              AND status IN ('pending','leased')
                            ORDER BY created_at,id LIMIT 1""",
                            (workspace_id, integration["source"], account_key, provider_stream_key),
                        ).fetchone()
                        if next_task_type is not None and next_task_type["task_type"] != "backfill":
                            raise ConnectorTransportError(
                                "Google Drive reconciliation is pending", retryable=True,
                                retry_after=60,
                            )
                        provider_task = self.os.store.claim_provider_sync_task(
                            workspace_id, integration["source"], account_key,
                            provider_stream_key, event_lease_owner, 60,
                            fence=provider_fence,
                        )
                        if provider_task is not None:
                            payload = json.loads(provider_task["payload"] or "{}")
                            if integration["source"] == "google_drive" and provider_task["task_type"] == "backfill":
                                page_integration = {
                                    **integration,
                                    "_runtime_backfill_task": DriveBackfillTask(
                                        provider_task["route_key"],
                                        provider_task["external_id"],
                                        provider_task["page_token"],
                                    ),
                                }
                            elif integration["source"] == "gmail" and provider_task["task_type"] == "backfill":
                                cursor = str(payload["cursor"])
                    events,next_cursor,has_more,page_meta = self._pull(
                        integration["source"],secret,external_key,workspace_id,cursor,page_integration
                    )
                    if page_meta.get("cursor_expired"):
                        if cursor is None:
                            raise ConnectorTransportError(
                                "Google provider cursor rebootstrap failed", retryable=False
                            )
                        with self.os.store.atomic(immediate=True):
                            self.os.store.cancel_provider_rebootstrap(
                                workspace_id, integration["source"], account_key,
                                provider_stream_key, external_key, provider_fence,
                            )
                        cursor = None
                        continue
                    events = [self._sanitize_source_event(event, secret) for event in events]
                    next_phase = self._google_cursor_phase(next_cursor) if self._is_google(integration["source"]) else None
                    durable_cursor_after = next_cursor
                    if self._is_google(integration["source"]) and (
                        next_phase == "backfill" or page_meta.get("backfill_tasks")
                    ):
                        durable_cursor_after = None
                    with self.os.store.atomic(immediate=True):
                        batch = self.inbox.record_pull(
                            identity.organization_id, workspace_id, integration["source"], account_key,
                            cursor, durable_cursor_after, events,
                            stream_lock_id=stream_lock_id,reservation_token=stream_reservation_token,
                            credential_binding_id=binding["id"],credential_generation=binding["generation"],
                            lifecycle_mutations=page_meta.get("lifecycle_mutations", ()),
                            manage_transaction=False,
                        )
                        generation = None
                        if self._is_google(integration["source"]):
                            generation = self.os.store.get_running_generation(
                                workspace_id, integration["source"], account_key,
                                provider_stream_key, external_key,
                            )
                            if generation is None and (cursor is None or self._google_cursor_phase(cursor) == "backfill"):
                                generation = self.os.store.start_provider_sync_generation(
                                    workspace_id, integration["source"], account_key,
                                    provider_stream_key, external_key,
                                    next_cursor if cursor is None else cursor,
                                    fence=provider_fence,
                                )
                            self._persist_google_page_state(
                                integration["source"], workspace_id, account_key,
                                provider_stream_key, external_key, next_cursor,
                                events, page_meta, generation, provider_fence,
                            )
                    batches.append(batch["id"])
                    total_seen += len(batch["events"])
                    processed_in_batch = 0
                    while True:
                        event = self.inbox.claim_event(
                            identity.organization_id, workspace_id, integration["source"], account_key,
                            event_lease_owner, lease_seconds=60,
                            stream_lock_id=stream_lock_id,reservation_token=stream_reservation_token,
                            credential_binding_id=binding["id"],credential_generation=binding["generation"],
                        )
                        if event is None:
                            break
                        try:
                            with self.os.store.atomic(immediate=True):
                                self.inbox._assert_stream_fence(
                                    stream_lock_id, stream_reservation_token, _now()
                                )
                                self.inbox._assert_credential_fence(
                                    binding["id"], binding["generation"]
                                )
                                staged = self.conn.execute(
                                    """SELECT operation FROM provider_route_mutation_staging
                                    WHERE batch_id=? AND event_dedupe_key=?""",
                                    (batch["id"], event["dedupe_key"]),
                                ).fetchall()
                                activates = any(row["operation"] == "activate" for row in staged)
                                if activates or not self._is_google(integration["source"]):
                                    result = self.os.ingest_text(
                                        workspace_id, actor_id, event["source_key"], event["content"], event["locator"],
                                        observed_at=self._observed_at(event["observed_at"]), media_type=event["media_type"],
                                        trust_level="external",
                                    )
                                else:
                                    result = None
                                self.inbox.complete_event(
                                    event["id"], event_lease_owner, event["lease_token"],
                                    stream_lock_id=stream_lock_id,
                                    reservation_token=stream_reservation_token,
                                    credential_binding_id=binding["id"],
                                    credential_generation=binding["generation"],
                                )
                                if self._is_google(integration["source"]):
                                    fence = self._provider_fence(
                                        stream_lock_id, stream_reservation_token, binding
                                    )
                                    if result is not None:
                                        self.os.store.bind_provider_event_source(
                                            batch["id"], event["dedupe_key"], workspace_id,
                                            result.source.id, fence,
                                        )
                                    self.os.store.apply_provider_event_mutations(
                                        batch["id"], event["dedupe_key"], fence
                                    )
                                    if generation is not None:
                                        activated = self.conn.execute(
                                            """SELECT route_key FROM provider_route_mutation_staging
                                            WHERE batch_id=? AND event_dedupe_key=? AND operation='activate'""",
                                            (batch["id"], event["dedupe_key"]),
                                        ).fetchall()
                                        for row in activated:
                                            self.os.store.mark_provider_object_seen(
                                                generation["id"], event["external_id"],
                                                row["route_key"], fence=fence,
                                            )
                        except Exception as exc:
                            self.os.rebuild_projections(workspace_id)
                            failed=self.inbox.fail_event(
                                event["id"],event_lease_owner,event["lease_token"],self._stable_error(exc),
                                stream_lock_id=stream_lock_id,reservation_token=stream_reservation_token,
                                credential_binding_id=binding["id"],credential_generation=binding["generation"],
                            )
                            if failed["status"] != "quarantined":
                                raise ConnectorTransportError(
                                    "connector inbox processing failed",retryable=True,retry_after=60
                                ) from exc
                            continue
                        total_created += int(result.created) if result is not None else 0
                        processed_in_batch += 1
                        if progress_callback is not None:
                            progress_callback(min(0.95, (page_count + .5) / 20))
                    try:
                        with self.os.store.atomic(immediate=True):
                            if provider_task is not None:
                                self.os.store.complete_provider_sync_task(
                                    provider_task["id"], provider_task["lease_token"],
                                    provider_fence,
                                )
                            if self._is_google(integration["source"]):
                                pending = self.os.store.pending_provider_task_count(
                                    workspace_id, integration["source"], account_key,
                                    provider_stream_key,
                                )
                                if generation is not None and pending == 0 and next_phase != "backfill":
                                    self.os.store.complete_provider_sync_generation(
                                        generation["id"], fence=provider_fence
                                    )
                                if pending == 0 and next_phase != "backfill":
                                    pending_batches = self.conn.execute(
                                        """SELECT id FROM connector_ingest_batches
                                        WHERE workspace_id=? AND connector=? AND account_key=?
                                          AND status='pending' ORDER BY created_at,id""",
                                        (workspace_id, integration["source"], account_key),
                                    ).fetchall()
                                    for pending_batch in pending_batches:
                                        self.inbox.complete_batch(
                                            pending_batch["id"], stream_lock_id,
                                            stream_reservation_token, binding["id"],
                                            binding["generation"], manage_transaction=False,
                                        )
                            else:
                                self.inbox.complete_batch(
                                    batch["id"],stream_lock_id,stream_reservation_token,
                                    binding["id"],binding["generation"],manage_transaction=False,
                                )
                    except ValidationError as exc:
                        raise ConnectorTransportError(
                            "connector inbox is waiting for retry",retryable=True,retry_after=60
                        ) from exc
                    total_quarantined += self.conn.execute(
                        """SELECT COUNT(*) FROM connector_batch_events be
                        JOIN connector_source_events e ON e.id=be.event_id
                        WHERE be.batch_id=? AND e.status='quarantined'""",(batch["id"],)
                    ).fetchone()[0]
                    if self._is_google(integration["source"]):
                        self.os.rebuild_projections(workspace_id)
                    cursor=next_cursor
                    page_count += 1
                    if not has_more:
                        break
                    if page_count >= 20:
                        backfill_remaining=True
                        break
                    if progress_callback is not None:
                        progress_callback(min(0.95,page_count/20))
            now = _now()
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.inbox._assert_stream_fence(stream_lock_id,stream_reservation_token,now)
                self.inbox._assert_credential_fence(binding["id"],binding["generation"])
                rollup=self._stream_rollup(integration)
                if self._is_google(integration["source"]):
                    current_objects = int(self.conn.execute(
                        """SELECT COUNT(DISTINCT external_id) FROM provider_object_routes
                        WHERE connector=? AND account_key LIKE ? AND status='active'""",
                        (integration["source"], f"{integration_id}:%"),
                    ).fetchone()[0])
                else:
                    current_objects = int(integration["object_count"]) + total_created
                self.conn.execute(
                    """UPDATE integrations SET status=?,health=?,last_sync_at=?,last_error=NULL,
                    object_count=? WHERE id=?""",
                    ("connected" if rollup["all_completed"] and not rollup["backfilling"] else "authorized",
                     "degraded" if rollup["quarantined"] else ("backfilling" if rollup["backfilling"] else ("healthy" if rollup["all_completed"] else "partial")),
                     now,current_objects,integration_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback();raise
            return {"integration_id": integration_id, "status": "completed", "seen": total_seen,
                    "created": total_created, "quarantined":total_quarantined,
                    "backfill_remaining":backfill_remaining,"batch_ids": batches}
        except ConnectorTransportError as exc:
            health, connection_state = self._transport_failure_state(exc)
            if self._owns_sync_fences(
                stream_lock_id, stream_reservation_token, binding["id"], binding["generation"]
            ):
                self._record_failure(integration_id, health, self._stable_error(exc), connection_state)
            raise
        except Exception as exc:
            if self._owns_sync_fences(
                stream_lock_id, stream_reservation_token, binding["id"], binding["generation"]
            ):
                self._record_failure(integration_id, "error", self._stable_error(exc))
            raise

    def resume_job_stream(self,job_id: str,worker_id: str,mapping_hash: str) -> dict[str,Any]:
        now=datetime.now(timezone.utc).replace(microsecond=0)
        token=self.os.jobs.new_id("streamtoken")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row=self.conn.execute(
                "SELECT * FROM connector_stream_locks WHERE job_id=? AND status='active'",(job_id,)
            ).fetchone()
            if row is None or row["mapping_hash"] != mapping_hash:
                raise ValidationError("active connector stream reservation is required")
            cursor=self.conn.execute(
                """UPDATE connector_stream_locks SET lease_owner=?,reservation_token=?,lease_expires_at=?,
                updated_at=?,version=version+1 WHERE id=? AND status='active' AND version=?""",
                (worker_id,token,(now+timedelta(days=7)).isoformat(),now.isoformat(),row["id"],row["version"]),
            )
            if cursor.rowcount != 1:
                raise ValidationError("connector stream reservation changed concurrently")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.inbox.get_stream_lock(row["id"])

    def release_job_stream(self,job_id: str) -> dict[str,Any] | None:
        row=self.conn.execute(
            "SELECT id,reservation_token FROM connector_stream_locks WHERE job_id=? AND status='active'",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return self.inbox.release_stream(row["id"],row["reservation_token"])

    def _owns_stream_fence(self,lock_id: str | None,token: str | None) -> bool:
        if lock_id is None and token is None:
            return True
        try:
            self.inbox._assert_stream_fence(lock_id,token,_now())
            return True
        except ValidationError:
            return False

    def _owns_sync_fences(
        self,
        lock_id: str | None,
        token: str | None,
        credential_binding_id: str,
        credential_generation: int,
    ) -> bool:
        if not self._owns_stream_fence(lock_id, token):
            return False
        try:
            self.inbox._assert_credential_fence(
                credential_binding_id, credential_generation
            )
            return True
        except ValidationError:
            return False

    def _pull(
        self, source: str, secret: str, external_key: str, workspace_id: str,
        cursor: str | None, integration: dict[str, Any],
    ) -> tuple[list[ConnectorSourceEvent], str | None, bool, dict[str, Any]]:
        if self.connector_factory is not None:
            result=self.connector_factory("pull", source, secret, external_key, workspace_id, cursor, integration)
            if len(result)==2:
                return result[0],result[1],False,{}
            if len(result)==3:
                return result[0],result[1],result[2],{}
            return result
        if source == "slack":
            connector = SlackConnector(secret, {external_key: workspace_id}, cursor=cursor)
            raw = connector.pull()
            return [self._normalize_event(item) for item in raw], connector.next_cursor, connector.has_more, {}
        if source == "clickup":
            connector = ClickUpConnector(secret, {external_key: workspace_id}, cursor=cursor)
            raw = connector.pull()
            return [self._normalize_event(item) for item in raw], connector.next_cursor, connector.has_more, {}
        if source == "google_drive":
            folders, drives = self._drive_mappings({external_key: workspace_id})
            account_key = f"{integration['id']}:{self._mapping_hash(source,external_key,workspace_id)}"
            connector = GoogleDriveConnector(
                secret,
                folder_workspace_mappings=folders,
                shared_drive_workspace_mappings=drives,
                expected_account_id=integration["expected_account_id"],
                granted_scopes=integration.get("_runtime_granted_permissions", ()),
                route_state=self.os.store.provider_route_state(workspace_id, source, account_key),
                ancestry_state=self._drive_ancestry_state(workspace_id, source, account_key),
                backfill_task=integration.get("_runtime_backfill_task"),
            )
            return self._google_pull_page(connector.pull(cursor), source, external_key, workspace_id)
        if source == "gmail":
            account_key = f"{integration['id']}:{self._mapping_hash(source,external_key,workspace_id)}"
            connector = GmailConnector(
                secret,
                label_workspace_mappings={external_key: workspace_id},
                expected_account_id=integration["expected_account_id"],
                granted_scopes=integration.get("_runtime_granted_permissions", ()),
                route_state=self.os.store.provider_route_state(workspace_id, source, account_key),
            )
            return self._google_pull_page(connector.pull(cursor), source, external_key, workspace_id)
        raise ValidationError("connector adapter is not enabled for live synchronization")

    def _google_pull_page(
        self, result: Any, source: str, external_key: str, workspace_id: str
    ) -> tuple[list[ConnectorSourceEvent], str | None, bool, dict[str, Any]]:
        if result.cursor_expired:
            return [], None, True, {"cursor_expired": True}
        if result.error:
            status = 401 if result.error_code == "authorization_required" else (
                403 if result.error_code in {"permission_denied", "not_found"} else (
                    429 if result.error_code in {"quota_exhausted", "rate_limited"} else 503
                )
            )
            raise ConnectorTransportError(
                "Google provider read failed", status=status,
                retryable=bool(result.retryable), retry_after=result.retry_after_seconds,
            )
        events_by_key = {event.dedupe_key: event for event in result.events}
        mutations = tuple(result.lifecycle_mutations)
        for mutation in mutations:
            if mutation.event_dedupe_key not in events_by_key:
                raise ValidationError("Google lifecycle mutation has no exact page event")
            if mutation.route_key != external_key or mutation.workspace_id != workspace_id:
                raise AuthorizationError("Google lifecycle mutation is outside the immutable mapped stream")
        return list(result.events), result.next_cursor, bool(result.has_more), {
            "lifecycle_mutations": mutations,
            "backfill_tasks": tuple(getattr(result, "backfill_tasks", ())),
            "reconciliation_requests": tuple(getattr(result, "reconciliation_requests", ())),
        }

    def _drive_ancestry_state(
        self, workspace_id: str, source: str, account_key: str
    ) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT external_id,root_route_keys,reconciliation_status
            FROM provider_object_ancestry
            WHERE workspace_id=? AND connector=? AND account_key=?""",
            (workspace_id, source, account_key),
        ).fetchall()
        return {
            row["external_id"]: {
                "root_route_keys": json.loads(row["root_route_keys"]),
                "reconciliation_status": row["reconciliation_status"],
            }
            for row in rows
        }

    @staticmethod
    def _provider_fence(
        stream_lock_id: str | None,
        reservation_token: str | None,
        binding: dict[str, Any],
    ) -> ProviderSyncFence | None:
        if stream_lock_id is None or reservation_token is None:
            return None
        return ProviderSyncFence(
            stream_lock_id, reservation_token, binding["id"], int(binding["generation"])
        )

    @staticmethod
    def _google_cursor_phase(cursor: str | None) -> str | None:
        if cursor is None:
            return None
        try:
            value = json.loads(cursor)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("Google provider cursor is invalid") from exc
        phase = value.get("phase") if isinstance(value, dict) else None
        if phase not in {"backfill", "changes", "history"}:
            raise ValidationError("Google provider cursor phase is invalid")
        return str(phase)

    def _persist_google_page_state(
        self,
        source: str,
        workspace_id: str,
        account_key: str,
        stream_key: str,
        route_key: str,
        next_cursor: str | None,
        events: list[ConnectorSourceEvent],
        page_meta: dict[str, Any],
        generation: dict[str, Any] | None,
        fence: ProviderSyncFence | None,
    ) -> None:
        generation_id = str(generation["id"]) if generation is not None else None
        if source == "google_drive":
            versions = {
                mutation.event_dedupe_key: mutation.provider_version
                for mutation in page_meta.get("lifecycle_mutations", ())
            }
            for event in events:
                file_node = event.payload.get("file")
                file_data = file_node if isinstance(file_node, dict) else {}
                parents = file_data.get("parents") or ()
                direct_routes = event.payload.get("route_keys") or ()
                self.os.store.resolve_provider_object_routes(
                    workspace_id, source, account_key, event.external_id,
                    versions.get(event.dedupe_key, event.event_type),
                    direct_route_keys=direct_routes,
                    parent_ids=parents,
                    is_container=file_data.get("mimeType") == (
                        "application/vnd.google-apps.folder"
                    ),
                    occurred_at=self._observed_at(event.observed_at),
                    fence=fence,
                )
            for task in page_meta.get("backfill_tasks", ()):
                self.os.store.enqueue_provider_sync_task(
                    workspace_id, source, account_key, stream_key, "backfill",
                    generation_id=generation_id, external_id=task.container_id,
                    route_key=task.route_key, page_token=task.page_token,
                    payload={"kind": "drive_tree"}, fence=fence,
                )
            for request in page_meta.get("reconciliation_requests", ()):
                self.os.store.enqueue_provider_sync_task(
                    workspace_id, source, account_key, stream_key,
                    "descendants" if request.descendants else "reconcile",
                    generation_id=generation_id, external_id=request.external_id,
                    route_key=route_key,
                    payload={
                        "parent_ids": list(request.parent_ids),
                        "reason": request.reason,
                    },
                    fence=fence,
                )
        elif source == "gmail" and self._google_cursor_phase(next_cursor) == "backfill":
            self.os.store.enqueue_provider_sync_task(
                workspace_id, source, account_key, stream_key, "backfill",
                generation_id=generation_id, external_id=route_key,
                route_key=route_key, page_token=hashlib.sha256(
                    next_cursor.encode("utf-8")
                ).hexdigest()[:24],
                payload={"cursor": next_cursor}, fence=fence,
            )

    def _provider_verify(self,integration: dict[str,Any],secret: str) -> Any:
        source=integration["source"]
        if self.connector_factory is not None:
            return self.connector_factory("verify",source,secret,integration)
        if source=="slack":
            return SlackConnector(secret,integration["workspace_mappings"],
                expected_team_id=integration["expected_account_id"]).verify_credentials()
        if source=="clickup":
            teams=ClickUpConnector(secret,integration["workspace_mappings"],
                expected_team_id=integration["expected_account_id"]).verify_credentials()
            return next(team for team in teams if team.team_id==integration["expected_account_id"])
        if source == "google_drive":
            folders, drives = self._drive_mappings(integration["workspace_mappings"])
            return GoogleDriveConnector(
                secret,
                folder_workspace_mappings=folders,
                shared_drive_workspace_mappings=drives,
                expected_account_id=integration["expected_account_id"],
                granted_scopes=integration.get("_runtime_granted_permissions", ()),
            ).verify_credentials()
        if source == "gmail":
            return GmailConnector(
                secret,
                label_workspace_mappings=integration["workspace_mappings"],
                expected_account_id=integration["expected_account_id"],
                granted_scopes=integration.get("_runtime_granted_permissions", ()),
            ).verify_credentials()
        raise ValidationError("unsupported live connector")

    def _refresh_google_access(
        self, integration: dict[str, Any], raw_secret: str
    ) -> tuple[str, tuple[str, ...]]:
        bundle = self._parse_google_credential_bundle(raw_secret)
        if self.connector_factory is not None:
            value = self.connector_factory("refresh", integration["source"], raw_secret, integration)
            if isinstance(value, dict):
                result = OAuthRefreshResult(
                    value.get("access_token"), value.get("expires_at"),
                    tuple(str(scope) for scope in value.get("scopes", ())),
                    bool(value.get("rate_limited", False)), value.get("retry_after_seconds"),
                    value.get("error"), value.get("error_code"), bool(value.get("retryable", False)),
                )
            else:
                result = value
        else:
            result = GoogleOAuthClient().refresh_access_token(
                bundle["client_id"], bundle["client_secret"], bundle["refresh_token"]
            )
        if not isinstance(result, OAuthRefreshResult):
            raise ValidationError("Google credential refresh returned an invalid result")
        if result.error or not result.access_token:
            status = 401 if result.error_code == "authorization_required" else (
                403 if result.error_code == "permission_denied" else (
                    429 if result.rate_limited else (503 if result.retryable else 400)
                )
            )
            raise ConnectorTransportError(
                "Google credential refresh failed", status=status,
                retryable=result.retryable or result.rate_limited,
                retry_after=result.retry_after_seconds,
            )
        scopes = tuple(sorted({str(scope).strip() for scope in result.scopes if str(scope).strip()}))
        if not scopes:
            raise ConnectorTransportError(
                "Google credential grant could not be proven", status=403, retryable=False
            )
        missing = self._missing_permissions(integration["source"], set(integration["permissions"]), set(scopes))
        if missing:
            raise ConnectorTransportError(
                "Google credential is missing required permissions", status=403, retryable=False
            )
        return result.access_token, scopes

    @staticmethod
    def _parse_google_credential_bundle(raw_secret: str) -> dict[str, str]:
        try:
            value = json.loads(raw_secret)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValidationError("Google credential reference must resolve to a JSON credential bundle") from exc
        required = {"client_id", "client_secret", "refresh_token"}
        if not isinstance(value, dict) or set(value) != required:
            raise ValidationError("Google credential bundle must contain exactly client_id, client_secret, and refresh_token")
        result: dict[str, str] = {}
        for key in sorted(required):
            item = value.get(key)
            if not isinstance(item, str) or not item.strip() or item != item.strip():
                raise ValidationError("Google credential bundle fields must be non-empty strings")
            result[key] = item
        return result

    @staticmethod
    def _is_google(source: str) -> bool:
        return source in {"google_drive", "gmail"}

    @staticmethod
    def _drive_mappings(workspace_mappings: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
        folders: dict[str, str] = {}
        drives: dict[str, str] = {}
        for route_key, workspace_id in workspace_mappings.items():
            kind, identifier = route_key.split(":", 1)
            (folders if kind == "folder" else drives)[identifier] = workspace_id
        return folders, drives

    @staticmethod
    def _normalize_event(event: Any) -> ConnectorSourceEvent:
        revision = hashlib.sha256(event.content.encode("utf-8")).hexdigest()[:16]
        external_id = event.source_key.rsplit(":", 1)[-1]
        return ConnectorSourceEvent(
            dedupe_key=f"{event.source_key}:{revision}", external_id=external_id,
            event_type="upsert", source_key=event.source_key, locator=event.locator,
            content=event.content, payload={"connector": event.connector},
            observed_at=event.observed_at.isoformat() if event.observed_at else None,
            media_type=event.media_type,
        )

    @staticmethod
    def _sanitize_source_event(event: ConnectorSourceEvent, secret: str) -> ConnectorSourceEvent:
        return ConnectorSourceEvent(
            dedupe_key=event.dedupe_key,external_id=event.external_id,event_type=event.event_type,
            source_key=event.source_key,locator=event.locator,
            content=str(sanitize_content(event.content,(secret,))),
            payload=sanitize_content(event.payload,(secret,)),observed_at=event.observed_at,
            media_type=event.media_type,
        )

    def _resolve(self, identity: AuthenticatedIdentity, integration: dict[str, Any]) -> str:
        row = self.conn.execute(
            """SELECT id FROM secret_bindings WHERE integration_id=? AND organization_id=?
            AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1""",
            (integration["id"], identity.organization_id),
        ).fetchone()
        if row is None:
            raise ValidationError("integration credential binding is required")
        return self.os.secrets.resolve_for_use(identity, row["id"], f"connector:{integration['source']}")

    def _verified_binding(self,identity: AuthenticatedIdentity,integration: dict[str,Any]) -> dict[str,Any]:
        row=self.conn.execute(
            """SELECT * FROM secret_bindings WHERE integration_id=? AND organization_id=?
            AND revoked_at IS NULL ORDER BY created_at DESC LIMIT 1""",
            (integration["id"],identity.organization_id),
        ).fetchone()
        if row is None or row["status"]!="active":
            raise ValidationError("provider-verified integration credential is required")
        return dict(row)

    def _get_for_job(self, identity: AuthenticatedIdentity, integration_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM integrations WHERE id=? AND organization_id=?", (integration_id, identity.organization_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("integration not found")
        item = dict(row)
        item["workspace_mappings"] = json.loads(item["workspace_mappings"])
        item["permissions"] = json.loads(item["permissions"])
        item["granted_permissions"] = json.loads(item.get("granted_permissions") or "[]")
        if identity.workspace_id is None or set(item["workspace_mappings"].values()) >= {identity.workspace_id}:
            return item
        raise AuthorizationError("integration is outside the job workspace scope")

    def _has_active_sync(self, integration_id: str, external_key: str | None = None) -> bool:
        rows=self.conn.execute(
            "SELECT account_key,job_id FROM connector_stream_locks WHERE status='active' AND account_key LIKE ?",
            (f"{integration_id}:%",),
        ).fetchall()
        if external_key is None:
            return bool(rows)
        for row in rows:
            job=self.conn.execute("SELECT payload FROM jobs WHERE id=?",(row["job_id"],)).fetchone()
            if job is not None and json.loads(job["payload"]).get("external_key")==external_key:
                return True
        return False

    def _stream_rollup(self, integration: dict[str, Any]) -> dict[str,Any]:
        all_completed=True;backfilling=False;quarantined=0
        for external_key,workspace_id in integration["workspace_mappings"].items():
            account_key=f"{integration['id']}:{self._mapping_hash(integration['source'],external_key,workspace_id)}"
            row=self.conn.execute(
                """SELECT 1 FROM connector_ingest_batches WHERE organization_id=? AND workspace_id=?
                AND connector=? AND account_key=? AND status='completed' LIMIT 1""",
                (integration["organization_id"],workspace_id,integration["source"],account_key),
            ).fetchone()
            if row is None:
                all_completed=False
            cursor=self.inbox.get_cursor(integration["organization_id"],workspace_id,integration["source"],account_key)
            if self._cursor_has_more(integration["source"],cursor):
                backfilling=True
            quarantined += self.conn.execute(
                """SELECT COUNT(*) FROM connector_source_events WHERE organization_id=? AND workspace_id=?
                AND connector=? AND account_key=? AND status='quarantined'""",
                (integration["organization_id"],workspace_id,integration["source"],account_key),
            ).fetchone()[0]
        return {"all_completed":all_completed,"backfilling":backfilling,"quarantined":quarantined}

    @staticmethod
    def _cursor_has_more(source: str,cursor: str | None) -> bool:
        if not cursor:
            return False
        if source=="clickup":
            return cursor!="0"
        if source=="slack":
            try:
                value=json.loads(cursor)
            except (TypeError,ValueError):
                return True
            if isinstance(value,dict) and "page_cursor" in value:
                return bool(value.get("page_cursor"))
            if isinstance(value,dict):
                return any(isinstance(item,dict) and item.get("page_cursor") for item in value.values())
        return False

    @staticmethod
    def _mapping_hash(source: str, external_key: str, workspace_id: str) -> str:
        value = json.dumps([source, external_key, workspace_id], separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _credential_metadata(self, integration_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT id,name,provider,scopes,fingerprint,status,last_verified_at,created_at,updated_at,revoked_at,generation
            FROM secret_bindings WHERE integration_id=? ORDER BY created_at DESC LIMIT 1""", (integration_id,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["scopes"] = json.loads(item["scopes"])
        return item

    def _record_failure(self, integration_id: str, health: str, error: str, status: str | None = None) -> None:
        if status is None:
            self.conn.execute("UPDATE integrations SET health=?,last_error=? WHERE id=?", (health, error, integration_id))
        else:
            self.conn.execute(
                "UPDATE integrations SET status=?,health=?,last_error=? WHERE id=?",
                (status, health, error, integration_id),
            )
        self.conn.commit()

    @staticmethod
    def _verified_account(source: str, value: Any) -> tuple[str, str, tuple[str, ...]]:
        if isinstance(value, dict):
            account_id=str(value.get("account_id") or value.get("team_id") or "")
            account_name=str(value.get("account_name") or value.get("team_name") or "")
            granted=tuple(str(item) for item in value.get("granted_permissions",()))
            return account_id,account_name,granted
        if source == "slack":
            return value.team_id,value.team_name,tuple(value.granted_scopes)
        if source == "clickup":
            return value.team_id,value.team_name,("authorized_team",)
        if source == "google_drive":
            return value.account_id,value.display_name,tuple(
                getattr(value, "granted_scopes", getattr(value, "scopes", ()))
            )
        if source == "gmail":
            return value.email_address,value.email_address,tuple(
                getattr(value, "granted_scopes", getattr(value, "scopes", ()))
            )
        raise ValidationError("unsupported provider identity")

    @staticmethod
    def _validate_permissions(source: str, permissions: set[str]) -> None:
        if source == "slack" and not permissions.intersection(
            {"channels:history", "groups:history", "im:history", "mpim:history"}
        ):
            raise ValidationError("Slack synchronization requires an explicit conversation history permission")
        if source == "clickup" and permissions != {"authorized_team"}:
            raise ValidationError("ClickUp synchronization requires authorized_team permission")
        if source == "google_drive" and GOOGLE_DRIVE_READ_SCOPE not in permissions:
            raise ValidationError("Google Drive synchronization requires drive.readonly permission")
        if source == "gmail" and GMAIL_READ_SCOPE not in permissions:
            raise ValidationError("Gmail synchronization requires gmail.readonly permission")

    @staticmethod
    def _validate_mapping_keys(source: str, workspace_mappings: dict[str, str]) -> None:
        keys = {str(key).strip() for key in workspace_mappings}
        if source == "google_drive":
            invalid = [key for key in keys if not (
                key.startswith("folder:") and key[7:].strip()
                or key.startswith("drive:") and key[6:].strip()
            )]
            if invalid:
                raise ValidationError("Google Drive mappings must use folder:<id> or drive:<id>")
        if source == "gmail" and any(not key.startswith("label:") or not key[6:].strip() for key in keys):
            raise ValidationError("Gmail mappings must use label:<id>")

    @staticmethod
    def _canonicalize_mappings(workspace_mappings: dict[str, str]) -> dict[str, str]:
        if not isinstance(workspace_mappings, dict) or not workspace_mappings:
            raise ValidationError("at least one external-container to workspace mapping is required")
        canonical: dict[str, str] = {}
        for raw_key, raw_workspace_id in workspace_mappings.items():
            if not isinstance(raw_key, str) or not isinstance(raw_workspace_id, str):
                raise ValidationError("mapping keys and workspace IDs must be strings")
            key = raw_key.strip()
            workspace_id = raw_workspace_id.strip()
            if not key or not workspace_id:
                raise ValidationError("mapping keys and workspace IDs must be non-empty")
            if key in canonical:
                raise ValidationError("mapping keys must be unique after normalization")
            canonical[key] = workspace_id
        return canonical

    @staticmethod
    def _canonicalize_permissions(permissions: list[str]) -> set[str]:
        if not isinstance(permissions, list):
            raise ValidationError("permissions must be a list of non-empty strings")
        canonical: set[str] = set()
        for value in permissions:
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("permissions must contain only non-empty strings")
            canonical.add(value.strip())
        return canonical

    @staticmethod
    def _normalize_expected_account_id(source: str, expected_account_id: str) -> str:
        if not isinstance(expected_account_id, str):
            return ""
        value = expected_account_id.strip()
        if source == "google_drive" and "@" in value:
            raise ValidationError("Google Drive expected account ID must be the stable permissionId, not an email")
        return value.casefold() if source == "gmail" else value

    @staticmethod
    def _account_ids_match(source: str, actual: str, expected: str) -> bool:
        if source == "gmail":
            return str(actual).strip().casefold() == str(expected).strip().casefold()
        return str(actual).strip() == str(expected).strip()

    @staticmethod
    def _missing_permissions(source: str, required: set[str], granted: set[str]) -> set[str]:
        missing = set(required) - set(granted)
        if source == "google_drive" and GOOGLE_DRIVE_READ_SCOPE in missing and granted.intersection({
            GOOGLE_DRIVE_READ_SCOPE, "https://www.googleapis.com/auth/drive"
        }):
            missing.remove(GOOGLE_DRIVE_READ_SCOPE)
        if source == "gmail" and GMAIL_READ_SCOPE in missing and granted.intersection({
            GMAIL_READ_SCOPE,
            "https://www.googleapis.com/auth/gmail.modify",
            "https://mail.google.com/",
        }):
            missing.remove(GMAIL_READ_SCOPE)
        return missing

    @staticmethod
    def _transport_failure_state(exc: ConnectorTransportError) -> tuple[str, str | None]:
        health = "rate_limited" if exc.status == 429 else "degraded"
        status = "reauth_required" if exc.status == 401 else (
            "action_required" if exc.status == 403 else None
        )
        return health, status

    @staticmethod
    def _stable_error(exc: Exception) -> str:
        if isinstance(exc, ConnectorTransportError):
            return f"connector_transport:{exc.status or 'network'}"
        return exc.__class__.__name__

    @staticmethod
    def _observed_at(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _public_identity(value: Any) -> Any:
        def json_safe(item: Any) -> Any:
            if hasattr(item,"__dict__"):
                return json_safe(dict(item.__dict__))
            if isinstance(item,dict):
                return {str(key):json_safe(entry) for key,entry in item.items()}
            if isinstance(item,(set,frozenset)):
                return sorted(json_safe(entry) for entry in item)
            if isinstance(item,(list,tuple)):
                return [json_safe(entry) for entry in item]
            return item
        return redact(json_safe(value))
