from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


VALID_SCOPES = ("organization", "workspace", "connector")
VALID_ACTIONS = ("archive", "delete", "redact")
DELETABLE_TABLES = frozenset({
    "projects",
    "deliverables",
    "work_items",
    "campaigns",
    "creative_assets",
    "campaign_metric_snapshots",
    "creative_performance",
    "content_performance",
    "feedback_patterns",
    "feedback_events",
    "performance_insights",
    "revenues",
    "contracts",
    "meetings",
    "documents",
})

SENSITIVE_EXPORT_TABLES = frozenset({
    "api_tokens",
    "auth_invites",
    "auth_principals",
    "auth_sessions",
    "local_secret_vault",
    "oauth_states",
    "principal_actor_bindings",
    "secret_bindings",
    "system_state",
})

SENSITIVE_EXPORT_COLUMN_MARKERS = (
    "ciphertext",
    "credential",
    "fingerprint",
    "hash",
    "idempotency_key",
    "lease_owner",
    "lease_token",
    "reference",
    "reservation_token",
    "secret",
    "token",
)


class RetentionOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any]) -> None:
        self.conn, self.new_id, self.authorize = conn, new_id, authorize

    def create_policy(self, organization_id: str, person_id: str, scope: str,
        data_category: str, max_age_days: int, action: str, scope_id: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id, person_id, write=True)
        if scope not in VALID_SCOPES:
            raise ValidationError(f"invalid scope: {scope}")
        if action not in VALID_ACTIONS:
            raise ValidationError(f"invalid action: {action}")
        if max_age_days < 1:
            raise ValidationError("max_age_days must be positive")
        now = _now().isoformat()
        pid = self.new_id("rp")
        self.conn.execute(
            "INSERT INTO retention_policies (id, organization_id, scope, scope_id, data_category, max_age_days, action, created_by_person_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, organization_id, scope, scope_id, data_category, max_age_days, action, person_id, now))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM retention_policies WHERE id=?", (pid,)).fetchone())

    def list_policies(self, organization_id: str, person_id: str, scope: str | None = None) -> list[dict[str, Any]]:
        self.authorize(organization_id, person_id)
        sql = "SELECT * FROM retention_policies WHERE organization_id=?"
        params: list[Any] = [organization_id]
        if scope:
            sql += " AND scope=?"; params.append(scope)
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def execute_deletion(self, organization_id: str, person_id: str,
        table_name: str, record_ids: list[str], reason: str, policy_id: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id, person_id, write=True)
        if table_name not in DELETABLE_TABLES:
            raise ValidationError(f"deletion is not allowed for table: {table_name}")
        if not record_ids:
            raise ValidationError("record_ids must not be empty")
        now = _now().isoformat()
        deleted = []
        for rid in record_ids:
            row = self.conn.execute(f"SELECT * FROM {table_name} WHERE id=? AND organization_id=?", (rid, organization_id)).fetchone()
            if row is None:
                continue
            snapshot = json.dumps(dict(row))
            audit_id = self.new_id("da")
            self.conn.execute(
                "INSERT INTO deletion_audit (id, organization_id, table_name, record_id, reason, initiated_by, retention_policy_id, snapshot_json, deleted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (audit_id, organization_id, table_name, rid, reason, person_id, policy_id, snapshot, now))
            if table_name == "campaigns":
                # Metric snapshots are campaign-owned evidence. Retaining them after
                # deleting their campaign would leave orphaned performance data that
                # can survive lifecycle cleanup through direct metric reads/counts.
                self.conn.execute(
                    "DELETE FROM campaign_metric_snapshots WHERE organization_id=? AND campaign_id=?",
                    (organization_id, rid),
                )
            elif table_name == "creative_assets":
                # Creative performance is asset-owned evidence. Remove it with the
                # asset so lifecycle deletion cannot leave orphaned metrics behind.
                self.conn.execute(
                    "DELETE FROM creative_performance WHERE asset_id=?",
                    (rid,),
                )
            self.conn.execute(f"DELETE FROM {table_name} WHERE id=? AND organization_id=?", (rid, organization_id))
            deleted.append({"id": rid, "audit_id": audit_id})
        self.conn.commit()
        return {"deleted": deleted, "count": len(deleted)}

    def export_workspace(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id)
        data: dict[str, list[dict[str, Any]]] = {}
        table_columns = self._table_columns()
        for table, columns in table_columns.items():
            if table in SENSITIVE_EXPORT_TABLES or "workspace_id" not in columns:
                continue
            rows = self.conn.execute(
                f"SELECT * FROM {table} WHERE workspace_id=? ORDER BY {self._order_by(table, columns)}",
                (workspace_id,),
            ).fetchall()
            if rows:
                data[table] = [self._export_row(row) for row in rows]
        data["_meta"] = {
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "exported_at": _now().isoformat(),
            "redacted_columns": sorted(SENSITIVE_EXPORT_COLUMN_MARKERS),
            "omitted_tables": sorted(SENSITIVE_EXPORT_TABLES),
        }
        return data

    def _table_columns(self) -> dict[str, set[str]]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'"
        ).fetchall()
        tables: dict[str, set[str]] = {}
        for row in rows:
            table = row["name"]
            tables[table] = {column[1] for column in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        return tables

    def _order_by(self, table: str, columns: set[str]) -> str:
        if "created_at" in columns and "id" in columns:
            return "created_at,id"
        if "recorded_at" in columns and "id" in columns:
            return "recorded_at,id"
        if "id" in columns:
            return "id"
        return ",".join(sorted(columns))

    def _export_row(self, row: Any) -> dict[str, Any]:
        exported = dict(row)
        for column in list(exported):
            lowered = column.lower()
            if any(marker in lowered for marker in SENSITIVE_EXPORT_COLUMN_MARKERS):
                exported[column] = "[REDACTED]"
        return exported
