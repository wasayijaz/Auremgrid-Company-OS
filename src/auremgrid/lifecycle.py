"""Process lifecycle: startup health gates, graceful shutdown, readiness."""
from __future__ import annotations

import logging
import os
import signal
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_READINESS_FILE = ".auremgrid-ready"


def startup_health(conn: sqlite3.Connection, db_path: str | Path) -> list[str]:
    """Run startup health gates. Returns list of warnings (empty = healthy)."""
    warnings = []
    db = str(db_path)

    # Recovery mode must not be active
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = ?", ("recovery_mode",)
    ).fetchone()
    if row and row["value"] == "1":
        warnings.append("recovery_mode is active; outbound dispatch is disabled")

    # WAL mode for file databases
    if db != ":memory:":
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.upper() != "WAL":
            warnings.append("journal_mode is " + str(mode) + " (expected WAL)")

    # Foreign keys enforced
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    if fk != 1:
        warnings.append("foreign_keys is not enforced")

    # Schema version present
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    version = row["v"] if row["v"] else 0
    if version == 0:
        warnings.append("no schema migrations applied")

    return warnings


class GracefulShutdown:
    """Register signal handlers and manage graceful shutdown."""

    def __init__(
        self,
        *,
        on_shutdown: Callable[[], None] | None = None,
        drain_timeout: float = 10.0,
    ) -> None:
        self._on_shutdown = on_shutdown
        self._drain_timeout = drain_timeout
        self._triggered = threading.Event()
        self._lock = threading.Lock()

    @property
    def shutdown_requested(self) -> bool:
        return self._triggered.is_set()

    def request_shutdown(self) -> None:
        with self._lock:
            if self._triggered.is_set():
                return
            self._triggered.set()
            logger.info("shutdown requested")
            if self._on_shutdown:
                try:
                    self._on_shutdown()
                except Exception:
                    logger.exception("shutdown callback failed")

    def install_signal_handlers(self) -> None:
        """Register SIGINT and SIGTERM handlers (no-op on Windows SIGTERM)."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._handle_signal)
            except (OSError, ValueError):
                pass

    def _handle_signal(self, signum: int, frame: Any) -> None:
        self.request_shutdown()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until shutdown is requested. Returns True if shutdown."""
        return self._triggered.wait(timeout)


def write_readiness_sentinel(directory: str | Path) -> Path:
    """Write a readiness sentinel file. Returns its path."""
    p = Path(directory) / _READINESS_FILE
    p.write_text("ready")
    return p


def remove_readiness_sentinel(directory: str | Path) -> None:
    """Remove the readiness sentinel file if present."""
    p = Path(directory) / _READINESS_FILE
    p.unlink(missing_ok=True)


def is_ready(directory: str | Path) -> bool:
    """Check if the readiness sentinel exists."""
    return (Path(directory) / _READINESS_FILE).is_file()