"""Tests for structured client-meeting-taker notes service."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.calendar_connector import CalendarConnectorService, MeetingAttendee
from auremgrid.services.meeting_notes import MeetingNotesService


class MeetingNotesServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_meetings.sqlite"
        self.current_time = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)
        self.clock = lambda: self.current_time

        self.calendar = CalendarConnectorService(db_path=self.db_path, clock=self.clock)
        self.service = MeetingNotesService(calendar_service=self.calendar, clock=self.clock)

        self.org_a = "org_growth_collective"
        self.org_b = "org_competing_agency"
        self.ws_client_1 = "ws_client_lumina"
        self.ws_client_2 = "ws_client_apex"

        # Create baseline calendar meetings in Org A
        self.meeting_1 = self.calendar.create_meeting(
            organization_id=self.org_a,
            workspace_id=self.ws_client_1,
            title="Q3 Strategy & Creative Review",
            scheduled_at="2026-09-07T14:00:00Z",
            attendees=[
                MeetingAttendee(name="Alice Lead", agency_role="facilitator", person_id="p_alice"),
                MeetingAttendee(name="Bob Strategist", agency_role="strategist", person_id="p_bob"),
                MeetingAttendee(name="Carol Client", agency_role="client_lead", person_id="p_carol"),
            ],
            agenda="1. Attribution 2. Creative refresh 3. Q4 budgets",
        )

        self.meeting_2 = self.calendar.create_meeting(
            organization_id=self.org_a,
            workspace_id=self.ws_client_2,
            title="Apex Paid Search Kickoff",
            scheduled_at="2026-09-08T10:00:00Z",
            attendees=[
                MeetingAttendee(name="Dave Buyer", agency_role="media_buyer", person_id="p_dave"),
                MeetingAttendee(name="Emma Client", agency_role="client_contact", person_id="p_emma"),
            ],
            agenda="Google PMax campaign setup",
        )

        # Create calendar meeting in Org B
        self.meeting_b = self.calendar.create_meeting(
            organization_id=self.org_b,
            title="Private Org B Strategy",
            scheduled_at="2026-09-07T16:00:00Z",
            attendees=[MeetingAttendee(name="Exec B", agency_role="facilitator", person_id="p_exec_b")],
        )

    def tearDown(self) -> None:
        self.service.close()
        self.calendar.close()
        self.temp_dir.cleanup()

    def advance_time(self, seconds: int = 3600) -> None:
        self.current_time = datetime.fromtimestamp(
            self.current_time.timestamp() + seconds, tz=timezone.utc
        )

    def test_record_meeting_notes_success_and_structure(self) -> None:
        self.advance_time(1800)
        note = self.service.record_meeting_notes(
            organization_id=self.org_a,
            calendar_event_id=self.meeting_1["id"],
            summary="Client approved the Q4 budget expansion and greenlit 4 UGC creative variations.",
            attendees=["p_alice", "p_bob", "p_carol"],
            decisions=[
                "Shift $15,000 from GDN to Meta prospecting.",
                "Target ROAS adjusted to 3.2x for holiday season.",
            ],
            risks=[
                "Shopify inventory on top SKU may run low by November.",
            ],
            action_items=[
                {
                    "description": "Deliver 4 revised UGC cutdowns",
                    "owner_person_id": "p_bob",
                    "due_date": "2026-09-12T17:00:00Z",
                },
                {
                    "description": "Set up Meta campaign budget reallocation",
                    "owner_person_id": "p_alice",
                    "due_date": "2026-09-09T12:00:00Z",
                },
            ],
            raw_notes="Detailed scratchpad notes: Client expressed satisfaction with weekly report.",
        )

        self.assertEqual(note["organization_id"], self.org_a)
        self.assertEqual(note["calendar_event_id"], self.meeting_1["id"])
        self.assertEqual(note["workspace_id"], self.ws_client_1)
        self.assertEqual(note["title"], "Q3 Strategy & Creative Review")
        self.assertIn("Client approved the Q4 budget", note["summary"])
        self.assertEqual(len(note["attendees"]), 3)
        self.assertEqual(len(note["decisions"]), 2)
        self.assertEqual(len(note["risks"]), 1)
        self.assertEqual(len(note["action_items"]), 2)

        descriptions = {a["description"]: a for a in note["action_items"]}
        self.assertIn("Deliver 4 revised UGC cutdowns", descriptions)
        self.assertIn("Set up Meta campaign budget reallocation", descriptions)
        bob_act = descriptions["Deliver 4 revised UGC cutdowns"]
        self.assertEqual(bob_act["owner_person_id"], "p_bob")
        self.assertEqual(bob_act["due_date"], "2026-09-12T17:00:00Z")
        self.assertEqual(bob_act["status"], "open")

        # Verify calendar connector meeting lifecycle was synchronized
        cal_m = self.calendar.get_meeting(self.org_a, self.meeting_1["id"])
        self.assertIn(cal_m["status"], ("held", "notes_captured"))

    def test_record_meeting_notes_validation_and_fencing(self) -> None:
        # 1. Non-existent calendar event raises NotFoundError
        with self.assertRaises(NotFoundError):
            self.service.record_meeting_notes(
                organization_id=self.org_a,
                calendar_event_id="cal_nonexistent_999",
                summary="Ghost meeting notes",
            )

        # 2. Calendar event belonging to Org B cannot be noted by Org A (AuthorizationError)
        with self.assertRaises(AuthorizationError):
            self.service.record_meeting_notes(
                organization_id=self.org_a,
                calendar_event_id=self.meeting_b["id"],
                summary="Unauthorized access attempt",
            )

        # 3. Missing summary raises ValidationError
        with self.assertRaises(ValidationError):
            self.service.record_meeting_notes(
                organization_id=self.org_a,
                calendar_event_id=self.meeting_1["id"],
                summary="",
            )

        # 4. Malformed action item missing owner raises ValidationError
        with self.assertRaises(ValidationError):
            self.service.record_meeting_notes(
                organization_id=self.org_a,
                calendar_event_id=self.meeting_1["id"],
                summary="Valid summary",
                action_items=[{"description": "Missing owner", "due_date": "2026-09-10T00:00:00Z"}],
            )

    def test_list_meeting_notes_with_filters(self) -> None:
        # Record note for meeting 1 (client 1, 2026-09-07)
        self.service.record_meeting_notes(
            organization_id=self.org_a,
            calendar_event_id=self.meeting_1["id"],
            summary="Lumina meeting notes",
            attendees=["p_alice", "p_carol"],
            meeting_date="2026-09-07T14:00:00Z",
        )

        # Record note for meeting 2 (client 2, 2026-09-08)
        self.service.record_meeting_notes(
            organization_id=self.org_a,
            calendar_event_id=self.meeting_2["id"],
            summary="Apex meeting notes",
            attendees=["p_dave", "p_emma"],
            meeting_date="2026-09-08T10:00:00Z",
        )

        # List all for Org A
        all_notes = self.service.list_meeting_notes(self.org_a)
        self.assertEqual(len(all_notes), 2)

        # Filter by client/workspace
        client_1_notes = self.service.list_meeting_notes(self.org_a, workspace_id=self.ws_client_1)
        self.assertEqual(len(client_1_notes), 1)
        self.assertEqual(client_1_notes[0]["summary"], "Lumina meeting notes")

        client_2_notes = self.service.list_meeting_notes(self.org_a, client_id=self.ws_client_2)
        self.assertEqual(len(client_2_notes), 1)
        self.assertEqual(client_2_notes[0]["summary"], "Apex meeting notes")

        # Filter by date range
        filtered_date = self.service.list_meeting_notes(
            self.org_a,
            date_from="2026-09-08T00:00:00Z",
            date_to="2026-09-08T23:59:59Z",
        )
        self.assertEqual(len(filtered_date), 1)
        self.assertEqual(filtered_date[0]["title"], "Apex Paid Search Kickoff")

        # Filter by attendee
        alice_notes = self.service.list_meeting_notes(self.org_a, attendee_person_id="p_alice")
        self.assertEqual(len(alice_notes), 1)
        dave_notes = self.service.list_meeting_notes(self.org_a, attendee_person_id="p_dave")
        self.assertEqual(len(dave_notes), 1)
        unknown_notes = self.service.list_meeting_notes(self.org_a, attendee_person_id="p_unknown")
        self.assertEqual(len(unknown_notes), 0)

        # Strict organization isolation: Org B sees 0 notes
        org_b_notes = self.service.list_meeting_notes(self.org_b)
        self.assertEqual(len(org_b_notes), 0)

    def test_action_item_surfacing_and_lifecycle_updates(self) -> None:
        self.service.record_meeting_notes(
            organization_id=self.org_a,
            calendar_event_id=self.meeting_1["id"],
            summary="Action item test notes",
            action_items=[
                {"description": "Action 1 for Alice", "owner_person_id": "p_alice", "due_date": "2026-09-09T10:00:00Z"},
                {"description": "Action 2 for Alice", "owner_person_id": "p_alice", "due_date": "2026-09-11T10:00:00Z"},
                {"description": "Action 3 for Bob", "owner_person_id": "p_bob", "due_date": "2026-09-10T10:00:00Z"},
            ],
        )

        # Surface open action items for Org A
        open_items = self.service.get_open_action_items(self.org_a)
        self.assertEqual(len(open_items), 3)
        self.assertEqual(open_items[0]["description"], "Action 1 for Alice")

        # Surface open action items for Alice only
        alice_items = self.service.get_open_action_items(self.org_a, owner_person_id="p_alice")
        self.assertEqual(len(alice_items), 2)

        # Complete Action 1
        act_1_id = alice_items[0]["id"]
        updated = self.service.update_action_item_status(self.org_a, act_1_id, "completed")
        self.assertEqual(updated["status"], "completed")

        # Verify completed item is no longer surfaced in open items
        open_alice_after = self.service.get_open_action_items(self.org_a, owner_person_id="p_alice")
        self.assertEqual(len(open_alice_after), 1)
        self.assertEqual(open_alice_after[0]["description"], "Action 2 for Alice")

        # Cross-org status update blocked
        with self.assertRaises(AuthorizationError):
            self.service.update_action_item_status(self.org_b, act_1_id, "open")

        # Org B sees 0 open items
        self.assertEqual(len(self.service.get_open_action_items(self.org_b)), 0)

    def test_meeting_notes_attendees_fallback_from_calendar_event(self) -> None:
        note = self.service.record_meeting_notes(
            organization_id=self.org_a,
            calendar_event_id=self.meeting_2["id"],
            summary="Apex sync with auto attendees",
        )
        self.assertEqual(note["attendees"], ["p_dave", "p_emma"])

    def test_action_item_status_validation_and_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.update_action_item_status(self.org_a, "nonexistent_action", "completed")
        with self.assertRaises(ValidationError):
            self.service.update_action_item_status(self.org_a, "nonexistent_action", "invalid_status")

    def test_open_action_items_filtered_by_workspace(self) -> None:
        self.service.record_meeting_notes(
            organization_id=self.org_a,
            calendar_event_id=self.meeting_1["id"],
            summary="Lumina note with action",
            workspace_id=self.ws_client_1,
            action_items=[{"description": "Lumina task", "owner_person_id": "p_alice", "due_date": "2026-09-10T00:00:00Z"}],
        )
        self.service.record_meeting_notes(
            organization_id=self.org_a,
            calendar_event_id=self.meeting_2["id"],
            summary="Apex note with action",
            workspace_id=self.ws_client_2,
            action_items=[{"description": "Apex task", "owner_person_id": "p_dave", "due_date": "2026-09-11T00:00:00Z"}],
        )

        lumina_items = self.service.get_open_action_items(self.org_a, workspace_id=self.ws_client_1)
        self.assertEqual(len(lumina_items), 1)
        self.assertEqual(lumina_items[0]["description"], "Lumina task")

        apex_items = self.service.get_open_action_items(self.org_a, workspace_id=self.ws_client_2)
        self.assertEqual(len(apex_items), 1)
        self.assertEqual(apex_items[0]["description"], "Apex task")

if __name__ == "__main__":
    unittest.main()
