from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS


class P10P12CompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.client = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.client.id, self.owner.id, "admin")

    def tearDown(self) -> None:
        self.os.close()

    def test_explain_health_is_read_only_and_returns_component_evidence(self) -> None:
        work = self.os.work_ops.create(
            self.org.id,
            self.client.id,
            self.owner.id,
            "Late launch",
            "Ship the overdue launch",
            "Client",
            deadline="2020-01-01",
        )
        conversation = self.os.client_ops.create_conversation(
            self.org.id, self.client.id, self.owner.id, "gmail", "email", "Need response"
        )
        message = self.os.client_ops.add_message(
            self.org.id,
            self.client.id,
            self.owner.id,
            conversation.id,
            "contact",
            "contact_1",
            "Can you confirm the new date?",
            datetime.now(timezone.utc) - timedelta(days=3),
            requires_reply=True,
        )
        risk = self.os.client_ops.create_risk(
            self.org.id,
            self.client.id,
            self.owner.id,
            "delivery",
            "high",
            0.7,
            "Launch timeline is exposed",
            "Client asked for a missed date",
            "Replan the launch",
        )

        before = self.os.store.conn.execute("SELECT COUNT(*) FROM client_health_snapshots").fetchone()[0]
        explained = self.os.client_ops.explain_health(self.org.id, self.client.id, self.owner.id)
        after = self.os.store.conn.execute("SELECT COUNT(*) FROM client_health_snapshots").fetchone()[0]

        self.assertEqual(before, after)
        self.assertEqual(explained["components"]["delivery"]["evidence_refs"], [{"table": "work_items", "id": work.id}])
        self.assertEqual(explained["components"]["communication"]["evidence_refs"], [{"table": "messages", "id": message.id}])
        self.assertEqual(explained["components"]["relationship"]["evidence_refs"], [{"table": "risks", "id": risk.id}])
        self.assertIn("overdue work items", explained["explanation"])
        self.assertLess(explained["overall"], 100)

        snapshot = self.os.client_ops.calculate_health(self.org.id, self.client.id, self.owner.id)
        self.assertEqual(snapshot.overall, explained["overall"])
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM client_health_snapshots").fetchone()[0], before + 1)

    def test_risk_and_opportunity_lifecycle_events_are_append_only(self) -> None:
        risk = self.os.client_ops.create_risk(
            self.org.id,
            self.client.id,
            self.owner.id,
            "relationship",
            "medium",
            0.5,
            "Stakeholder concern needs follow-up",
            "Call notes",
            "Assign account lead",
        )
        resolved = self.os.client_ops.resolve_risk(
            self.org.id, self.client.id, self.owner.id, risk.id, "Account lead replied and logged next steps"
        )
        self.assertEqual(resolved["status"], "resolved")
        reopened = self.os.client_ops.reopen_risk(
            self.org.id, self.client.id, self.owner.id, risk.id, "Client reopened the concern"
        )
        self.assertEqual(reopened["status"], "open")
        self.assertEqual([event["action"] for event in reopened["events"]], ["created", "resolved", "reopened"])

        opportunity = self.os.client_ops.create_opportunity(
            self.org.id,
            self.client.id,
            self.owner.id,
            "upsell",
            "Client requested more landing pages",
            "Meeting output",
            "Prepare expanded scope",
            2500,
        )
        advanced = self.os.client_ops.advance_opportunity(
            self.org.id, self.client.id, self.owner.id, opportunity.id, "qualified", "Budget owner confirmed interest"
        )
        self.assertEqual(advanced["status"], "qualified")
        closed = self.os.client_ops.close_opportunity(
            self.org.id, self.client.id, self.owner.id, opportunity.id, "won", "Change order accepted"
        )
        self.assertEqual(closed["status"], "won")
        self.assertEqual([event["action"] for event in closed["events"]], ["created", "advanced", "closed"])

        risk_event_id = reopened["events"][0]["id"]
        with self.assertRaises(Exception):
            self.os.store.conn.execute("UPDATE risk_events SET note='mutated' WHERE id=?", (risk_event_id,))
        self.os.store.conn.rollback()
        opportunity_event_id = closed["events"][0]["id"]
        with self.assertRaises(Exception):
            self.os.store.conn.execute("DELETE FROM opportunity_events WHERE id=?", (opportunity_event_id,))
        self.os.store.conn.rollback()

    def test_scope_status_reports_states_history_and_generated_links(self) -> None:
        self.assertEqual(
            self.os.client_ops.scope_status(self.org.id, self.client.id, self.owner.id)["status"],
            "no_contract",
        )
        contract = self.os.client_ops.create_contract(
            self.org.id, self.client.id, self.owner.id, "retainer", "monthly", "2026-08-01", 5000
        )
        allowance = self.os.client_ops.add_scope_allowance(
            self.org.id,
            self.client.id,
            self.owner.id,
            contract["id"],
            "creative",
            "monthly",
            included_quantity=12,
        )
        self.assertEqual(
            self.os.client_ops.scope_status(self.org.id, self.client.id, self.owner.id)["status"],
            "no_usage",
        )

        usage = self.os.client_ops.record_scope_usage(
            self.org.id, self.client.id, self.owner.id, contract["id"], allowance["id"], "2026-08-01", 16, 4, 3
        )
        status = self.os.client_ops.scope_status(self.org.id, self.client.id, self.owner.id)
        row = status["allowances"][0]

        self.assertEqual(status["status"], "over_scope")
        self.assertEqual(row["latest_usage"]["id"], usage["id"])
        self.assertEqual(row["latest_usage"]["basis"], "quantity")
        self.assertEqual(row["period_history"][0]["period_start"], "2026-08-01")
        self.assertTrue(row["generated"]["risk_ids"])
        self.assertTrue(row["generated"]["opportunity_ids"])

    def test_lifecycle_methods_reject_invalid_transitions(self) -> None:
        risk = self.os.client_ops.create_risk(
            self.org.id, self.client.id, self.owner.id, "delivery", "low", 0.2, "Minor delay", "Timeline", "Watch"
        )
        with self.assertRaises(ValidationError):
            self.os.client_ops.reopen_risk(self.org.id, self.client.id, self.owner.id, risk.id, "already open")

        opportunity = self.os.client_ops.create_opportunity(
            self.org.id, self.client.id, self.owner.id, "retention", "Renewal possible", "QBR", "Send plan"
        )
        with self.assertRaises(ValidationError):
            self.os.client_ops.advance_opportunity(
                self.org.id, self.client.id, self.owner.id, opportunity.id, "won", "use close instead"
            )
        self.os.client_ops.close_opportunity(
            self.org.id, self.client.id, self.owner.id, opportunity.id, "lost", "Client declined"
        )
        with self.assertRaises(ValidationError):
            self.os.client_ops.advance_opportunity(
                self.org.id, self.client.id, self.owner.id, opportunity.id, "qualified", "too late"
            )


if __name__ == "__main__":
    unittest.main()
