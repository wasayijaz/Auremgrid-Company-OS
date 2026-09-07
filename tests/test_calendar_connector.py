"""Tests for offline Calendar and Agency Meeting Lifecycle Connector."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.calendar_connector import (
    CalendarConnectorService,
    MeetingAttendee,
    REQUIRED_SCOPES,
)


class CalendarConnectorLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_calendar.sqlite"
        self.current_time = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
        self.service = CalendarConnectorService(
            db_path=self.db_path,
            clock=lambda: self.current_time,
        )
        self.org_a = "org_media_prime"
        self.org_b = "org_brand_rival"

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    def advance_time(self, seconds: int = 3600) -> None:
        self.current_time = datetime.fromtimestamp(
            self.current_time.timestamp() + seconds, tz=timezone.utc
        )

    def test_meeting_creation_and_initial_scheduled_state(self) -> None:
        attendees = [
            MeetingAttendee(name="Alice Facilitator", agency_role="facilitator", person_id="p_alice"),
            MeetingAttendee(name="Bob Strategist", agency_role="strategist", person_id="p_bob"),
            MeetingAttendee(name="Carol Client", agency_role="client_lead", email="carol@client.test"),
        ]

        meeting = self.service.create_meeting(
            organization_id=self.org_a,
            title="Q3 Strategy & Performance Sync",
            scheduled_at="2026-09-08T14:00:00Z",
            attendees=attendees,
            agenda="1. Pipeline Review | 2. Q4 Budget | 3. Creative Deliverables",
        )

        self.assertEqual(meeting["organization_id"], self.org_a)
        self.assertEqual(meeting["title"], "Q3 Strategy & Performance Sync")
        self.assertEqual(meeting["status"], "scheduled")
        self.assertEqual(len(meeting["attendees"]), 3)
        self.assertEqual(meeting["agenda"], "1. Pipeline Review | 2. Q4 Budget | 3. Creative Deliverables")
        self.assertIsNone(meeting["held_at"])
        self.assertIsNone(meeting["closed_at"])

        # Check initial lifecycle event
        self.assertEqual(len(meeting["lifecycle_events"]), 1)
        self.assertEqual(meeting["lifecycle_events"][0]["to_status"], "scheduled")

    def test_meeting_validation_errors(self) -> None:
        # Missing org
        with self.assertRaises(ValidationError):
            self.service.create_meeting("", "Title", "2026-09-08T14:00:00Z", [{"name": "A", "agency_role": "attendee"}])

        # Missing title
        with self.assertRaises(ValidationError):
            self.service.create_meeting(self.org_a, "", "2026-09-08T14:00:00Z", [{"name": "A", "agency_role": "attendee"}])

        # Empty attendees
        with self.assertRaises(ValidationError):
            self.service.create_meeting(self.org_a, "Title", "2026-09-08T14:00:00Z", [])

    def test_complete_meeting_lifecycle_and_deterministic_transitions(self) -> None:
        # 1. Schedule Meeting
        meeting = self.service.create_meeting(
            organization_id=self.org_a,
            title="Creative Review",
            scheduled_at="2026-09-07T11:00:00Z",
            attendees=[{"name": "Designer Dave", "agency_role": "creative_director"}],
            agenda="Initial agenda",
        )
        mid = meeting["id"]
        self.assertEqual(meeting["status"], "scheduled")

        # Capture updated agenda while scheduled
        self.service.capture_agenda(self.org_a, mid, "Updated Agenda: Concept A, Concept B")
        m_agenda = self.service.get_meeting(self.org_a, mid)
        self.assertEqual(m_agenda["agenda"], "Updated Agenda: Concept A, Concept B")

        # 2. Transition: scheduled -> held
        self.advance_time(3600)  # +1 hour
        held_time_iso = self.current_time.isoformat()
        m_held = self.service.start_meeting(self.org_a, mid, actor_person_id="p_dave")
        self.assertEqual(m_held["status"], "held")
        self.assertEqual(m_held["held_at"], held_time_iso)

        # 3. Transition: held -> notes_captured
        self.advance_time(1800)  # +30 min
        notes_time_iso = self.current_time.isoformat()
        actions = [
            {
                "title": "Revise hero banner copy",
                "owner_person_id": "p_copywriter",
                "owner_name": "Clara Copy",
                "due_date": "2026-09-10T17:00:00Z",
            },
            {
                "title": "Deliver 3 TikTok cutdowns",
                "owner_person_id": "p_editor",
                "owner_name": "Ed Editor",
                "due_date": "2026-09-12T17:00:00Z",
            },
        ]
        m_notes = self.service.capture_notes_and_actions(
            self.org_a,
            mid,
            notes="Client approved Concept A with copy tweaks. Video variants needed.",
            action_items=actions,
            actor_person_id="p_dave",
        )
        self.assertEqual(m_notes["status"], "notes_captured")
        self.assertEqual(m_notes["notes_captured_at"], notes_time_iso)
        self.assertEqual(len(m_notes["action_items"]), 2)

        # 4. Transition: notes_captured -> closed
        self.advance_time(600)  # +10 min
        closed_time_iso = self.current_time.isoformat()
        m_closed = self.service.close_meeting(
            self.org_a,
            mid,
            summary="Meeting concluded successfully with 2 assigned action items.",
            actor_person_id="p_dave",
        )
        self.assertEqual(m_closed["status"], "closed")
        self.assertEqual(m_closed["closed_at"], closed_time_iso)
        self.assertEqual(m_closed["summary"], "Meeting concluded successfully with 2 assigned action items.")

        # Verify full lifecycle event chain (scheduled -> held -> notes_captured -> closed)
        event_transitions = [
            (e["from_status"], e["to_status"]) for e in m_closed["lifecycle_events"]
        ]
        self.assertEqual(
            event_transitions,
            [
                (None, "scheduled"),
                ("scheduled", "held"),
                ("held", "notes_captured"),
                ("notes_captured", "closed"),
            ],
        )

    def test_invalid_lifecycle_transitions_fail_closed(self) -> None:
        meeting = self.service.create_meeting(
            organization_id=self.org_a,
            title="Discipline Meeting",
            scheduled_at="2026-09-07T12:00:00Z",
            attendees=[{"name": "Manager", "agency_role": "facilitator"}],
        )
        mid = meeting["id"]

        # Attempt scheduled -> closed directly (bypassing held and notes_captured)
        with self.assertRaises(ValidationError) as cm1:
            self.service.close_meeting(self.org_a, mid)
        self.assertIn("expected status 'notes_captured'", str(cm1.exception))

        # Attempt scheduled -> notes_captured directly
        with self.assertRaises(ValidationError) as cm2:
            self.service.capture_notes_and_actions(self.org_a, mid, "Skipped notes")
        self.assertIn("must be 'held'", str(cm2.exception))

        # Start meeting (scheduled -> held)
        self.service.start_meeting(self.org_a, mid)

        # Attempt held -> closed directly (bypassing notes_captured)
        with self.assertRaises(ValidationError) as cm3:
            self.service.close_meeting(self.org_a, mid)
        self.assertIn("expected status 'notes_captured'", str(cm3.exception))

    def test_action_item_ownership_due_dates_and_query_scoping(self) -> None:
        meeting = self.service.create_meeting(
            self.org_a,
            "Sprint Planning",
            "2026-09-07T10:00:00Z",
            [{"name": "Lead", "agency_role": "facilitator"}],
        )
        mid = meeting["id"]
        self.service.start_meeting(self.org_a, mid)

        # Action item validation: missing owner
        with self.assertRaises(ValidationError) as cm:
            self.service.capture_notes_and_actions(
                self.org_a,
                mid,
                "Notes",
                [{"title": "Task 1", "due_date": "2026-09-10T00:00:00Z"}],
            )
        self.assertIn("requires owner_person_id", str(cm.exception))

        # Capture 3 action items across 2 owners
        actions = [
            {"title": "Audit ROAS", "owner_person_id": "p_alice", "due_date": "2026-09-09T18:00:00Z"},
            {"title": "Reallocate Budget", "owner_person_id": "p_alice", "due_date": "2026-09-11T18:00:00Z"},
            {"title": "Deploy Landing Page", "owner_person_id": "p_bob", "due_date": "2026-09-10T12:00:00Z"},
        ]
        self.service.capture_notes_and_actions(self.org_a, mid, "Sprint notes", actions)

        # List per org
        all_org_items = self.service.list_action_items_for_org(self.org_a)
        self.assertEqual(len(all_org_items), 3)

        # List per owner
        alice_items = self.service.list_action_items_for_owner(self.org_a, "p_alice")
        self.assertEqual(len(alice_items), 2)
        self.assertEqual(alice_items[0]["title"], "Audit ROAS")
        self.assertEqual(alice_items[1]["title"], "Reallocate Budget")

        bob_items = self.service.list_action_items_for_owner(self.org_a, "p_bob")
        self.assertEqual(len(bob_items), 1)
        self.assertEqual(bob_items[0]["title"], "Deploy Landing Page")

        # Update action item status
        item_id = bob_items[0]["id"]
        updated = self.service.update_action_item_status(self.org_a, item_id, "completed")
        self.assertEqual(updated["status"], "completed")

        # List with status filter
        open_alice = self.service.list_action_items_for_owner(self.org_a, "p_alice", status="open")
        self.assertEqual(len(open_alice), 2)

    def test_strict_organization_isolation_and_cross_org_fencing(self) -> None:
        meeting_a = self.service.create_meeting(
            self.org_a,
            "Confidential Agency A Strategy",
            "2026-09-07T10:00:00Z",
            [{"name": "Exec A", "agency_role": "client_lead"}],
        )
        mid_a = meeting_a["id"]

        # Org B cannot read Org A's meeting
        with self.assertRaises(AuthorizationError):
            self.service.get_meeting(self.org_b, mid_a)

        # Org B cannot mutate Org A's meeting
        with self.assertRaises(AuthorizationError):
            self.service.start_meeting(self.org_b, mid_a)

        with self.assertRaises(AuthorizationError):
            self.service.capture_agenda(self.org_b, mid_a, "Hacked Agenda")

        # Org B listing returns only Org B meetings
        self.service.create_meeting(
            self.org_b,
            "Agency B Meeting",
            "2026-09-07T11:00:00Z",
            [{"name": "Exec B", "agency_role": "client_lead"}],
        )
        list_b = self.service.list_meetings(self.org_b)
        self.assertEqual(len(list_b), 1)
        self.assertEqual(list_b[0]["title"], "Agency B Meeting")

    def test_substance_sync_reconciliation_envelope_and_idempotency(self) -> None:
        # Simulated provider response
        calendar_feed = {
            "provider": "google_calendar",
            "account_id": "acc-agency-1",
            "calendar_id": "primary",
            "scopes": sorted(REQUIRED_SCOPES),
            "provider_version": "rev-101",
            "data": [
                {
                    "id": "gcal-1",
                    "title": "Client Onboarding Kickoff",
                    "scheduled_at": "2026-09-10T15:00:00Z",
                    "description": "Agenda: Scope alignment",
                    "attendees": [{"name": "Lead", "agency_role": "facilitator"}],
                },
                {
                    "id": "gcal-2",
                    "title": "Weekly Retainer Check-in",
                    "scheduled_at": "2026-09-11T16:00:00Z",
                    "attendees": [{"name": "Account Dri", "agency_role": "client_lead"}],
                },
            ],
        }

        # 1. Sync first page
        res1 = self.service.sync(self.org_a, "acc-agency-1", "primary", calendar_feed)
        self.assertEqual(res1["status"], "ok")
        self.assertEqual(res1["imported"], 2)
        self.assertEqual(res1["duplicates"], 0)
        self.assertEqual(res1["cursor_after"], "rev-101")

        # 2. Replay with identical cursor is no-op
        res_replay = self.service.sync(
            self.org_a, "acc-agency-1", "primary", calendar_feed, cursor=res1["cursor_after"]
        )
        self.assertEqual(res_replay["imported"], 0)

        # 3. Provider fence verification fails if scopes missing or accounts mismatch
        bad_feed = {**calendar_feed, "account_id": "acc-other"}
        with self.assertRaises(AuthorizationError):
            self.service.sync(self.org_a, "acc-agency-1", "primary", bad_feed)

        bad_scopes = {**calendar_feed, "scopes": []}
        with self.assertRaises(ValidationError):
            self.service.sync(self.org_a, "acc-agency-1", "primary", bad_scopes)


if __name__ == "__main__":
    unittest.main()
