from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.asset_layer import AssetLayerService
from auremgrid.services.brain import CompanyOS


class AssetLayerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org-assets")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client", "ws-assets")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner", person_id="person-assets")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.service = AssetLayerService(self.os)
        self.scope = {"organization_id": self.org.id, "workspace_id": self.ws.id, "person_id": self.person.id}
        self.asset = self.service.register_asset(
            self.scope, title="Launch Hero", asset_kind="image", locator="s3://bucket/hero-v1.png",
        )

    def tearDown(self) -> None:
        self.os.close()

    def test_register_add_version_and_compare(self) -> None:
        self.service.add_version(self.scope, self.asset["id"], locator="s3://bucket/hero-v2.png", dimensions="1920x1080")
        self.service.add_version(self.scope, self.asset["id"], locator="s3://bucket/hero-v3.png")
        rows = self.service.versions(self.scope, self.asset["id"])
        self.assertEqual([r["version"] for r in rows], [1, 2, 3])
        diff = self.service.compare_versions(self.scope, self.asset["id"], from_version=1, to_version=2)
        self.assertEqual(diff["changes"]["locator"], ("s3://bucket/hero-v1.png", "s3://bucket/hero-v2.png"))
        self.assertIn("dimensions", diff["changes"])
        with self.assertRaises(NotFoundError):
            self.service.compare_versions(self.scope, self.asset["id"], from_version=1, to_version=9)

    def test_approval_transition_matrix(self) -> None:
        moved = self.service.set_approval(self.scope, self.asset["id"], state="in_review")
        self.assertEqual(moved["approval_state"], "in_review")
        approved = self.service.set_approval(self.scope, self.asset["id"], state="approved")
        self.assertEqual(approved["reviewer_person_id"], self.person.id)
        with self.assertRaises(ValidationError):
            self.service.set_approval(self.scope, self.asset["id"], state="draft")
        with self.assertRaises(ValidationError):
            self.service.set_approval(self.scope, self.asset["id"], state="archived")
        fresh = self.service.register_asset(self.scope, title="Rejected path", asset_kind="deck", locator="s3://bucket/deck.pdf")
        self.service.set_approval(self.scope, fresh["id"], state="in_review")
        rejected = self.service.set_approval(self.scope, fresh["id"], state="rejected")
        self.assertEqual(rejected["approval_state"], "rejected")
        back = self.service.set_approval(self.scope, fresh["id"], state="draft")
        self.assertEqual(back["approval_state"], "draft")

    def test_review_thread_lifecycle_and_anchors(self) -> None:
        version = self.service.add_version(self.scope, self.asset["id"], locator="s3://bucket/hero-v2.png")
        thread = self.service.create_thread(
            self.scope, self.asset["id"], kind="region", body="Logo contrast is low",
            version_id=version["id"], anchor={"x": 10, "y": 20, "w": 100, "h": 40},
        )
        self.assertEqual(thread["status"], "open")
        self.assertEqual(thread["anchor_json"], '{"x": 10, "y": 20, "w": 100, "h": 40}')
        resolved = self.service.set_thread_status(self.scope, thread["id"], status="resolved")
        self.assertEqual(resolved["status"], "resolved")
        reopened = self.service.set_thread_status(self.scope, thread["id"], status="reopened")
        self.assertEqual(reopened["status"], "reopened")
        with self.assertRaises(ValidationError):
            self.service.set_thread_status(self.scope, thread["id"], status="open")

    def test_unknown_kinds_and_required_fields_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.register_asset(self.scope, title="X", asset_kind="hologram", locator="s3://x")
        with self.assertRaises(ValidationError):
            self.service.register_asset(self.scope, title="", asset_kind="image", locator="s3://x")
        with self.assertRaises(ValidationError):
            self.service.create_thread(self.scope, self.asset["id"], kind="voice", body="hi")

    def test_fencing_scopes_assets_threads_and_other_org(self) -> None:
        other = self.os.create_organization("Other", "org-other-assets")
        outsider = self.os.create_person(other.id, "Other Owner", role="owner", person_id="person-other-assets")
        other_ws = self.os.create_organization_workspace(other.id, "Other", "client", "ws-other-assets")
        self.os.add_person_to_workspace(other.id, other_ws.id, outsider.id, "admin")
        other_scope = {"organization_id": other.id, "workspace_id": other_ws.id, "person_id": outsider.id}
        foreign = self.service.register_asset(other_scope, title="Foreign", asset_kind="link", locator="https://x.example")
        self.assertEqual(foreign["organization_id"], other.id)
        with self.assertRaises(NotFoundError):
            self.service.set_approval(self.scope, foreign["id"], state="in_review")
        with self.assertRaises(NotFoundError):
            self.service.set_thread_status(self.scope, "asset_thread-missing", status="resolved")
        unscoped = {"workspace_id": self.ws.id, "person_id": self.person.id}
        with self.assertRaises(ValidationError):
            self.service.register_asset(unscoped, title="No org", asset_kind="file", locator="x")
        outsider_no_member = {"organization_id": self.org.id, "person_id": outsider.id}
        with self.assertRaises(AuthorizationError):
            self.service.register_asset(outsider_no_member, title="Sneak", asset_kind="file", locator="x")


if __name__ == "__main__":
    unittest.main()
