from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


VALID_SCOPES = ("organization", "workspace", "connector")
VALID_ACTIONS = ("delete",)
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
    "sources",
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
        table_columns = self._table_columns()
        columns = table_columns.get(table_name)
        if columns is None:
            raise ValidationError(f"deletion table is unavailable: {table_name}")
        for rid in record_ids:
            row = self._find_deletable_row(organization_id, table_name, columns, rid)
            if row is None:
                continue
            row_data = dict(row)
            workspace_id = row_data.get("workspace_id")
            snapshot = json.dumps(self._audit_snapshot(table_name, row_data), sort_keys=True, separators=(",", ":"))
            audit_id = self.new_id("da")
            self._insert_audit(audit_id, organization_id, workspace_id, table_name, rid, reason, person_id, policy_id, snapshot, now)
            if table_name == "documents":
                self._delete_document_evidence(row_data)
            elif table_name == "sources":
                self._delete_source_evidence(row_data)
            elif table_name == "campaigns":
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
            if table_name not in {"documents", "sources"}:
                self.conn.execute(f"DELETE FROM {table_name} WHERE id=?", (rid,))
            deleted.append({"id": rid, "audit_id": audit_id})
        self.conn.commit()
        return {"deleted": deleted, "count": len(deleted)}

    def _find_deletable_row(self, organization_id: str, table_name: str, columns: set[str], record_id: str) -> Any | None:
        if "id" not in columns:
            raise ValidationError(f"deletion table has no id column: {table_name}")
        if "organization_id" in columns:
            return self.conn.execute(
                f"SELECT * FROM {table_name} WHERE id=? AND organization_id=?",
                (record_id, organization_id),
            ).fetchone()
        if "workspace_id" in columns:
            return self.conn.execute(
                f"""SELECT target.* FROM {table_name} target
                    JOIN workspace_organization scope ON scope.workspace_id=target.workspace_id
                    WHERE target.id=? AND scope.organization_id=?""",
                (record_id, organization_id),
            ).fetchone()
        raise ValidationError(f"deletion table is not organization-scoped: {table_name}")

    def _insert_audit(self, audit_id: str, organization_id: str, workspace_id: str | None, table_name: str,
        record_id: str, reason: str, person_id: str, policy_id: str | None, snapshot: str, now: str) -> None:
        columns = self._table_columns().get("deletion_audit", set())
        if "workspace_id" in columns:
            self.conn.execute(
                "INSERT INTO deletion_audit (id, organization_id, workspace_id, table_name, record_id, reason, initiated_by, retention_policy_id, snapshot_json, deleted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (audit_id, organization_id, workspace_id, table_name, record_id, reason, person_id, policy_id, snapshot, now),
            )
            return
        self.conn.execute(
            "INSERT INTO deletion_audit (id, organization_id, table_name, record_id, reason, initiated_by, retention_policy_id, snapshot_json, deleted_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (audit_id, organization_id, table_name, record_id, reason, person_id, policy_id, snapshot, now),
        )

    def _audit_snapshot(self, table_name: str, row: dict[str, Any]) -> dict[str, Any]:
        sensitive_fields = {
            "content", "evidence", "evidence_span", "sample_evidence", "raw_feedback",
            "locator", "source_locator", "source_url", "final_url", "thumbnail_url",
            "subject", "predicate", "object", "from_entity", "relation", "to_entity",
        }
        snapshot = {"table_name": table_name}
        for key, value in row.items():
            lowered = key.lower()
            if lowered in sensitive_fields or any(marker in lowered for marker in SENSITIVE_EXPORT_COLUMN_MARKERS):
                snapshot[key] = "[REDACTED]"
            else:
                snapshot[key] = value
        return snapshot

    def _delete_document_evidence(self, document: dict[str, Any]) -> None:
        workspace_id = document["workspace_id"]
        document_id = document["id"]
        source_id = document["source_id"]
        self._delete_optional("facts", "workspace_id=? AND document_id=?", (workspace_id, document_id))
        self._delete_optional("relations", "workspace_id=? AND document_id=?", (workspace_id, document_id))
        self._delete_optional("document_embedding_projection", "workspace_id=? AND document_id=?", (workspace_id, document_id))
        if self._table_exists("documents_fts"):
            self.conn.execute("DELETE FROM documents_fts WHERE workspace_id=? AND document_id=?", (workspace_id, document_id))
        self._delete_optional("brain_document_tags", "workspace_id=? AND document_id=?", (workspace_id, document_id))
        self._delete_optional(
            "brain_collection_items",
            "workspace_id=? AND item_type='document' AND item_id=?",
            (workspace_id, document_id),
        )
        self.conn.execute("DELETE FROM documents WHERE workspace_id=? AND id=?", (workspace_id, document_id))
        remaining = self.conn.execute(
            "SELECT 1 FROM documents WHERE workspace_id=? AND source_id=? LIMIT 1",
            (workspace_id, source_id),
        ).fetchone()
        if remaining is None:
            source = self.conn.execute(
                "SELECT * FROM sources WHERE workspace_id=? AND id=?",
                (workspace_id, source_id),
            ).fetchone()
            if source is not None:
                self._delete_source_evidence(dict(source), delete_documents=False)

    def _delete_source_evidence(self, source: dict[str, Any], *, delete_documents: bool = True) -> None:
        workspace_id = source["workspace_id"]
        source_id = source["id"]
        if delete_documents:
            documents = self.conn.execute(
                "SELECT * FROM documents WHERE workspace_id=? AND source_id=?",
                (workspace_id, source_id),
            ).fetchall()
            for document in documents:
                self._delete_document_evidence(dict(document))
        self._delete_optional("facts", "workspace_id=? AND source_id=?", (workspace_id, source_id))
        self._delete_optional("relations", "workspace_id=? AND source_id=?", (workspace_id, source_id))
        self._delete_optional("brain_source_tags", "workspace_id=? AND source_id=?", (workspace_id, source_id))
        self._delete_optional(
            "brain_collection_items",
            "workspace_id=? AND item_type='source' AND item_id=?",
            (workspace_id, source_id),
        )
        self._retire_source_lifecycle(workspace_id, source_id)
        self._retire_provider_routes(workspace_id, source_id)
        if self._source_can_be_deleted(workspace_id, source_id):
            self.conn.execute("DELETE FROM sources WHERE workspace_id=? AND id=?", (workspace_id, source_id))

    def _delete_optional(self, table_name: str, where_sql: str, params: tuple[Any, ...]) -> None:
        if table_name in self._table_columns():
            self.conn.execute(f"DELETE FROM {table_name} WHERE {where_sql}", params)

    def _table_exists(self, table_name: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','virtual table') AND name=?",
            (table_name,),
        ).fetchone() is not None

    def _retire_source_lifecycle(self, workspace_id: str, source_id: str) -> None:
        if "source_lifecycle_intervals" not in self._table_columns():
            return
        now = _now().isoformat()
        self.conn.execute(
            """UPDATE source_lifecycle_intervals
               SET retired_at=?, retirement_reason='retention_delete', effective_until=?
               WHERE workspace_id=? AND source_id=? AND retired_at IS NULL""",
            (now, now, workspace_id, source_id),
        )

    def _retire_provider_routes(self, workspace_id: str, source_id: str) -> None:
        if "provider_object_routes" not in self._table_columns():
            return
        now = _now().isoformat()
        self.conn.execute(
            """UPDATE provider_object_routes
               SET active_source_id=NULL, status='retired', retired_at=?, updated_at=?
               WHERE workspace_id=? AND active_source_id=? AND status='active'""",
            (now, now, workspace_id, source_id),
        )

    def _source_can_be_deleted(self, workspace_id: str, source_id: str) -> bool:
        for table_name in (
            "source_lifecycle_intervals",
            "provider_object_route_events",
            "provider_route_mutation_staging",
            "graphiti_episode_mappings",
        ):
            columns = self._table_columns().get(table_name)
            if not columns or "source_id" not in columns:
                continue
            if self.conn.execute(
                f"SELECT 1 FROM {table_name} WHERE workspace_id=? AND source_id=? LIMIT 1",
                (workspace_id, source_id),
            ).fetchone() is not None:
                return False
        return True

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
