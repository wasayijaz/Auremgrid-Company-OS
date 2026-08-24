from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auremgrid.domain.errors import ValidationError
from auremgrid.storage.migrations import schema_version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".manifest.json")


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def verify_backup(path: str | Path, expected_sha256: str | None = None) -> dict[str, Any]:
    backup_path = Path(path).resolve()
    if not backup_path.is_file():
        raise ValidationError(f"backup does not exist: {backup_path}")
    actual_hash = _sha256(backup_path)
    if expected_sha256 and not hmac.compare_digest(actual_hash, expected_sha256):
        raise ValidationError("backup checksum does not match its manifest")
    connection = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if integrity != "ok":
            raise ValidationError(f"backup integrity check failed: {integrity}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ValidationError(f"backup foreign-key check failed with {len(foreign_key_errors)} violations")
        version = schema_version(connection)
        table_count = int(
            connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        )
        representative_counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("organizations", "workspaces", "documents", "workflow_runs", "ledger_audit")
            if _table_exists(connection, table)
        }
    finally:
        connection.close()
    return {
        "path": str(backup_path),
        "sha256": actual_hash,
        "schema_version": version,
        "table_count": table_count,
        "integrity": integrity,
        "foreign_key_violations": 0,
        "representative_counts": representative_counts,
        "size_bytes": backup_path.stat().st_size,
    }


