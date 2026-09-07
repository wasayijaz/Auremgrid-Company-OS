"""Connector synchronization orchestration operations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from auremgrid.connectors.figma import FigmaMappingOverlap
from auremgrid.connectors.fireflies import FirefliesMappingOverlap
from auremgrid.connectors.gmail import GmailMappingOverlap
from auremgrid.connectors.google_drive import DriveBackfillTask, GoogleDriveMappingOverlap
from auremgrid.connectors.http import ConnectorTransportError
from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.integration_ops_shared import _now


class IntegrationOpsSyncMixin:
    def enqueue_sync(
        self, identity: AuthenticatedIdentity, integration_id: str, priority: int = 0,
        max_attempts: int = 5, idempotency_key: str | None = None,
    ) -> list[dict[str, Any]]:
        identity.require("integration_sync")
        integration = self.get(identity, integration_id)
        if integration["source"] not in self._live_sources():
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
        if integration["source"] not in self._live_sources():
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
                    pending_provider_tasks_after_page: int | None = None
                    page_integration = integration
                    if self._is_google(integration["source"]):
                        running_generation = self.os.store.get_running_generation(
                            workspace_id, integration["source"], account_key,
                            provider_stream_key, external_key,
                        )
                        if cursor is None and running_generation is not None:
                            cursor = running_generation["baseline_cursor"]
                        provider_task = self.os.store.claim_provider_sync_task(
                            workspace_id, integration["source"], account_key,
                            provider_stream_key, event_lease_owner, 60,
                            fence=provider_fence,
                        )
                        if provider_task is not None:
                            payload = json.loads(provider_task["payload"] or "{}")
                            if integration["source"] == "google_drive":
                                page_integration = {
                                    **integration,
                                    "_runtime_backfill_task": DriveBackfillTask(
                                        provider_task["route_key"],
                                        provider_task["external_id"] or provider_task["route_key"].split(":", 1)[1],
                                        provider_task["page_token"],
                                    ),
                                    "_runtime_provider_task_type": provider_task["task_type"],
                                    "_runtime_provider_task_payload": payload,
                                }
                            elif integration["source"] == "gmail" and provider_task["task_type"] == "backfill":
                                cursor = str(payload["cursor"])
                    cursor_before_page = cursor
                    pull_cursor = cursor
                    if (
                        provider_task is not None
                        and integration["source"] == "google_drive"
                        and provider_task["task_type"] in {"reconcile", "descendants"}
                    ):
                        # Reconciliation is an auxiliary operation wave.  Run
                        # it through the bounded backfill/reconcile endpoint
                        # while retaining the original changes cursor below.
                        try:
                            current_state = json.loads(cursor) if cursor else {}
                        except (TypeError, json.JSONDecodeError) as exc:
                            raise ValidationError("Google Drive task cursor is invalid") from exc
                        pull_cursor = json.dumps({
                            "v": 1, "phase": "backfill",
                            "checkpoint": str(current_state.get("checkpoint") or "task"),
                        }, sort_keys=True, separators=(",", ":"))
                    events,next_cursor,has_more,page_meta = self._pull(
                        integration["source"],secret,external_key,workspace_id,pull_cursor,page_integration
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
                        (provider_task is not None and provider_task["task_type"] != "backfill")
                        or next_phase == "backfill" or page_meta.get("backfill_tasks")
                        or page_meta.get("reconciliation_requests")
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
                        # A restarted worker may claim an older pending event
                        # while draining a newly-recorded provider page.  Use
                        # the batch that actually owns this event's staged
                        # lifecycle mutation; binding/applying against the
                        # current page batch would reject the event and leave
                        # the durable backfill wave stuck.
                        event_batch = self.conn.execute(
                            """SELECT link.batch_id
                               FROM connector_batch_events link
                               LEFT JOIN provider_route_mutation_staging mutation
                                 ON mutation.batch_id=link.batch_id AND mutation.event_id=link.event_id
                               WHERE link.event_id=?
                               ORDER BY CASE WHEN mutation.id IS NULL THEN 1 ELSE 0 END,
                                        link.rowid
                               LIMIT 1""",
                            (event["id"],),
                        ).fetchone()
                        if event_batch is None:
                            raise ValidationError("connector event has no durable ingest batch")
                        event_batch_id = event_batch["batch_id"]
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
                                    (event_batch_id, event["dedupe_key"]),
                                ).fetchall()
                                activates = any(row["operation"] == "activate" for row in staged)
                                if activates or not self._uses_provider_lifecycle(integration["source"]) or (
                                    integration["source"] in {"figma", "fireflies"} and not staged
                                ):
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
                                if self._uses_provider_lifecycle(integration["source"]):
                                    fence = self._provider_fence(
                                        stream_lock_id, stream_reservation_token, binding
                                    )
                                    if result is not None:
                                        self.os.store.bind_provider_event_source(
                                            event_batch_id, event["dedupe_key"], workspace_id,
                                            result.source.id, fence,
                                        )
                                    self.os.store.apply_provider_event_mutations(
                                        event_batch_id, event["dedupe_key"], fence
                                    )
                                    if generation is not None:
                                        activated = self.conn.execute(
                                            """SELECT route_key FROM provider_route_mutation_staging
                                            WHERE batch_id=? AND event_dedupe_key=? AND operation='activate'""",
                                            (event_batch_id, event["dedupe_key"]),
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
                                self._acknowledge_descendant_wave_if_drained(
                                    workspace_id, integration["source"], account_key,
                                    provider_stream_key, provider_task, provider_fence,
                                )
                            if self._is_google(integration["source"]):
                                pending = self.os.store.pending_provider_task_count(
                                    workspace_id, integration["source"], account_key,
                                    provider_stream_key,
                                )
                                pending_provider_tasks_after_page = pending
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
                    if self._uses_provider_lifecycle(integration["source"]):
                        self.os.rebuild_projections(workspace_id)
                    # Task pages reconcile durable state while the original
                    # provider cursor remains parked.  Only a regular provider
                    # page may advance the stream checkpoint.
                    cursor = (
                        cursor_before_page
                        if provider_task is not None and provider_task["task_type"] != "backfill"
                        else next_cursor
                    )
                    page_count += 1
                    if not has_more:
                        if (
                            provider_task is not None
                            and pending_provider_tasks_after_page is not None
                            and (
                                pending_provider_tasks_after_page > 0
                                or provider_task["task_type"] != "backfill"
                            )
                        ):
                            # Keep draining a leased operation wave in this
                            # resumed worker; when it reaches zero the parked
                            # provider cursor is consumed immediately below.
                            continue
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
                if self._uses_provider_lifecycle(integration["source"]):
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
        except (GoogleDriveMappingOverlap, GmailMappingOverlap, FigmaMappingOverlap, FirefliesMappingOverlap) as exc:
            # Quarantine only a redacted organization-level digest.  The
            # stream cursor is still untouched because the exception occurs
            # before ``record_pull``; neither workspace learns object existence.
            mapping_digest = exc.evidence_digest or hashlib.sha256(
                json.dumps(integration.get("workspace_mappings") or {}, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:32]
            self.os.store.quarantine_provider_sync(
                identity.organization_id, integration_id, integration["source"],
                "mapping_overlap", mapping_digest,
            )
            raise ConnectorTransportError(
                "provider mapping requires operator resolution", status=403, retryable=False
            ) from exc
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

