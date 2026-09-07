from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.github_connector_substance import (
    GithubConnectorSubstanceService, REQUIRED_SCOPES,
)


class GithubConnectorSubstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = GithubConnectorSubstanceService()
        self.base = {
            "provider": "github", "account_id": "app-1", "repository_id": "repo-1",
            "scopes": sorted(REQUIRED_SCOPES), "provider_version": "v1",
            "data": [
                {"type": "project_event", "id": "evt-1", "title": "Moved roadmap card", "file_content": "must not persist"},
                {"type": "release_event", "id": "rel-1", "tag_name": "v1.2.0", "body": "Quoted release notes", "diff": "must not persist"},
            ],
        }

    def test_offline_sync_maps_bounded_org_scoped_evidence_and_cursor(self) -> None:
        result = self.service.sync("org-1", "app-1", "repo-1", self.base)
        self.assertEqual(result["imported"], 2)
        self.assertTrue(result["verification"]["read_scope_verified"])
        self.assertEqual(result["baseline"], {"provider_count": 2})
        self.assertEqual(len(self.service.records), 2)
        release = next(item for item in self.service.records.values() if item.object_type == "release_event")
        self.assertEqual(release.quoted_text, "Quoted release notes")
        self.assertNotIn("diff", release.__dict__)
        project_event = next(item for item in self.service.records.values() if item.object_type == "project_event")
        self.assertEqual(project_event.quoted_text, "Moved roadmap card")
        self.assertNotIn("file_content", project_event.__dict__)
        self.assertIn('"provider_version":"v1"', result["cursor_after"])

    def test_incremental_replay_dedupes_and_unchanged_version_is_empty(self) -> None:
        first = self.service.sync("org-1", "app-1", "repo-1", self.base)
        replay = self.service.sync("org-1", "app-1", "repo-1", self.base, cursor=first["cursor_after"])
        self.assertEqual(replay["imported"], 0)
        self.assertEqual(replay["duplicates"], 0)
        changed = {**self.base, "provider_version": "v2"}
        changed["data"] = [{"type": "release_event", "id": "rel-2", "tag_name": "v1.3.0", "body": "Second release"}]
        result = self.service.sync("org-1", "app-1", "repo-1", changed, cursor=first["cursor_after"])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(len(self.service.records), 3)

    def test_malformed_payload_quarantines_and_replay_is_org_fenced(self) -> None:
        bad = {**self.base, "data": [{"type": "release_event", "id": "bad", "tag_name": "v1.2.1"}]}
        result = self.service.sync("org-1", "app-1", "repo-1", bad)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["status"], "degraded")
        quarantine = {**self.service.quarantines[0], "account_id": "app-1", "provider_version": "v1"}
        replay = self.service.replay_quarantine(quarantine, {"type": "release_event", "id": "bad", "tag_name": "v1.2.1", "body": "Fixed note"})
        self.assertEqual(replay["imported"], 1)
        cross_org = {**quarantine, "organization_id": "org-2"}
        self.assertEqual(self.service.replay_quarantine(cross_org, {"type": "release_event", "id": "bad", "tag_name": "v1.2.1", "body": "Other org"})["imported"], 1)
        self.assertEqual(len(self.service.records), 2)

    def test_provider_and_repository_fences_fail_closed(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.sync("org-1", "app-1", "repo-1", {**self.base, "scopes": []})
        with self.assertRaises(AuthorizationError):
            self.service.sync("org-1", "app-1", "repo-1", self.base, repository_mappings={"repo-1": "org-2"})


if __name__ == "__main__":
    unittest.main()
