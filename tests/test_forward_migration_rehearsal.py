from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from auremgrid.storage.migrations import MIGRATIONS


ROOT = Path(__file__).resolve().parents[1]
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


class ForwardMigrationRehearsalTests(unittest.TestCase):
    def test_prior_schema_fixture_migrates_to_current_schema(self) -> None:
        script = ROOT / "scripts" / "forward_migration_rehearsal.py"
        spec = importlib.util.spec_from_file_location("forward_migration_rehearsal", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            result = module.run_rehearsal(Path(directory) / "prior-schema.sqlite")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["target_schema_version"], LATEST_SCHEMA_VERSION)
        self.assertEqual(result["upgraded_schema_version"], LATEST_SCHEMA_VERSION)
        self.assertEqual(result["integrity"], "ok")


if __name__ == "__main__":
    unittest.main()
