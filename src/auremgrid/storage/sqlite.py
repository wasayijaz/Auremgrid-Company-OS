from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

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

    def create_source(self, source: SourceArtifact) -> SourceArtifact:
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
        self._commit()
        return source

    def get_source(self, workspace_id: str, source_id: str) -> SourceArtifact | None:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE workspace_id = ? AND id = ?",
            (workspace_id, source_id),
        ).fetchone()
        return self._source_from_row(row) if row else None

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

    def allowed_sources(self, workspace_id: str, actor: Actor) -> list[SourceArtifact]:
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE workspace_id = ? ORDER BY recorded_at ASC",
            (workspace_id,),
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
        if char.isalnum() or char in {"-", "_"}:
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return [token for token in tokens if token not in FTS_STOPWORDS]
