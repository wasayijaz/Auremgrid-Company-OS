from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auremgrid.domain.errors import AuthenticationError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.storage.backup import create_backup, restore_backup, rotate_backups, verify_backup
from tests.auth_support import LATEST_SCHEMA_VERSION


class BackupRestoreTests(unittest.TestCase):
    def test_backup_manifest_integrity_and_restore_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live_path = root / "live.sqlite"
            backup_path = root / "backups" / "company.sqlite"
            restored_path = root / "restored.sqlite"
            os = CompanyOS(live_path)
            organization = os.create_organization("Auremgrid")
            person = os.create_person(organization.id, "Owner", "owner@backup.test", role="owner")
            principal = os.auth.create_principal(organization.id, person.id, "owner@backup.test")
            session = os.auth.create_session(principal["id"])
            manifest = create_backup(os.store.conn, backup_path)
            os.close()

            self.assertEqual(manifest["integrity"], "ok")
            self.assertEqual(manifest["schema_version"], LATEST_SCHEMA_VERSION)
            sidecar = json.loads(backup_path.with_suffix(".sqlite.manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["sha256"], manifest["sha256"])
            restored = restore_backup(backup_path, restored_path)
            self.assertEqual(restored["integrity"], "ok")
            reopened = CompanyOS(restored_path)
            self.assertEqual(reopened.company.get_organization(organization.id).name, "Auremgrid")
            with self.assertRaises(AuthenticationError):
                reopened.auth.authenticate_bearer(session["token"])
            state = dict(reopened.store.conn.execute("SELECT key,value FROM system_state").fetchall())
            self.assertEqual(state["recovery_mode"], "1")
            self.assertEqual(state["outbound_dispatch"], "disabled")
            self.assertEqual(reopened.rebuild_projections()["status"], "healthy")
            reopened.close()

    def test_restore_refuses_overwrite_without_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = CompanyOS(root / "live.sqlite")
            backup_path = root / "backup.sqlite"
            create_backup(source.store.conn, backup_path)
            source.close()
            destination = root / "existing.sqlite"
            destination.write_bytes(b"do not overwrite")
            with self.assertRaises(ValidationError):
                restore_backup(backup_path, destination)
            self.assertEqual(destination.read_bytes(), b"do not overwrite")

    def test_explicit_overwrite_creates_verified_safety_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path, destination = root / "source.sqlite", root / "active.sqlite"
            source = CompanyOS(source_path)
            source_org = source.create_organization("Restored organization")
            backup_path = root / "backup.sqlite"
            create_backup(source.store.conn, backup_path)
            source.close()
            active = CompanyOS(destination)
            active_org = active.create_organization("Safety organization")
            active.close()

            restored = restore_backup(backup_path, destination, overwrite=True)
            safety_path = Path(restored["safety_backup"]["path"])
            self.assertTrue(safety_path.is_file())
            recovered_active = CompanyOS(destination)
            self.assertEqual(recovered_active.company.get_organization(source_org.id).name, "Restored organization")
            recovered_active.close()
            safety = CompanyOS(safety_path)
            self.assertEqual(safety.company.get_organization(active_org.id).name, "Safety organization")
            safety.close()

    def test_tampered_backup_fails_manifest_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = CompanyOS(root / "live.sqlite")
            backup_path = root / "backup.sqlite"
            manifest = create_backup(source.store.conn, backup_path)
            source.close()
            with backup_path.open("ab") as handle:
                handle.write(b"tamper")
            with self.assertRaises(ValidationError):
                verify_backup(backup_path, manifest["sha256"])

    def test_rotate_backups_honors_daily_and_weekly_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = datetime(2026, 8, 24, tzinfo=timezone.utc)
            for days_ago in range(18):
                backup = root / f"backup-{days_ago:02d}.sqlite"
                backup.write_bytes(b"backup")
                created_at = (base - timedelta(days=days_ago)).isoformat()
                backup.with_suffix(".sqlite.manifest.json").write_text(
                    json.dumps({"created_at": created_at}) + "\n",
                    encoding="utf-8",
                )

            removed = rotate_backups(root, keep_daily=3, keep_weekly=2)
            remaining = sorted(path.name for path in root.glob("*.sqlite"))
            self.assertEqual(
                remaining,
                ["backup-00.sqlite", "backup-01.sqlite", "backup-02.sqlite", "backup-03.sqlite", "backup-08.sqlite"],
            )
            self.assertEqual(len(removed), 13)
            self.assertFalse((root / "backup-17.sqlite.manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
