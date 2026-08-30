from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_SKIP_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".uv-cache",
    "__pycache__",
    "build",
    "dist",
}


def _word(*codepoints: int) -> str:
    return "".join(chr(codepoint) for codepoint in codepoints)


# Cosmo is the current operator identity. Only the obsolete two-word source
# label remains retired; the product name itself is deliberately allowed.
_RETIRED_LABEL = _word(99, 111, 115, 109, 111)
_RETIRED_LABELS = (
    _RETIRED_LABEL + " " + _word(80, 114, 111, 99, 101, 115, 115, 101, 115),
)


def _repository_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(ROOT).parts
        if any(part in _SKIP_DIRECTORIES for part in relative_parts):
            continue
        files.append(path)
    return files


class RepositoryPolicyTests(unittest.TestCase):
    def test_git_hooks_do_not_depend_on_nonportable_path_or_case_tools(self) -> None:
        hook_paths = [
            ROOT.joinpath(".githooks", "pre-commit"),
            ROOT.joinpath(".githooks", "commit-msg"),
            ROOT.joinpath(".githooks", "pre-merge-commit"),
        ]
        for path in hook_paths:
            text = path.read_text(encoding="utf-8")
            self.assertIn("${0%/*}", text, path.as_posix())
            self.assertNotIn("dirname", text, path.as_posix())

        library = ROOT.joinpath(".githooks", "lib", "attribution-guard.sh").read_text(encoding="utf-8")
        self.assertIn('case "$identity_kind" in', library)
        self.assertNotIn("| tr ", library)
        self.assertNotIn(" tr '", library)

    def test_ci_runs_deterministic_non_browser_contracts(self) -> None:
        workflow = ROOT.joinpath(".github", "workflows", "ci.yml").read_text(encoding="utf-8")
        for marker in (
            "python -m auremgrid.cli evaluate-intelligence",
            "python scripts/private_host_smoke.py",
            "python scripts/performance_baseline.py",
            "./scripts/test-git-guard.ps1",
        ):
            self.assertIn(marker, workflow)
        self.assertIn("pip install -e .", workflow)
        self.assertIn(".[browser]", workflow)
        self.assertIn("playwright install", workflow.lower())

    def test_retired_source_labels_are_absent_from_current_tree(self) -> None:
        findings: list[str] = []
        labels = tuple(label.casefold().encode("utf-8") for label in _RETIRED_LABELS)
        for path in _repository_files():
            relative = path.relative_to(ROOT).as_posix()
            relative_folded = relative.casefold()
            try:
                content = path.read_bytes().lower()
            except OSError as exc:
                self.fail(f"unable to inspect {relative}: {exc}")
            for label, needle in zip(_RETIRED_LABELS, labels):
                if label.casefold() in relative_folded or needle in content:
                    findings.append(relative)
                    break
        self.assertEqual([], findings, "retired source/client labels found: " + ", ".join(findings))


if __name__ == "__main__":
    unittest.main()
