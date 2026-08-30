from __future__ import annotations

import unittest

from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS


class ReviewAnnotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client")
        self.person = self.os.create_person(self.org.id, "Reviewer", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.project = self.os.create_project(self.org.id, self.ws.id, self.person.id, "Project")

    def tearDown(self) -> None:
        self.os.close()

    def _review(self, kind: str, url: str | None = None):
        deliverable = self.os.create_deliverable(self.org.id, self.ws.id, self.person.id, self.project.id, kind, kind)
        if url:
            self.os.add_deliverable_version(self.org.id, self.ws.id, self.person.id, deliverable.id, "source", url)
        return deliverable, self.os.open_review(self.org.id, self.ws.id, self.person.id, deliverable.id)

    def test_general_comment_is_idempotent_and_resolvable(self) -> None:
        _, review = self._review("copy")
        first = self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, review.id, "general_comment", "Use the approved CTA", idempotency_key="same")
        replay = self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, review.id, "general_comment", "different", idempotency_key="same")
        self.assertEqual(first["id"], replay["id"])
        self.os.resolve_review_annotation(self.org.id, self.ws.id, self.person.id, first["id"], idempotency_key="resolve")
        self.assertEqual(self.os.list_review_annotations(self.org.id, self.ws.id, self.person.id, review.id)[0]["status"], "resolved")

    def test_media_geometry_requires_attached_source_and_bounds(self) -> None:
        _, review = self._review("design_asset")
        with self.assertRaises(ValidationError):
            self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, review.id, "image_point", "x", coordinates={"x": 0.2, "y": 0.3})
        deliverable, review = self._review("design_asset", "https://assets.test/image.png")
        point = self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, review.id, "image_point", "point", coordinates={"x": 0.2, "y": 0.3}, source_locator="https://assets.test/image.png")
        self.assertEqual(point["annotation_type"], "image_point")
        with self.assertRaises(ValidationError):
            self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, review.id, "image_region", "overflow", coordinates={"x": 0.8, "y": 0.2, "width": 0.4, "height": 0.2})

    def test_document_page_and_video_range_validate_source(self) -> None:
        _, review = self._review("document", "https://assets.test/brief.pdf")
        page = self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, review.id, "document_page", "p2", page_number=2)
        self.assertEqual(page["page_number"], 2)
        region = self.os.create_review_annotation(
            self.org.id, self.ws.id, self.person.id, review.id, "document_region", "callout",
            coordinates={"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}, page_number=2,
        )
        self.assertEqual(region["coordinates"]["width"], 0.3)
        with self.assertRaises(ValidationError):
            self.os.create_review_annotation(
                self.org.id, self.ws.id, self.person.id, review.id, "document_region", "overflow",
                coordinates={"x": 0.8, "y": 0.2, "width": 0.4, "height": 0.2}, page_number=2,
            )
        _, video_review = self._review("video", "https://assets.test/video.mp4")
        video = self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, video_review.id, "video_range", "trim", start_seconds=2, end_seconds=4)
        self.assertEqual(video["end_seconds"], 4)

    def test_supersede_cannot_cross_review_scope(self) -> None:
        _, first_review = self._review("copy")
        _, second_review = self._review("copy")
        first = self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, first_review.id, "general_comment", "old")
        replacement = self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, second_review.id, "general_comment", "new")
        with self.assertRaises(ValidationError):
            self.os.supersede_review_annotation(self.org.id, self.ws.id, self.person.id, first["id"], replacement["id"])

    def test_review_center_exposes_capability_gated_annotation_action(self) -> None:
        _, review = self._review("design_asset")
        queue = self.os.dashboard.review_center(self.org.id, self.person.id)
        row = next(item for item in queue["waiting_for_me"] + queue["waiting_for_team"] if item["id"] == review.id)
        self.assertEqual(row["allowed_actions"][0]["route"], "/reviews/annotations")
        self.assertEqual(row["annotation_capabilities"]["image_points"]["status"], "not_available")


if __name__ == "__main__":
    unittest.main()
