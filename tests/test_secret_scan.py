from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.secret_scan import scan_paths, scan_text, should_scan


class SecretScanTests(unittest.TestCase):
    def test_detects_known_credential_shapes_without_echoing_values(self) -> None:
        findings = scan_text(Path("src/client.py"), "token = 'ghp_abcdefghijklmnopqrstuvwxyz1234567890'\n")
        self.assertEqual([(finding.line, finding.kind) for finding in findings], [(1, "GitHub token")])

    def test_detects_long_non_placeholder_credential_assignment(self) -> None:
        findings = scan_text(Path("deploy/settings.yml"), "webhook_secret: 1234567890abcdefghij\n")
        self.assertEqual(findings[0].kind, "credential assignment")

    def test_allows_placeholder_assignments_and_ordinary_security_text(self) -> None:
        text = "api_key: <replace-me>\npassword = ${DATABASE_PASSWORD}\nDocs call this a secret.\n"
        self.assertEqual(scan_text(Path("docs/setup.md"), text), [])

    def test_allows_runtime_lookup_assigned_to_a_credential_named_variable(self) -> None:
        self.assertEqual(scan_text(Path("src/client.py"), "api_key = os.environ.get('API_KEY')\n"), [])

    def test_excludes_test_and_fixture_paths_but_scans_docs_and_config(self) -> None:
        self.assertFalse(should_scan(Path("tests/test_example.py")))
        self.assertFalse(should_scan(Path("fixtures/example.yml")))
        self.assertTrue(should_scan(Path("docs/setup.md")))
        self.assertTrue(should_scan(Path(".github/workflows/ci.yml")))

    def test_scan_paths_uses_relative_paths_and_ignores_excluded_test_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir(); (root / "tests").mkdir()
            (root / "src" / "live.py").write_text("api_secret='1234567890abcdefghij'\n", encoding="utf-8")
            (root / "tests" / "test_fixture.py").write_text("ghp_abcdefghijklmnopqrstuvwxyz1234567890\n", encoding="utf-8")
            findings = scan_paths(root, [Path("src/live.py"), Path("tests/test_fixture.py")])
        self.assertEqual([(finding.path, finding.kind) for finding in findings], [(Path("src/live.py"), "credential assignment")])


if __name__ == "__main__":
    unittest.main()
