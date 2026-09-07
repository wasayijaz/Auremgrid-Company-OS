"""Generate a deterministic CycloneDX 1.5 SBOM from installed metadata."""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _distribution_name(requirement: str) -> str | None:
    match = _REQUIREMENT_NAME.match(requirement)
    return match.group(1) if match else None


def _component(distribution: metadata.Distribution) -> dict[str, Any]:
    name = distribution.metadata.get("Name") or distribution.name
    version = distribution.version
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    return {"type": "library", "name": name, "version": version, "purl": f"pkg:pypi/{normalized}@{version}"}


def _distributions(project: str) -> list[metadata.Distribution]:
    pending = [project]
    seen: set[str] = set()
    found: dict[str, metadata.Distribution] = {}
    while pending:
        requested = pending.pop(0)
        key = requested.lower().replace("-", "_")
        if key in seen:
            continue
        seen.add(key)
        try:
            distribution = metadata.distribution(requested)
        except metadata.PackageNotFoundError:
            continue
        actual_name = distribution.metadata.get("Name") or distribution.name
        found[actual_name.lower().replace("-", "_")] = distribution
        for requirement in distribution.requires or ():
            dependency = _distribution_name(requirement)
            if dependency:
                pending.append(dependency)
    return sorted(found.values(), key=lambda dist: ((dist.metadata.get("Name") or dist.name).lower(), dist.version))


def generate_sbom(project: str = "auremgrid-company-os") -> dict[str, Any]:
    """Return a deterministic CycloneDX 1.5 BOM for *project* and dependencies."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "components": [_component(distribution) for distribution in _distributions(project)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-p", "--project", default="auremgrid-company-os")
    parser.add_argument("-o", "--output", type=Path, help="write JSON to this file")
    args = parser.parse_args(argv)
    rendered = json.dumps(generate_sbom(args.project), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
