from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.request_intake import RequestIntakeService


class RequestIntakeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org-intake")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client", "ws-intake")
        self.person = self.os.create_person(self.org.id, "Client", role="client", person_id="person-intake")
        self.owner = self.os.create_person(self.org.id, "Worker", role="member", person_id="person-worker")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "client")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.service = RequestIntakeService(self.os)
        self.scope = {"organization_id": self.org.id, "workspace_id": self.ws.id, "person_id": self.person.id}
        self.staff_scope = {"organization_id": self.org.id, "workspace_id": self.ws.id, "person_id": self.owner.id}

    def tearDown(self) -> None:
        self.os.close()

    def test_full_pipeline_walk_and_status_transitions(self) -> None:
        created = self.service.create_request(self.scope, "campaign", "Launch spring campaign", "2026-10-01", priority="high")
        intake_id = created["intake"]["id"]
        signalled = self.service.create_signal(self.staff_scope, intake_id)
        self.assertEqual(signalled["status"], "signalled")
        work = self.service.create_work(self.staff_scope, intake_id)["work"]
        owned = self.service.assign_owner(self.staff_scope, work["id"], self.owner.id)
        self.assertEqual(owned["status"], "owned")
        self.assertEqual(self.service.transition(self.staff_scope, work["id"], "in_progress")["status"], "in_progress")
        status = self.service.pipeline_status(self.staff_scope, intake_id)
        self.assertEqual(status["status"], "in_progress")
        self.assertEqual(status["work"]["assignee_person_id"], self.owner.id)

    def test_validation_rejects_missing_type_goal_or_deadline(self) -> None:
        for values in (("", "goal", "2026-10-01"), ("type", "", "2026-10-01"), ("type", "goal", "")):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.service.create_request(self.scope, *values)

    def test_attachments_and_references_are_captured(self) -> None:
        created = self.service.create_request(self.scope, "design", "Approve landing page", "2026-10-02", attachments=["brief.pdf"], references=["campaign-1"], priority="urgent")
        metadata = created["metadata"]
        self.assertEqual(metadata["attachments"], ["brief.pdf"])
        self.assertEqual(metadata["references"], ["campaign-1"])
        self.assertEqual(metadata["priority"], "urgent")

    def test_fencing_rejects_other_organization(self) -> None:
        other = self.os.create_organization("Other", "org-other-intake")
        person = self.os.create_person(other.id, "Other", role="owner", person_id="person-other-intake")
        with self.assertRaises(AuthorizationError):
            self.service.create_request({"organization_id": other.id, "workspace_id": self.ws.id, "person_id": person.id}, "type", "goal", "2026-10-01")

    def test_pipeline_requires_signal_before_work(self) -> None:
        intake_id = self.service.create_request(self.scope, "content", "Write brief", "2026-10-03")["intake"]["id"]
        with self.assertRaises(ValidationError):
            self.service.create_work(self.staff_scope, intake_id)


if __name__ == "__main__":
    unittest.main()
