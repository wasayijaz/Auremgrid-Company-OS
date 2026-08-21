from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.brain import CompanyOS


class ReviewCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.reviewer = self.os.create_person(self.org.id, "Reviewer", role="member")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.reviewer.id, "operator")
        self.project = self.os.create_project(self.org.id, self.ws.id, self.owner.id, "Launch")

    def tearDown(self) -> None:
        self.os.close()

    def _deliverable(self, title: str = "Landing page"):
        return self.os.create_deliverable(self.org.id, self.ws.id, self.owner.id, self.project.id, title, "landing_page")

    def test_requires_organization_membership(self) -> None:
        stranger = self.os.create_person(self.org.id, "Stranger", role="member")
        self.os.company.conn.execute("DELETE FROM organization_memberships WHERE person_id=?", (stranger.id,))
        self.os.company.conn.commit()
        with self.assertRaises(AuthorizationError):
            self.os.dashboard.review_center(self.org.id, stranger.id)

    def test_empty_when_no_workspace_memberships(self) -> None:
        lonely = self.os.create_person(self.org.id, "Lonely", role="member")
        result = self.os.dashboard.review_center(self.org.id, lonely.id)
        self.assertEqual(result["waiting_for_me"], [])
        self.assertEqual(result["waiting_for_team"], [])
        self.assertIn("generated_at", result)

    def test_review_assigned_to_caller_appears_in_waiting_for_me(self) -> None:
        deliverable = self._deliverable()
        review = self.os.open_review(
            self.org.id, self.ws.id, self.owner.id, deliverable.id,
            kind="internal", reviewer_person_id=self.reviewer.id,
        )
        result = self.os.dashboard.review_center(self.org.id, self.reviewer.id)
        self.assertEqual([r["id"] for r in result["waiting_for_me"]], [review.id])
        self.assertEqual(result["waiting_for_me"][0]["deliverable_title"], "Landing page")
        self.assertEqual(result["waiting_for_team"], [])

    def test_review_assigned_to_someone_else_appears_in_waiting_for_team(self) -> None:
        deliverable = self._deliverable()
        review = self.os.open_review(
            self.org.id, self.ws.id, self.owner.id, deliverable.id,
            kind="internal", reviewer_person_id=self.reviewer.id,
        )
        result = self.os.dashboard.review_center(self.org.id, self.owner.id)
        self.assertEqual(result["waiting_for_me"], [])
        self.assertEqual([r["id"] for r in result["waiting_for_team"]], [review.id])

    def test_client_kind_review_always_reported_as_waiting_for_client(self) -> None:
        deliverable = self._deliverable()
        review = self.os.open_review(
            self.org.id, self.ws.id, self.owner.id, deliverable.id,
            kind="client", reviewer_person_id=self.owner.id,
        )
        result = self.os.dashboard.review_center(self.org.id, self.owner.id)
        self.assertEqual([r["id"] for r in result["waiting_for_client"]], [review.id])
        self.assertEqual(result["waiting_for_me"], [])

    def test_revision_requested_review_is_separated_from_open_queues(self) -> None:
        deliverable = self._deliverable()
        review = self.os.open_review(
            self.org.id, self.ws.id, self.owner.id, deliverable.id,
            kind="internal", reviewer_person_id=self.owner.id,
        )
        self.os.decide_review(self.org.id, self.ws.id, self.owner.id, review.id, "revision_requested")
        result = self.os.dashboard.review_center(self.org.id, self.owner.id)
        self.assertEqual([r["id"] for r in result["revision_requested"]], [review.id])
        self.assertEqual(result["waiting_for_me"], [])

    def test_review_open_over_48_hours_is_reported_as_stalled(self) -> None:
        deliverable = self._deliverable()
        review = self.os.open_review(
            self.org.id, self.ws.id, self.owner.id, deliverable.id,
            kind="internal", reviewer_person_id=self.owner.id,
        )
        stale_opened_at = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        self.os.company.conn.execute(
            "UPDATE reviews SET opened_at=? WHERE id=?", (stale_opened_at, review.id),
        )
        self.os.company.conn.commit()
        result = self.os.dashboard.review_center(self.org.id, self.owner.id)
        self.assertEqual([r["id"] for r in result["stalled"]], [review.id])
        self.assertGreaterEqual(result["stalled"][0]["stalled_hours"], 48)

    def test_approved_today_review_is_reported_separately(self) -> None:
        deliverable = self._deliverable()
        review = self.os.open_review(
            self.org.id, self.ws.id, self.owner.id, deliverable.id,
            kind="internal", reviewer_person_id=self.owner.id,
        )
        self.os.decide_review(self.org.id, self.ws.id, self.owner.id, review.id, "approved")
        result = self.os.dashboard.review_center(self.org.id, self.owner.id)
        self.assertEqual([r["id"] for r in result["approved_today"]], [review.id])

    def test_only_reviews_from_caller_workspaces_are_visible(self) -> None:
        other_owner = self.os.create_person(self.org.id, "Other Owner", role="owner")
        other_ws = self.os.create_organization_workspace(self.org.id, "Base", "client")
        self.os.add_person_to_workspace(self.org.id, other_ws.id, other_owner.id, "admin")
        other_project = self.os.create_project(self.org.id, other_ws.id, other_owner.id, "Other launch")
        other_deliverable = self.os.create_deliverable(
            self.org.id, other_ws.id, other_owner.id, other_project.id, "Other deck", "presentation",
        )
        self.os.open_review(self.org.id, other_ws.id, other_owner.id, other_deliverable.id, kind="internal")
        deliverable = self._deliverable()
        review = self.os.open_review(
            self.org.id, self.ws.id, self.owner.id, deliverable.id,
            kind="internal", reviewer_person_id=self.owner.id,
        )
        # owner is not a member of other_ws, so review center must only
        # surface the workspace they actually belong to.
        result = self.os.dashboard.review_center(self.org.id, self.owner.id)
        all_ids = {r["id"] for section in result.values() if isinstance(section, list) for r in section}
        self.assertIn(review.id, all_ids)
        self.assertEqual(len(all_ids), 1)


if __name__ == "__main__":
    unittest.main()
