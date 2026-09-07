from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.dependency_audit import audit_dependencies, main


class DependencyAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_reports_pyproject_risk_markers_deterministically(self) -> None:
        (self.root / "pyproject.toml").write_text(
            """
[project]
dependencies = ["requests==2.31.0", "flask>=3,<4", "demo==1.0.0rc1", "wild==1.*"]

[project.optional-dependencies]
browser = ["pytest>=8"]

[build-system]
requires = ["setuptools>=68"]
""".strip(),
            encoding="utf-8",
        )

        report = audit_dependencies(self.root)

        self.assertEqual(report["status"], "findings")
        self.assertEqual(report["exit_code"], 1)
        findings_by_name = {
            item["normalized_name"]: [finding["code"] for finding in item["findings"]]
            for item in report["packages"]
        }
        self.assertEqual(findings_by_name["requests"], [])
        self.assertEqual(findings_by_name["flask"], ["pin-format"])
        self.assertEqual(findings_by_name["demo"], ["pre-release-pin"])
        self.assertEqual(findings_by_name["wild"], ["wildcard-version"])
        self.assertEqual(report["summary"]["dependency_files"], ["pyproject.toml"])

    def test_reports_duplicate_and_conflicting_pins_across_files(self) -> None:
        (self.root / "pyproject.toml").write_text(
            """
[project]
dependencies = ["requests==2.31.0"]
""".strip(),
            encoding="utf-8",
        )
        (self.root / "requirements.txt").write_text(
            """
requests==2.30.0
urllib3==2.2.0
""".strip(),
            encoding="utf-8",
        )
        (self.root / "requirements-dev.txt").write_text("urllib3==2.2.0\n", encoding="utf-8")

        report = audit_dependencies(self.root)

        duplicates = {item["normalized_name"]: item["code"] for item in report["duplicates"]}
        self.assertEqual(duplicates, {"requests": "conflicting-pins", "urllib3": "duplicate-pin"})
        self.assertEqual(report["summary"]["duplicate_conflicts"], 2)

    def test_clean_report_exits_zero_for_exact_unique_pins(self) -> None:
        (self.root / "requirements.txt").write_text(
            """
requests==2.31.0
urllib3==2.2.0
""".strip(),
            encoding="utf-8",
        )

        report = audit_dependencies(self.root)

        self.assertEqual(report["status"], "clean")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["summary"]["findings"], 0)

    def test_main_writes_report_and_returns_findings_exit_code(self) -> None:
        (self.root / "requirements.txt").write_text("demo>=1\n", encoding="utf-8")
        output = self.root / "report.json"

        exit_code = main(["--root", str(self.root), "--output", str(output)])

        self.assertEqual(exit_code, 1)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["packages"][0]["findings"][0]["code"], "pin-format")


if __name__ == "__main__":
    unittest.main()
