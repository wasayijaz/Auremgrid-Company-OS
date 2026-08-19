from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from auremgrid.domain.errors import ValidationError
from auremgrid.domain.models import (
    Actor,
    AuditEvent,
    Citation,
    Document,
    Fact,
    Memory,
    Relation,
    SourceArtifact,
    Workspace,
)
from auremgrid.domain.ops import (
    ClientBrainPack,
    Playbook,
    StatusPost,
    Touchpoint,
    WorkEvent,
    WorkItem,
)
from auremgrid.storage.migrations import migrate, schema_version


@dataclass(frozen=True)
class ProviderSyncFence:
    stream_lock_id: str
    reservation_token: str
    credential_binding_id: str
    credential_generation: int

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actors (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    locator TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    media_type TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    allowed_actor_ids TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    version INTEGER NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_workspace_key_hash
    ON sources(workspace_id, source_key, content_hash);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    document_id UNINDEXED,
    workspace_id UNINDEXED,
    content
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    confidence REAL NOT NULL,
    superseded_by TEXT,
    conflict_group TEXT,
    evidence_span TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY(source_id) REFERENCES sources(id),
    FOREIGN KEY(document_id) REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    from_entity TEXT NOT NULL,
    relation TEXT NOT NULL,
    to_entity TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_span TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY(source_id) REFERENCES sources(id),
    FOREIGN KEY(document_id) REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY(actor_id) REFERENCES actors(id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    title TEXT NOT NULL,
    request TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    needed_by TEXT,
    status TEXT NOT NULL,
    assignee_id TEXT,
    playbook_id TEXT,
    decision_maker TEXT,
    definition_of_done TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS work_events (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    detail TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY(work_item_id) REFERENCES work_items(id)
);

CREATE TABLE IF NOT EXISTS touchpoints (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS playbooks (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_playbooks_scope_slug
    ON playbooks(workspace_id, slug);

CREATE TABLE IF NOT EXISTS client_brains (
    workspace_id TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL,
    brand_rules TEXT NOT NULL,
    landing_pages TEXT NOT NULL,
    ads TEXT NOT NULL,
    design TEXT NOT NULL,
    email TEXT NOT NULL,
    dos TEXT NOT NULL,
    donts TEXT NOT NULL,
    open_loops TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS status_posts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    body TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
);
"""


def parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class SqliteStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            self.conn.execute("PRAGMA wal_autocheckpoint = 1000")
        self.conn.executescript(SCHEMA)
        migrate(self.conn)
        self._lock = threading.RLock()
        self._transaction_depth = 0

    @property
    def schema_version(self) -> int:
        return schema_version(self.conn)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self.conn

    @contextmanager
    def atomic(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Own one SQLite transaction while repository helpers suppress inner commits."""
        with self._lock:
            outermost = self._transaction_depth == 0
            if outermost:
                self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            self._transaction_depth += 1
            try:
                yield self.conn
                self._transaction_depth -= 1
                if outermost:
                    self.conn.commit()
            except Exception:
                self._transaction_depth -= 1
                if outermost:
                    self.conn.rollback()
                raise

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def create_workspace(self, workspace: Workspace) -> Workspace:
        self.conn.execute(
            "INSERT INTO workspaces(id, name, created_at) VALUES (?, ?, ?)",
            (workspace.id, workspace.name, workspace.created_at.isoformat()),
        )
        self._commit()
        return workspace

    def get_workspace(self, workspace_id: str) -> Workspace | None:
        row = self.conn.execute(
            "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        if row is None:
            return None
        return Workspace(id=row["id"], name=row["name"], created_at=parse_dt(row["created_at"]))

    def create_actor(self, actor: Actor) -> Actor:
        self.conn.execute(
            "INSERT INTO actors(id, workspace_id, name, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (actor.id, actor.workspace_id, actor.name, actor.role, actor.created_at.isoformat()),
        )
        self._commit()
        return actor

    def get_actor(self, workspace_id: str, actor_id: str) -> Actor | None:
        row = self.conn.execute(
            "SELECT * FROM actors WHERE workspace_id = ? AND id = ?",
            (workspace_id, actor_id),
        ).fetchone()
        if row is None:
            return None
        return Actor(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            role=row["role"],
            created_at=parse_dt(row["created_at"]),
        )

    def get_actor_any(self, actor_id: str) -> Actor | None:
        row = self.conn.execute(
            "SELECT * FROM actors WHERE id = ?",
            (actor_id,),
        ).fetchone()
        if row is None:
            return None
        return Actor(
            id=row["id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            role=row["role"],
            created_at=parse_dt(row["created_at"]),
        )

    def find_source(self, workspace_id: str, source_key: str, content_hash: str) -> SourceArtifact | None:
        row = self.conn.execute(
            """
            SELECT * FROM sources
            WHERE workspace_id = ? AND source_key = ? AND content_hash = ?
            """,
            (workspace_id, source_key, content_hash),
        ).fetchone()
        return self._source_from_row(row) if row else None

    def latest_source(self, workspace_id: str, source_key: str) -> SourceArtifact | None:
        row = self.conn.execute(
            """
            SELECT * FROM sources
            WHERE workspace_id = ? AND source_key = ?
            ORDER BY version DESC
            LIMIT 1
            """,
            (workspace_id, source_key),
        ).fetchone()
        return self._source_from_row(row) if row else None

    def create_source(
        self,
        source: SourceArtifact,
        replaces_source_id: str | None = None,
        lifecycle_at: datetime | None = None,
        activate: bool = True,
    ) -> SourceArtifact:
        effective_time = (lifecycle_at or source.observed_at).isoformat()
        with self.atomic(immediate=True):
            self.conn.execute(
                """
                INSERT INTO sources(
                    id, workspace_id, source_key, locator, content_hash, media_type,
                    trust_level, allowed_actor_ids, observed_at, recorded_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.id,
                    source.workspace_id,
                    source.source_key,
                    source.locator,
                    source.content_hash,
                    source.media_type,
                    source.trust_level,
                    json.dumps(list(source.allowed_actor_ids)),
                    source.observed_at.isoformat(),
                    source.recorded_at.isoformat(),
                    source.version,
                ),
            )
            if activate:
                self._activate_source_tx(
                    source.workspace_id,
                    source.id,
                    source.recorded_at.isoformat(),
                    "source_created",
                    effective_time,
                )
        return source

    def get_source(self, workspace_id: str, source_id: str) -> SourceArtifact | None:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE workspace_id = ? AND id = ?",
            (workspace_id, source_id),
        ).fetchone()
        return self._source_from_row(row) if row else None

    def activate_source(
        self,
        workspace_id: str,
        source_id: str,
        activated_at: datetime | None = None,
        reason: str = "explicit_activation",
        effective_from: datetime | None = None,
    ) -> bool:
        """Open a source visibility interval, returning False for an idempotent no-op."""

        when = (activated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        with self.atomic(immediate=True):
            source = self._require_scoped_source(workspace_id, source_id)
            return self._activate_source_tx(
                workspace_id,
                source_id,
                when.isoformat(),
                reason,
                (effective_from.isoformat() if effective_from else source["observed_at"]),
            )

    def retire_source(
        self,
        workspace_id: str,
        source_id: str,
        retired_at: datetime | None = None,
        reason: str = "explicit_retirement",
    ) -> bool:
        """Close current visibility without deleting historical evidence."""

        when = (retired_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        with self.atomic(immediate=True):
            self._require_scoped_source(workspace_id, source_id)
            return self._retire_source_tx(workspace_id, source_id, when.isoformat(), reason)

    def source_is_active(
        self,
        workspace_id: str,
        source_id: str,
        as_of: datetime | None = None,
    ) -> bool:
        params: tuple[object, ...]
        if as_of is None:
            sql = """
                SELECT 1 FROM source_lifecycle_intervals
                WHERE workspace_id=? AND source_id=? AND retired_at IS NULL
                LIMIT 1
            """
            params = (workspace_id, source_id)
        else:
            moment = as_of.astimezone(timezone.utc).replace(microsecond=0).isoformat()
            sql = """
                SELECT 1 FROM source_lifecycle_intervals lifecycle
                JOIN sources source ON source.id=lifecycle.source_id
                WHERE lifecycle.workspace_id=? AND lifecycle.source_id=?
                  AND lifecycle.effective_from <= ?
                  AND (lifecycle.effective_until IS NULL OR lifecycle.effective_until > ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM source_lifecycle_intervals candidate
                      JOIN sources candidate_source ON candidate_source.id=candidate.source_id
                      WHERE candidate.workspace_id=lifecycle.workspace_id
                        AND candidate.source_key=lifecycle.source_key
                        AND candidate.effective_from <= ?
                        AND (candidate.effective_until IS NULL OR candidate.effective_until > ?)
                        AND (
                            candidate.effective_from > lifecycle.effective_from
                            OR (candidate.effective_from=lifecycle.effective_from
                                AND candidate_source.version > source.version)
                        )
                  )
                LIMIT 1
            """
            params = (workspace_id, source_id, moment, moment, moment, moment)
        return self.conn.execute(sql, params).fetchone() is not None

    def activate_provider_route(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        external_id: str,
        route_key: str,
        source_key: str,
        source_id: str,
        provider_version: str,
        occurred_at: datetime | None = None,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object]:
        """Atomically activate a provider membership and its current evidence."""

        self._validate_route_identity(connector, account_key, external_id, route_key, source_key, provider_version)
        when = (occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        with self.atomic(immediate=True):
            self._assert_provider_fence(workspace_id, connector, account_key, fence, when)
            source = self._require_scoped_source(workspace_id, source_id)
            if source["source_key"] != source_key:
                raise ValueError("provider route source_key does not match source")
            duplicate = self._provider_route_event(
                workspace_id, connector, account_key, external_id, route_key, provider_version, "activate"
            )
            if duplicate is not None:
                return {**self._get_provider_route(workspace_id, connector, account_key, external_id, route_key), "idempotent": True}
            previous = self.conn.execute(
                """SELECT * FROM provider_object_routes
                   WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=? AND route_key=?""",
                (workspace_id, connector, account_key, external_id, route_key),
            ).fetchone()
            if previous is not None and previous["source_key"] != source_key:
                raise ValidationError("provider route source_key is immutable")
            previous_source_id = previous["active_source_id"] if previous and previous["status"] == "active" else None
            self._activate_source_tx(
                workspace_id, source_id, when, "provider_route_activation", source["observed_at"]
            )
            related_previous = {
                row["active_source_id"]
                for row in self.conn.execute(
                    """SELECT active_source_id FROM provider_object_routes
                       WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=?
                         AND source_key=? AND status='active' AND active_source_id IS NOT NULL""",
                    (workspace_id, connector, account_key, external_id, source_key),
                ).fetchall()
            }
            # All active memberships for one immutable provider object represent
            # the same current evidence version. Repoint them together so retiring
            # one label/root never resurrects an older version.
            self.conn.execute(
                """UPDATE provider_object_routes SET active_source_id=?, updated_at=?
                   WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=?
                     AND source_key=? AND status='active'""",
                (source_id, when, workspace_id, connector, account_key, external_id, source_key),
            )
            self.conn.execute(
                """INSERT INTO provider_object_routes(
                       workspace_id,connector,account_key,external_id,route_key,source_key,
                       active_source_id,provider_version,status,activated_at,retired_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?, 'active', ?,NULL,?)
                   ON CONFLICT(workspace_id,connector,account_key,external_id,route_key) DO UPDATE SET
                       active_source_id=excluded.active_source_id,
                       provider_version=excluded.provider_version, status='active',
                       activated_at=excluded.activated_at, retired_at=NULL, updated_at=excluded.updated_at""",
                (workspace_id, connector, account_key, external_id, route_key, source_key,
                 source_id, provider_version, when, when),
            )
            self._insert_provider_route_event(
                workspace_id, connector, account_key, external_id, route_key, source_key,
                source_id, provider_version, "activate", when,
            )
            if previous_source_id:
                related_previous.add(previous_source_id)
            for old_source_id in related_previous.difference({source_id}):
                self._retire_source_if_unrouted_tx(workspace_id, old_source_id, when, "provider_version_replaced")
            return {**self._get_provider_route(workspace_id, connector, account_key, external_id, route_key), "idempotent": False}

    def retire_provider_route(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        external_id: str,
        route_key: str,
        source_key: str,
        provider_version: str,
        occurred_at: datetime | None = None,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object]:
        """Atomically retire a membership and hide evidence once no route references it."""

        self._validate_route_identity(connector, account_key, external_id, route_key, source_key, provider_version)
        when = (occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        with self.atomic(immediate=True):
            self._assert_provider_fence(workspace_id, connector, account_key, fence, when)
            if connector == "google_drive":
                ancestry = self.conn.execute(
                    """SELECT reconciliation_status FROM provider_object_ancestry
                       WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=?""",
                    (workspace_id, connector, account_key, external_id),
                ).fetchone()
                if ancestry is not None and ancestry["reconciliation_status"] == "required":
                    raise ValidationError("Google Drive ancestry requires reconciliation before retirement")
            duplicate = self._provider_route_event(
                workspace_id, connector, account_key, external_id, route_key, provider_version, "retire"
            )
            if duplicate is not None:
                return {**self._get_provider_route(workspace_id, connector, account_key, external_id, route_key), "idempotent": True}
            previous = self.conn.execute(
                """SELECT * FROM provider_object_routes
                   WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=? AND route_key=?""",
                (workspace_id, connector, account_key, external_id, route_key),
            ).fetchone()
            if previous is not None and previous["source_key"] != source_key:
                raise ValidationError("provider route source_key is immutable")
            previous_source_id = previous["active_source_id"] if previous and previous["status"] == "active" else None
            self.conn.execute(
                """INSERT INTO provider_object_routes(
                       workspace_id,connector,account_key,external_id,route_key,source_key,
                       active_source_id,provider_version,status,activated_at,retired_at,updated_at
                   ) VALUES (?,?,?,?,?,?,NULL,?,'retired',NULL,?,?)
                   ON CONFLICT(workspace_id,connector,account_key,external_id,route_key) DO UPDATE SET
                       active_source_id=NULL,
                       provider_version=excluded.provider_version, status='retired',
                       retired_at=excluded.retired_at, updated_at=excluded.updated_at""",
                (workspace_id, connector, account_key, external_id, route_key, source_key,
                 provider_version, when, when),
            )
            self._insert_provider_route_event(
                workspace_id, connector, account_key, external_id, route_key, source_key,
                previous_source_id, provider_version, "retire", when,
            )
            if previous_source_id:
                self._retire_source_if_unrouted_tx(workspace_id, previous_source_id, when, "provider_route_retired")
            return {**self._get_provider_route(workspace_id, connector, account_key, external_id, route_key), "idempotent": False}

    def provider_route_state(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
    ) -> dict[str, tuple[str, ...]]:
        rows = self.conn.execute(
            """SELECT external_id, route_key FROM provider_object_routes
               WHERE workspace_id=? AND connector=? AND account_key=? AND status='active'
               ORDER BY external_id, route_key""",
            (workspace_id, connector, account_key),
        ).fetchall()
        state: dict[str, list[str]] = {}
        for row in rows:
            state.setdefault(row["external_id"], []).append(row["route_key"])
        return {key: tuple(value) for key, value in state.items()}

    def resolve_provider_object_routes(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        external_id: str,
        provider_version: str,
        *,
        direct_route_keys: Iterable[str] = (),
        parent_ids: Iterable[str] = (),
        is_container: bool = False,
        occurred_at: datetime | None = None,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object]:
        """Resolve durable root membership without guessing through missing ancestry.

        Unknown parents preserve the object's last resolved roots and set
        ``may_retire`` false. A container move additionally requests descendant
        reconciliation because every child inherits its roots.
        """

        values = (connector, account_key, external_id, provider_version)
        if any(not str(value).strip() for value in values):
            raise ValidationError("provider ancestry identity fields are required")
        direct = {str(route) for route in direct_route_keys if str(route)}
        parents = tuple(sorted({str(parent) for parent in parent_ids if str(parent)}))
        when = (occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        with self.atomic(immediate=True):
            self._assert_provider_fence(workspace_id, connector, account_key, fence, when)
            previous = self.conn.execute(
                """SELECT * FROM provider_object_ancestry
                   WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=?""",
                (workspace_id, connector, account_key, external_id),
            ).fetchone()
            previous_roots = set(json.loads(previous["root_route_keys"])) if previous else set()
            previous_parents = tuple(json.loads(previous["parent_ids"])) if previous else ()
            roots = set(direct)
            unknown_parents: list[str] = []
            for parent_id in parents:
                parent = self.conn.execute(
                    """SELECT root_route_keys,reconciliation_status FROM provider_object_ancestry
                       WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=?""",
                    (workspace_id, connector, account_key, parent_id),
                ).fetchone()
                if parent is None or parent["reconciliation_status"] == "required":
                    unknown_parents.append(parent_id)
                    continue
                roots.update(json.loads(parent["root_route_keys"]))
            moved_container = bool(previous and is_container and tuple(parents) != tuple(previous_parents))
            if unknown_parents:
                roots.update(previous_roots)
                status = "required"
            elif moved_container:
                status = "descendants_required"
            else:
                status = "resolved"
            self.conn.execute(
                """INSERT INTO provider_object_ancestry(
                       workspace_id,connector,account_key,external_id,parent_ids,root_route_keys,
                       is_container,provider_version,reconciliation_status,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(workspace_id,connector,account_key,external_id) DO UPDATE SET
                       parent_ids=excluded.parent_ids, root_route_keys=excluded.root_route_keys,
                       is_container=excluded.is_container, provider_version=excluded.provider_version,
                       reconciliation_status=excluded.reconciliation_status, updated_at=excluded.updated_at""",
                (workspace_id, connector, account_key, external_id, json.dumps(parents),
                 json.dumps(sorted(roots)), int(is_container), provider_version, status, when),
            )
            return {
                "external_id": external_id,
                "route_keys": tuple(sorted(roots)),
                "unknown_parent_ids": tuple(unknown_parents),
                "reconciliation_required": status == "required",
                "descendant_reconciliation_required": status == "descendants_required",
                "may_retire": status == "resolved",
            }

    def acknowledge_descendant_reconciliation(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        external_id: str,
        fence: ProviderSyncFence | None = None,
    ) -> bool:
        with self.atomic(immediate=True):
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            self._assert_provider_fence(workspace_id, connector, account_key, fence, now)
            cursor = self.conn.execute(
                """UPDATE provider_object_ancestry
                   SET reconciliation_status='resolved', updated_at=?
                   WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=?
                     AND reconciliation_status='descendants_required'""",
                (now, workspace_id, connector, account_key, external_id),
            )
            return cursor.rowcount == 1

    def objects_requiring_reconciliation(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
    ) -> list[dict[str, object]]:
        return [
            dict(row)
            for row in self.conn.execute(
                """SELECT * FROM provider_object_ancestry
                   WHERE workspace_id=? AND connector=? AND account_key=?
                     AND reconciliation_status != 'resolved'
                   ORDER BY updated_at, external_id""",
                (workspace_id, connector, account_key),
            ).fetchall()
        ]

    def stage_provider_route_mutation(
        self,
        batch_id: str,
        event_id: str,
        workspace_id: str,
        connector: str,
        account_key: str,
        external_id: str,
        route_key: str,
        source_key: str,
        source_id: str | None,
        provider_version: str,
        operation: str,
        occurred_at: datetime,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object]:
        if operation not in {"activate", "retire"}:
            raise ValidationError("provider route operation is invalid")
        self._validate_route_identity(connector, account_key, external_id, route_key, source_key, provider_version)
        with self.atomic(immediate=True):
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            self._assert_provider_fence(workspace_id, connector, account_key, fence, now)
            event = self.conn.execute(
                """SELECT event.id,event.external_id,event.source_key,event.dedupe_key FROM connector_source_events event
                   JOIN connector_batch_events link ON link.event_id=event.id
                   JOIN connector_ingest_batches batch ON batch.id=link.batch_id
                   WHERE event.id=? AND link.batch_id=? AND event.workspace_id=?
                     AND event.connector=? AND event.account_key=?""",
                (event_id, batch_id, workspace_id, connector, account_key),
            ).fetchone()
            if event is None:
                raise ValidationError("provider mutation is not linked to the scoped connector event")
            if event["external_id"] != external_id or event["source_key"] != source_key:
                raise ValidationError("provider mutation identity does not match connector event")
            if source_id is not None:
                self._require_scoped_source(workspace_id, source_id)
            mutation_id = _stable_id("pmut", event_id, route_key, provider_version, operation)
            self.conn.execute(
                """INSERT OR IGNORE INTO provider_route_mutation_staging(
                       id,batch_id,event_id,workspace_id,connector,account_key,external_id,
                       route_key,source_key,source_id,provider_version,operation,occurred_at,
                       status,created_at,applied_at,event_dedupe_key
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'staged',?,NULL,?)""",
                (mutation_id, batch_id, event_id, workspace_id, connector, account_key,
                 external_id, route_key, source_key, source_id, provider_version, operation,
                 occurred_at.astimezone(timezone.utc).isoformat(),
                 now, event["dedupe_key"]),
            )
            return dict(self.conn.execute(
                "SELECT * FROM provider_route_mutation_staging WHERE id=?", (mutation_id,)
            ).fetchone())

    def bind_provider_event_source(
        self,
        batch_id: str,
        event_dedupe_key: str,
        workspace_id: str,
        source_id: str,
        fence: ProviderSyncFence | None = None,
    ) -> int:
        """Bind staged activations to ingested evidence exactly once."""

        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.atomic(immediate=True):
            event = self.conn.execute(
                """SELECT event.*,batch.connector,batch.account_key FROM connector_source_events event
                   JOIN connector_batch_events link ON link.event_id=event.id
                   JOIN connector_ingest_batches batch ON batch.id=link.batch_id
                   WHERE link.batch_id=? AND event.dedupe_key=? AND event.workspace_id=?""",
                (batch_id, event_dedupe_key, workspace_id),
            ).fetchone()
            if event is None:
                raise ValidationError("exact provider event was not found in batch")
            self._assert_provider_fence(
                workspace_id, event["connector"], event["account_key"], fence, now
            )
            source = self._require_scoped_source(workspace_id, source_id)
            if source["source_key"] != event["source_key"]:
                raise ValidationError("provider event source binding does not match evidence")
            rows = self.conn.execute(
                """SELECT id,source_id FROM provider_route_mutation_staging
                   WHERE batch_id=? AND event_dedupe_key=? AND operation='activate'""",
                (batch_id, event_dedupe_key),
            ).fetchall()
            if not rows:
                return 0
            if any(row["source_id"] not in {None, source_id} for row in rows):
                raise ValidationError("provider event source is already bound differently")
            cursor = self.conn.execute(
                """UPDATE provider_route_mutation_staging SET source_id=?
                   WHERE batch_id=? AND event_dedupe_key=? AND operation='activate'
                     AND source_id IS NULL""",
                (source_id, batch_id, event_dedupe_key),
            )
            return cursor.rowcount

    def apply_provider_event_mutations(
        self,
        batch_id: str,
        event_dedupe_key: str,
        fence: ProviderSyncFence | None = None,
    ) -> list[dict[str, object]]:
        """Apply only one exact event's staged route transitions."""

        applied: list[dict[str, object]] = []
        with self.atomic(immediate=True):
            event = self.conn.execute(
                """SELECT event.*,batch.connector,batch.account_key FROM connector_source_events event
                   JOIN connector_batch_events link ON link.event_id=event.id
                   JOIN connector_ingest_batches batch ON batch.id=link.batch_id
                   WHERE link.batch_id=? AND event.dedupe_key=?""",
                (batch_id, event_dedupe_key),
            ).fetchone()
            if event is None or event["status"] not in {"ingested", "skipped"}:
                raise ValidationError("provider event is not safely ingested")
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            self._assert_provider_fence(
                event["workspace_id"], event["connector"], event["account_key"], fence, now
            )
            rows = self.conn.execute(
                """SELECT * FROM provider_route_mutation_staging
                   WHERE batch_id=? AND event_dedupe_key=? AND status='staged'
                   ORDER BY created_at,id""",
                (batch_id, event_dedupe_key),
            ).fetchall()
            for row in rows:
                applied.append(self._apply_provider_mutation_row(row))
        return applied

    def apply_staged_provider_route_mutations(
        self, batch_id: str, fence: ProviderSyncFence | None = None
    ) -> list[dict[str, object]]:
        """Apply an ingested batch's staged memberships in one local transaction."""

        applied: list[dict[str, object]] = []
        with self.atomic(immediate=True):
            batch = self.conn.execute(
                "SELECT workspace_id,connector,account_key FROM connector_ingest_batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise ValidationError("connector batch not found")
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            self._assert_provider_fence(
                batch["workspace_id"], batch["connector"], batch["account_key"], fence, now
            )
            rows = self.conn.execute(
                """SELECT mutation.* FROM provider_route_mutation_staging mutation
                   JOIN connector_source_events event ON event.id=mutation.event_id
                   WHERE mutation.batch_id=? AND mutation.status='staged'
                     AND event.status IN ('ingested','skipped')
                   ORDER BY mutation.created_at, mutation.id""",
                (batch_id,),
            ).fetchall()
            for row in rows:
                applied.append(self._apply_provider_mutation_row(row))
        return applied

    def _apply_provider_mutation_row(self, row: sqlite3.Row) -> dict[str, object]:
        occurred = parse_dt(row["occurred_at"])
        if row["operation"] == "activate":
            if row["source_id"] is None:
                raise ValidationError("activation mutation requires source_id")
            result = self.activate_provider_route(
                row["workspace_id"], row["connector"], row["account_key"],
                row["external_id"], row["route_key"], row["source_key"],
                row["source_id"], row["provider_version"], occurred,
            )
        else:
            result = self.retire_provider_route(
                row["workspace_id"], row["connector"], row["account_key"],
                row["external_id"], row["route_key"], row["source_key"],
                row["provider_version"], occurred,
            )
        self.conn.execute(
            """UPDATE provider_route_mutation_staging
               SET status='applied', applied_at=? WHERE id=? AND status='staged'""",
            (datetime.now(timezone.utc).replace(microsecond=0).isoformat(), row["id"]),
        )
        return result

    def enqueue_provider_sync_task(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        stream_key: str,
        task_type: str,
        *,
        generation_id: str | None = None,
        external_id: str | None = None,
        route_key: str | None = None,
        page_token: str | None = None,
        payload: dict[str, object] | None = None,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object]:
        if task_type not in {"backfill", "reconcile", "descendants"}:
            raise ValidationError("provider sync task type is invalid")
        identity = tuple(value or "" for value in (workspace_id, connector, account_key, stream_key, task_type,
                                                     external_id, route_key, page_token, generation_id))
        task_id = _stable_id("ptask", *identity)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.atomic(immediate=True):
            self._assert_provider_fence(workspace_id, connector, account_key, fence, now)
            if generation_id is not None:
                generation = self.conn.execute(
                    """SELECT 1 FROM provider_sync_generations
                       WHERE id=? AND workspace_id=? AND connector=? AND account_key=? AND status='running'""",
                    (generation_id, workspace_id, connector, account_key),
                ).fetchone()
                if generation is None:
                    raise ValidationError("provider sync task generation is not running")
            self.conn.execute(
                """INSERT OR IGNORE INTO provider_sync_tasks(
                       id,workspace_id,connector,account_key,stream_key,generation_id,task_type,
                       external_id,route_key,page_token,payload,status,lease_owner,lease_token,
                       lease_expires_at,created_at,updated_at,completed_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,NULL,?,?,NULL)""",
                (task_id, workspace_id, connector, account_key, stream_key, generation_id,
                 task_type, external_id, route_key, page_token,
                 json.dumps(payload or {}, sort_keys=True), now, now),
            )
            return dict(self.conn.execute("SELECT * FROM provider_sync_tasks WHERE id=?", (task_id,)).fetchone())

    def claim_provider_sync_task(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        stream_key: str,
        lease_owner: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object] | None:
        if lease_seconds <= 0 or not lease_owner:
            raise ValidationError("provider task lease is invalid")
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        now_text = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self.atomic(immediate=True):
            self._assert_provider_fence(workspace_id, connector, account_key, fence, now_text)
            row = self.conn.execute(
                """SELECT * FROM provider_sync_tasks
                   WHERE workspace_id=? AND connector=? AND account_key=? AND stream_key=?
                     AND (status='pending' OR (status='leased' AND lease_expires_at <= ?))
                     AND (generation_id IS NULL OR EXISTS (
                         SELECT 1 FROM provider_sync_generations generation
                         WHERE generation.id=provider_sync_tasks.generation_id AND generation.status='running'
                     ))
                   ORDER BY created_at,id LIMIT 1""",
                (workspace_id, connector, account_key, stream_key, now_text),
            ).fetchone()
            if row is None:
                return None
            token = _stable_id("please", row["id"], lease_owner, now_text, str(row["updated_at"]))
            cursor = self.conn.execute(
                """UPDATE provider_sync_tasks SET status='leased',lease_owner=?,lease_token=?,
                       lease_expires_at=?,updated_at=?
                   WHERE id=? AND (status='pending' OR (status='leased' AND lease_expires_at <= ?))""",
                (lease_owner, token, expires, now_text, row["id"], now_text),
            )
            if cursor.rowcount != 1:
                return None
            return dict(self.conn.execute("SELECT * FROM provider_sync_tasks WHERE id=?", (row["id"],)).fetchone())

    def heartbeat_provider_sync_task(
        self,
        task_id: str,
        lease_token: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object]:
        if lease_seconds <= 0:
            raise ValidationError("provider task lease is invalid")
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        now_text = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        with self.atomic(immediate=True):
            task = self.conn.execute("SELECT * FROM provider_sync_tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise ValidationError("provider sync task not found")
            self._assert_provider_fence(
                task["workspace_id"], task["connector"], task["account_key"], fence, now_text
            )
            if task["generation_id"] is not None:
                generation = self.conn.execute(
                    "SELECT status FROM provider_sync_generations WHERE id=?",
                    (task["generation_id"],),
                ).fetchone()
                if generation is None or generation["status"] != "running":
                    raise ValidationError("provider sync task generation is not running")
            cursor = self.conn.execute(
                """UPDATE provider_sync_tasks SET lease_expires_at=?,updated_at=?
                   WHERE id=? AND status='leased' AND lease_token=? AND lease_expires_at>?""",
                (expires, now_text, task_id, lease_token, now_text),
            )
            if cursor.rowcount != 1:
                raise ValidationError("provider sync task lease is stale")
            return dict(self.conn.execute("SELECT * FROM provider_sync_tasks WHERE id=?", (task_id,)).fetchone())

    def complete_provider_sync_task(
        self, task_id: str, lease_token: str, fence: ProviderSyncFence | None = None
    ) -> bool:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.atomic(immediate=True):
            task = self.conn.execute("SELECT * FROM provider_sync_tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise ValidationError("provider sync task not found")
            self._assert_provider_fence(
                task["workspace_id"], task["connector"], task["account_key"], fence, now
            )
            if task["generation_id"] is not None:
                generation = self.conn.execute(
                    "SELECT status FROM provider_sync_generations WHERE id=?",
                    (task["generation_id"],),
                ).fetchone()
                if generation is None or generation["status"] != "running":
                    raise ValidationError("provider sync task generation is not running")
            cursor = self.conn.execute(
                """UPDATE provider_sync_tasks SET status='completed', completed_at=?, updated_at=?,
                       lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL
                   WHERE id=? AND status='leased' AND lease_token=? AND lease_expires_at > ?""",
                (now, now, task_id, lease_token, now),
            )
            return cursor.rowcount == 1

    def start_provider_sync_generation(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        stream_key: str,
        route_key: str,
        baseline_cursor: str | None,
        started_at: datetime | None = None,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object]:
        when = (started_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        with self.atomic(immediate=True):
            self._assert_provider_fence(workspace_id, connector, account_key, fence, when)
            running = self.conn.execute(
                """SELECT * FROM provider_sync_generations
                   WHERE workspace_id=? AND connector=? AND account_key=? AND stream_key=?
                     AND route_key=? AND status='running'""",
                (workspace_id, connector, account_key, stream_key, route_key),
            ).fetchone()
            if running is not None:
                return {**dict(running), "idempotent": True}
            count = self.conn.execute(
                """SELECT COUNT(*) FROM provider_sync_generations
                   WHERE workspace_id=? AND connector=? AND account_key=? AND stream_key=? AND route_key=?""",
                (workspace_id, connector, account_key, stream_key, route_key),
            ).fetchone()[0]
            generation_id = _stable_id(
                "pgen", workspace_id, connector, account_key, stream_key, route_key, when, str(count)
            )
            self.conn.execute(
                """INSERT INTO provider_sync_generations(
                       id,workspace_id,connector,account_key,stream_key,route_key,status,
                       baseline_cursor,started_at,completed_at
                   ) VALUES (?,?,?,?,?,?,'running',?,?,NULL)""",
                (generation_id, workspace_id, connector, account_key, stream_key,
                 route_key, baseline_cursor, when),
            )
            return {**dict(self.conn.execute(
                "SELECT * FROM provider_sync_generations WHERE id=?", (generation_id,)
            ).fetchone()), "idempotent": False}

    def mark_provider_object_seen(
        self,
        generation_id: str,
        external_id: str,
        route_key: str,
        seen_at: datetime | None = None,
        fence: ProviderSyncFence | None = None,
    ) -> bool:
        when = (seen_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
        with self.atomic(immediate=True):
            generation = self.conn.execute(
                "SELECT * FROM provider_sync_generations WHERE id=? AND status='running'",
                (generation_id,),
            ).fetchone()
            if generation is None or generation["route_key"] != route_key:
                raise ValidationError("provider sync generation does not own route")
            self._assert_provider_fence(
                generation["workspace_id"], generation["connector"], generation["account_key"], fence, when
            )
            cursor = self.conn.execute(
                """INSERT OR IGNORE INTO provider_object_generation_seen(
                       generation_id,workspace_id,connector,account_key,external_id,route_key,seen_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (generation_id, generation["workspace_id"], generation["connector"],
                 generation["account_key"], external_id, route_key, when),
            )
            return cursor.rowcount == 1

    def complete_provider_sync_generation(
        self,
        generation_id: str,
        completed_at: datetime | None = None,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object]:
        """Retire only unseen objects after every generation task is durable and complete."""

        when_dt = (completed_at or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
        when = when_dt.isoformat()
        with self.atomic(immediate=True):
            generation = self.conn.execute(
                "SELECT * FROM provider_sync_generations WHERE id=?",
                (generation_id,),
            ).fetchone()
            if generation is None:
                raise ValidationError("provider sync generation not found")
            if generation["status"] == "completed":
                return {"generation_id": generation_id, "retired": 0, "idempotent": True}
            if generation["status"] != "running":
                raise ValidationError("cancelled provider generation cannot retire unseen objects")
            self._assert_provider_fence(
                generation["workspace_id"], generation["connector"], generation["account_key"], fence, when
            )
            pending = self.conn.execute(
                """SELECT COUNT(*) FROM provider_sync_tasks
                   WHERE generation_id=? AND status NOT IN ('completed','cancelled')""",
                (generation_id,),
            ).fetchone()[0]
            if pending:
                raise ValidationError("provider sync generation still has pending tasks")
            unseen = self.conn.execute(
                """SELECT route.* FROM provider_object_routes route
                   WHERE route.workspace_id=? AND route.connector=? AND route.account_key=?
                     AND route.route_key=? AND route.status='active'
                     AND NOT EXISTS (
                         SELECT 1 FROM provider_object_generation_seen seen
                         WHERE seen.generation_id=? AND seen.external_id=route.external_id
                           AND seen.route_key=route.route_key
                     )""",
                (generation["workspace_id"], generation["connector"], generation["account_key"],
                 generation["route_key"], generation_id),
            ).fetchall()
            for route in unseen:
                self.retire_provider_route(
                    route["workspace_id"], route["connector"], route["account_key"],
                    route["external_id"], route["route_key"], route["source_key"],
                    f"generation:{generation_id}", when_dt,
                )
            self.conn.execute(
                """UPDATE provider_sync_generations SET status='completed', completed_at=?
                   WHERE id=? AND status='running'""",
                (when, generation_id),
            )
            return {"generation_id": generation_id, "retired": len(unseen), "idempotent": False}

    def get_running_generation(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        stream_key: str,
        route_key: str | None = None,
    ) -> dict[str, object] | None:
        sql = """SELECT * FROM provider_sync_generations
                 WHERE workspace_id=? AND connector=? AND account_key=? AND stream_key=?
                   AND status='running'"""
        params: tuple[object, ...] = (workspace_id, connector, account_key, stream_key)
        if route_key is not None:
            sql += " AND route_key=?"
            params += (route_key,)
        sql += " ORDER BY started_at,id LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def pending_provider_task_count(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        stream_key: str,
        generation_id: str | None = None,
    ) -> int:
        sql = """SELECT COUNT(*) FROM provider_sync_tasks
                 WHERE workspace_id=? AND connector=? AND account_key=? AND stream_key=?
                   AND status IN ('pending','leased')"""
        params: tuple[object, ...] = (workspace_id, connector, account_key, stream_key)
        if generation_id is not None:
            sql += " AND generation_id=?"
            params += (generation_id,)
        return int(self.conn.execute(sql, params).fetchone()[0])

    def cancel_provider_rebootstrap(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        stream_key: str,
        route_key: str,
        fence: ProviderSyncFence | None = None,
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.atomic(immediate=True):
            self._assert_provider_fence(workspace_id, connector, account_key, fence, now)
            generation = self.conn.execute(
                """SELECT * FROM provider_sync_generations
                   WHERE workspace_id=? AND connector=? AND account_key=? AND stream_key=?
                     AND route_key=? AND status='running'""",
                (workspace_id, connector, account_key, stream_key, route_key),
            ).fetchone()
            if generation is None:
                return {"generation_id": None, "cancelled_tasks": 0, "idempotent": True}
            tasks = self.conn.execute(
                """UPDATE provider_sync_tasks SET status='cancelled',updated_at=?,completed_at=?,
                       lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL
                   WHERE generation_id=? AND status IN ('pending','leased')""",
                (now, now, generation["id"]),
            ).rowcount
            self.conn.execute(
                """UPDATE provider_sync_generations
                   SET status='cancelled',cancelled_at=?
                   WHERE id=? AND status='running'""",
                (now, generation["id"]),
            )
            return {"generation_id": generation["id"], "cancelled_tasks": tasks, "idempotent": False}

    def _require_scoped_source(self, workspace_id: str, source_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE workspace_id=? AND id=?",
            (workspace_id, source_id),
        ).fetchone()
        if row is None:
            raise ValidationError("source does not belong to workspace")
        return row

    def _assert_provider_fence(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        fence: ProviderSyncFence | None,
        now: str,
    ) -> None:
        if fence is None:
            return
        lock = self.conn.execute(
            """SELECT 1 FROM connector_stream_locks
               WHERE id=? AND workspace_id=? AND connector=? AND account_key=?
                 AND status='active' AND reservation_token=? AND lease_expires_at>?""",
            (fence.stream_lock_id, workspace_id, connector, account_key,
             fence.reservation_token, now),
        ).fetchone()
        if lock is None:
            raise ValidationError("provider sync stream fence is stale")
        credential = self.conn.execute(
            """SELECT 1 FROM secret_bindings
               WHERE id=? AND status='active' AND revoked_at IS NULL AND generation=?""",
            (fence.credential_binding_id, int(fence.credential_generation)),
        ).fetchone()
        if credential is None:
            raise ValidationError("provider sync credential fence is stale")

    def _activate_source_tx(
        self,
        workspace_id: str,
        source_id: str,
        when: str,
        reason: str,
        effective_from: str,
    ) -> bool:
        source = self._require_scoped_source(workspace_id, source_id)
        current = self.conn.execute(
            """SELECT 1 FROM source_lifecycle_intervals
               WHERE workspace_id=? AND source_id=? AND retired_at IS NULL""",
            (workspace_id, source_id),
        ).fetchone()
        if current is not None:
            return False
        active = self.conn.execute(
            """SELECT * FROM source_lifecycle_intervals
               WHERE workspace_id=? AND source_key=? AND retired_at IS NULL""",
            (workspace_id, source["source_key"]),
        ).fetchone()
        if active is not None:
            self._retire_source_tx(
                workspace_id,
                active["source_id"],
                when,
                "source_version_replaced",
                close_semantic=False,
            )
        prior = self.conn.execute(
            """SELECT MAX(retired_at) AS retired_at, COUNT(*) AS count
               FROM source_lifecycle_intervals WHERE workspace_id=? AND source_id=?""",
            (workspace_id, source_id),
        ).fetchone()
        if prior["retired_at"] is not None and when < prior["retired_at"]:
            raise ValidationError("source activation precedes its prior retirement")
        interval_id = _stable_id("slife", workspace_id, source_id, when, str(prior["count"]))
        self.conn.execute(
            """INSERT INTO source_lifecycle_intervals(
                   id,workspace_id,source_id,source_key,activated_at,retired_at,
                   effective_from,effective_until,activation_reason,retirement_reason
               ) VALUES (?,?,?,?,?,NULL,?,NULL,?,NULL)""",
            (interval_id, workspace_id, source_id, source["source_key"], when, effective_from, reason),
        )
        return True

    def _retire_source_tx(
        self,
        workspace_id: str,
        source_id: str,
        when: str,
        reason: str,
        *,
        close_semantic: bool = True,
    ) -> bool:
        current = self.conn.execute(
            """SELECT * FROM source_lifecycle_intervals
               WHERE workspace_id=? AND source_id=? AND retired_at IS NULL""",
            (workspace_id, source_id),
        ).fetchone()
        if current is None:
            return False
        if when < current["activated_at"]:
            raise ValidationError("source retirement precedes activation")
        cursor = self.conn.execute(
            """UPDATE source_lifecycle_intervals
               SET retired_at=?, retirement_reason=?
               WHERE id=? AND retired_at IS NULL""",
            (when, reason, current["id"]),
        )
        if cursor.rowcount == 1 and close_semantic:
            self.conn.execute(
                """UPDATE source_lifecycle_intervals
                   SET effective_until=CASE
                       WHEN effective_from > ? THEN effective_from ELSE ? END
                   WHERE workspace_id=? AND source_key=?
                     AND effective_until IS NULL""",
                (when, when, workspace_id, current["source_key"]),
            )
        return cursor.rowcount == 1

    def _retire_source_if_unrouted_tx(
        self, workspace_id: str, source_id: str, when: str, reason: str
    ) -> bool:
        still_routed = self.conn.execute(
            """SELECT 1 FROM provider_object_routes
               WHERE workspace_id=? AND active_source_id=? AND status='active' LIMIT 1""",
            (workspace_id, source_id),
        ).fetchone()
        if still_routed is not None:
            return False
        return self._retire_source_tx(
            workspace_id,
            source_id,
            when,
            reason,
            close_semantic=reason != "provider_version_replaced",
        )

    @staticmethod
    def _validate_route_identity(
        connector: str,
        account_key: str,
        external_id: str,
        route_key: str,
        source_key: str,
        provider_version: str,
    ) -> None:
        values = (connector, account_key, external_id, route_key, source_key, provider_version)
        if any(not str(value).strip() for value in values):
            raise ValidationError("provider route identity fields are required")

    def _provider_route_event(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        external_id: str,
        route_key: str,
        provider_version: str,
        operation: str,
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM provider_object_route_events
               WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=?
                 AND route_key=? AND provider_version=? AND operation=?""",
            (workspace_id, connector, account_key, external_id, route_key, provider_version, operation),
        ).fetchone()

    def _insert_provider_route_event(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        external_id: str,
        route_key: str,
        source_key: str,
        source_id: str | None,
        provider_version: str,
        operation: str,
        when: str,
    ) -> None:
        event_id = _stable_id(
            "proute", workspace_id, connector, account_key, external_id,
            route_key, provider_version, operation,
        )
        self.conn.execute(
            """INSERT INTO provider_object_route_events(
                   id,workspace_id,connector,account_key,external_id,route_key,source_key,
                   source_id,provider_version,operation,occurred_at,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, workspace_id, connector, account_key, external_id, route_key,
             source_key, source_id, provider_version, operation, when,
             datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
        )

    def _get_provider_route(
        self,
        workspace_id: str,
        connector: str,
        account_key: str,
        external_id: str,
        route_key: str,
    ) -> dict[str, object]:
        row = self.conn.execute(
            """SELECT * FROM provider_object_routes
               WHERE workspace_id=? AND connector=? AND account_key=? AND external_id=? AND route_key=?""",
            (workspace_id, connector, account_key, external_id, route_key),
        ).fetchone()
        if row is None:
            raise ValidationError("provider route state is unavailable")
        return dict(row)

    def create_document(self, document: Document) -> Document:
        self.conn.execute(
            """
            INSERT INTO documents(
                id, workspace_id, source_id, content, content_hash, observed_at, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document.id,
                document.workspace_id,
                document.source_id,
                document.content,
                document.content_hash,
                document.observed_at.isoformat(),
                document.recorded_at.isoformat(),
            ),
        )
        self.conn.execute(
            "INSERT INTO documents_fts(document_id, workspace_id, content) VALUES (?, ?, ?)",
            (document.id, document.workspace_id, document.content),
        )
        self._commit()
        return document

    def get_document(self, workspace_id: str, document_id: str) -> Document | None:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE workspace_id = ? AND id = ?",
            (workspace_id, document_id),
        ).fetchone()
        return self._document_from_row(row) if row else None

    def create_fact(self, fact: Fact) -> Fact:
        self.conn.execute(
            """
            INSERT INTO facts(
                id, workspace_id, source_id, document_id, subject, predicate, object,
                valid_from, valid_until, observed_at, recorded_at, confidence,
                superseded_by, conflict_group, evidence_span
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.id,
                fact.workspace_id,
                fact.source_id,
                fact.document_id,
                fact.subject,
                fact.predicate,
                fact.object,
                fact.valid_from.isoformat(),
                fact.valid_until.isoformat() if fact.valid_until else None,
                fact.observed_at.isoformat(),
                fact.recorded_at.isoformat(),
                fact.confidence,
                fact.superseded_by,
                fact.conflict_group,
                fact.citation.evidence_span,
            ),
        )
        self._commit()
        return fact

    def mark_fact_superseded(self, workspace_id: str, fact_id: str, successor_id: str) -> None:
        self.conn.execute(
            "UPDATE facts SET superseded_by = ? WHERE workspace_id = ? AND id = ?",
            (successor_id, workspace_id, fact_id),
        )
        self._commit()

    def create_relation(self, relation: Relation) -> Relation:
        self.conn.execute(
            """
            INSERT INTO relations(
                id, workspace_id, source_id, document_id, from_entity, relation, to_entity,
                valid_from, valid_until, observed_at, recorded_at, confidence, evidence_span
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relation.id,
                relation.workspace_id,
                relation.source_id,
                relation.document_id,
                relation.from_entity,
                relation.relation,
                relation.to_entity,
                relation.valid_from.isoformat(),
                relation.valid_until.isoformat() if relation.valid_until else None,
                relation.observed_at.isoformat(),
                relation.recorded_at.isoformat(),
                relation.confidence,
                relation.citation.evidence_span,
            ),
        )
        self._commit()
        return relation

    def create_memory(self, memory: Memory) -> Memory:
        self.conn.execute(
            """
            INSERT INTO memories(
                id, workspace_id, actor_id, kind, content, observed_at, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.id,
                memory.workspace_id,
                memory.actor_id,
                memory.kind,
                memory.content,
                memory.observed_at.isoformat(),
                memory.recorded_at.isoformat(),
            ),
        )
        self._commit()
        return memory

    def create_audit(self, event: AuditEvent) -> AuditEvent:
        self.conn.execute(
            """
            INSERT INTO audit_events(
                id, workspace_id, actor_id, action, target, outcome, detail, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.workspace_id,
                event.actor_id,
                event.action,
                event.target,
                event.outcome,
                event.detail,
                event.recorded_at.isoformat(),
            ),
        )
        self._commit()
        return event

    def list_audit(self, workspace_id: str) -> list[AuditEvent]:
        rows = self.conn.execute(
            "SELECT * FROM audit_events WHERE workspace_id = ? ORDER BY recorded_at ASC",
            (workspace_id,),
        ).fetchall()
        return [
            AuditEvent(
                id=row["id"],
                workspace_id=row["workspace_id"],
                actor_id=row["actor_id"],
                action=row["action"],
                target=row["target"],
                outcome=row["outcome"],
                detail=row["detail"],
                recorded_at=parse_dt(row["recorded_at"]),
            )
            for row in rows
        ]

    def upsert_work_item(self, item: WorkItem) -> WorkItem:
        self.conn.execute(
            """
            INSERT INTO work_items(
                id, workspace_id, title, request, requested_by, needed_by, status,
                assignee_id, playbook_id, decision_maker, definition_of_done, created_at, updated_at,
                project_id,campaign_id,parent_id,owner_person_id,assignee_person_id,reviewer_person_id,
                priority,tags,estimate_hours,actual_effort_hours,start_date,deadline,blocking_reason,brief,brain_context,financial_value
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                request=excluded.request,
                requested_by=excluded.requested_by,
                needed_by=excluded.needed_by,
                status=excluded.status,
                assignee_id=excluded.assignee_id,
                playbook_id=excluded.playbook_id,
                decision_maker=excluded.decision_maker,
                definition_of_done=excluded.definition_of_done,
                project_id=excluded.project_id,campaign_id=excluded.campaign_id,parent_id=excluded.parent_id,
                owner_person_id=excluded.owner_person_id,assignee_person_id=excluded.assignee_person_id,
                reviewer_person_id=excluded.reviewer_person_id,priority=excluded.priority,tags=excluded.tags,
                estimate_hours=excluded.estimate_hours,actual_effort_hours=excluded.actual_effort_hours,
                start_date=excluded.start_date,deadline=excluded.deadline,blocking_reason=excluded.blocking_reason,
                brief=excluded.brief,brain_context=excluded.brain_context,financial_value=excluded.financial_value,
                updated_at=excluded.updated_at
            """,
            (
                item.id,
                item.workspace_id,
                item.title,
                item.request,
                item.requested_by,
                item.needed_by,
                item.status,
                item.assignee_id,
                item.playbook_id,
                item.decision_maker,
                json.dumps(item.definition_of_done),
                item.created_at.isoformat(),
                item.updated_at.isoformat(),
                item.project_id,item.campaign_id,item.parent_id,item.owner_person_id,item.assignee_person_id,
                item.reviewer_person_id,item.priority,json.dumps(item.tags),item.estimate_hours,item.actual_effort_hours,
                item.start_date,item.deadline,item.blocking_reason,item.brief,item.brain_context,item.financial_value,
            ),
        )
        self._commit()
        return item

    def get_work_item(self, workspace_id: str, work_item_id: str) -> WorkItem | None:
        row = self.conn.execute(
            "SELECT * FROM work_items WHERE workspace_id = ? AND id = ?",
            (workspace_id, work_item_id),
        ).fetchone()
        return self._work_item_from_row(row) if row else None

    def list_work_items(self, workspace_id: str, open_only: bool = False) -> list[WorkItem]:
        sql = "SELECT * FROM work_items WHERE workspace_id = ?"
        if open_only:
            sql += " AND status != 'shipped'"
        sql += " ORDER BY created_at ASC"
        rows = self.conn.execute(sql, (workspace_id,)).fetchall()
        return [self._work_item_from_row(row) for row in rows]

    def create_work_event(self, event: WorkEvent) -> WorkEvent:
        self.conn.execute(
            """
            INSERT INTO work_events(
                id, workspace_id, work_item_id, actor_id, action, from_status, to_status, detail, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.workspace_id,
                event.work_item_id,
                event.actor_id,
                event.action,
                event.from_status,
                event.to_status,
                event.detail,
                event.recorded_at.isoformat(),
            ),
        )
        self._commit()
        return event

    def create_touchpoint(self, touchpoint: Touchpoint) -> Touchpoint:
        self.conn.execute(
            """
            INSERT INTO touchpoints(
                id, workspace_id, actor_id, kind, summary, occurred_at, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                touchpoint.id,
                touchpoint.workspace_id,
                touchpoint.actor_id,
                touchpoint.kind,
                touchpoint.summary,
                touchpoint.occurred_at.isoformat(),
                touchpoint.recorded_at.isoformat(),
            ),
        )
        self._commit()
        return touchpoint

    def latest_touchpoint(self, workspace_id: str) -> Touchpoint | None:
        row = self.conn.execute(
            "SELECT * FROM touchpoints WHERE workspace_id = ? ORDER BY occurred_at DESC LIMIT 1",
            (workspace_id,),
        ).fetchone()
        return self._touchpoint_from_row(row) if row else None

    def upsert_playbook(self, playbook: Playbook) -> Playbook:
        self.conn.execute(
            """
            INSERT INTO playbooks(id, workspace_id, slug, title, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, slug) DO UPDATE SET
                title=excluded.title,
                body=excluded.body
            """,
            (
                playbook.id,
                playbook.workspace_id or "",
                playbook.slug,
                playbook.title,
                playbook.body,
                playbook.created_at.isoformat(),
            ),
        )
        self._commit()
        return playbook

    def list_playbooks(self, workspace_id: str | None = None) -> list[Playbook]:
        if workspace_id is None:
            rows = self.conn.execute(
                "SELECT * FROM playbooks WHERE workspace_id = '' ORDER BY slug"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM playbooks
                WHERE workspace_id = '' OR workspace_id = ?
                ORDER BY CASE WHEN workspace_id = '' THEN 0 ELSE 1 END, slug
                """,
                (workspace_id,),
            ).fetchall()
        return [self._playbook_from_row(row) for row in rows]

    def get_playbook(self, slug: str, workspace_id: str | None = None) -> Playbook | None:
        if workspace_id:
            row = self.conn.execute(
                "SELECT * FROM playbooks WHERE slug = ? AND workspace_id = ?",
                (slug, workspace_id),
            ).fetchone()
            if row:
                return self._playbook_from_row(row)
        row = self.conn.execute(
            "SELECT * FROM playbooks WHERE slug = ? AND workspace_id = ''",
            (slug,),
        ).fetchone()
        return self._playbook_from_row(row) if row else None

    def upsert_client_brain(self, brain: ClientBrainPack) -> ClientBrainPack:
        self.conn.execute(
            """
            INSERT INTO client_brains(
                workspace_id, snapshot, brand_rules, landing_pages, ads, design, email,
                dos, donts, open_loops, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                snapshot=excluded.snapshot,
                brand_rules=excluded.brand_rules,
                landing_pages=excluded.landing_pages,
                ads=excluded.ads,
                design=excluded.design,
                email=excluded.email,
                dos=excluded.dos,
                donts=excluded.donts,
                open_loops=excluded.open_loops,
                updated_at=excluded.updated_at
            """,
            (
                brain.workspace_id,
                brain.snapshot,
                brain.brand_rules,
                brain.landing_pages,
                brain.ads,
                brain.design,
                brain.email,
                json.dumps(list(brain.dos)),
                json.dumps(list(brain.donts)),
                json.dumps(list(brain.open_loops)),
                brain.updated_at.isoformat(),
            ),
        )
        self._commit()
        return brain

    def get_client_brain(self, workspace_id: str) -> ClientBrainPack | None:
        row = self.conn.execute(
            "SELECT * FROM client_brains WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            return None
        return ClientBrainPack(
            workspace_id=row["workspace_id"],
            snapshot=row["snapshot"],
            brand_rules=row["brand_rules"],
            landing_pages=row["landing_pages"],
            ads=row["ads"],
            design=row["design"],
            email=row["email"],
            dos=tuple(json.loads(row["dos"])),
            donts=tuple(json.loads(row["donts"])),
            open_loops=tuple(json.loads(row["open_loops"])),
            updated_at=parse_dt(row["updated_at"]),
        )

    def create_status_post(self, post: StatusPost) -> StatusPost:
        self.conn.execute(
            """
            INSERT INTO status_posts(id, workspace_id, actor_id, body, posted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post.id, post.workspace_id, post.actor_id, post.body, post.posted_at.isoformat()),
        )
        self._commit()
        return post

    def list_status_posts(self, workspace_id: str) -> list[StatusPost]:
        rows = self.conn.execute(
            "SELECT * FROM status_posts WHERE workspace_id = ? ORDER BY posted_at DESC",
            (workspace_id,),
        ).fetchall()
        return [
            StatusPost(
                id=row["id"],
                workspace_id=row["workspace_id"],
                actor_id=row["actor_id"],
                body=row["body"],
                posted_at=parse_dt(row["posted_at"]),
            )
            for row in rows
        ]

    def _work_item_from_row(self, row: sqlite3.Row) -> WorkItem:
        return WorkItem(
            id=row["id"],
            workspace_id=row["workspace_id"],
            title=row["title"],
            request=row["request"],
            requested_by=row["requested_by"],
            needed_by=row["needed_by"],
            status=row["status"],
            assignee_id=row["assignee_id"],
            playbook_id=row["playbook_id"],
            decision_maker=row["decision_maker"],
            definition_of_done=json.loads(row["definition_of_done"]),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            project_id=row["project_id"],campaign_id=row["campaign_id"],parent_id=row["parent_id"],
            owner_person_id=row["owner_person_id"],assignee_person_id=row["assignee_person_id"],
            reviewer_person_id=row["reviewer_person_id"],priority=row["priority"],tags=tuple(json.loads(row["tags"])),
            estimate_hours=row["estimate_hours"],actual_effort_hours=row["actual_effort_hours"],
            start_date=row["start_date"],deadline=row["deadline"],blocking_reason=row["blocking_reason"],
            brief=row["brief"],brain_context=row["brain_context"],financial_value=row["financial_value"],
        )

    def _touchpoint_from_row(self, row: sqlite3.Row) -> Touchpoint:
        return Touchpoint(
            id=row["id"],
            workspace_id=row["workspace_id"],
            actor_id=row["actor_id"],
            kind=row["kind"],
            summary=row["summary"],
            occurred_at=parse_dt(row["occurred_at"]),
            recorded_at=parse_dt(row["recorded_at"]),
        )

    def _playbook_from_row(self, row: sqlite3.Row) -> Playbook:
        return Playbook(
            id=row["id"],
            workspace_id=row["workspace_id"] or None,
            slug=row["slug"],
            title=row["title"],
            body=row["body"],
            created_at=parse_dt(row["created_at"]),
        )

    def allowed_sources(
        self,
        workspace_id: str,
        actor: Actor,
        as_of: datetime | None = None,
        include_retired: bool = False,
    ) -> list[SourceArtifact]:
        if include_retired:
            rows = self.conn.execute(
                "SELECT * FROM sources WHERE workspace_id=? ORDER BY recorded_at ASC",
                (workspace_id,),
            ).fetchall()
        elif as_of is None:
            lifecycle_clause = "lifecycle.retired_at IS NULL"
            params: tuple[object, ...] = (workspace_id,)
        else:
            moment = as_of.astimezone(timezone.utc).isoformat()
            lifecycle_clause = """lifecycle.effective_from <= ?
                AND (lifecycle.effective_until IS NULL OR lifecycle.effective_until > ?)
                AND NOT EXISTS (
                    SELECT 1 FROM source_lifecycle_intervals candidate
                    JOIN sources candidate_source ON candidate_source.id=candidate.source_id
                    WHERE candidate.workspace_id=lifecycle.workspace_id
                      AND candidate.source_key=lifecycle.source_key
                      AND candidate.effective_from <= ?
                      AND (candidate.effective_until IS NULL OR candidate.effective_until > ?)
                      AND (
                          candidate.effective_from > lifecycle.effective_from
                          OR (candidate.effective_from=lifecycle.effective_from
                              AND candidate_source.version > sources.version)
                      )
                )"""
            params = (workspace_id, moment, moment, moment, moment)
        if not include_retired:
            rows = self.conn.execute(
                f"""SELECT DISTINCT sources.* FROM sources
                    JOIN source_lifecycle_intervals lifecycle
                      ON lifecycle.workspace_id=sources.workspace_id AND lifecycle.source_id=sources.id
                    WHERE sources.workspace_id=? AND {lifecycle_clause}
                    ORDER BY sources.recorded_at ASC""",
                params,
            ).fetchall()
        sources = [self._source_from_row(row) for row in rows]
        if actor.is_admin:
            return sources
        return [
            source
            for source in sources
            if not source.allowed_actor_ids or actor.id in source.allowed_actor_ids
        ]

    def search_documents(
        self,
        workspace_id: str,
        allowed_source_ids: Iterable[str],
        query: str,
        limit: int,
    ) -> list[tuple[Document, SourceArtifact, float]]:
        source_ids = list(allowed_source_ids)
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        match = " OR ".join(token for token in _fts_tokens(query))
        if not match:
            return []
        rows = self.conn.execute(
            f"""
            SELECT
                d.id AS document_id,
                d.workspace_id AS workspace_id,
                d.source_id AS source_id,
                d.content AS content,
                d.content_hash AS document_hash,
                d.observed_at AS document_observed_at,
                d.recorded_at AS document_recorded_at,
                s.source_key AS source_key,
                s.locator AS locator,
                s.content_hash AS source_hash,
                s.media_type AS media_type,
                s.trust_level AS trust_level,
                s.allowed_actor_ids AS allowed_actor_ids,
                s.observed_at AS source_observed_at,
                s.recorded_at AS source_recorded_at,
                s.version AS version,
                bm25(documents_fts) AS rank
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.document_id
            JOIN sources s ON s.id = d.source_id
            WHERE documents_fts.workspace_id = ?
              AND d.workspace_id = ?
              AND d.source_id IN ({placeholders})
              AND documents_fts MATCH ?
            ORDER BY rank ASC
            LIMIT ?
            """,
            (workspace_id, workspace_id, *source_ids, match, limit),
        ).fetchall()
        results: list[tuple[Document, SourceArtifact, float]] = []
        for row in rows:
            document = Document(
                id=row["document_id"],
                workspace_id=row["workspace_id"],
                source_id=row["source_id"],
                content=row["content"],
                content_hash=row["document_hash"],
                observed_at=parse_dt(row["document_observed_at"]),
                recorded_at=parse_dt(row["document_recorded_at"]),
            )
            source = SourceArtifact(
                id=row["source_id"],
                workspace_id=row["workspace_id"],
                source_key=row["source_key"],
                locator=row["locator"],
                content_hash=row["source_hash"],
                media_type=row["media_type"],
                trust_level=row["trust_level"],
                allowed_actor_ids=tuple(json.loads(row["allowed_actor_ids"])),
                observed_at=parse_dt(row["source_observed_at"]),
                recorded_at=parse_dt(row["source_recorded_at"]),
                version=row["version"],
            )
            score = 1.0 / (1.0 + max(float(row["rank"]), 0.0))
            results.append((document, source, score))
        return results

    def list_facts(
        self,
        workspace_id: str,
        allowed_source_ids: Iterable[str],
        as_of: datetime | None = None,
        include_superseded: bool = False,
    ) -> list[Fact]:
        source_ids = list(allowed_source_ids)
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        rows = self.conn.execute(
            f"""
            SELECT f.*, s.source_key, s.locator, s.content_hash
            FROM facts f
            JOIN sources s ON s.id = f.source_id
            WHERE f.workspace_id = ? AND f.source_id IN ({placeholders})
            ORDER BY f.observed_at ASC, f.recorded_at ASC
            """,
            (workspace_id, *source_ids),
        ).fetchall()
        facts = [self._fact_from_row(row) for row in rows]
        if as_of is not None:
            facts = [
                fact
                for fact in facts
                if fact.valid_from <= as_of and (fact.valid_until is None or fact.valid_until > as_of)
            ]
        elif not include_superseded:
            facts = [fact for fact in facts if fact.superseded_by is None]
        return facts

    def list_relations(
        self,
        workspace_id: str,
        allowed_source_ids: Iterable[str],
        as_of: datetime | None = None,
    ) -> list[Relation]:
        source_ids = list(allowed_source_ids)
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        rows = self.conn.execute(
            f"""
            SELECT r.*, s.source_key, s.locator, s.content_hash
            FROM relations r
            JOIN sources s ON s.id = r.source_id
            WHERE r.workspace_id = ? AND r.source_id IN ({placeholders})
            ORDER BY r.observed_at ASC, r.recorded_at ASC
            """,
            (workspace_id, *source_ids),
        ).fetchall()
        relations = [self._relation_from_row(row) for row in rows]
        if as_of is None:
            return relations
        return [
            relation
            for relation in relations
            if relation.valid_from <= as_of
            and (relation.valid_until is None or relation.valid_until > as_of)
        ]

    def list_memories(self, workspace_id: str, actor_id: str) -> list[Memory]:
        rows = self.conn.execute(
            """
            SELECT * FROM memories
            WHERE workspace_id = ? AND actor_id = ?
            ORDER BY recorded_at ASC
            """,
            (workspace_id, actor_id),
        ).fetchall()
        return [
            Memory(
                id=row["id"],
                workspace_id=row["workspace_id"],
                actor_id=row["actor_id"],
                kind=row["kind"],
                content=row["content"],
                observed_at=parse_dt(row["observed_at"]),
                recorded_at=parse_dt(row["recorded_at"]),
            )
            for row in rows
        ]

    def list_recent_documents(
        self,
        workspace_id: str,
        allowed_source_ids: Iterable[str],
        limit: int,
    ) -> list[tuple[Document, SourceArtifact]]:
        source_ids = list(allowed_source_ids)
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        rows = self.conn.execute(
            f"""
            SELECT d.*, s.source_key, s.locator, s.content_hash, s.media_type,
                   s.trust_level, s.allowed_actor_ids, s.observed_at AS source_observed_at,
                   s.recorded_at AS source_recorded_at, s.version
            FROM documents d
            JOIN sources s ON s.id = d.source_id
            WHERE d.workspace_id = ? AND d.source_id IN ({placeholders})
            ORDER BY d.recorded_at DESC
            LIMIT ?
            """,
            (workspace_id, *source_ids, limit),
        ).fetchall()
        results: list[tuple[Document, SourceArtifact]] = []
        for row in rows:
            document = self._document_from_row(row)
            source = SourceArtifact(
                id=row["source_id"],
                workspace_id=row["workspace_id"],
                source_key=row["source_key"],
                locator=row["locator"],
                content_hash=row["content_hash"],
                media_type=row["media_type"],
                trust_level=row["trust_level"],
                allowed_actor_ids=tuple(json.loads(row["allowed_actor_ids"])),
                observed_at=parse_dt(row["source_observed_at"]),
                recorded_at=parse_dt(row["source_recorded_at"]),
                version=row["version"],
            )
            results.append((document, source))
        return results

    def _source_from_row(self, row: sqlite3.Row) -> SourceArtifact:
        return SourceArtifact(
            id=row["id"],
            workspace_id=row["workspace_id"],
            source_key=row["source_key"],
            locator=row["locator"],
            content_hash=row["content_hash"],
            media_type=row["media_type"],
            trust_level=row["trust_level"],
            allowed_actor_ids=tuple(json.loads(row["allowed_actor_ids"])),
            observed_at=parse_dt(row["observed_at"]),
            recorded_at=parse_dt(row["recorded_at"]),
            version=row["version"],
        )

    def _source_from_prefixed_row(self, row: sqlite3.Row) -> SourceArtifact:
        keys = row.keys()
        source_id = row["source_id"] if "source_id" in keys else row["id"]
        return SourceArtifact(
            id=source_id,
            workspace_id=row["workspace_id"],
            source_key=row["source_key"],
            locator=row["locator"],
            content_hash=row["content_hash"] if "content_hash" in keys else row["content_hash"],
            media_type=row["media_type"],
            trust_level=row["trust_level"],
            allowed_actor_ids=tuple(json.loads(row["allowed_actor_ids"])),
            observed_at=parse_dt(row["observed_at"]),
            recorded_at=parse_dt(row["recorded_at"]),
            version=row["version"],
        )

    def _document_from_row(self, row: sqlite3.Row) -> Document:
        return Document(
            id=row["id"],
            workspace_id=row["workspace_id"],
            source_id=row["source_id"],
            content=row["content"],
            content_hash=row["content_hash"],
            observed_at=parse_dt(row["observed_at"]),
            recorded_at=parse_dt(row["recorded_at"]),
        )

    def _citation_from_row(self, row: sqlite3.Row) -> Citation:
        return Citation(
            source_id=row["source_id"],
            source_key=row["source_key"],
            locator=row["locator"],
            content_hash=row["content_hash"],
            evidence_span=row["evidence_span"],
            observed_at=parse_dt(row["observed_at"]),
            valid_from=parse_dt(row["valid_from"]),
            valid_until=parse_dt(row["valid_until"]),
            confidence=row["confidence"],
        )

    def _fact_from_row(self, row: sqlite3.Row) -> Fact:
        citation = self._citation_from_row(row)
        return Fact(
            id=row["id"],
            workspace_id=row["workspace_id"],
            source_id=row["source_id"],
            document_id=row["document_id"],
            subject=row["subject"],
            predicate=row["predicate"],
            object=row["object"],
            valid_from=parse_dt(row["valid_from"]),
            valid_until=parse_dt(row["valid_until"]),
            observed_at=parse_dt(row["observed_at"]),
            recorded_at=parse_dt(row["recorded_at"]),
            confidence=row["confidence"],
            superseded_by=row["superseded_by"],
            conflict_group=row["conflict_group"],
            citation=citation,
        )

    def _relation_from_row(self, row: sqlite3.Row) -> Relation:
        citation = self._citation_from_row(row)
        return Relation(
            id=row["id"],
            workspace_id=row["workspace_id"],
            source_id=row["source_id"],
            document_id=row["document_id"],
            from_entity=row["from_entity"],
            relation=row["relation"],
            to_entity=row["to_entity"],
            valid_from=parse_dt(row["valid_from"]),
            valid_until=parse_dt(row["valid_until"]),
            observed_at=parse_dt(row["observed_at"]),
            recorded_at=parse_dt(row["recorded_at"]),
            confidence=row["confidence"],
            citation=citation,
        )


FTS_STOPWORDS = {
    "and",
    "or",
    "not",
    "the",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "only",
}


def _fts_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for char in query.lower():
        if char.isalnum() or char == "_":
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return [token for token in tokens if token not in FTS_STOPWORDS]


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"
