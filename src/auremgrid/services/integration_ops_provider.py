"""Provider adapters, page mapping, and provider lifecycle helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from auremgrid.connectors.clickup import ClickUpConnector
from auremgrid.connectors.figma import FigmaConnector, FigmaMappingOverlap
from auremgrid.connectors.fireflies import FirefliesConnector, FirefliesMappingOverlap
from auremgrid.connectors.gmail import GmailConnector, GmailMappingOverlap
from auremgrid.connectors.google_auth import ConnectorSourceEvent, GoogleOAuthClient, OAuthRefreshResult
from auremgrid.connectors.google_drive import GoogleDriveConnector, GoogleDriveMappingOverlap
from auremgrid.connectors.http import ConnectorTransportError, sanitize_content
from auremgrid.connectors.slack import SlackConnector
from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.storage.sqlite import ProviderSyncFence


class IntegrationOpsProviderMixin:
    def _pull(
        self, source: str, secret: str, external_key: str, workspace_id: str,
        cursor: str | None, integration: dict[str, Any],
    ) -> tuple[list[ConnectorSourceEvent], str | None, bool, dict[str, Any]]:
        if self.connector_factory is not None:
            result=self.connector_factory("pull", source, secret, external_key, workspace_id, cursor, integration)
            if self._uses_provider_lifecycle(source) and len(result) >= 4:
                factory_events, factory_cursor, factory_more, factory_meta = result[:4]
                for event in factory_events:
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    workspace_ids = payload.get("workspace_ids") or ()
                    if any(str(value) != workspace_id for value in workspace_ids):
                        overlap_type = self._overlap_type(source)
                        raise overlap_type(evidence_digest=hashlib.sha256(
                            f"{event.external_id}:{','.join(sorted(str(value) for value in workspace_ids))}".encode()
                        ).hexdigest()[:32])
                for mutation in tuple(factory_meta.get("lifecycle_mutations", ())):
                    if mutation.route_key != external_key or mutation.workspace_id != workspace_id:
                        overlap_type = self._overlap_type(source)
                        raise overlap_type(evidence_digest=hashlib.sha256(
                            f"{mutation.external_id}:{mutation.route_key}:{mutation.workspace_id}".encode()
                        ).hexdigest()[:32])
            if len(result)==2:
                return result[0],result[1],False,{}
            if len(result)==3:
                return result[0],result[1],result[2],{}
            if source == "google_drive" and len(result) >= 4:
                factory_meta = dict(result[3] or {})
                factory_meta.setdefault(
                    "task_type", integration.get("_runtime_provider_task_type") or "backfill"
                )
                factory_meta.setdefault(
                    "task_payload", integration.get("_runtime_provider_task_payload") or {}
                )
                return result[0], result[1], result[2], factory_meta
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
            # Instantiate the adapter with the complete integration registry so
            # ownership collisions are classified before an inbox row/cursor
            # can be committed.  The adapter receives this stream's route as
            # ``owned_route_key`` and filters all emitted mutations to it.
            full_mappings = dict(integration.get("workspace_mappings") or {external_key: workspace_id})
            folders, drives = self._drive_mappings(full_mappings)
            account_key = f"{integration['id']}:{self._mapping_hash(source,external_key,workspace_id)}"
            registry_keys = [
                f"{integration['id']}:{self._mapping_hash(source, route, mapped_workspace)}"
                for route, mapped_workspace in full_mappings.items()
            ]
            connector = GoogleDriveConnector(
                secret,
                folder_workspace_mappings=folders,
                shared_drive_workspace_mappings=drives,
                expected_account_id=integration["expected_account_id"],
                granted_scopes=integration.get("_runtime_granted_permissions", ()),
                route_state=self.os.store.provider_route_state_for_mappings(source, registry_keys),
                ancestry_state=self._drive_ancestry_state_for_mappings(source, registry_keys),
                backfill_task=integration.get("_runtime_backfill_task"),
                owned_route_key=external_key,
                task_type=str(integration.get("_runtime_provider_task_type") or "backfill"),
                task_payload=integration.get("_runtime_provider_task_payload") or {},
            )
            page = self._google_pull_page(connector.pull(cursor), source, external_key, workspace_id)
            page[3]["task_type"] = connector.task_type
            page[3]["task_payload"] = dict(connector.task_payload)
            return page
        if source == "gmail":
            account_key = f"{integration['id']}:{self._mapping_hash(source,external_key,workspace_id)}"
            full_mappings = dict(integration.get("workspace_mappings") or {external_key: workspace_id})
            registry_keys = [
                f"{integration['id']}:{self._mapping_hash(source, route, mapped_workspace)}"
                for route, mapped_workspace in full_mappings.items()
            ]
            connector = GmailConnector(
                secret,
                label_workspace_mappings=full_mappings,
                expected_account_id=integration["expected_account_id"],
                granted_scopes=integration.get("_runtime_granted_permissions", ()),
                route_state=self.os.store.provider_route_state_for_mappings(source, registry_keys),
                owned_route_key=external_key,
            )
            return self._google_pull_page(connector.pull(cursor), source, external_key, workspace_id)
        if source == "figma":
            full_mappings = dict(integration.get("workspace_mappings") or {external_key: workspace_id})
            registry_keys = [
                f"{integration['id']}:{self._mapping_hash(source, route, mapped_workspace)}"
                for route, mapped_workspace in full_mappings.items()
            ]
            common = {
                "expected_account_id": integration["expected_account_id"],
                "granted_permissions": integration.get("granted_permissions", ()),
                "route_state": self.os.store.provider_route_state_for_mappings(source, registry_keys),
                "owned_route_key": external_key,
            }
            connector = FigmaConnector(secret, file_workspace_mappings=full_mappings, **common)
            return self._provider_pull_page(connector.pull(cursor), source, external_key, workspace_id)
        if source == "fireflies":
            full_mappings = dict(integration.get("workspace_mappings") or {external_key: workspace_id})
            connector = FirefliesConnector(
                secret, workspace_mappings=full_mappings,
                expected_account_id=integration["expected_account_id"],
            )
            return self._provider_pull_page(connector.pull(cursor), source, external_key, workspace_id)
        raise ValidationError("connector adapter is not enabled for live synchronization")

    def _provider_pull_page(
        self, result: Any, source: str, external_key: str, workspace_id: str
    ) -> tuple[list[ConnectorSourceEvent], str | None, bool, dict[str, Any]]:
        events = list(result.events)
        event_keys = {event.dedupe_key for event in events}
        for event in events:
            payload = event.payload if isinstance(event.payload, dict) else {}
            workspace_ids = {str(value) for value in payload.get("workspace_ids") or ()}
            if workspace_ids != {workspace_id}:
                raise self._overlap_type(source)(hashlib.sha256(
                    f"{event.external_id}:{','.join(sorted(workspace_ids))}".encode()
                ).hexdigest()[:32])
        mutations = tuple(result.lifecycle_mutations)
        for mutation in mutations:
            if mutation.event_dedupe_key not in event_keys:
                raise ValidationError("provider lifecycle mutation has no exact page event")
            if mutation.route_key != external_key or mutation.workspace_id != workspace_id:
                raise AuthorizationError("provider lifecycle mutation is outside the immutable mapped stream")
        return events, result.next_cursor, bool(result.has_more), {"lifecycle_mutations": mutations}

    @staticmethod
    def _overlap_type(source: str) -> type[Exception]:
        return {
            "google_drive": GoogleDriveMappingOverlap, "gmail": GmailMappingOverlap,
            "figma": FigmaMappingOverlap, "fireflies": FirefliesMappingOverlap,
        }[source]

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
        if source in {"google_drive", "gmail"}:
            for event in result.events:
                payload = event.payload if isinstance(event.payload, dict) else {}
                workspace_ids = payload.get("workspace_ids") or ()
                if any(str(value) != workspace_id for value in workspace_ids):
                    overlap_type = GoogleDriveMappingOverlap if source == "google_drive" else GmailMappingOverlap
                    raise overlap_type(evidence_digest=hashlib.sha256(
                        f"{event.external_id}:{','.join(sorted(str(value) for value in workspace_ids))}".encode()
                    ).hexdigest()[:32])
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
            "ancestry_resolutions": tuple(getattr(result, "ancestry_resolutions", ())),
        }

    def _drive_ancestry_state(
        self, workspace_id: str, source: str, account_key: str
    ) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT external_id,parent_ids,root_route_keys,is_container,reconciliation_status
            FROM provider_object_ancestry
            WHERE workspace_id=? AND connector=? AND account_key=?""",
            (workspace_id, source, account_key),
        ).fetchall()
        return {
            row["external_id"]: {
                "parent_ids": json.loads(row["parent_ids"]),
                "root_route_keys": json.loads(row["root_route_keys"]),
                "is_container": bool(row["is_container"]),
                "reconciliation_status": row["reconciliation_status"],
            }
            for row in rows
        }

    def _drive_ancestry_state_for_mappings(
        self, source: str, account_keys: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Merge ancestry snapshots across all streams of one integration."""
        if not account_keys:
            return {}
        placeholders = ",".join("?" for _ in account_keys)
        rows = self.conn.execute(
            f"""SELECT external_id,parent_ids,root_route_keys,is_container,reconciliation_status
                FROM provider_object_ancestry
                WHERE connector=? AND account_key IN ({placeholders})""",
            (source, *account_keys),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            inherited = json.loads(row["root_route_keys"])
            current = result.setdefault(row["external_id"], {
                "parent_ids": [], "root_route_keys": [], "is_container": bool(row["is_container"]),
                "reconciliation_status": row["reconciliation_status"],
            })
            current["parent_ids"] = sorted(set(current["parent_ids"]).union(json.loads(row["parent_ids"])))
            current["root_route_keys"] = sorted(set(current["root_route_keys"]).union(inherited))
            current["is_container"] = current["is_container"] or bool(row["is_container"])
            if row["reconciliation_status"] != "resolved":
                current["reconciliation_status"] = row["reconciliation_status"]
        return result

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
            # Reconciliation returns parent-first ancestry snapshots. Persist
            # them before applying target lifecycle mutations so an accessible
            # unmapped parent is a resolved empty route, not an unknown parent
            # that would preserve stale evidence.
            for resolution in page_meta.get("ancestry_resolutions", ()):
                self.os.store.resolve_provider_object_routes(
                    workspace_id, source, account_key, resolution.external_id,
                    resolution.provider_version,
                    direct_route_keys=resolution.root_route_keys,
                    parent_ids=resolution.parent_ids,
                    is_container=resolution.is_container,
                    fence=fence,
                )
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
                task_payload = page_meta.get("task_payload") or {}
                parent_wave = task_payload.get("parent_wave") or (
                    task_payload.get("parent_external_id") if page_meta.get("task_type") == "descendants" else None
                )
                self.os.store.enqueue_provider_sync_task(
                    workspace_id, source, account_key, stream_key, "backfill",
                    generation_id=generation_id, external_id=task.container_id,
                    route_key=task.route_key, page_token=task.page_token,
                    operation_key=hashlib.sha256(
                        f"drive-tree:{task.route_key}:{task.container_id}:{task.page_token or ''}:{parent_wave or ''}".encode()
                    ).hexdigest()[:32],
                    payload={"kind": "drive_tree", "parent_wave": parent_wave}, fence=fence,
                )
            for request in page_meta.get("reconciliation_requests", ()):
                self.os.store.enqueue_provider_sync_task(
                    workspace_id, source, account_key, stream_key,
                    "descendants" if request.descendants else "reconcile",
                    generation_id=generation_id, external_id=request.external_id,
                    route_key=route_key,
                    operation_key=request.operation_key or hashlib.sha256(
                        f"drive-reconcile:{request.external_id}:{request.reason}:{','.join(request.parent_ids)}".encode()
                    ).hexdigest()[:32],
                    payload={
                        "parent_ids": list(request.parent_ids),
                        "reason": request.reason,
                        "parent_external_id": request.external_id,
                        "descendant_ids": list(getattr(request, "descendant_ids", ())),
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
                operation_key=hashlib.sha256(f"gmail-backfill:{route_key}:{next_cursor}".encode()).hexdigest()[:32],
                payload={"cursor": next_cursor}, fence=fence,
            )

    def _acknowledge_descendant_wave_if_drained(
        self,
        workspace_id: str,
        source: str,
        account_key: str,
        stream_key: str,
        completed_task: dict[str, Any],
        fence: ProviderSyncFence | None,
    ) -> None:
        """Close a moved-folder wave only after every spawned task is done."""
        payload = json.loads(completed_task.get("payload") or "{}")
        wave_root = payload.get("parent_wave")
        if completed_task.get("task_type") == "descendants":
            wave_root = completed_task.get("external_id")
        if not wave_root:
            return
        rows = self.conn.execute(
            """SELECT payload FROM provider_sync_tasks
               WHERE workspace_id=? AND connector=? AND account_key=? AND stream_key=?
                 AND status IN ('pending','leased')""",
            (workspace_id, source, account_key, stream_key),
        ).fetchall()
        for row in rows:
            child_payload = json.loads(row["payload"] or "{}")
            if child_payload.get("parent_wave") == wave_root:
                return
        self.os.store.acknowledge_descendant_reconciliation(
            workspace_id, source, account_key, str(wave_root), fence=fence
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
        if source == "figma":
            return FigmaConnector(
                secret, file_workspace_mappings=integration["workspace_mappings"],
                expected_account_id=integration["expected_account_id"],
                granted_permissions=integration.get("permissions", ()),
            ).verify_credentials()
        if source == "gmail":
            return GmailConnector(
                secret,
                label_workspace_mappings=integration["workspace_mappings"],
                expected_account_id=integration["expected_account_id"],
                granted_scopes=integration.get("_runtime_granted_permissions", ()),
            ).verify_credentials()
        if source == "fireflies":
            return FirefliesConnector(
                secret, workspace_mappings=integration["workspace_mappings"],
                expected_account_id=integration["expected_account_id"],
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
    def _uses_provider_lifecycle(source: str) -> bool:
        return source in {"google_drive", "gmail", "figma", "fireflies"}

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

