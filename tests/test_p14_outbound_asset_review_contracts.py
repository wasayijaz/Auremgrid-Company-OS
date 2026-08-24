from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.integration_security import OutboundSendService, ProviderInstallationService
from auremgrid.storage.backup import create_backup


class P14OutboundAssetReviewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")

    def tearDown(self) -> None:
        self.os.close()

    def test_attempt_ledger_is_append_only_and_redacted(self) -> None:
        identity = AuthenticatedIdentity(self.person.id, self.org.id, "person", "s", frozenset({"integration_configure", "external_send"}))
        install = ProviderInstallationService(self.os.store.conn, new_id).create(identity, self.org.id, None, "github", "acct", "https://app.test/cb")
        self.os.store.conn.execute("UPDATE provider_installations SET status='active' WHERE id=?", (install["id"],))
        approval = new_id("approval"); now = "2026-08-22T00:00:00+00:00"
        self.os.store.conn.execute("INSERT INTO approval_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (approval, self.org.id, None, "person", self.person.id, "provider", "external_send", "{}", "test", "person", "human", "approved", now, None, "", now)); self.os.store.conn.commit()
        service = OutboundSendService(self.os.store.conn, new_id)
        intent = service.create_intent(identity, install["id"], approval, "idem", {"message": "hello"})
        service.dispatch(intent["id"], lambda _: None)
        attempts = service.list_attempts(self.org.id, intent["id"])
        self.assertEqual([item["status"] for item in attempts], ["claimed", "sent"])
        self.assertNotIn("lease_token_digest", attempts[0])
        with self.assertRaises(Exception):
            self.os.store.conn.execute("DELETE FROM outbound_send_attempts WHERE intent_id=?", (intent["id"],))
        self.os.store.conn.rollback()

    def test_asset_backup_contract_and_media_contract_require_matching_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_backup = Path(directory) / "backup.sqlite"
            ident = AuthenticatedIdentity(self.person.id, self.org.id, "person", "s", frozenset({"backup_create", "backup_restore"}), workspace_id=self.ws.id)
            asset = self.os.asset_recovery.register_asset(ident, self.org.id, self.ws.id, "clip", "video", "s3://bucket/clip.mp4", size_bytes=1, sha256="a" * 64)
            create_backup(self.os.store.conn, db_backup)
            manifest = self.os.asset_recovery.record_backup(AuthenticatedIdentity(self.person.id, self.org.id, "person", "s", frozenset({"backup_create", "backup_restore"})), self.org.id, str(db_backup))
            linked = self.os.asset_recovery.register_asset_backup(ident, self.org.id, self.ws.id, asset["id"], manifest["id"])
            self.assertEqual(linked["status"], "verified")
            project = self.os.create_project(self.org.id, self.ws.id, self.person.id, "P")
            deliverable = self.os.create_deliverable(self.org.id, self.ws.id, self.person.id, project.id, "Clip", "video")
            self.os.add_deliverable_version(self.org.id, self.ws.id, self.person.id, deliverable.id, "source", "fixture://clip.mp4")
            review = self.os.open_review(self.org.id, self.ws.id, self.person.id, deliverable.id)
            contract = self.os.register_review_media_contract(self.org.id, self.ws.id, self.person.id, review.id, "fixture://clip.mp4", "video", duration_seconds=9.5, frame_rate=24)
            self.assertEqual(contract["duration_seconds"], 9.5)
            annotation = self.os.create_review_annotation(self.org.id, self.ws.id, self.person.id, review.id, "video_range", "trim", start_seconds=1, end_seconds=2, metadata={"frame": 24})
            self.assertEqual(annotation["metadata"]["frame"], 24)


if __name__ == "__main__":
    unittest.main()
