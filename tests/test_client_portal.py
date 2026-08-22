from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class ClientPortalOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.other_ws = self.os.create_organization_workspace(self.org.id, "Base", "client")
        self.staff = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.staff.id, "admin")
        self.client_person = self.os.create_person(self.org.id, "Client Rep", role="client")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.client_person.id, "client")

    def tearDown(self) -> None:
        self.os.close()

    def test_client_role_requires_a_client_workspace(self) -> None:
        internal_ws = self.os.create_organization_workspace(self.org.id, "Internal", "internal")
        with self.assertRaises(ValidationError):
            self.os.add_person_to_workspace(self.org.id, internal_ws.id, self.client_person.id, "client")

    def test_client_can_submit_intake_and_staff_accepts_into_canonical_work(self) -> None:
        item = self.os.client_portal.submit_intake_request(
            self.org.id, self.ws.id, self.client_person.id, "New landing page", "Need a page for launch",
        )
        self.assertEqual(item["status"], "pending")
        queue = self.os.client_portal.list_intake_queue(self.org.id, self.ws.id, self.staff.id)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["id"], item["id"])

        result = self.os.client_portal.accept_intake_request(
            self.org.id, self.ws.id, self.staff.id, item["id"],
        )
        self.assertEqual(result["status"], "accepted")
        work_item = self.os.store.get_work_item(self.ws.id, result["work_item_id"])
        self.assertIsNotNone(work_item)
        self.assertEqual(work_item.title, "New landing page")
        self.assertEqual(work_item.status, "captured")
        self.assertEqual(work_item.requested_by, self.client_person.id)
        # The intake queue for pending items is now empty.
        self.assertEqual(self.os.client_portal.list_intake_queue(self.org.id, self.ws.id, self.staff.id), [])
        rows = self.os.store.conn.execute(
            """SELECT action,entity_type,entity_id,principal_id FROM ledger_audit
               WHERE entity_type='client_intake_request' ORDER BY rowid"""
        ).fetchall()
        self.assertEqual(
            [(row["action"], row["entity_id"], row["principal_id"]) for row in rows],
            [("create", item["id"], self.client_person.id), ("accept", item["id"], self.staff.id)],
        )

    def test_double_decision_on_intake_request_is_rejected(self) -> None:
        item = self.os.client_portal.submit_intake_request(
            self.org.id, self.ws.id, self.client_person.id, "Title", "Request",
        )
        self.os.client_portal.accept_intake_request(self.org.id, self.ws.id, self.staff.id, item["id"])
        with self.assertRaises(ValidationError):
            self.os.client_portal.accept_intake_request(self.org.id, self.ws.id, self.staff.id, item["id"])
        with self.assertRaises(ValidationError):
            self.os.client_portal.decline_intake_request(self.org.id, self.ws.id, self.staff.id, item["id"])

    def test_staff_can_decline_intake_request_with_note(self) -> None:
        item = self.os.client_portal.submit_intake_request(
            self.org.id, self.ws.id, self.client_person.id, "Title", "Request",
        )
        result = self.os.client_portal.decline_intake_request(
            self.org.id, self.ws.id, self.staff.id, item["id"], note="Out of scope for this retainer",
        )
        self.assertEqual(result["status"], "declined")
        rows = self.os.client_portal.list_intake_requests(self.org.id, self.ws.id, self.client_person.id)
        self.assertEqual(rows[0]["status"], "declined")
        self.assertEqual(rows[0]["decision_note"], "Out of scope for this retainer")
        audit = self.os.store.conn.execute(
            """SELECT action,entity_type,principal_id,detail FROM ledger_audit
               WHERE entity_type='client_intake_request' ORDER BY rowid DESC LIMIT 1"""
        ).fetchone()
        self.assertEqual((audit["action"], audit["principal_id"], audit["detail"]), ("decline", self.staff.id, "Out of scope for this retainer"))

    def test_client_cannot_submit_intake_into_another_workspace(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.client_portal.submit_intake_request(
                self.org.id, self.other_ws.id, self.client_person.id, "Title", "Request",
            )

    def test_staff_person_without_client_role_cannot_submit_intake(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.client_portal.submit_intake_request(
                self.org.id, self.ws.id, self.staff.id, "Title", "Request",
            )

    def test_client_lists_only_their_own_intake_requests(self) -> None:
        other_client = self.os.create_person(self.org.id, "Other Client", role="client")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, other_client.id, "client")
        own = self.os.client_portal.submit_intake_request(
            self.org.id, self.ws.id, self.client_person.id, "Own", "Visible",
        )
        self.os.client_portal.submit_intake_request(
            self.org.id, self.ws.id, other_client.id, "Other", "Hidden",
        )

        rows = self.os.client_portal.list_intake_requests(self.org.id, self.ws.id, self.client_person.id)
        self.assertEqual([row["id"] for row in rows], [own["id"]])

    def test_client_cannot_read_staff_queue_or_decide_intake(self) -> None:
        item = self.os.client_portal.submit_intake_request(
            self.org.id, self.ws.id, self.client_person.id, "Title", "Request",
        )
        with self.assertRaises(AuthorizationError):
            self.os.client_portal.list_intake_queue(self.org.id, self.ws.id, self.client_person.id)
        with self.assertRaises(AuthorizationError):
            self.os.client_portal.accept_intake_request(self.org.id, self.ws.id, self.client_person.id, item["id"])
        with self.assertRaises(AuthorizationError):
            self.os.client_portal.decline_intake_request(self.org.id, self.ws.id, self.client_person.id, item["id"])

    def test_accept_intake_validates_workspace_assignee_and_decision_maker(self) -> None:
        other_staff = self.os.create_person(self.org.id, "Other Staff", role="member")
        self.os.add_person_to_workspace(self.org.id, self.other_ws.id, other_staff.id, "operator")
        item = self.os.client_portal.submit_intake_request(
            self.org.id, self.ws.id, self.client_person.id, "Title", "Request",
        )

        with self.assertRaises(AuthorizationError):
            self.os.client_portal.accept_intake_request(
                self.org.id, self.ws.id, self.staff.id, item["id"], assignee_id="actor_missing",
            )
        with self.assertRaises(AuthorizationError):
            self.os.client_portal.accept_intake_request(
                self.org.id, self.ws.id, self.staff.id, item["id"], decision_maker=other_staff.id,
            )

    def test_missing_intake_request_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.os.client_portal.accept_intake_request(self.org.id, self.ws.id, self.staff.id, "intake_missing")

    def test_intake_requires_title_and_request(self) -> None:
        with self.assertRaises(ValidationError):
            self.os.client_portal.submit_intake_request(self.org.id, self.ws.id, self.client_person.id, "  ", "  ")

    def test_client_can_comment_and_decide_on_client_kind_review_only(self) -> None:
        project = self.os.create_project(self.org.id, self.ws.id, self.staff.id, "Launch")
        deliverable = self.os.create_deliverable(
            self.org.id, self.ws.id, self.staff.id, project.id, "Landing page draft", "landing_page",
        )
        client_review = self.os.open_review(self.org.id, self.ws.id, self.staff.id, deliverable.id, kind="client")
        internal_review = self.os.open_review(self.org.id, self.ws.id, self.staff.id, deliverable.id, kind="internal")

        comment = self.os.client_portal.add_client_review_comment(
            self.org.id, self.ws.id, self.client_person.id, client_review.id, "Tweak the CTA",
        )
        self.assertEqual(comment.body, "Tweak the CTA")

        decided = self.os.client_portal.decide_client_review(
            self.org.id, self.ws.id, self.client_person.id, client_review.id, "revision_requested",
        )
        self.assertEqual(decided.status, "revision_requested")
        self.assertEqual(decided.decision, "revision_requested")
        updated_deliverable = self.os.company.get_deliverable(self.ws.id, deliverable.id)
        self.assertEqual(updated_deliverable.revision_count, 1)
        audit = self.os.store.conn.execute(
            """SELECT action,entity_type,principal_id,detail FROM ledger_audit
               WHERE principal_id=? ORDER BY rowid""",
            (self.client_person.id,),
        ).fetchall()
        self.assertIn(("create", "review_comment", self.client_person.id, client_review.id), [tuple(row) for row in audit])
        self.assertIn(("decide", "review", self.client_person.id, "revision_requested"), [tuple(row) for row in audit])

        with self.assertRaises(AuthorizationError):
            self.os.client_portal.add_client_review_comment(
                self.org.id, self.ws.id, self.client_person.id, internal_review.id, "Should not work",
            )
        with self.assertRaises(AuthorizationError):
            self.os.client_portal.decide_client_review(
                self.org.id, self.ws.id, self.client_person.id, internal_review.id, "approved",
            )

    def test_client_review_list_excludes_internal_reviews(self) -> None:
        project = self.os.create_project(self.org.id, self.ws.id, self.staff.id, "Launch")
        deliverable = self.os.create_deliverable(
            self.org.id, self.ws.id, self.staff.id, project.id, "Deck", "presentation",
        )
        self.os.open_review(self.org.id, self.ws.id, self.staff.id, deliverable.id, kind="internal")
        client_review = self.os.open_review(self.org.id, self.ws.id, self.staff.id, deliverable.id, kind="client")
        reviews = self.os.client_portal.list_client_reviews(self.org.id, self.ws.id, self.client_person.id)
        self.assertEqual([r["id"] for r in reviews], [client_review.id])

    def test_client_capability_cannot_write_general_workspace_state(self) -> None:
        # The "client" workspace role must not gain the broader
        # "workspace_write" capability just because it can act through the
        # narrow client_portal surface.
        _, identity = issue_identity(self.os, self.org.id, self.client_person.id, self.ws.id)
        self.assertNotIn("workspace_write", identity.capabilities)
        self.assertIn("client_portal", identity.capabilities)
        self.assertIn("workspace_read", identity.capabilities)


if __name__ == "__main__":
    unittest.main()
