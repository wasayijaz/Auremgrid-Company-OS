from __future__ import annotations

import unittest
from datetime import datetime, timezone

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.understanding.pipeline import EXTRACTOR_VERSION, extract


class UnderstandingPipelineTests(unittest.TestCase):
    def test_extracts_typed_proposals_with_exact_spans(self) -> None:
        text = "\n".join(
            [
                "FACT: Client Alpha | pricing | 199 USD.",
                "DECISION: Launch | approved red variant.",
                "COMMITMENT: Launch | Maya | send assets Friday.",
                "PREFERENCE: Client Alpha | prefers concise copy.",
                "REQUEST: Client Alpha | please send a launch plan.",
                "METRIC: Launch | conversion rate | 12%.",
                "RISK: Launch | approval may slip.",
                "OPPORTUNITY: Client Alpha | upsell reporting package.",
                "ENTITY: Maya Patel | person.",
                "REL: Maya Patel | manages | Launch.",
            ]
        )
        proposals = extract(text, source_ref="source_1")
        kinds = {proposal.kind for proposal in proposals}
        self.assertEqual(
            kinds,
            {
                "commitment",
                "decision",
                "entity",
                "fact",
                "metric",
                "opportunity",
                "preference",
                "relationship",
                "request",
                "risk",
            },
        )
        for proposal in proposals:
            evidence = proposal.evidence
            self.assertEqual(evidence["text"], text[evidence["start"]:evidence["end"]])
            self.assertGreaterEqual(proposal.confidence, 0)
            self.assertLessEqual(proposal.confidence, 1)
            self.assertEqual(proposal.extractor_version, EXTRACTOR_VERSION)
            self.assertEqual(proposal.status, "proposed")

    def test_natural_language_heuristics_are_deterministic(self) -> None:
        text = "We decided to use the shorter onboarding flow. Revenue is $12000. Risk: approval may slip."
        first = extract(text, source_ref="source_1")
        second = extract(text, source_ref="source_1")
        self.assertEqual(first, second)
        self.assertIn("decision", {proposal.kind for proposal in first})
        self.assertIn("metric", {proposal.kind for proposal in first})
        self.assertIn("risk", {proposal.kind for proposal in first})


class UnderstandingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner")
        self.other_org = self.os.create_organization("Other")
        self.other_person = self.os.create_person(self.other_org.id, "Other Owner", role="owner")

    def tearDown(self) -> None:
        self.os.close()

    def test_record_list_promote_lifecycle_with_audit(self) -> None:
        recorded = self.os.understanding.record_source(
            self.org.id,
            self.person.id,
            "meeting_note",
            "FACT: Client Alpha | pricing | 199 USD. DECISION: Launch | approve red variant.",
            datetime.now(timezone.utc),
        )
        self.assertEqual(recorded["source"]["workspace_id"], None)
        self.assertGreaterEqual(len(recorded["proposals"]), 2)

        facts = self.os.understanding.list_proposals(self.org.id, self.person.id, status="proposed", kind="fact")
        self.assertEqual(len(facts), 1)
        reviewed = self.os.understanding.promote(self.org.id, self.person.id, facts[0]["id"], "confirmed")
        self.assertEqual(reviewed["status"], "confirmed")

        events = self.os.store.conn.execute(
            "SELECT * FROM understanding_proposal_events WHERE proposal_id=?",
            (facts[0]["id"],),
        ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reviewer_person_id"], self.person.id)
        self.assertEqual(events[0]["from_status"], "proposed")
        self.assertEqual(events[0]["to_status"], "confirmed")
        with self.assertRaises(ValidationError):
            self.os.understanding.promote(self.org.id, self.person.id, facts[0]["id"], "rejected")

    def test_rejected_transition_is_audited(self) -> None:
        recorded = self.os.understanding.record_source(
            self.org.id,
            self.person.id,
            "chat",
            "REQUEST: Client Alpha | please send a launch plan.",
            datetime.now(timezone.utc),
        )
        proposal_id = recorded["proposals"][0]["id"]
        reviewed = self.os.understanding.promote(self.org.id, self.person.id, proposal_id, "rejected")
        self.assertEqual(reviewed["status"], "rejected")
        event = self.os.store.conn.execute(
            "SELECT to_status FROM understanding_proposal_events WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        self.assertEqual(event["to_status"], "rejected")

    def test_org_isolation(self) -> None:
        recorded = self.os.understanding.record_source(
            self.org.id,
            self.person.id,
            "meeting_note",
            "FACT: Client Alpha | pricing | 199 USD.",
            datetime.now(timezone.utc),
        )
        self.assertEqual(len(self.os.understanding.list_proposals(self.org.id, self.person.id)), 1)
        self.assertEqual(len(self.os.understanding.list_proposals(self.other_org.id, self.other_person.id)), 0)
        with self.assertRaises(AuthorizationError):
            self.os.understanding.list_proposals(self.org.id, self.other_person.id)
        with self.assertRaises(AuthorizationError):
            self.os.understanding.promote(self.org.id, self.other_person.id, recorded["proposals"][0]["id"], "confirmed")

    def test_validation(self) -> None:
        with self.assertRaises(ValidationError):
            self.os.understanding.record_source(self.org.id, self.person.id, "meeting", " ", datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
