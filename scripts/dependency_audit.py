"""Offline dependency declaration audit for CI.

The audit is deterministic and stdlib-only.  It inspects local dependency
manifests, reports risky version markers, and exits non-zero when findings are
present so CI can opt into gating when ready.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXACT_PIN = re.compile(r"==\s*([^,;\s]+)")
_VERSION_TOKEN = re.compile(r"(?:==|!=|<=|>=|<|>|~=)\s*([^,;\s]+)")
_PRERELEASE = re.compile(r"(?i)(?:^|[0-9.\-_])(?:a|alpha|b|beta|rc|pre|preview|dev)\d*")


@dataclass(frozen=True)
class DependencyDeclaration:
    name: str
    normalized_name: str
    raw: str
    source: str
    section: str


def audit_dependencies(root: str | Path = ".") -> dict[str, Any]:
    repo_root = Path(root)
    declarations = _declared_dependencies(repo_root)
    package_entries = [_package_entry(item) for item in declarations]
    duplicate_findings = _duplicates(declarations)
    findings_count = sum(len(item["findings"]) for item in package_entries) + len(duplicate_findings)
    return {
        "status": "findings" if findings_count else "clean",
        "exit_code": 1 if findings_count else 0,
        "summary": {
            "dependency_files": sorted({item.source for item in declarations}),
            "packages_scanned": len(declarations),
            "findings": findings_count,
            "duplicate_conflicts": len(duplicate_findings),
        },
        "packages": package_entries,
        "duplicates": duplicate_findings,
    }


def _declared_dependencies(root: Path) -> list[DependencyDeclaration]:
    declarations: list[DependencyDeclaration] = []
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        declarations.extend(_dependencies_from_pyproject(pyproject))
    for requirements in sorted(root.glob("requirements*.txt")):
        declarations.extend(_dependencies_from_requirements(requirements, root))
    return sorted(declarations, key=lambda item: (item.normalized_name, item.source, item.section, item.raw))


def _dependencies_from_pyproject(path: Path) -> list[DependencyDeclaration]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    result: list[DependencyDeclaration] = []
    for raw in _as_strings(project.get("dependencies", [])):
        result.append(_declaration(raw, path.name, "project.dependencies"))
    optional = project.get("optional-dependencies", {})
    if isinstance(optional, dict):
        for extra in sorted(optional):
            for raw in _as_strings(optional[extra]):
                result.append(_declaration(raw, path.name, f"project.optional-dependencies.{extra}"))
    build_system = data.get("build-system", {})
    if isinstance(build_system, dict):
        for raw in _as_strings(build_system.get("requires", [])):
            result.append(_declaration(raw, path.name, "build-system.requires"))
    return result


def _dependencies_from_requirements(path: Path, root: Path) -> list[DependencyDeclaration]:
    result: list[DependencyDeclaration] = []
    source = path.relative_to(root).as_posix()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = line.split("#", 1)[0].strip()
        if not raw or raw.startswith(("-", "--")):
            continue
        result.append(_declaration(raw, source, f"line.{line_number}"))
    return result


def _declaration(raw: str, source: str, section: str) -> DependencyDeclaration:
    match = _NAME.match(raw)
    if not match:
        return DependencyDeclaration("", "", raw, source, section)
    name = match.group(1)
    return DependencyDeclaration(name, _normalize(name), raw, source, section)


def _package_entry(declaration: DependencyDeclaration) -> dict[str, Any]:
    findings = []
    if not declaration.name:
        findings.append({"code": "invalid-requirement", "message": "dependency declaration could not be parsed"})
    elif _has_wildcard(declaration.raw):
        findings.append({"code": "wildcard-version", "message": "dependency uses a wildcard version"})
    elif not _is_exact_pin(declaration.raw):
        findings.append({"code": "pin-format", "message": "dependency is not pinned with an exact == version"})
    if _has_prerelease(declaration.raw):
        findings.append({"code": "pre-release-pin", "message": "dependency references a pre-release version"})
    return {
        "name": declaration.name,
        "normalized_name": declaration.normalized_name,
        "source": declaration.source,
        "section": declaration.section,
        "requirement": declaration.raw,
        "findings": findings,
    }


def _duplicates(declarations: Iterable[DependencyDeclaration]) -> list[dict[str, Any]]:
    grouped: dict[str, list[DependencyDeclaration]] = {}
    for item in declarations:
        if item.normalized_name:
            grouped.setdefault(item.normalized_name, []).append(item)
    findings = []
    for name in sorted(grouped):
        items = grouped[name]
        if len(items) < 2:
            continue
        pins = sorted({_exact_pin(item.raw) or item.raw for item in items})
        code = "conflicting-pins" if len(pins) > 1 else "duplicate-pin"
        findings.append({
            "code": code,
            "normalized_name": name,
            "requirements": [
                {"source": item.source, "section": item.section, "requirement": item.raw}
                for item in sorted(items, key=lambda value: (value.source, value.section, value.raw))
            ],
        })
    return findings


def _as_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)]


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _is_exact_pin(raw: str) -> bool:
    return _exact_pin(raw) is not None and "," not in raw.split(";", 1)[0]


def _exact_pin(raw: str) -> str | None:
    match = _EXACT_PIN.search(raw)
    return match.group(1) if match else None


def _has_wildcard(raw: str) -> bool:
    return "*" in raw or ".*" in raw


def _has_prerelease(raw: str) -> bool:
    return any(_PRERELEASE.search(version) for version in _VERSION_TOKEN.findall(raw))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("-o", "--output", type=Path, help="write JSON report to this file")
    args = parser.parse_args(argv)
    report = audit_dependencies(args.root)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
