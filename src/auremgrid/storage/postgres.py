"""PostgreSQL storage backend implementing the StoragePort protocol."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any, Iterator

try:
    import psycopg
    from psycopg.rows import dict_row
    _HAS_PSYCOPG = True
except ImportError:
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment,misc]
    _HAS_PSYCOPG = False

from auremgrid.domain.errors import ValidationError
from auremgrid.storage.migrations import schema_version as _schema_version


class PostgresStore:
    """PostgreSQL storage backend. Requires the [postgres] optional dependency."""

    def __init__(self, url: str) -> None:
        if not _HAS_PSYCOPG:
            raise ValidationError(
                "psycopg is required for PostgreSQL storage; install with: pip install auremgrid-company-os[postgres]"
            )
        self.url = url
        self.conn = psycopg.connect(url, row_factory=dict_row, autocommit=False)
        self._lock = threading.RLock()
        self._transaction_depth = 0

    @property
    def schema_version(self) -> int:
        row = self.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"]) if row and row["v"] else 0

    @property
    def raw_connection(self) -> Any:
        return self.conn

    @contextmanager
    def atomic(self, *, immediate: bool = False) -> Iterator[Any]:
        """Own one transaction. The immediate flag is accepted for interface compat but Postgres uses standard BEGIN."""
        with self._lock:
            outermost = self._transaction_depth == 0
            if outermost:
                self.conn.execute("BEGIN")
            self._transaction_depth += 1
            try:
                yield self.conn
                self._transaction_depth -= 1
                if outermost:
                    self.conn.commit()
            except Exception:
                self._transaction_depth -= 1
                if outermost:
                    self.conn.rollback()
                raise

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, params_list: list[tuple]) -> Any:
        return self.conn.executemany(sql, params_list)

    def fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
