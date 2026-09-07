from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.figma_connector_substance import (
    FigmaConnectorSubstanceService, REQUIRED_SCOPES,
)


class FigmaConnectorSubstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FigmaConnectorSubstanceService()
        self.base = {
            "provider": "figma", "account_id": "user-1", "file_id": "file-1",
            "scopes": sorted(REQUIRED_SCOPES), "provider_version": "v1",
            "data": [
                {"type": "comment", "id": "c1", "body": "Quoted review note", "design_content": "must not persist"},
                {"type": "version", "id": "ver-1", "label": "Review"},
            ],
        }

    def test_offline_sync_maps_bounded_org_scoped_evidence_and_cursor(self) -> None:
        result = self.service.sync("org-1", "ws-1", "user-1", "file-1", self.base)
        self.assertEqual(result["imported"], 2)
        self.assertTrue(result["verification"]["read_scope_verified"])
        self.assertEqual(result["baseline"], {"provider_count": 2})
        self.assertEqual(len(self.service.records), 2)
        comment = next(item for item in self.service.records.values() if item.object_type == "comment")
        self.assertEqual(comment.quoted_text, "Quoted review note")
        self.assertNotIn("design_content", comment.__dict__)
        self.assertIn('"provider_version":"v1"', result["cursor_after"])

    def test_incremental_replay_dedupes_and_unchanged_version_is_empty(self) -> None:
        first = self.service.sync("org-1", "ws-1", "user-1", "file-1", self.base)
        replay = self.service.sync("org-1", "ws-1", "user-1", "file-1", self.base, cursor=first["cursor_after"])
        self.assertEqual(replay["imported"], 0)
        self.assertEqual(replay["duplicates"], 0)
        changed = {**self.base, "provider_version": "v2"}
        changed["data"] = [{"type": "comment", "id": "c2", "body": "Second note"}]
        result = self.service.sync("org-1", "ws-1", "user-1", "file-1", changed, cursor=first["cursor_after"])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(len(self.service.records), 3)

    def test_malformed_payload_quarantines_and_replay_is_org_fenced(self) -> None:
        bad = {**self.base, "data": [{"type": "comment", "id": "bad"}]}
        result = self.service.sync("org-1", "ws-1", "user-1", "file-1", bad)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["status"], "degraded")
        quarantine = {**self.service.quarantines[0], "workspace_id": "ws-1", "account_id": "user-1", "provider_version": "v1"}
        replay = self.service.replay_quarantine(quarantine, {"type": "comment", "id": "bad", "body": "Fixed quote"})
        self.assertEqual(replay["imported"], 1)
        cross_org = {**quarantine, "organization_id": "org-2"}
        self.assertEqual(self.service.replay_quarantine(cross_org, {"type": "comment", "id": "bad", "body": "Other org"})["imported"], 1)
        self.assertEqual(len(self.service.records), 2)

    def test_provider_and_workspace_fences_fail_closed(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.sync("org-1", "ws-1", "user-1", "file-1", {**self.base, "scopes": []})
        with self.assertRaises(AuthorizationError):
            self.service.sync("org-1", "ws-1", "user-1", "file-1", self.base, workspace_mappings={"file-1": "ws-2"})


if __name__ == "__main__":
    unittest.main()
