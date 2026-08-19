"""Credential-backed, workspace-scoped connector synchronization."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from auremgrid.connectors.clickup import ClickUpConnector
from auremgrid.connectors.google_auth import ConnectorInboxRepository, ConnectorSourceEvent
from auremgrid.connectors.http import ConnectorTransportError, sanitize_content
from auremgrid.connectors.slack import SlackConnector
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.secrets import redact


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
        source = source.strip()
        if source not in LIVE_SOURCES:
            raise ValidationError("unsupported live connector")
        expected_account_id = expected_account_id.strip()
        if not expected_account_id:
            raise ValidationError("expected provider account ID is required")
        if not workspace_mappings or any(not str(key).strip() or not str(value).strip() for key, value in workspace_mappings.items()):
            raise ValidationError("at least one external-container to workspace mapping is required")
        requested_permissions=set(permissions)
        if source=="slack" and not requested_permissions.intersection(
            {"channels:history","groups:history","im:history","mpim:history"}
        ):
            raise ValidationError("Slack synchronization requires an explicit conversation history permission")
        if source=="clickup" and requested_permissions != {"authorized_team"}:
            raise ValidationError("ClickUp synchronization requires authorized_team permission")
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
            json.dumps(sorted(set(permissions)), separators=(",", ":")),
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
        secret = self._resolve(identity, integration)
        verified = self._provider_verify(integration,secret)
        account_id, account_name, granted = self._verified_account(integration["source"], verified)
        if account_id != integration["expected_account_id"]:
            raise AuthorizationError("provider account identity mismatch")
        missing = set(integration["permissions"]) - set(granted)
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
            if current_account_id != integration["provider_account_id"] or current_account_id != integration["expected_account_id"]:
                raise ConnectorTransportError("provider account identity changed",status=401,retryable=False)
            if set(integration["permissions"]) - set(current_granted):
                raise ConnectorTransportError("provider permission grant changed",status=403,retryable=False)
            for external_key, workspace_id in selected_mappings.items():
                if identity.workspace_id not in {None, workspace_id}:
                    raise AuthorizationError("job principal cannot access a mapped workspace")
                actor_id = self.os.auth.actor_for_identity(identity, workspace_id)
                account_key = f"{integration_id}:{self._mapping_hash(integration['source'],external_key,workspace_id)}"
                cursor = self.inbox.get_cursor(identity.organization_id, workspace_id, integration["source"], account_key)
                page_count=0
                while True:
                    events,next_cursor,has_more = self._pull(
                        integration["source"],secret,external_key,workspace_id,cursor,integration
                    )
                    events = [self._sanitize_source_event(event, secret) for event in events]
                    batch = self.inbox.record_pull(
                        identity.organization_id, workspace_id, integration["source"], account_key,
                        cursor, next_cursor, events,
                        stream_lock_id=stream_lock_id,reservation_token=stream_reservation_token,
                        credential_binding_id=binding["id"],credential_generation=binding["generation"],
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
                                result = self.os.ingest_text(
                                    workspace_id, actor_id, event["source_key"], event["content"], event["locator"],
                                    observed_at=self._observed_at(event["observed_at"]), media_type=event["media_type"],
                                    trust_level="external",
                                )
                                self.inbox.complete_event(
                                    event["id"], event_lease_owner, event["lease_token"],
                                    stream_lock_id=stream_lock_id,
                                    reservation_token=stream_reservation_token,
                                    credential_binding_id=binding["id"],
                                    credential_generation=binding["generation"],
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
                        total_created += int(result.created)
                        processed_in_batch += 1
                        if progress_callback is not None:
                            progress_callback(min(0.95, (page_count + .5) / 20))
                    try:
                        self.inbox.complete_batch(
                            batch["id"],stream_lock_id,stream_reservation_token,binding["id"],binding["generation"]
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
                self.conn.execute(
                    """UPDATE integrations SET status=?,health=?,last_sync_at=?,last_error=NULL,
                    object_count=object_count+? WHERE id=?""",
                    ("connected" if rollup["all_completed"] and not rollup["backfilling"] else "authorized",
                     "degraded" if rollup["quarantined"] else ("backfilling" if rollup["backfilling"] else ("healthy" if rollup["all_completed"] else "partial")),
                     now,total_created,integration_id),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback();raise
            return {"integration_id": integration_id, "status": "completed", "seen": total_seen,
                    "created": total_created, "quarantined":total_quarantined,
                    "backfill_remaining":backfill_remaining,"batch_ids": batches}
        except ConnectorTransportError as exc:
            health = "rate_limited" if exc.status == 429 else "degraded"
            connection_state = "reauth_required" if exc.status == 401 else ("action_required" if exc.status == 403 else None)
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
    ) -> tuple[list[ConnectorSourceEvent], str | None, bool]:
        if self.connector_factory is not None:
            result=self.connector_factory("pull", source, secret, external_key, workspace_id, cursor, integration)
            if len(result)==2:
                return result[0],result[1],False
            return result
        if source == "slack":
            connector = SlackConnector(secret, {external_key: workspace_id}, cursor=cursor)
            raw = connector.pull()
            return [self._normalize_event(item) for item in raw], connector.next_cursor, connector.has_more
        if source == "clickup":
            connector = ClickUpConnector(secret, {external_key: workspace_id}, cursor=cursor)
            raw = connector.pull()
            return [self._normalize_event(item) for item in raw], connector.next_cursor, connector.has_more
        raise ValidationError("connector adapter is not enabled for live synchronization")

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
        raise ValidationError("unsupported live connector")

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
        raise ValidationError("unsupported provider identity")

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
