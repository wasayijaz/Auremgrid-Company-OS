from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
LAUNCHER = ROOT / "scripts" / "auremgrid.py"


class OfflineLauncherTests(unittest.TestCase):
    def run_launcher(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        if cwd is not None:
            return subprocess.run([sys.executable, str(LAUNCHER), *args], cwd=cwd, text=True, capture_output=True)
        with tempfile.TemporaryDirectory(prefix="auremgrid unrelated path ") as unrelated:
            return subprocess.run([sys.executable, str(LAUNCHER), *args], cwd=unrelated, text=True, capture_output=True)

    def test_help_works_from_unrelated_cwd(self) -> None:
        result = self.run_launcher("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage", result.stdout.lower())

    def test_launcher_is_path_based_and_source_exists(self) -> None:
        self.assertTrue((ROOT / "src" / "auremgrid" / "cli.py").is_file())
        self.assertIn("sys.path.insert(0, str(source))", LAUNCHER.read_text(encoding="utf-8"))

    def test_old_python_guard_is_before_package_import(self) -> None:
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertLess(source.index("_check_python()"), source.index("from auremgrid.cli"))

    def test_readme_primary_commands_use_launcher(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        setup = readme.split("## First 30 minutes", 1)[0]
        for command in ("demo", "setup-agency", "bootstrap-auth", "serve", "worker-once", "backup", "verify-backup"):
            self.assertIn(f"python scripts/auremgrid.py {command}", setup)
        self.assertNotIn("\nauremgrid demo", setup)


if __name__ == "__main__":
    unittest.main()
