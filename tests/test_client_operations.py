from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS


class ClientOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.client = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.other = self.os.create_organization_workspace(self.org.id, "BASE", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.client.id, self.owner.id, "admin")

    def tearDown(self) -> None:
        self.os.close()

    def test_meeting_transcript_becomes_signal_not_canonical_fact(self) -> None:
        meeting = self.os.client_ops.create_meeting(
            self.org.id, self.client.id, self.owner.id, "Weekly call", datetime.now(timezone.utc),
            transcript="The price might become 250 next month.",
        )
        signals = self.os.client_ops.list_signals(self.org.id, self.client.id, self.owner.id)
        self.assertEqual(signals[0]["source_id"], meeting.id)
        self.assertEqual(signals[0]["status"], "new")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 0)

    def test_meeting_output_route_is_proposed_without_work_creation(self) -> None:
        agent = self.os.agent_ops.seed_primary_agents(self.org.id, self.owner.id)[0]
        self.os.agent_ops.configure_agent(self.org.id, self.owner.id, agent["id"], "test", [], [self.client.id], [])
        meeting = self.os.client_ops.create_meeting(self.org.id, self.client.id, self.owner.id, "Weekly", datetime.now(timezone.utc))
        before = self.os.store.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
        output = self.os.client_ops.add_meeting_output(self.org.id, self.client.id, self.owner.id, meeting.id, "action_item", "Draft recap", 0.8, proposed_targets=[{"type": "agent", "id": agent["id"]}])
        self.assertEqual(output["proposed_routes"][0]["status"], "proposed")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0], before)

    def test_signal_routes_once_and_preserves_evidence(self) -> None:
        signal = self.os.client_ops.create_signal(
            self.org.id, self.client.id, self.owner.id, "risk", "manual", "Client expressed concern"
        )
        result = self.os.client_ops.route_signal(self.org.id, self.client.id, self.owner.id, signal.id, "risk")
        self.assertIn("risk_id", result)
        self.assertEqual(self.os.client_ops.list_risks(self.org.id, self.client.id, self.owner.id)[0]["evidence"], "Client expressed concern")
        with self.assertRaises(ValidationError):
            self.os.client_ops.route_signal(self.org.id, self.client.id, self.owner.id, signal.id, "risk")

    def test_scope_overage_creates_risk_and_opportunity(self) -> None:
        contract = self.os.client_ops.create_contract(
            self.org.id, self.client.id, self.owner.id, "retainer", "monthly", "2026-08-01", 5000
        )
        allowance = self.os.client_ops.add_scope_allowance(
            self.org.id, self.client.id, self.owner.id, contract["id"], "creative", "monthly", included_quantity=12
        )
        usage = self.os.client_ops.record_scope_usage(
            self.org.id, self.client.id, self.owner.id, contract["id"], allowance["id"], "2026-08-01", 16, 4, 3
        )
        self.assertAlmostEqual(usage["usage_percent"], 191.666, places=2)
        self.assertEqual(self.os.client_ops.list_risks(self.org.id, self.client.id, self.owner.id)[0]["type"], "scope")
        self.assertEqual(self.os.store.conn.execute("SELECT type FROM opportunities").fetchone()[0], "scope_expansion")

    def test_unanswered_message_reduces_explainable_health(self) -> None:
        conversation = self.os.client_ops.create_conversation(
            self.org.id, self.client.id, self.owner.id, "gmail", "email", "Need approval"
        )
        self.os.client_ops.add_message(
            self.org.id, self.client.id, self.owner.id, conversation.id, "contact", "contact_1",
            "Can you send the revision?", datetime.now(timezone.utc) - timedelta(days=5), requires_reply=True, important=True,
        )
        health = self.os.client_ops.calculate_health(self.org.id, self.client.id, self.owner.id)
        self.assertLess(health.communication, 100)
        self.assertIn("unanswered client messages", health.explanation)
        self.assertIsNone(health.finance)
        self.assertIsNone(health.performance)

    def test_client_ops_do_not_leak_without_membership(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.client_ops.list_signals(self.org.id, self.other.id, self.owner.id)


if __name__ == "__main__":
    unittest.main()
