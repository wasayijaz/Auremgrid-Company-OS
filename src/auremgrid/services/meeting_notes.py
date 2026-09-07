"""Structured client-meeting-taker notes service.

Ties structured meeting notes, decisions, risks, and action items to calendar events
with strict organization/workspace scoping and action item surfacing.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.calendar_connector import CalendarConnectorService


def _default_clock() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _iso(val: datetime | str) -> str:
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat()
    raw = str(val).strip()
    if not raw:
        raise ValidationError("date/time string cannot be empty")
    return raw


@dataclass(frozen=True)
class ActionItem:
    id: str
    organization_id: str
    meeting_note_id: str
    description: str
    owner_person_id: str
    due_date: str
    workspace_id: str | None = None
    calendar_event_id: str | None = None
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StructuredMeetingNote:
    id: str
    organization_id: str
    calendar_event_id: str
    title: str
    meeting_date: str
    summary: str
    attendees: list[str]
    decisions: list[str]
    risks: list[str]
    workspace_id: str | None = None
    raw_notes: str | None = None
    status: str = "captured"
    action_items: list[ActionItem] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action_items"] = [item.to_dict() for item in self.action_items]
        return data


class MeetingNotesService:
    """Client-meeting-taker service managing structured notes and open action items."""

    def __init__(
        self,
        calendar_service: CalendarConnectorService | None = None,
        db_path: str | Path | None = None,
        conn: sqlite3.Connection | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.clock = clock or _default_clock
        if calendar_service is not None:
            self.calendar_service = calendar_service
            self.conn = calendar_service.conn
            self._owns_conn = False
        elif conn is not None:
            self.conn = conn
            self.conn.row_factory = sqlite3.Row
            self._owns_conn = False
            self.calendar_service = CalendarConnectorService(clock=self.clock)
        else:
            path_str = str(db_path) if db_path else ":memory:"
            self.conn = sqlite3.connect(path_str)
            self.conn.row_factory = sqlite3.Row
            self._owns_conn = True
            self.calendar_service = CalendarConnectorService(db_path=path_str, clock=self.clock)

        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        if self._owns_conn and self.conn:
            self.conn.close()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meeting_notes (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    workspace_id TEXT,
                    calendar_event_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    meeting_date TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    attendees TEXT NOT NULL,
                    decisions TEXT NOT NULL,
                    risks TEXT NOT NULL,
                    raw_notes TEXT,
                    status TEXT NOT NULL DEFAULT 'captured',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meeting_note_action_items (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    workspace_id TEXT,
                    meeting_note_id TEXT NOT NULL,
                    calendar_event_id TEXT,
                    description TEXT NOT NULL,
                    owner_person_id TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open', 'in_progress', 'completed', 'cancelled')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (meeting_note_id) REFERENCES meeting_notes(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_meeting_notes_org ON meeting_notes(organization_id);
                CREATE INDEX IF NOT EXISTS idx_meeting_notes_ws ON meeting_notes(organization_id, workspace_id);
                CREATE INDEX IF NOT EXISTS idx_meeting_notes_cal ON meeting_notes(calendar_event_id);
                CREATE INDEX IF NOT EXISTS idx_meeting_action_items_org ON meeting_note_action_items(organization_id);
                CREATE INDEX IF NOT EXISTS idx_meeting_action_items_owner ON meeting_note_action_items(organization_id, owner_person_id);
                """
            )

    def _now(self) -> str:
        return _iso(self.clock())

    def record_meeting_notes(
        self,
        organization_id: str,
        calendar_event_id: str,
        summary: str,
        attendees: Sequence[str] | None = None,
        decisions: Sequence[str] | None = None,
        action_items: Sequence[Mapping[str, Any]] | None = None,
        risks: Sequence[str] | None = None,
        workspace_id: str | None = None,
        raw_notes: str | None = None,
        meeting_date: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Record structured meeting notes validated against an existing calendar event."""
        if not organization_id or not str(organization_id).strip():
            raise ValidationError("organization_id is required")
        if not calendar_event_id or not str(calendar_event_id).strip():
            raise ValidationError("calendar_event_id is required")
        if not summary or not str(summary).strip():
            raise ValidationError("meeting summary is required")

        # 1. Validate that the calendar event exists via the calendar connector service
        calendar_meeting = self.calendar_service.get_meeting(organization_id, calendar_event_id)
        if calendar_meeting["organization_id"] != organization_id:
            raise AuthorizationError("calendar event belongs to another organization")

        resolved_workspace = workspace_id or calendar_meeting.get("workspace_id")
        meeting_title = calendar_meeting.get("title", "Client Meeting")
        date_iso = _iso(meeting_date) if meeting_date else calendar_meeting.get("scheduled_at", self._now())

        # Advance calendar meeting lifecycle if in scheduled state
        if calendar_meeting["status"] == "scheduled":
            try:
                self.calendar_service.start_meeting(organization_id, calendar_event_id)
            except Exception:
                pass

        # Parse sections
        parsed_attendees = [str(a).strip() for a in (attendees or ()) if str(a).strip()]
        if not parsed_attendees and calendar_meeting.get("attendees"):
            parsed_attendees = [
                str(att.get("person_id") or att.get("name"))
                for att in calendar_meeting["attendees"]
                if att.get("person_id") or att.get("name")
            ]

        parsed_decisions = [str(d).strip() for d in (decisions or ()) if str(d).strip()]
        parsed_risks = [str(r).strip() for r in (risks or ()) if str(r).strip()]

        # Parse action items
        parsed_actions: list[dict[str, Any]] = []
        for idx, act in enumerate(action_items or ()):
            if not isinstance(act, Mapping):
                raise ValidationError(f"action item at index {idx} must be an object")
            desc = act.get("description") or act.get("title") or act.get("name")
            if not desc or not str(desc).strip():
                raise ValidationError(f"action item at index {idx} requires a description")
            owner_id = act.get("owner_person_id") or act.get("owner")
            if not owner_id or not str(owner_id).strip():
                raise ValidationError(f"action item at index {idx} requires owner_person_id")
            due = act.get("due_date") or act.get("due")
            if not due or not str(due).strip():
                raise ValidationError(f"action item at index {idx} requires due_date")

            parsed_actions.append({
                "id": _new_id("mna"),
                "description": str(desc).strip(),
                "owner_person_id": str(owner_id).strip(),
                "due_date": _iso(due),
                "status": str(act.get("status", "open")).lower(),
            })

        now = self._now()
        note_id = _new_id("note")

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO meeting_notes (
                    id, organization_id, workspace_id, calendar_event_id, title,
                    meeting_date, summary, attendees, decisions, risks, raw_notes,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note_id,
                    organization_id,
                    resolved_workspace,
                    calendar_event_id,
                    meeting_title,
                    date_iso,
                    str(summary).strip(),
                    json.dumps(parsed_attendees),
                    json.dumps(parsed_decisions),
                    json.dumps(parsed_risks),
                    str(raw_notes).strip() if raw_notes else None,
                    "captured",
                    now,
                    now,
                ),
            )

            for act in parsed_actions:
                self.conn.execute(
                    """
                    INSERT INTO meeting_note_action_items (
                        id, organization_id, workspace_id, meeting_note_id, calendar_event_id,
                        description, owner_person_id, due_date, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        act["id"],
                        organization_id,
                        resolved_workspace,
                        note_id,
                        calendar_event_id,
                        act["description"],
                        act["owner_person_id"],
                        act["due_date"],
                        act["status"],
                        now,
                        now,
                    ),
                )

        # Synchronize back with calendar meeting service lifecycle
        try:
            cal_actions = [
                {"title": a["description"], "owner_person_id": a["owner_person_id"], "due_date": a["due_date"]}
                for a in parsed_actions
            ]
            self.calendar_service.capture_notes_and_actions(
                organization_id,
                calendar_event_id,
                notes=summary,
                action_items=cal_actions,
            )
        except Exception:
            pass

        return self.get_meeting_note(organization_id, note_id)

    def get_meeting_note(self, organization_id: str, note_id: str) -> dict[str, Any]:
        """Fetch a meeting note record including its action items."""
        row = self.conn.execute("SELECT * FROM meeting_notes WHERE id=?", (note_id,)).fetchone()
        if not row:
            raise NotFoundError(f"meeting note not found: {note_id}")

        if row["organization_id"] != organization_id:
            raise AuthorizationError("meeting note belongs to another organization")

        actions = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM meeting_note_action_items WHERE meeting_note_id=? ORDER BY due_date ASC",
                (note_id,),
            ).fetchall()
        ]

        result = dict(row)
        result["attendees"] = json.loads(result["attendees"])
        result["decisions"] = json.loads(result["decisions"])
        result["risks"] = json.loads(result["risks"])
        result["action_items"] = actions
        return result

    def list_meeting_notes(
        self,
        organization_id: str,
        workspace_id: str | None = None,
        *,
        client_id: str | None = None,
        date_from: datetime | str | None = None,
        date_to: datetime | str | None = None,
        attendee_person_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List meeting notes filtered by client/workspace, date range, or attendee."""
        if not organization_id:
            raise ValidationError("organization_id is required")

        resolved_ws = workspace_id or client_id
        query = "SELECT id FROM meeting_notes WHERE organization_id=?"
        params: list[Any] = [organization_id]

        if resolved_ws:
            query += " AND workspace_id=?"
            params.append(resolved_ws)

        if date_from:
            query += " AND meeting_date >= ?"
            params.append(_iso(date_from))

        if date_to:
            query += " AND meeting_date <= ?"
            params.append(_iso(date_to))

        query += " ORDER BY meeting_date DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        notes = [self.get_meeting_note(organization_id, r["id"]) for r in rows]

        if attendee_person_id:
            clean_att = str(attendee_person_id).strip()
            notes = [n for n in notes if clean_att in n.get("attendees", [])]

        return notes

    def get_open_action_items(
        self,
        organization_id: str,
        workspace_id: str | None = None,
        owner_person_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return unfinished action items (open or in_progress) with owner and due date."""
        if not organization_id:
            raise ValidationError("organization_id is required")

        query = """
            SELECT a.id, a.organization_id, a.workspace_id, a.meeting_note_id,
                   a.calendar_event_id, a.description, a.owner_person_id, a.due_date,
                   a.status, a.created_at, a.updated_at, m.title as meeting_title
            FROM meeting_note_action_items a
            JOIN meeting_notes m ON m.id = a.meeting_note_id
            WHERE a.organization_id=? AND a.status IN ('open', 'in_progress')
        """
        params: list[Any] = [organization_id]

        if workspace_id:
            query += " AND a.workspace_id=?"
            params.append(workspace_id)

        if owner_person_id:
            query += " AND a.owner_person_id=?"
            params.append(owner_person_id)

        query += " ORDER BY a.due_date ASC, a.created_at ASC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def update_action_item_status(
        self,
        organization_id: str,
        action_item_id: str,
        status: str,
    ) -> dict[str, Any]:
        """Update an action item's status with organization scoping."""
        clean_status = str(status).strip().lower()
        if clean_status not in ("open", "in_progress", "completed", "cancelled"):
            raise ValidationError(f"invalid action item status: {status}")

        row = self.conn.execute(
            "SELECT * FROM meeting_note_action_items WHERE id=?",
            (action_item_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"action item not found: {action_item_id}")

        if row["organization_id"] != organization_id:
            raise AuthorizationError("action item belongs to another organization")

        now = self._now()
        with self.conn:
            self.conn.execute(
                "UPDATE meeting_note_action_items SET status=?, updated_at=? WHERE id=?",
                (clean_status, now, action_item_id),
            )

        updated = self.conn.execute(
            "SELECT * FROM meeting_note_action_items WHERE id=?",
            (action_item_id,),
        ).fetchone()
        return dict(updated)

