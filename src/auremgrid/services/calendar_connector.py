"""Offline Calendar and Agency Meeting Lifecycle Connector.

Enforces:
1. Complete agency meeting lifecycle with deterministic transitions:
   scheduled -> held -> notes_captured -> closed
2. Meeting-taker flow: agenda capture, meeting notes, action items with owners and due dates.
3. Strict organization-level isolation and scoping on all reads and writes.
4. Offline, disk-backed SQLite storage (or in-memory for testing).
5. Injected clock for fully deterministic time control.
6. Substance provider verification and reconciliation envelope mirroring
   github_connector_substance and figma_connector_substance conventions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError

MAX_TITLE_LENGTH = 240
MAX_TEXT_LENGTH = 8_000
MAX_AGENCY_ROLE_LENGTH = 64

VALID_MEETING_STATUSES = ("scheduled", "held", "notes_captured", "closed")
VALID_ACTION_STATUSES = ("open", "in_progress", "completed", "cancelled")
REQUIRED_SCOPES = frozenset({"calendar.events:read", "calendar.readonly"})

AGENCY_MEETING_ROLES = frozenset({
    "facilitator",
    "note_taker",
    "client_lead",
    "account_manager",
    "strategist",
    "media_buyer",
    "creative_director",
    "attendee",
    "client_contact",
    "stakeholder",
    "delivery_dri",
})


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
class MeetingAttendee:
    name: str
    agency_role: str
    person_id: str | None = None
    email: str | None = None
    rsvp_status: str = "accepted"

    def __post_init__(self) -> None:
        if not self.name or not str(self.name).strip():
            raise ValidationError("attendee name is required")
        role = str(self.agency_role).strip().lower()
        if not role:
            raise ValidationError("attendee agency_role is required")


@dataclass(frozen=True)
class MeetingActionItem:
    id: str
    organization_id: str
    meeting_id: str
    title: str
    owner_person_id: str
    due_date: str
    owner_name: str | None = None
    description: str | None = None
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CalendarConnectorService:
    """Disk-backed, offline agency meeting lifecycle connector with strict org fencing."""

    provider = "calendar"

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.clock = clock or _default_clock
        self._db_path = str(db_path)
        self.conn = sqlite3.connect(self._db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

        # In-memory tracking for external simulated syncs (mirroring substance services)
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.cursors: dict[tuple[str, str], str | None] = {}
        self.quarantines: list[dict[str, Any]] = []

    def close(self) -> None:
        if self.conn:
            self.conn.close()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS calendar_meetings (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    workspace_id TEXT,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('scheduled', 'held', 'notes_captured', 'closed')),
                    scheduled_at TEXT NOT NULL,
                    held_at TEXT,
                    notes_captured_at TEXT,
                    closed_at TEXT,
                    agenda TEXT,
                    notes TEXT,
                    summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS calendar_attendees (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    meeting_id TEXT NOT NULL,
                    person_id TEXT,
                    name TEXT NOT NULL,
                    email TEXT,
                    agency_role TEXT NOT NULL,
                    rsvp_status TEXT NOT NULL DEFAULT 'accepted',
                    FOREIGN KEY (meeting_id) REFERENCES calendar_meetings(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS calendar_action_items (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    meeting_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    owner_person_id TEXT NOT NULL,
                    owner_name TEXT,
                    due_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open', 'in_progress', 'completed', 'cancelled')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (meeting_id) REFERENCES calendar_meetings(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS calendar_lifecycle_events (
                    id TEXT PRIMARY KEY,
                    organization_id TEXT NOT NULL,
                    meeting_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    actor_person_id TEXT,
                    details TEXT,
                    FOREIGN KEY (meeting_id) REFERENCES calendar_meetings(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_calendar_meetings_org ON calendar_meetings(organization_id);
                CREATE INDEX IF NOT EXISTS idx_calendar_action_items_org ON calendar_action_items(organization_id);
                CREATE INDEX IF NOT EXISTS idx_calendar_action_items_owner ON calendar_action_items(organization_id, owner_person_id);
                """
            )

    def _now(self) -> str:
        return _iso(self.clock())

    def _get_raw_meeting(self, meeting_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM calendar_meetings WHERE id=?", (meeting_id,)).fetchone()
        if not row:
            raise NotFoundError(f"meeting not found: {meeting_id}")
        return row

    def _check_meeting_org(self, row: sqlite3.Row, organization_id: str) -> None:
        if row["organization_id"] != organization_id:
            raise AuthorizationError("meeting belongs to another organization")

    # -----------------------------------------------------------------------
    # Agency Meeting Lifecycle Operations
    # -----------------------------------------------------------------------

    def create_meeting(
        self,
        organization_id: str,
        title: str,
        scheduled_at: datetime | str,
        attendees: Sequence[MeetingAttendee | Mapping[str, Any]],
        agenda: str | None = None,
        workspace_id: str | None = None,
        creator_person_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a scheduled agency meeting with validated attendees and roles."""
        if not organization_id or not str(organization_id).strip():
            raise ValidationError("organization_id is required")
        if not title or not str(title).strip():
            raise ValidationError("meeting title is required")
        clean_title = str(title).strip()[:MAX_TITLE_LENGTH]
        scheduled_iso = _iso(scheduled_at)

        if not attendees:
            raise ValidationError("at least one attendee is required for an agency meeting")

        parsed_attendees: list[MeetingAttendee] = []
        for att in attendees:
            if isinstance(att, MeetingAttendee):
                parsed_attendees.append(att)
            elif isinstance(att, Mapping):
                parsed_attendees.append(
                    MeetingAttendee(
                        name=str(att.get("name", "")),
                        agency_role=str(att.get("agency_role", att.get("role", "attendee"))),
                        person_id=att.get("person_id"),
                        email=att.get("email"),
                        rsvp_status=str(att.get("rsvp_status", "accepted")),
                    )
                )
            else:
                raise ValidationError("invalid attendee format")

        meeting_id = _new_id("meet")
        now = self._now()

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO calendar_meetings (
                    id, organization_id, workspace_id, title, status, scheduled_at,
                    held_at, notes_captured_at, closed_at, agenda, notes, summary,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meeting_id,
                    organization_id,
                    workspace_id,
                    clean_title,
                    "scheduled",
                    scheduled_iso,
                    None,
                    None,
                    None,
                    agenda[:MAX_TEXT_LENGTH] if agenda else None,
                    None,
                    None,
                    now,
                    now,
                ),
            )

            for att in parsed_attendees:
                att_id = _new_id("att")
                self.conn.execute(
                    """
                    INSERT INTO calendar_attendees (
                        id, organization_id, meeting_id, person_id, name, email, agency_role, rsvp_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        att_id,
                        organization_id,
                        meeting_id,
                        att.person_id,
                        att.name,
                        att.email,
                        att.agency_role,
                        att.rsvp_status,
                    ),
                )

            # Record initial lifecycle event
            self.conn.execute(
                """
                INSERT INTO calendar_lifecycle_events (
                    id, organization_id, meeting_id, from_status, to_status, occurred_at, actor_person_id, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("mle"),
                    organization_id,
                    meeting_id,
                    None,
                    "scheduled",
                    now,
                    creator_person_id,
                    "Meeting scheduled",
                ),
            )

        return self.get_meeting(organization_id, meeting_id)

    def capture_agenda(
        self,
        organization_id: str,
        meeting_id: str,
        agenda: str,
        actor_person_id: str | None = None,
    ) -> dict[str, Any]:
        """Capture or update meeting agenda prior to completion."""
        row = self._get_raw_meeting(meeting_id)
        self._check_meeting_org(row, organization_id)

        if row["status"] in ("notes_captured", "closed"):
            raise ValidationError(f"cannot modify agenda for meeting in status '{row['status']}'")

        clean_agenda = str(agenda).strip()[:MAX_TEXT_LENGTH] if agenda else ""
        now = self._now()

        with self.conn:
            self.conn.execute(
                "UPDATE calendar_meetings SET agenda=?, updated_at=? WHERE id=?",
                (clean_agenda, now, meeting_id),
            )

        return self.get_meeting(organization_id, meeting_id)

    def start_meeting(
        self,
        organization_id: str,
        meeting_id: str,
        actor_person_id: str | None = None,
    ) -> dict[str, Any]:
        """Transition meeting state: scheduled -> held."""
        row = self._get_raw_meeting(meeting_id)
        self._check_meeting_org(row, organization_id)

        current_status = row["status"]
        if current_status != "scheduled":
            raise ValidationError(
                f"invalid transition: expected status 'scheduled', got '{current_status}'"
            )

        now = self._now()
        with self.conn:
            self.conn.execute(
                """
                UPDATE calendar_meetings
                SET status='held', held_at=?, updated_at=?
                WHERE id=?
                """,
                (now, now, meeting_id),
            )
            self.conn.execute(
                """
                INSERT INTO calendar_lifecycle_events (
                    id, organization_id, meeting_id, from_status, to_status, occurred_at, actor_person_id, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("mle"),
                    organization_id,
                    meeting_id,
                    "scheduled",
                    "held",
                    now,
                    actor_person_id,
                    "Meeting held",
                ),
            )

        return self.get_meeting(organization_id, meeting_id)

    def capture_notes_and_actions(
        self,
        organization_id: str,
        meeting_id: str,
        notes: str,
        action_items: Sequence[Mapping[str, Any]] | None = None,
        actor_person_id: str | None = None,
    ) -> dict[str, Any]:
        """Meeting-taker flow: record notes and action items (transition: held -> notes_captured)."""
        row = self._get_raw_meeting(meeting_id)
        self._check_meeting_org(row, organization_id)

        current_status = row["status"]
        if current_status not in ("held", "notes_captured"):
            raise ValidationError(
                f"cannot capture notes on meeting in status '{current_status}'; must be 'held'"
            )

        if not notes or not str(notes).strip():
            raise ValidationError("meeting notes cannot be empty")

        clean_notes = str(notes).strip()[:MAX_TEXT_LENGTH]
        now = self._now()

        # Validate action items
        parsed_actions: list[dict[str, Any]] = []
        for idx, act in enumerate(action_items or ()):
            if not isinstance(act, Mapping):
                raise ValidationError(f"action item at index {idx} must be a dictionary")
            title = act.get("title") or act.get("name")
            if not title or not str(title).strip():
                raise ValidationError(f"action item at index {idx} requires a title")
            owner_id = act.get("owner_person_id") or act.get("owner")
            if not owner_id or not str(owner_id).strip():
                raise ValidationError(f"action item at index {idx} requires owner_person_id")
            due = act.get("due_date") or act.get("due")
            if not due or not str(due).strip():
                raise ValidationError(f"action item at index {idx} requires due_date")

            parsed_actions.append({
                "id": _new_id("act"),
                "organization_id": organization_id,
                "meeting_id": meeting_id,
                "title": str(title).strip()[:MAX_TITLE_LENGTH],
                "description": str(act.get("description", "")).strip()[:MAX_TEXT_LENGTH] if act.get("description") else None,
                "owner_person_id": str(owner_id).strip(),
                "owner_name": str(act.get("owner_name", "")).strip() or None,
                "due_date": _iso(due),
                "status": str(act.get("status", "open")).lower(),
                "created_at": now,
                "updated_at": now,
            })

        with self.conn:
            self.conn.execute(
                """
                UPDATE calendar_meetings
                SET status='notes_captured', notes=?, notes_captured_at=COALESCE(notes_captured_at, ?), updated_at=?
                WHERE id=?
                """,
                (clean_notes, now, now, meeting_id),
            )

            for act_dict in parsed_actions:
                self.conn.execute(
                    """
                    INSERT INTO calendar_action_items (
                        id, organization_id, meeting_id, title, description,
                        owner_person_id, owner_name, due_date, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        act_dict["id"],
                        act_dict["organization_id"],
                        act_dict["meeting_id"],
                        act_dict["title"],
                        act_dict["description"],
                        act_dict["owner_person_id"],
                        act_dict["owner_name"],
                        act_dict["due_date"],
                        act_dict["status"],
                        act_dict["created_at"],
                        act_dict["updated_at"],
                    ),
                )

            self.conn.execute(
                """
                INSERT INTO calendar_lifecycle_events (
                    id, organization_id, meeting_id, from_status, to_status, occurred_at, actor_person_id, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("mle"),
                    organization_id,
                    meeting_id,
                    current_status,
                    "notes_captured",
                    now,
                    actor_person_id,
                    f"Captured notes and {len(parsed_actions)} action items",
                ),
            )

        return self.get_meeting(organization_id, meeting_id)

    def close_meeting(
        self,
        organization_id: str,
        meeting_id: str,
        summary: str | None = None,
        actor_person_id: str | None = None,
    ) -> dict[str, Any]:
        """Transition meeting state: notes_captured -> closed."""
        row = self._get_raw_meeting(meeting_id)
        self._check_meeting_org(row, organization_id)

        current_status = row["status"]
        if current_status != "notes_captured":
            raise ValidationError(
                f"invalid transition: expected status 'notes_captured' before closing, got '{current_status}'"
            )

        now = self._now()
        with self.conn:
            self.conn.execute(
                """
                UPDATE calendar_meetings
                SET status='closed', closed_at=?, summary=?, updated_at=?
                WHERE id=?
                """,
                (now, summary[:MAX_TEXT_LENGTH] if summary else None, now, meeting_id),
            )
            self.conn.execute(
                """
                INSERT INTO calendar_lifecycle_events (
                    id, organization_id, meeting_id, from_status, to_status, occurred_at, actor_person_id, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("mle"),
                    organization_id,
                    meeting_id,
                    "notes_captured",
                    "closed",
                    now,
                    actor_person_id,
                    "Meeting closed",
                ),
            )

        return self.get_meeting(organization_id, meeting_id)

    # -----------------------------------------------------------------------
    # Action Item Operations
    # -----------------------------------------------------------------------

    def update_action_item_status(
        self,
        organization_id: str,
        action_item_id: str,
        status: str,
        actor_person_id: str | None = None,
    ) -> dict[str, Any]:
        """Update an action item's status with organization scoping."""
        clean_status = str(status).strip().lower()
        if clean_status not in VALID_ACTION_STATUSES:
            raise ValidationError(f"invalid action item status: {status}")

        row = self.conn.execute(
            "SELECT * FROM calendar_action_items WHERE id=?",
            (action_item_id,),
        ).fetchone()
        if not row:
            raise NotFoundError(f"action item not found: {action_item_id}")

        if row["organization_id"] != organization_id:
            raise AuthorizationError("action item belongs to another organization")

        now = self._now()
        with self.conn:
            self.conn.execute(
                "UPDATE calendar_action_items SET status=?, updated_at=? WHERE id=?",
                (clean_status, now, action_item_id),
            )

        updated = self.conn.execute("SELECT * FROM calendar_action_items WHERE id=?", (action_item_id,)).fetchone()
        return dict(updated)

    def list_action_items_for_org(
        self,
        organization_id: str,
        status: str | None = None,
        meeting_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List action items scoped to an organization."""
        query = "SELECT * FROM calendar_action_items WHERE organization_id=?"
        params: list[Any] = [organization_id]

        if status:
            query += " AND status=?"
            params.append(status.lower())
        if meeting_id:
            query += " AND meeting_id=?"
            params.append(meeting_id)

        query += " ORDER BY due_date ASC, created_at ASC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def list_action_items_for_owner(
        self,
        organization_id: str,
        owner_person_id: str,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List action items assigned to an owner, strictly scoped to the organization."""
        query = "SELECT * FROM calendar_action_items WHERE organization_id=? AND owner_person_id=?"
        params: list[Any] = [organization_id, owner_person_id]

        if status:
            query += " AND status=?"
            params.append(status.lower())

        query += " ORDER BY due_date ASC, created_at ASC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Query & Retrieval
    # -----------------------------------------------------------------------

    def get_meeting(self, organization_id: str, meeting_id: str) -> dict[str, Any]:
        """Fetch a full meeting record including attendees, actions, and lifecycle history."""
        row = self._get_raw_meeting(meeting_id)
        self._check_meeting_org(row, organization_id)

        attendees = [
            dict(r) for r in self.conn.execute(
                "SELECT id, person_id, name, email, agency_role, rsvp_status FROM calendar_attendees WHERE meeting_id=?",
                (meeting_id,),
            ).fetchall()
        ]

        action_items = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM calendar_action_items WHERE meeting_id=? ORDER BY due_date ASC",
                (meeting_id,),
            ).fetchall()
        ]

        events = [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM calendar_lifecycle_events WHERE meeting_id=? ORDER BY occurred_at ASC",
                (meeting_id,),
            ).fetchall()
        ]

        result = dict(row)
        result["attendees"] = attendees
        result["action_items"] = action_items
        result["lifecycle_events"] = events
        return result

    def list_meetings(
        self,
        organization_id: str,
        workspace_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List meetings scoped to an organization."""
        query = "SELECT * FROM calendar_meetings WHERE organization_id=?"
        params: list[Any] = [organization_id]

        if workspace_id:
            query += " AND workspace_id=?"
            params.append(workspace_id)
        if status:
            query += " AND status=?"
            params.append(status.lower())

        query += " ORDER BY scheduled_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Substance Provider Sync (conventions matching figma / github substance)
    # -----------------------------------------------------------------------

    def verify_provider(
        self,
        response: Mapping[str, Any],
        *,
        account_id: str,
        calendar_id: str,
    ) -> dict[str, Any]:
        """Verify external provider identity, account fence, calendar fence, and read scopes."""
        if not isinstance(response, Mapping):
            raise ValidationError("provider response must be an object")
        prov = response.get("provider", self.provider)
        if prov not in (self.provider, "google_calendar", "outlook_calendar"):
            raise ValidationError("provider identity verification failed")
        if str(response.get("account_id", account_id)) != account_id:
            raise AuthorizationError("provider account fence verification failed")
        if str(response.get("calendar_id", calendar_id)) != calendar_id:
            raise AuthorizationError("provider calendar fence verification failed")

        scopes = response.get("scopes", response.get("read_scopes", ()))
        granted = frozenset(str(s) for s in scopes) if isinstance(scopes, (list, tuple, set)) else frozenset()
        missing = sorted(REQUIRED_SCOPES - granted)
        if missing:
            raise ValidationError(f"provider read scope verification failed: missing {missing}")

        return {
            "provider": prov,
            "account_id": account_id,
            "calendar_id": calendar_id,
            "required_scopes": sorted(REQUIRED_SCOPES),
            "granted_scopes": sorted(granted),
            "read_scope_verified": True,
        }

    def sync(
        self,
        organization_id: str,
        account_id: str,
        calendar_id: str,
        response: Mapping[str, Any],
        *,
        cursor: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Map simulated provider calendar events into the agency meeting lifecycle."""
        if not organization_id or not calendar_id:
            raise ValidationError("organization and calendar_id are required")

        verification = self.verify_provider(response, account_id=account_id, calendar_id=calendar_id)
        provider_version = str(response.get("provider_version") or response.get("version") or "").strip()
        if not provider_version:
            raise ValidationError("calendar provider version is required")

        cursor_key = (organization_id, calendar_id)
        cursor_before = cursor if cursor is not None else self.cursors.get(cursor_key)
        if cursor_before and cursor_before == provider_version:
            return {
                "status": "ok",
                "cursor_before": cursor_before,
                "cursor_after": cursor_before,
                "verification": verification,
                "provider_version": provider_version,
                "baseline": {"provider_count": 0},
                "imported": 0,
                "duplicates": 0,
                "quarantined": 0,
                "records": [],
            }

        values = response.get("data", response.get("records", response.get("events", [])))
        if not isinstance(values, list):
            raise ValidationError("calendar page records must be a list")

        imported = 0
        duplicates = 0
        quarantined = 0
        imported_records: list[dict[str, Any]] = []

        for index, item in enumerate(values):
            if not isinstance(item, Mapping):
                quarantined += 1
                self.quarantines.append({"index": index, "reason": "non_object_record", "organization_id": organization_id})
                continue

            title = item.get("title") or item.get("summary")
            scheduled_at = item.get("scheduled_at") or item.get("start_time") or item.get("start")
            if not title or not scheduled_at:
                quarantined += 1
                self.quarantines.append({"index": index, "reason": "missing_required_fields", "record": item, "organization_id": organization_id})
                continue

            external_id = str(item.get("id") or item.get("external_id") or "")
            dedupe_key = f"{calendar_id}:{external_id}:{provider_version}"
            rec_key = (organization_id, dedupe_key)

            if rec_key in self.records:
                duplicates += 1
                continue

            raw_attendees = item.get("attendees") or [
                {"name": "Organizer", "agency_role": "facilitator"}
            ]
            try:
                meeting = self.create_meeting(
                    organization_id=organization_id,
                    title=str(title),
                    scheduled_at=str(scheduled_at),
                    attendees=raw_attendees,
                    agenda=item.get("agenda") or item.get("description"),
                    workspace_id=workspace_id,
                )
                self.records[rec_key] = meeting
                imported_records.append(meeting)
                imported += 1
            except Exception as exc:
                quarantined += 1
                self.quarantines.append({"index": index, "reason": "creation_failure", "error": str(exc), "organization_id": organization_id})

        self.cursors[cursor_key] = provider_version
        status = "ok" if quarantined == 0 else "degraded"

        return {
            "status": status,
            "cursor_before": cursor_before,
            "cursor_after": provider_version,
            "verification": verification,
            "provider_version": provider_version,
            "baseline": {"provider_count": len(values)},
            "imported": imported,
            "duplicates": duplicates,
            "quarantined": quarantined,
            "records": imported_records,
        }

