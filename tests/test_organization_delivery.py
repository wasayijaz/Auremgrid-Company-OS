from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.storage.sqlite import SCHEMA


class OrganizationDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Auremgrid", "org_auremgrid")
        self.internal = self.os.create_organization_workspace(self.org.id, "Company Brain", "internal", "ws_internal")
        self.prime = self.os.create_organization_workspace(self.org.id, "Prime", "client", "ws_prime")
        self.base = self.os.create_organization_workspace(self.org.id, "BASE", "client", "ws_base")
        self.owner = self.os.create_person(self.org.id, "Wasay", "owner@example.test", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.internal.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.prime.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.base.id, self.owner.id, "admin")

    def tearDown(self) -> None:
        self.os.close()

    def test_one_person_spans_multiple_client_workspaces(self) -> None:
        workspaces = self.os.company.list_workspaces(self.org.id)
        self.assertEqual({item["id"] for item in workspaces}, {"ws_internal", "ws_prime", "ws_base"})
        self.assertEqual(self.os.company.workspace_membership("ws_prime", self.owner.id).person_id, self.owner.id)
        self.assertEqual(self.os.company.workspace_membership("ws_base", self.owner.id).person_id, self.owner.id)

    def test_workspace_membership_prevents_cross_client_inference(self) -> None:
        designer = self.os.create_person(self.org.id, "Designer")
        self.os.add_person_to_workspace(self.org.id, self.prime.id, designer.id, "operator")
        project = self.os.create_project(self.org.id, self.prime.id, designer.id, "Prime launch")
        self.assertEqual(self.os.list_projects(self.org.id, self.prime.id, designer.id)[0].id, project.id)
        with self.assertRaises(AuthorizationError):
            self.os.list_projects(self.org.id, self.base.id, designer.id)

    def test_project_deliverable_review_and_decision_vertical_slice(self) -> None:
        project = self.os.create_project(
            self.org.id, self.prime.id, self.owner.id, "Q4 Campaign", priority="high", tags=["paid-social"]
        )
        deliverable = self.os.create_deliverable(
            self.org.id, self.prime.id, self.owner.id, project.id, "Consultation creative", "ad_creative"
        )
        review = self.os.open_review(
            self.org.id, self.prime.id, self.owner.id, deliverable.id, reviewer_person_id=self.owner.id
        )
        closed = self.os.decide_review(self.org.id, self.prime.id, self.owner.id, review.id, "approved")
        decision = self.os.create_decision(
            self.org.id, self.owner.id, "Use consultation-room imagery", "It outperformed generic imagery",
            workspace_id=self.prime.id, project_id=project.id, evidence="creative review 1", tags=["creative"]
        )
        self.assertEqual(closed.status, "approved")
        self.assertEqual(self.os.company.list_decisions(self.org.id, self.prime.id)[0].id, decision.id)
        self.assertEqual(self.os.company.list_reviews(self.prime.id)[0].deliverable_id, deliverable.id)

    def test_delivery_records_cannot_link_across_workspaces(self) -> None:
        project = self.os.create_project(self.org.id, self.prime.id, self.owner.id, "Prime only")
        with self.assertRaises(NotFoundError):
            self.os.create_deliverable(
                self.org.id, self.base.id, self.owner.id, project.id, "Leak", "document"
            )

    def test_review_cannot_be_decided_twice(self) -> None:
        project = self.os.create_project(self.org.id, self.prime.id, self.owner.id, "Review rules")
        deliverable = self.os.create_deliverable(self.org.id, self.prime.id, self.owner.id, project.id, "Deck", "presentation")
        review = self.os.open_review(self.org.id, self.prime.id, self.owner.id, deliverable.id)
        self.os.decide_review(self.org.id, self.prime.id, self.owner.id, review.id, "revision_requested")
        with self.assertRaises(ValidationError):
            self.os.decide_review(self.org.id, self.prime.id, self.owner.id, review.id, "approved")


class MigrationTests(unittest.TestCase):
    def test_schema_is_versioned_and_persists_organization_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "company.sqlite"
            first = CompanyOS(path)
            self.assertGreaterEqual(first.store.schema_version, 2)
            first.create_organization("Auremgrid", "org_persisted")
            first.close()
            second = CompanyOS(path)
            self.assertEqual(second.company.get_organization("org_persisted").name, "Auremgrid")
            self.assertGreaterEqual(second.store.schema_version, 2)
            second.close()

    def test_legacy_v1_rows_migrate_forward_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"legacy.sqlite";conn=sqlite3.connect(path);conn.executescript(SCHEMA)
            conn.execute("INSERT INTO workspaces VALUES ('ws_legacy','Legacy Client','2026-01-01T00:00:00+00:00')")
            conn.execute("INSERT INTO actors VALUES ('act_legacy','ws_legacy','Legacy Admin','admin','2026-01-01T00:00:00+00:00')")
            conn.execute("""INSERT INTO work_items(id,workspace_id,title,request,requested_by,needed_by,status,assignee_id,playbook_id,decision_maker,definition_of_done,created_at,updated_at)
                VALUES ('work_legacy','ws_legacy','Legacy work','Preserve me','Client',NULL,'captured',NULL,NULL,NULL,'{}','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""");conn.commit();conn.close()
            os=CompanyOS(path);item=os.store.get_work_item("ws_legacy","work_legacy")
            self.assertEqual(os.store.schema_version,15);self.assertEqual(item.title,"Legacy work");self.assertEqual(item.priority,"normal");os.close()


if __name__ == "__main__":
    unittest.main()