def create_backup(connection: sqlite3.Connection, destination: str | Path) -> dict[str, Any]:
    backup_path = Path(destination).resolve()
    if backup_path.exists():
        raise ValidationError("backup destination already exists")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(backup_path)
    try:
        connection.backup(target)
    finally:
        target.close()
    verification = verify_backup(backup_path)
    manifest = {
        **verification,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "format": "sqlite-backup-v1",
    }
    _manifest_path(backup_path).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def restore_backup(source: str | Path, destination: str | Path, overwrite: bool = False) -> dict[str, Any]:
    backup_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if backup_path == destination_path:
        raise ValidationError("backup source and restore destination must differ")
    if destination_path.exists() and not overwrite:
        raise ValidationError("restore destination already exists; explicit overwrite is required")
    expected_hash = None
    manifest_path = _manifest_path(backup_path)
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_hash = str(manifest.get("sha256") or "") or None
    source_verification = verify_backup(backup_path, expected_hash)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    safety_backup = None
    if destination_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety_path = destination_path.with_name(f"{destination_path.name}.pre-restore-{stamp}.sqlite")
        active_connection = sqlite3.connect(destination_path)
        try:
            safety_backup = create_backup(active_connection, safety_path)
        finally:
            active_connection.close()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", suffix=".restore", dir=destination_path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
        target_connection = sqlite3.connect(temporary_path)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
            source_connection.close()
        verify_backup(temporary_path, source_verification["sha256"])
        recovery_connection = sqlite3.connect(temporary_path)
        try:
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            if _table_exists(recovery_connection, "auth_sessions"):
                recovery_connection.execute(
                    "UPDATE auth_sessions SET revoked_at=COALESCE(revoked_at, ?)", (now,)
                )
            if _table_exists(recovery_connection, "api_tokens"):
                recovery_connection.execute(
                    "UPDATE api_tokens SET revoked_at=COALESCE(revoked_at, ?)", (now,)
                )
            if _table_exists(recovery_connection, "jobs"):
                columns = {row[1] for row in recovery_connection.execute("PRAGMA table_info(jobs)").fetchall()}
                if {"status", "lease_owner", "lease_expires_at"}.issubset(columns):
                    recovery_connection.execute(
                        "UPDATE jobs SET status='retry_wait', lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, updated_at=? "
                        "WHERE status IN ('leased','running')"
                        , (now,)
                    )
            if _table_exists(recovery_connection, "outbox_events"):
                recovery_connection.execute(
                    "UPDATE outbox_events SET lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=? "
                    "WHERE status!='published'", (now,)
                )
            if _table_exists(recovery_connection, "system_state"):
                recovery_connection.execute(
                    "INSERT INTO system_state(key,value,updated_at) VALUES ('recovery_mode','1',?) "
                    "ON CONFLICT(key) DO UPDATE SET value='1',updated_at=excluded.updated_at",
                    (now,),
                )
                recovery_connection.execute(
                    "INSERT INTO system_state(key,value,updated_at) VALUES ('outbound_dispatch','disabled',?) "
                    "ON CONFLICT(key) DO UPDATE SET value='disabled',updated_at=excluded.updated_at",
                    (now,),
                )
            recovery_connection.commit()
        finally:
            recovery_connection.close()
        restored = verify_backup(temporary_path)
        os.replace(temporary_path, destination_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return {
        **restored,
        "path": str(destination_path),
        "restored_from": str(backup_path),
        "source_sha256": source_verification["sha256"],
        "safety_backup": safety_backup,
        "recovery_mode": True,
        "outbound_dispatch": "disabled",
    }

def rotate_backups(directory: str | Path, keep_daily: int = 7, keep_weekly: int = 4) -> list[dict[str, Any]]:
    """Delete old backups beyond retention policy. Returns list of removed paths."""
    backup_dir = Path(directory)
    if not backup_dir.is_dir():
        raise ValidationError(f"backup directory does not exist: {backup_dir}")
    backups = []
    for f in backup_dir.iterdir():
        if not f.is_file() or f.suffix != ".sqlite":
            continue
        manifest = _manifest_path(f)
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                backups.append({"path": f, "manifest": data})
            except (json.JSONDecodeError, KeyError):
                backups.append({"path": f, "manifest": {}})
        else:
            backups.append({"path": f, "manifest": {}})
    if keep_daily < 0 or keep_weekly < 0:
        raise ValidationError("backup retention counts must be non-negative")
    backups.sort(key=lambda b: b["manifest"].get("created_at", ""), reverse=True)
    kept_paths: set[Path] = set()
    weekly_seen: set[str] = set()
    daily_kept = 0
    weekly_kept = 0
    for b in backups:
        created_at = str(b["manifest"].get("created_at") or "")
        if daily_kept < keep_daily:
            kept_paths.add(b["path"])
            daily_kept += 1
            continue
        try:
            weekly_key = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date().isocalendar()[:2]
            weekly_label = f"{weekly_key[0]}-W{weekly_key[1]:02d}"
        except ValueError:
            weekly_label = ""
        if weekly_label and weekly_label not in weekly_seen and weekly_kept < keep_weekly:
            kept_paths.add(b["path"])
            weekly_seen.add(weekly_label)
            weekly_kept += 1
    removed = []
    for b in backups:
        if b["path"] not in kept_paths:
            b["path"].unlink(missing_ok=True)
            mp = _manifest_path(b["path"])
            mp.unlink(missing_ok=True)
            removed.append({"path": str(b["path"]), "deleted": True})
    return removed


def check_integrity(connection: sqlite3.Connection) -> dict[str, Any]:
    """Run integrity checks and return structured result."""
    quick = str(connection.execute("PRAGMA quick_check").fetchone()[0] or "")
    fk_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    wal = connection.execute("PRAGMA journal_mode").fetchone()[0]
    version = schema_version(connection)
    return {
        "integrity": quick,
        "foreign_key_violations": len(fk_errors),
        "journal_mode": str(wal),
        "schema_version": version,
        "healthy": quick == "ok" and len(fk_errors) == 0,
    }


def list_backup_points(directory: str | Path) -> list[dict[str, Any]]:
    """Read manifests and return a timeline of backup points."""
    backup_dir = Path(directory)
    if not backup_dir.is_dir():
        return []
    points = []
    for f in backup_dir.iterdir():
        if not f.is_file() or f.suffix != ".sqlite":
            continue
        manifest = _manifest_path(f)
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            points.append({
                "path": str(f),
                "created_at": data.get("created_at"),
                "schema_version": data.get("schema_version"),
                "sha256": data.get("sha256"),
                "size_bytes": data.get("size_bytes"),
                "integrity": data.get("integrity"),
            })
        except (json.JSONDecodeError, KeyError):
            continue
    points.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    return points
