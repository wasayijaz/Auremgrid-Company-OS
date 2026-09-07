from __future__ import annotations

import unittest

from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.proposal_extraction import ProposalExtractionService


class ProposalExtractionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org-proposal-extraction")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client", "ws-proposal-extraction")
        self.person = self.os.create_person(self.org.id, "Client", role="client", person_id="person-proposal-extraction")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "client")
        self.service = ProposalExtractionService(self.os)
        self.scope = {"organization_id": self.org.id, "workspace_id": self.ws.id, "person_id": self.person.id}

    def tearDown(self) -> None:
        self.os.close()

    def test_explicit_iso_date_is_extracted(self) -> None:
        draft = self.service.draft_from_text(
            self.scope,
            "Please build a reporting dashboard by 2026-09-20. Reference https://example.test/brief.",
        )
        self.assertEqual(draft["deadline"], "2026-09-20")
        self.assertEqual(draft["confidence"]["deadline"], "high")
        self.assertEqual(draft["references"], ["https://example.test/brief"])

    def test_relative_end_of_month_is_extracted(self) -> None:
        draft = self.service.draft_from_text(self.scope, "Create campaign analytics by end of month.")
        self.assertEqual(draft["deadline"], "2026-09-30")
        self.assertEqual(draft["confidence"]["deadline"], "medium")

    def test_urgent_keyword_sets_priority_and_attachment_is_captured(self) -> None:
        draft = self.service.draft_from_text(
            self.scope,
            "ASAP design the launch creative. Attached brand brief.pdf and https://example.test/mockup.png",
        )
        self.assertEqual(draft["priority"], "urgent")
        self.assertIn("brief.pdf", draft["attachments"])
        self.assertIn("https://example.test/mockup.png", draft["attachments"])

    def test_unknown_deadline_lands_in_unknowns_with_none_deadline(self) -> None:
        draft = self.service.draft_from_text(self.scope, "Write social content for our launch.")
        self.assertIsNone(draft["deadline"])
        self.assertIn("deadline", draft["unknowns"])
        self.assertEqual(draft["confidence"]["deadline"], "low")

    def test_empty_text_raises_validation_error(self) -> None:
        with self.assertRaisesRegex(ValidationError, "proposal text is required"):
            self.service.draft_from_text(self.scope, "  ")

    def test_classifies_design_request(self) -> None:
        draft = self.service.draft_from_text(self.scope, "Please design a new logo for September 20.")
        self.assertEqual(draft["request_type"], "design")

    def test_classifies_campaign_request(self) -> None:
        draft = self.service.draft_from_text(self.scope, "Launch a paid media campaign tomorrow for the fall offer.")
        self.assertEqual(draft["request_type"], "campaign")


if __name__ == "__main__":
    unittest.main()
