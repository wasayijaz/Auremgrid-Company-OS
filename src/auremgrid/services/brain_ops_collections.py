from __future__ import annotations
from auremgrid.services.brain_ops_shared import *

class BrainOpsCollectionsMixin:
    def create_folder(self, identity: Any, workspace_id: str, name: str,
            parent_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
            organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
            name = self._clean_name(name, "folder name")
            self._validate_folder_parent(workspace_id, parent_id)
            payload = {"name": name, "parent_id": parent_id}
            prior = self._idempotent_row("brain_folders", workspace_id, person_id, idempotency_key)
            if prior is not None:
                self._require_same_payload(dict(prior), payload, ("name", "parent_id"))
                return dict(prior)
            now = _now().isoformat()
            item = {
                "id": self._id("brain_folder"), "organization_id": organization_id,
                "workspace_id": workspace_id, "parent_id": parent_id, "name": name,
                "created_by_person_id": person_id, "created_at": now, "updated_at": now,
                "version": 1, "idempotency_key": idempotency_key,
            }
            with self.os.store.atomic(immediate=True):
                self.conn.execute("""INSERT INTO brain_folders(
                    id,organization_id,workspace_id,parent_id,name,created_by_person_id,
                    created_at,updated_at,version,idempotency_key
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""", tuple(item.values()))
                self._brain_audit(organization_id, workspace_id, "folder", item["id"], "create", person_id, 1, idempotency_key, payload, now)
            return item

    def create_collection(self, identity: Any, workspace_id: str, name: str,
            folder_id: str | None = None, description: str = "", visibility: str = "owner",
            idempotency_key: str | None = None) -> dict[str, Any]:
            organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
            name = self._clean_name(name, "collection name")
            visibility = self._visibility(visibility)
            self._require_folder(workspace_id, folder_id)
            payload = {"name": name, "folder_id": folder_id, "description": description, "visibility": visibility}
            prior = self._idempotent_row("brain_collections", workspace_id, person_id, idempotency_key, "owner_person_id")
            if prior is not None:
                self._require_same_payload(dict(prior), payload, ("name", "folder_id", "description", "visibility"))
                return dict(prior)
            now = _now().isoformat()
            item = {
                "id": self._id("brain_collection"), "organization_id": organization_id,
                "workspace_id": workspace_id, "folder_id": folder_id, "name": name,
                "description": description, "owner_person_id": person_id, "visibility": visibility,
                "created_at": now, "updated_at": now, "version": 1, "idempotency_key": idempotency_key,
            }
            with self.os.store.atomic(immediate=True):
                self.conn.execute("""INSERT INTO brain_collections(
                    id,organization_id,workspace_id,folder_id,name,description,owner_person_id,
                    visibility,created_at,updated_at,version,idempotency_key
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(item.values()))
                self._brain_audit(organization_id, workspace_id, "collection", item["id"], "create", person_id, 1, idempotency_key, payload, now)
            return item

    def create_tag(self, identity: Any, workspace_id: str, name: str, color: str | None = None,
            idempotency_key: str | None = None) -> dict[str, Any]:
            organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
            name = self._clean_name(name, "tag name")
            normalized = _norm(name)
            if not normalized:
                raise ValidationError("tag name is required")
            payload = {"name": name, "normalized_name": normalized, "color": color}
            prior = self._idempotent_row("brain_tags", workspace_id, person_id, idempotency_key)
            if prior is not None:
                self._require_same_payload(dict(prior), payload, ("name", "normalized_name", "color"))
                return dict(prior)
            existing = self.conn.execute(
                "SELECT * FROM brain_tags WHERE workspace_id=? AND normalized_name=?",
                (workspace_id, normalized),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            now = _now().isoformat()
            item = {
                "id": self._id("brain_tag"), "organization_id": organization_id,
                "workspace_id": workspace_id, "name": name, "normalized_name": normalized,
                "color": color, "created_by_person_id": person_id,
                "created_at": now, "updated_at": now, "version": 1,
                "idempotency_key": idempotency_key,
            }
            with self.os.store.atomic(immediate=True):
                self.conn.execute("""INSERT INTO brain_tags(
                    id,organization_id,workspace_id,name,normalized_name,color,created_by_person_id,
                    created_at,updated_at,version,idempotency_key
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", tuple(item.values()))
                self._brain_audit(organization_id, workspace_id, "tag", item["id"], "create", person_id, 1, idempotency_key, payload, now)
            return item

    def tag_source(self, identity: Any, workspace_id: str, source_id: str, tag_id: str,
            idempotency_key: str | None = None) -> dict[str, Any]:
            organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
            self._require_tag(workspace_id, tag_id)
            self._require_allowed_source(identity, workspace_id, source_id)
            prior = self._idempotent_row("brain_source_tags", workspace_id, person_id, idempotency_key, "tagged_by_person_id")
            if prior is not None:
                self._require_same_payload(dict(prior), {"source_id": source_id, "tag_id": tag_id}, ("source_id", "tag_id"))
                return dict(prior)
            existing = self.conn.execute(
                "SELECT * FROM brain_source_tags WHERE workspace_id=? AND source_id=? AND tag_id=?",
                (workspace_id, source_id, tag_id),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            now = _now().isoformat()
            item = {
                "organization_id": organization_id, "workspace_id": workspace_id,
                "source_id": source_id, "tag_id": tag_id,
                "tagged_by_person_id": person_id, "created_at": now,
                "idempotency_key": idempotency_key,
            }
            with self.os.store.atomic(immediate=True):
                self.conn.execute("INSERT INTO brain_source_tags VALUES (?,?,?,?,?,?,?)", tuple(item.values()))
                self._brain_audit(organization_id, workspace_id, "source_tag", source_id, "tag", person_id, None, idempotency_key, {"source_id": source_id, "tag_id": tag_id}, now)
            return item

    def tag_document(self, identity: Any, workspace_id: str, document_id: str, tag_id: str,
            idempotency_key: str | None = None) -> dict[str, Any]:
            organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
            self._require_tag(workspace_id, tag_id)
            document = self.os.store.get_document(workspace_id, document_id)
            if document is None:
                raise NotFoundError("document not found")
            self._require_allowed_source(identity, workspace_id, document.source_id)
            prior = self._idempotent_row("brain_document_tags", workspace_id, person_id, idempotency_key, "tagged_by_person_id")
            if prior is not None:
                self._require_same_payload(dict(prior), {"document_id": document_id, "tag_id": tag_id}, ("document_id", "tag_id"))
                return dict(prior)
            existing = self.conn.execute(
                "SELECT * FROM brain_document_tags WHERE workspace_id=? AND document_id=? AND tag_id=?",
                (workspace_id, document_id, tag_id),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            now = _now().isoformat()
            item = {
                "organization_id": organization_id, "workspace_id": workspace_id,
                "document_id": document_id, "tag_id": tag_id,
                "tagged_by_person_id": person_id, "created_at": now,
                "idempotency_key": idempotency_key,
            }
            with self.os.store.atomic(immediate=True):
                self.conn.execute("INSERT INTO brain_document_tags VALUES (?,?,?,?,?,?,?)", tuple(item.values()))
                self._brain_audit(organization_id, workspace_id, "document_tag", document_id, "tag", person_id, None, idempotency_key, {"document_id": document_id, "tag_id": tag_id}, now)
            return item

    def add_collection_item(self, identity: Any, workspace_id: str, collection_id: str,
            item_type: str, item_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
            organization_id, person_id = self._brain_identity(identity, workspace_id, "brain_propose", write=True)
            collection = self._require_collection_for_write(workspace_id, collection_id, person_id)
            if item_type == "source":
                self._require_allowed_source(identity, workspace_id, item_id)
            elif item_type == "document":
                document = self.os.store.get_document(workspace_id, item_id)
                if document is None:
                    raise NotFoundError("document not found")
                self._require_allowed_source(identity, workspace_id, document.source_id)
            else:
                raise ValidationError("collection item type is invalid")
            prior = self._idempotent_row("brain_collection_items", workspace_id, person_id, idempotency_key, "added_by_person_id")
            if prior is not None:
                self._require_same_payload(dict(prior), {"collection_id": collection_id, "item_type": item_type, "item_id": item_id}, ("collection_id", "item_type", "item_id"))
                return dict(prior)
            existing = self.conn.execute(
                """SELECT * FROM brain_collection_items
                   WHERE workspace_id=? AND collection_id=? AND item_type=? AND item_id=?""",
                (workspace_id, collection_id, item_type, item_id),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            now = _now().isoformat()
            item = {
                "organization_id": organization_id, "workspace_id": workspace_id,
                "collection_id": collection["id"], "item_type": item_type,
                "item_id": item_id, "added_by_person_id": person_id,
                "created_at": now, "idempotency_key": idempotency_key,
            }
            with self.os.store.atomic(immediate=True):
                self.conn.execute("INSERT INTO brain_collection_items VALUES (?,?,?,?,?,?,?,?)", tuple(item.values()))
                self._brain_audit(organization_id, workspace_id, "collection_item", collection_id, "add_item", person_id, None, idempotency_key, {"item_type": item_type, "item_id": item_id}, now)
            return item

    def list_tagged_sources(self, identity: Any, workspace_id: str, tag_id: str) -> list[dict[str, Any]]:
            self._brain_identity(identity, workspace_id, "brain_read", write=False)
            self._require_tag(workspace_id, tag_id)
            allowed = sorted(self._allowed_source_ids(identity, workspace_id))
            if not allowed:
                return []
            marks = ",".join("?" for _ in allowed)
            rows = self.conn.execute(
                f"""SELECT s.*, t.created_at AS tagged_at FROM brain_source_tags t
                    JOIN sources s ON s.id=t.source_id AND s.workspace_id=t.workspace_id
                    WHERE t.workspace_id=? AND t.tag_id=? AND t.source_id IN ({marks})
                    ORDER BY t.created_at ASC, s.id ASC""",
                (workspace_id, tag_id, *allowed),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_tagged_documents(self, identity: Any, workspace_id: str, tag_id: str) -> list[dict[str, Any]]:
            self._brain_identity(identity, workspace_id, "brain_read", write=False)
            self._require_tag(workspace_id, tag_id)
            allowed = sorted(self._allowed_source_ids(identity, workspace_id))
            if not allowed:
                return []
            marks = ",".join("?" for _ in allowed)
            rows = self.conn.execute(
                f"""SELECT d.*, t.created_at AS tagged_at FROM brain_document_tags t
                    JOIN documents d ON d.id=t.document_id AND d.workspace_id=t.workspace_id
                    WHERE t.workspace_id=? AND t.tag_id=? AND d.source_id IN ({marks})
                    ORDER BY t.created_at ASC, d.id ASC""",
                (workspace_id, tag_id, *allowed),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_collection_items(self, identity: Any, workspace_id: str, collection_id: str) -> list[dict[str, Any]]:
            _, person_id = self._brain_identity(identity, workspace_id, "brain_read", write=False)
            self._require_collection_for_read(workspace_id, collection_id, person_id)
            allowed = self._allowed_source_ids(identity, workspace_id)
            rows = self.conn.execute(
                """SELECT * FROM brain_collection_items
                   WHERE workspace_id=? AND collection_id=?
                   ORDER BY created_at ASC, item_type ASC, item_id ASC""",
                (workspace_id, collection_id),
            ).fetchall()
            output = []
            for row in rows:
                if row["item_type"] == "source":
                    if row["item_id"] not in allowed:
                        continue
                else:
                    document = self.os.store.get_document(workspace_id, row["item_id"])
                    if document is None or document.source_id not in allowed:
                        continue
                output.append(dict(row))
            return output
