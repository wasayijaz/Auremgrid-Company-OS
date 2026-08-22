from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.asset_recovery import AssetRecoveryService, validate_locator
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.storage.backup import create_backup


class AssetBackupPolicyTests(unittest.TestCase):
    def identity(self, org: str, workspace: str | None = None, *caps: str) -> AuthenticatedIdentity:
        return AuthenticatedIdentity("principal", org, "person", "session", frozenset(caps), workspace_id=workspace)

    def test_asset_hash_size_locator_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / "asset.bin"; path.write_bytes(b"asset")
            os = CompanyOS(":memory:"); org = os.create_organization("Org"); service = AssetRecoveryService(os.store.conn, new_id)
            identity = self.identity(org.id, None, "backup_create")
            asset = service.register_asset(identity, org.id, None, "asset", "binary", str(path), "critical")
            self.assertEqual(asset["size_bytes"], 5); self.assertEqual(asset["retention_class"], "critical")
            external = service.register_asset(identity, org.id, None, "remote", "binary", "s3://bucket/object", "standard", size_bytes=5, sha256="a" * 64)
            self.assertEqual(external["locator"], "s3://bucket/object")
            with self.assertRaises(ValidationError): validate_locator("https://cdn.test/a?X-Amz-Signature=secret")

    def test_independent_backup_manifest_and_recovery_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); db = root / "live.sqlite"; backup = root / "backup.sqlite"
            os = CompanyOS(db); org = os.create_organization("Org"); create_backup(os.store.conn, backup)
            service = AssetRecoveryService(os.store.conn, new_id); restore = self.identity(org.id, None, "backup_create", "backup_restore")
            try:
                manifest = service.record_backup(restore, org.id, str(backup)); self.assertEqual(manifest["status"], "verified")
                plan = service.create_recovery_plan(restore, org.id, None, manifest["id"], "s3", str(root / "target.sqlite"), 15, 30)
                self.assertEqual(service.update_recovery_status(restore, org.id, plan["id"], "ready")["status"], "ready")
            finally:
                os.close()

    def test_scope_and_capability_guards(self) -> None:
        os = CompanyOS(":memory:"); org = os.create_organization("Org"); service = AssetRecoveryService(os.store.conn, new_id)
        with self.assertRaises(AuthorizationError): service.record_backup(self.identity(org.id), org.id, "/tmp/nope")


if __name__ == "__main__": unittest.main()
