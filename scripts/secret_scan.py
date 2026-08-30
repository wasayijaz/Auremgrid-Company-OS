"""Fail CI when tracked, non-test project files contain credential material."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


EXCLUDED_PARTS = {".git", "tests", "fixtures", ".auremgrid-backups", ".tmp"}
TEXT_SUFFIXES = {
    ".cfg", ".conf", ".env", ".ini", ".json", ".md", ".ps1", ".py", ".sh",
    ".toml", ".txt", ".yaml", ".yml",
}
KNOWN_CREDENTIAL_PATTERNS = (
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe live key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}\b")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-|live-)?[A-Za-z0-9_-]{32,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
)
ASSIGNMENT_PATTERN = re.compile(
    r"(?im)\b(?:api[_-]?key|api[_-]?secret|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|webhook[_-]?secret)\b\s*(?:=|:)\s*['\"]?([A-Za-z0-9_./+=-]+)"
)
PLACEHOLDER_MARKERS = ("example", "placeholder", "changeme", "replace", "your_", "<", "${", "env:")


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str


def should_scan(path: Path) -> bool:
    """Limit the gate to tracked human-authored code, config, and documentation."""
    return not (EXCLUDED_PARTS & set(path.parts)) and (path.suffix.lower() in TEXT_SUFFIXES or path.name == "Dockerfile")


def _has_material_assignment(match: re.Match[str]) -> bool:
    value = match.group(1)
    normalized = value.lower()
    return len(value) >= 20 and not any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def scan_text(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for kind, pattern in KNOWN_CREDENTIAL_PATTERNS:
        findings.extend(Finding(path, text.count("\n", 0, match.start()) + 1, kind) for match in pattern.finditer(text))
    findings.extend(
        Finding(path, text.count("\n", 0, match.start()) + 1, "credential assignment")
        for match in ASSIGNMENT_PATTERN.finditer(text)
        if _has_material_assignment(match)
    )
    return findings


def scan_paths(root: Path, paths: Iterable[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        relative = path.relative_to(root) if path.is_absolute() else path
        if not should_scan(relative):
            continue
        candidate = root / relative
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(scan_text(relative, text))
    return findings


def tracked_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    return [Path(item) for item in completed.stdout.decode("utf-8").split("\0") if item]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        findings = scan_paths(root, tracked_files(root))
    except subprocess.CalledProcessError as error:
        print(error.stderr.decode("utf-8", errors="replace"), file=sys.stderr)
        return 2
    if not findings:
        print("Secret scan passed: no credential material found in tracked code, config, or docs.")
        return 0
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.kind} detected", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
