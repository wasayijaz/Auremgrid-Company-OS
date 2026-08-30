"""Rehearse upgrading a deterministic database from the prior supported schema."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
try:
    sys.path.remove(str(SRC_ROOT))
except ValueError:
    pass
sys.path.insert(0, str(SRC_ROOT))

from auremgrid.storage.migrations import MIGRATIONS, migrate, schema_version
from auremgrid.storage.sqlite import SCHEMA


def run_rehearsal(path: Path) -> dict[str, Any]:
    """Build the prior schema from the canonical fixture, then migrate it forward."""
    previous = MIGRATIONS[-2]
    latest = MIGRATIONS[-1]
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        source_version = migrate(conn, target_version=previous.version)
        if source_version != previous.version:
            raise RuntimeError(f"expected source schema {previous.version}, got {source_version}")
        upgraded_version = migrate(conn)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    return {
        "status": "ok" if upgraded_version == latest.version and integrity == "ok" else "failed",
        "source_schema_version": source_version,
        "target_schema_version": latest.version,
        "upgraded_schema_version": upgraded_version,
        "integrity": integrity,
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="auremgrid-forward-migration-") as directory:
        result = run_rehearsal(Path(directory) / "prior-schema.sqlite")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
