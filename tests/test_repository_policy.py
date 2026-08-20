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


# Keep client/source labels out of the repository while avoiding a copy of a
# retired label in this policy file itself.
_RETIRED_LABEL = _word(99, 111, 115, 109, 111)
_RETIRED_LABELS = (
    _RETIRED_LABEL,
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
