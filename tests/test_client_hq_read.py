from __future__ import annotations

import unittest
from datetime import datetime, timezone

from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.client_hq_read import ClientHQReadService


class ClientHQReadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org-hq")
        self.ws = self.os.create_organization_workspace(self.org.id, "Acme", "client", "client-hq")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner", person_id="person-hq")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.service = ClientHQReadService(self.os)

    def tearDown(self) -> None:
        self.os.close()

    def test_get_hq_composes_retainer_health_work_deliverables_reports_and_calendar(self) -> None:
        self.os.work_ops.create(self.org.id, self.ws.id, self.person.id, "Open task", "request", "person-hq", deadline="2026-09-10")
        view = self.service.get_client_hq({"organization_id": self.org.id, "workspace_id": self.ws.id, "person_id": self.person.id}, self.ws.id)
        self.assertEqual(view["client_name"], "Acme")
        self.assertIn("retainer", view)
        self.assertEqual(len(view["open_work_items"]), 1)
        self.assertIn("upcoming_meetings", view)
        self.assertEqual(view["organization_id"], self.org.id)

    def test_summaries_are_deterministic_and_org_scoped(self) -> None:
        other = self.os.create_organization("Other", "org-other")
        self.os.create_organization_workspace(other.id, "Hidden", "client", "client-hidden")
        summaries = self.service.list_client_hq_summaries({"organization_id": self.org.id, "person_id": self.person.id})
        self.assertEqual([item["client_id"] for item in summaries], [self.ws.id])
        self.assertEqual(summaries[0]["open_work_items_count"], 0)

    def test_cross_workspace_and_unknown_client_fail_closed(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.service.get_client_hq({"organization_id": self.org.id, "workspace_id": "other", "person_id": self.person.id}, self.ws.id)
        with self.assertRaises(Exception):
            self.service.get_client_hq({"organization_id": self.org.id, "workspace_id": "missing", "person_id": self.person.id}, "missing")

    def test_cross_organization_access_is_refused(self) -> None:
        other_org = self.os.create_organization("Other Agency", "org-other")
        other_person = self.os.create_person(other_org.id, "Other Owner", role="owner", person_id="person-other")
        with self.assertRaises(AuthorizationError):
            self.service.get_client_hq(
                {"organization_id": other_org.id, "workspace_id": self.ws.id, "person_id": other_person.id},
                self.ws.id,
            )


if __name__ == "__main__":
    unittest.main()
