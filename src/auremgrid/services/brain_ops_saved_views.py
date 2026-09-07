from __future__ import annotations
from auremgrid.services.brain_ops_shared import *

class BrainOpsSavedViewsMixin:
    def save_view(self, identity: Any, workspace_id: str, name: str, *,
            query: Any, filters: Any | None = None, sort: Any | None = None,
            folder_id: str | None = None, description: str = "", visibility: str = "owner",
            idempotency_key: str | None = None) -> dict[str, Any]:
            organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
            name = self._clean_name(name, "saved view name")
            visibility = self._visibility(visibility)
            self._require_folder(workspace_id, folder_id)
            query_json = self._json_payload(query, "query")
            filters_json = self._json_payload(filters or {}, "filters")
            sort_json = self._json_payload(sort or [], "sort")
            payload = {
                "name": name, "description": description, "visibility": visibility,
                "folder_id": folder_id, "query_json": query_json,
                "filters_json": filters_json, "sort_json": sort_json,
            }
            prior = self._idempotent_row("brain_saved_views", workspace_id, person_id, idempotency_key, "owner_person_id")
            if prior is not None:
                self._require_same_payload(dict(prior), payload, tuple(payload.keys()))
                return self._saved_view_from_row(prior)
            now = _now().isoformat()
            view_id = self._id("brain_view")
            item = {
                "id": view_id, "organization_id": organization_id, "workspace_id": workspace_id,
                "folder_id": folder_id, "name": name, "description": description,
                "owner_person_id": person_id, "visibility": visibility, "query_json": query_json,
                "filters_json": filters_json, "sort_json": sort_json, "created_at": now,
                "updated_at": now, "version": 1, "idempotency_key": idempotency_key,
            }
            with self.os.store.atomic(immediate=True):
                self.conn.execute("""INSERT INTO brain_saved_views(
                    id,organization_id,workspace_id,folder_id,name,description,owner_person_id,
                    visibility,query_json,filters_json,sort_json,created_at,updated_at,version,idempotency_key
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(item.values()))
                self._insert_saved_view_version(view_id, organization_id, workspace_id, 1, name, description, visibility, query_json, filters_json, sort_json, person_id, "created", now)
                self._brain_audit(organization_id, workspace_id, "saved_view", view_id, "create", person_id, 1, idempotency_key, payload, now)
            return self._saved_view_from_row(item)

    def update_view(self, identity: Any, workspace_id: str, saved_view_id: str, *,
            name: str | None = None, query: Any | None = None, filters: Any | None = None,
            sort: Any | None = None, description: str | None = None, visibility: str | None = None,
            folder_id: str | None = None, idempotency_key: str | None = None,
            reason: str = "updated") -> dict[str, Any]:
            organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
            row = self.os.store.get_brain_saved_view(workspace_id, saved_view_id)
            if row is None:
                raise NotFoundError("saved view not found")
            if row["owner_person_id"] != person_id:
                raise AuthorizationError("saved view owner required")
            prior = self._prior_mutation(workspace_id, "saved_view", saved_view_id, "update", idempotency_key)
            if prior is not None:
                return self._saved_view_from_row(self.os.store.get_brain_saved_view(workspace_id, saved_view_id))
            next_name = self._clean_name(name, "saved view name") if name is not None else str(row["name"])
            next_description = description if description is not None else str(row["description"])
            next_visibility = self._visibility(visibility) if visibility is not None else str(row["visibility"])
            next_folder_id = folder_id if folder_id is not None else row["folder_id"]
            self._require_folder(workspace_id, next_folder_id)
            query_json = self._json_payload(query, "query") if query is not None else str(row["query_json"])
            filters_json = self._json_payload(filters, "filters") if filters is not None else str(row["filters_json"])
            sort_json = self._json_payload(sort, "sort") if sort is not None else str(row["sort_json"])
            payload = {
                "name": next_name, "description": next_description, "visibility": next_visibility,
                "folder_id": next_folder_id, "query_json": query_json,
                "filters_json": filters_json, "sort_json": sort_json,
            }
            old_payload = {key: row[key] for key in payload}
            if payload == old_payload:
                return self._saved_view_from_row(row)
            now = _now().isoformat()
            next_version = int(row["version"]) + 1
            with self.os.store.atomic(immediate=True):
                cursor = self.conn.execute("""UPDATE brain_saved_views
                    SET folder_id=?,name=?,description=?,visibility=?,query_json=?,
                        filters_json=?,sort_json=?,updated_at=?,version=?
                    WHERE workspace_id=? AND id=? AND version=?""",
                    (next_folder_id, next_name, next_description, next_visibility, query_json,
                     filters_json, sort_json, now, next_version, workspace_id, saved_view_id, row["version"]))
                if cursor.rowcount != 1:
                    raise ValidationError("saved view version changed")
                self._insert_saved_view_version(saved_view_id, organization_id, workspace_id, next_version, next_name, next_description, next_visibility, query_json, filters_json, sort_json, person_id, reason, now)
                self._brain_audit(organization_id, workspace_id, "saved_view", saved_view_id, "update", person_id, next_version, idempotency_key, payload, now)
            return self._saved_view_from_row(self.os.store.get_brain_saved_view(workspace_id, saved_view_id))

    def get_view(self, identity: Any, workspace_id: str, saved_view_id: str) -> dict[str, Any]:
            _, person_id = self._brain_identity(identity, workspace_id, "brain_read", write=False)
            row = self.os.store.get_brain_saved_view(workspace_id, saved_view_id)
            if row is None:
                raise NotFoundError("saved view not found")
            if row["visibility"] != "shared" and row["owner_person_id"] != person_id:
                raise NotFoundError("saved view not found")
            return self._saved_view_from_row(row)

    def list_views(self, identity: Any, workspace_id: str) -> list[dict[str, Any]]:
            _, person_id = self._brain_identity(identity, workspace_id, "brain_read", write=False)
            rows = self.conn.execute(
                """SELECT * FROM brain_saved_views
                   WHERE workspace_id=? AND (visibility='shared' OR owner_person_id=?)
                   ORDER BY updated_at DESC, id DESC""",
                (workspace_id, person_id),
            ).fetchall()
            return [self._saved_view_from_row(row) for row in rows]

    def view_versions(self, identity: Any, workspace_id: str, saved_view_id: str) -> list[dict[str, Any]]:
            self.get_view(identity, workspace_id, saved_view_id)
            rows = self.os.store.list_brain_saved_view_versions(workspace_id, saved_view_id)
            return [self._saved_view_version_from_row(row) for row in rows]

    def _saved_view_from_row(self, row: Any) -> dict[str, Any]:
            item = dict(row)
            item["query"] = json.loads(str(item.pop("query_json")))
            item["filters"] = json.loads(str(item.pop("filters_json")))
            item["sort"] = json.loads(str(item.pop("sort_json")))
            return item

    def _saved_view_version_from_row(self, row: Any) -> dict[str, Any]:
            item = dict(row)
            item["query"] = json.loads(str(item.pop("query_json")))
            item["filters"] = json.loads(str(item.pop("filters_json")))
            item["sort"] = json.loads(str(item.pop("sort_json")))
            return item

    def _brain_audit(self, organization_id: str, workspace_id: str, entity_type: str, entity_id: str,
            action: str, actor_person_id: str, version: int | None, idempotency_key: str | None,
            payload: dict[str, object], created_at: str) -> None:
            self.os.store.record_brain_audit(
                audit_id=self._id("brain_audit"), organization_id=organization_id,
                workspace_id=workspace_id, entity_type=entity_type, entity_id=entity_id,
                action=action, actor_person_id=actor_person_id, version=version,
                idempotency_key=idempotency_key, payload=payload, created_at=created_at,
            )

    def _insert_saved_view_version(self, saved_view_id: str, organization_id: str, workspace_id: str,
            version: int, name: str, description: str, visibility: str, query_json: str,
            filters_json: str, sort_json: str, changed_by_person_id: str, reason: str, created_at: str) -> None:
            self.conn.execute("""INSERT INTO brain_saved_view_versions(
                id,saved_view_id,organization_id,workspace_id,version,name,description,visibility,
                query_json,filters_json,sort_json,changed_by_person_id,change_reason,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                self._id("brain_view_ver"), saved_view_id, organization_id, workspace_id, version,
                name, description, visibility, query_json, filters_json, sort_json,
                changed_by_person_id, reason, created_at,
            ))

    def _idempotent_row(self, table: str, workspace_id: str, person_id: str,
            idempotency_key: str | None, person_column: str = "created_by_person_id") -> Any:
            if idempotency_key is None:
                return None
            return self.conn.execute(
                f"SELECT * FROM {table} WHERE workspace_id=? AND {person_column}=? AND idempotency_key=?",
                (workspace_id, person_id, idempotency_key),
            ).fetchone()

    def _prior_mutation(self, workspace_id: str, entity_type: str, entity_id: str,
            action: str, idempotency_key: str | None) -> Any:
            if idempotency_key is None:
                return None
            return self.conn.execute(
                """SELECT * FROM brain_mutation_audit
                   WHERE workspace_id=? AND entity_type=? AND entity_id=? AND action=? AND idempotency_key=?
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (workspace_id, entity_type, entity_id, action, idempotency_key),
            ).fetchone()
