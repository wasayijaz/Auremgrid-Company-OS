from __future__ import annotations
from auremgrid.services.brain_ops_shared import *

class BrainOpsValidationMixin:
    @staticmethod
    def _clean_name(value: str, label: str) -> str:
            cleaned = " ".join(str(value).split())
            if not cleaned:
                raise ValidationError(f"{label} is required")
            return cleaned

    @staticmethod
    def _visibility(value: str | None) -> str:
            if value not in {"owner", "shared"}:
                raise ValidationError("visibility must be owner or shared")
            return value

    @staticmethod
    def _json_payload(value: Any, label: str) -> str:
            try:
                return json.dumps(value, sort_keys=True, separators=(",", ":"))
            except TypeError as exc:
                raise ValidationError(f"{label} must be JSON serializable") from exc

    @staticmethod
    def _require_same_payload(row: dict[str, Any], payload: dict[str, Any], fields: tuple[str, ...]) -> None:
            if any(row.get(field) != payload.get(field) for field in fields):
                raise ValidationError("idempotency key was already used for a different mutation")

    def _require_folder(self, workspace_id: str, folder_id: str | None) -> None:
            if folder_id is None:
                return
            if self.os.store.get_brain_folder(workspace_id, folder_id) is None:
                raise NotFoundError("folder not found")

    def _validate_folder_parent(self, workspace_id: str, parent_id: str | None) -> None:
            if parent_id is None:
                return
            seen: set[str] = set()
            cursor = parent_id
            while cursor is not None:
                if cursor in seen:
                    raise ValidationError("folder hierarchy has a cycle")
                seen.add(cursor)
                folder = self.os.store.get_brain_folder(workspace_id, cursor)
                if folder is None:
                    raise NotFoundError("parent folder not found")
                cursor = folder["parent_id"]
                if len(seen) > 25:
                    raise ValidationError("folder hierarchy is too deep")

    def _require_tag(self, workspace_id: str, tag_id: str) -> dict[str, object]:
            tag = self.os.store.get_brain_tag(workspace_id, tag_id)
            if tag is None:
                raise NotFoundError("tag not found")
            return tag

    def _require_collection_for_read(self, workspace_id: str, collection_id: str, person_id: str) -> dict[str, object]:
            collection = self.os.store.get_brain_collection(workspace_id, collection_id)
            if collection is None:
                raise NotFoundError("collection not found")
            if collection["visibility"] != "shared" and collection["owner_person_id"] != person_id:
                raise NotFoundError("collection not found")
            return collection

    def _require_collection_for_write(self, workspace_id: str, collection_id: str, person_id: str) -> dict[str, object]:
            collection = self.os.store.get_brain_collection(workspace_id, collection_id)
            if collection is None:
                raise NotFoundError("collection not found")
            if collection["owner_person_id"] != person_id:
                raise AuthorizationError("collection owner required")
            return collection

    def _require_allowed_source(self, identity: Any, workspace_id: str, source_id: str) -> None:
            if source_id not in self._allowed_source_ids(identity, workspace_id):
                raise NotFoundError("source not found")
