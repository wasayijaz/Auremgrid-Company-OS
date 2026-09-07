from __future__ import annotations
from dataclasses import replace
import json
from typing import Any
from auremgrid.domain.company import Deliverable, Review
from auremgrid.domain.errors import NotFoundError, ValidationError
from auremgrid.services.brain_shared import new_id, utcnow




class BrainAnnotationsMixin:
        @staticmethod
        def annotation_capabilities(deliverable: Deliverable | None) -> dict[str, dict[str, Any]]:
            """Return source-backed annotation capabilities without inventing media geometry."""
            result = {
                "general_comments": {"status": "ready", "route": "/reviews/annotations"},
                "text_comments": {"status": "ready", "route": "/reviews/comment"},
                "image_points": {"status": "not_available", "reason": "No image source is attached."},
                "image_regions": {"status": "not_available", "reason": "No image source is attached."},
                "document_pages": {"status": "not_available", "reason": "No document source is attached."},
                "document_regions": {"status": "not_available", "reason": "No document source is attached."},
                "video_timestamps": {"status": "not_available", "reason": "No video source is attached."},
                "video_ranges": {"status": "not_available", "reason": "No video source is attached."},
            }
            if deliverable is None:
                return result
            has_source = bool(deliverable.preview_url or deliverable.final_url)
            if not has_source:
                return result
            kind = deliverable.type
            if kind in {"design_asset", "ad_creative"}:
                result["image_points"] = {"status": "ready", "route": "/reviews/annotations"}
                result["image_regions"] = {"status": "ready", "route": "/reviews/annotations"}
            elif kind in {"document", "report", "presentation", "copy", "landing_page", "website"}:
                result["document_pages"] = {"status": "ready", "route": "/reviews/annotations"}
                result["document_regions"] = {"status": "ready", "route": "/reviews/annotations"}
            elif kind == "video":
                result["video_timestamps"] = {"status": "ready", "route": "/reviews/annotations"}
                result["video_ranges"] = {"status": "ready", "route": "/reviews/annotations"}
            return result
    
        def annotation_capabilities_for_deliverable(self, workspace_id: str, deliverable: Deliverable | None) -> dict[str, dict[str, Any]]:
            if deliverable is not None and not (deliverable.preview_url or deliverable.final_url):
                source = self.store.conn.execute("SELECT url FROM deliverable_files WHERE deliverable_id=? ORDER BY version DESC, created_at DESC LIMIT 1", (deliverable.id,)).fetchone()
                if source and source[0]:
                    deliverable = replace(deliverable, preview_url=source[0])
            return self.annotation_capabilities(deliverable)
    
        def _annotation_scope(self, organization_id: str, workspace_id: str, person_id: str,
            review_id: str) -> tuple[Review, Deliverable]:
            self._require_person_access(organization_id, workspace_id, person_id, write=True)
            review = self.company.get_review(workspace_id, review_id)
            if review is None or review.organization_id != organization_id:
                raise NotFoundError("review not found")
            deliverable = self.company.get_deliverable(workspace_id, review.deliverable_id)
            if deliverable is None:
                raise NotFoundError("deliverable not found")
            # Deliverable versions keep their source in deliverable_files.  Expose
            # that verified locator to capability checks without accepting caller
            # supplied URLs as evidence.
            if not (deliverable.preview_url or deliverable.final_url):
                source = self.store.conn.execute(
                    "SELECT url FROM deliverable_files WHERE deliverable_id=? ORDER BY version DESC, created_at DESC LIMIT 1",
                    (deliverable.id,),
                ).fetchone()
                if source and source[0]:
                    deliverable = replace(deliverable, preview_url=source[0])
            return review, deliverable
    
        def create_review_annotation(self, organization_id: str, workspace_id: str, person_id: str,
            review_id: str, annotation_type: str, body: str = "", source_locator: str | None = None,
            coordinates: dict[str, Any] | None = None, page_number: int | None = None,
            start_seconds: float | None = None, end_seconds: float | None = None,
            idempotency_key: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
            review, deliverable = self._annotation_scope(organization_id, workspace_id, person_id, review_id)
            allowed = {"general_comment", "image_point", "image_region", "document_page", "document_region", "video_timestamp", "video_range"}
            if annotation_type not in allowed:
                raise ValidationError("unsupported annotation type")
            if not body.strip() and annotation_type == "general_comment":
                raise ValidationError("annotation body is required")
            caps = self.annotation_capabilities(deliverable)
            cap_key = {"general_comment": "general_comments", "image_point": "image_points", "image_region": "image_regions",
                "document_page": "document_pages", "document_region": "document_regions", "video_timestamp": "video_timestamps", "video_range": "video_ranges"}[annotation_type]
            if caps[cap_key]["status"] != "ready":
                raise ValidationError(f"{annotation_type} is unavailable: {caps[cap_key].get('reason', 'source capability missing')}")
            if idempotency_key:
                prior = self.store.conn.execute("SELECT * FROM review_annotations WHERE organization_id=? AND workspace_id=? AND author_person_id=? AND idempotency_key=?", (organization_id, workspace_id, person_id, idempotency_key)).fetchone()
                if prior is not None:
                    return self._annotation_dict(prior)
            coords = coordinates or {}
            if annotation_type in {"image_point", "image_region", "document_region"}:
                required = ("x", "y") if annotation_type == "image_point" else ("x", "y", "width", "height")
                if any(key not in coords for key in required):
                    raise ValidationError(f"{annotation_type} requires explicit coordinates: {', '.join(required)}")
                try:
                    values = [float(coords[key]) for key in required]
                except (TypeError, ValueError):
                    raise ValidationError("annotation coordinates must be numeric")
                if any(value < 0 for value in values):
                    raise ValidationError("annotation coordinates are outside the source bounds")
                if annotation_type.startswith("image") and (any(value > 1 for value in values) or (annotation_type == "image_region" and (float(coords["x"])+float(coords["width"]) > 1 or float(coords["y"])+float(coords["height"]) > 1))):
                    raise ValidationError("image annotation coordinates are outside the source bounds")
                if annotation_type == "document_region" and (any(value > 1 for value in values) or float(coords["x"])+float(coords["width"]) > 1 or float(coords["y"])+float(coords["height"]) > 1):
                    raise ValidationError("document annotation coordinates are outside the source bounds")
                if annotation_type in {"image_region", "document_region"} and (float(coords["width"]) <= 0 or float(coords["height"]) <= 0):
                    raise ValidationError("annotation regions require positive width and height")
            if annotation_type in {"document_page", "document_region"} and (page_number is None or int(page_number) < 1):
                raise ValidationError("document annotations require a positive page_number")
            if annotation_type.startswith("video_"):
                if start_seconds is None or float(start_seconds) < 0 or (end_seconds is not None and float(end_seconds) < float(start_seconds)):
                    raise ValidationError("video annotations require a valid non-negative time range")
                if annotation_type == "video_range" and end_seconds is None:
                    raise ValidationError("video_range requires end_seconds")
            now = utcnow().isoformat()
            attached_sources = {value for value in (deliverable.preview_url, deliverable.final_url) if value}
            if source_locator and source_locator not in attached_sources:
                raise ValidationError("source_locator must match an attached deliverable source")
            item = {"id": new_id("annotation"), "organization_id": organization_id, "workspace_id": workspace_id,
                "review_id": review_id, "deliverable_id": deliverable.id, "version": review.version,
                "author_person_id": person_id, "annotation_type": annotation_type, "body": body.strip(),
                "source_locator": source_locator or deliverable.final_url or deliverable.preview_url,
                "coordinates_json": json.dumps(coords, separators=(",", ":")), "page_number": page_number,
                "start_seconds": start_seconds, "end_seconds": end_seconds, "created_at": now,
                "idempotency_key": idempotency_key, "metadata_json": json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))}
            self.store.conn.execute("INSERT INTO review_annotations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values()))
            event = {"id": new_id("annotationevent"), "organization_id": organization_id, "workspace_id": workspace_id,
                "annotation_id": item["id"], "actor_person_id": person_id, "action": "created",
                "replacement_annotation_id": None, "idempotency_key": (f"{idempotency_key}:created" if idempotency_key else None),
                "payload_json": json.dumps({"annotation_type": annotation_type}), "created_at": now}
            self.store.conn.execute("INSERT INTO review_annotation_events VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(event.values()))
            self.store.conn.commit()
            return {**item, "coordinates": coords, "metadata": metadata or {}, "status": "open"}
    
        def register_review_media_contract(
            self, organization_id: str, workspace_id: str, person_id: str, review_id: str,
            source_locator: str, media_kind: str, metadata: dict[str, Any] | None = None,
            width_px: int | None = None, height_px: int | None = None,
            duration_seconds: float | None = None, frame_rate: float | None = None,
            page_count: int | None = None,
        ) -> dict[str, Any]:
            """Persist source metadata used by richer visual/video review UIs."""
            review, deliverable = self._annotation_scope(organization_id, workspace_id, person_id, review_id)
            if media_kind not in {"image", "document", "video"} or not source_locator.strip():
                raise ValidationError("media_kind and source_locator are required")
            attached = {x for x in (deliverable.preview_url, deliverable.final_url) if x}
            if not attached:
                source = self.store.conn.execute("SELECT url FROM deliverable_files WHERE deliverable_id=? ORDER BY version DESC,created_at DESC LIMIT 1", (deliverable.id,)).fetchone()
                if source and source[0]: attached.add(source[0])
            if source_locator not in attached:
                raise ValidationError("source_locator must match an attached deliverable source")
            for label, value in (("width_px", width_px), ("height_px", height_px), ("page_count", page_count)):
                if value is not None and int(value) < 1:
                    raise ValidationError(f"{label} must be positive")
            for label, value in (("duration_seconds", duration_seconds), ("frame_rate", frame_rate)):
                if value is not None and float(value) < 0:
                    raise ValidationError(f"{label} must be non-negative")
            safe = metadata or {}
            payload = json.dumps(safe, sort_keys=True, separators=(",", ":"))
            prior = self.store.conn.execute("SELECT * FROM review_media_contracts WHERE review_id=? AND source_locator=?", (review_id, source_locator)).fetchone()
            if prior is not None:
                item = dict(prior); item["metadata"] = json.loads(item.pop("metadata_json") or "{}"); return item
            now = utcnow().isoformat()
            item = {"id": new_id("review_media"), "organization_id": organization_id, "workspace_id": workspace_id,
                "review_id": review_id, "deliverable_id": deliverable.id, "version": review.version,
                "source_locator": source_locator, "media_kind": media_kind, "width_px": width_px, "height_px": height_px,
                "duration_seconds": duration_seconds, "frame_rate": frame_rate, "page_count": page_count,
                "metadata_json": payload, "created_at": now}
            self.store.conn.execute("INSERT INTO review_media_contracts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values())); self.store.conn.commit()
            return {**item, "metadata": safe}
    
        def list_review_media_contracts(self, organization_id: str, workspace_id: str, person_id: str, review_id: str) -> list[dict[str, Any]]:
            self._require_person_access(organization_id, workspace_id, person_id)
            rows = self.store.conn.execute("SELECT * FROM review_media_contracts WHERE organization_id=? AND workspace_id=? AND review_id=? ORDER BY created_at,id", (organization_id, workspace_id, review_id)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try: item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
                except (TypeError, ValueError): item["metadata"] = {}; item.pop("metadata_json", None)
                result.append(item)
            return result
    
        def _annotation_dict(self, row: Any) -> dict[str, Any]:
            item = dict(row)
            item["coordinates"] = json.loads(item.pop("coordinates_json") or "{}")
            try: item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (TypeError, ValueError): item["metadata"] = {}; item.pop("metadata_json", None)
            # rowid preserves append order even when the UTC clock is second-level
            # precision and create/resolve happen in the same second.
            events = self.store.conn.execute("SELECT action,replacement_annotation_id,created_at FROM review_annotation_events WHERE annotation_id=? ORDER BY rowid DESC", (item["id"],)).fetchall()
            item["status"] = "open"
            if events:
                action = events[0]["action"]
                item["status"] = "resolved" if action == "resolved" else "superseded" if action == "superseded" else "open"
                item["last_event_at"] = events[0]["created_at"]
                if events[0]["replacement_annotation_id"]:
                    item["replacement_annotation_id"] = events[0]["replacement_annotation_id"]
            return item
    
        def list_review_annotations(self, organization_id: str, workspace_id: str, person_id: str,
            review_id: str | None = None, include_closed: bool = True) -> list[dict[str, Any]]:
            self._require_person_access(organization_id, workspace_id, person_id)
            sql = "SELECT * FROM review_annotations WHERE organization_id=? AND workspace_id=?"; args: list[Any] = [organization_id, workspace_id]
            if review_id: sql += " AND review_id=?"; args.append(review_id)
            rows = [self._annotation_dict(row) for row in self.store.conn.execute(sql + " ORDER BY created_at,id", args).fetchall()]
            return rows if include_closed else [row for row in rows if row["status"] == "open"]
    
        def resolve_review_annotation(self, organization_id: str, workspace_id: str, person_id: str,
            annotation_id: str, idempotency_key: str | None = None, note: str = "") -> dict[str, Any]:
            self._require_person_access(organization_id, workspace_id, person_id, write=True)
            row = self.store.conn.execute("SELECT * FROM review_annotations WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, annotation_id)).fetchone()
            if row is None: raise NotFoundError("annotation not found")
            if idempotency_key:
                prior = self.store.conn.execute("SELECT annotation_id FROM review_annotation_events WHERE organization_id=? AND workspace_id=? AND actor_person_id=? AND idempotency_key=?", (organization_id, workspace_id, person_id, idempotency_key)).fetchone()
                if prior: return self._annotation_dict(row)
            if self._annotation_dict(row)["status"] != "open": raise ValidationError("annotation is already closed")
            now = utcnow().isoformat(); event = {"id": new_id("annotationevent"), "organization_id": organization_id, "workspace_id": workspace_id, "annotation_id": annotation_id, "actor_person_id": person_id, "action": "resolved", "replacement_annotation_id": None, "idempotency_key": idempotency_key, "payload_json": json.dumps({"note": note}), "created_at": now}
            self.store.conn.execute("INSERT INTO review_annotation_events VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(event.values())); self.store.conn.commit(); return self._annotation_dict(row)
    
        def supersede_review_annotation(self, organization_id: str, workspace_id: str, person_id: str,
            annotation_id: str, replacement_annotation_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
            self._require_person_access(organization_id, workspace_id, person_id, write=True)
            row = self.store.conn.execute("SELECT * FROM review_annotations WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, annotation_id)).fetchone()
            if row is None: raise NotFoundError("annotation not found")
            if self._annotation_dict(row)["status"] != "open":
                raise ValidationError("annotation is already closed")
            if replacement_annotation_id:
                replacement = self.store.conn.execute("SELECT review_id,deliverable_id FROM review_annotations WHERE organization_id=? AND workspace_id=? AND id=?", (organization_id, workspace_id, replacement_annotation_id)).fetchone()
                if replacement is None: raise NotFoundError("replacement annotation not found")
                if replacement["review_id"] != row["review_id"] or replacement["deliverable_id"] != row["deliverable_id"]:
                    raise ValidationError("replacement annotation must belong to the same review and deliverable")
            if idempotency_key:
                prior = self.store.conn.execute("SELECT annotation_id FROM review_annotation_events WHERE organization_id=? AND workspace_id=? AND actor_person_id=? AND idempotency_key=?", (organization_id, workspace_id, person_id, idempotency_key)).fetchone()
                if prior: return self._annotation_dict(row)
            now = utcnow().isoformat(); event = {"id": new_id("annotationevent"), "organization_id": organization_id, "workspace_id": workspace_id, "annotation_id": annotation_id, "actor_person_id": person_id, "action": "superseded", "replacement_annotation_id": replacement_annotation_id, "idempotency_key": idempotency_key, "payload_json": json.dumps({"replacement_annotation_id": replacement_annotation_id}), "created_at": now}
            self.store.conn.execute("INSERT INTO review_annotation_events VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(event.values())); self.store.conn.commit(); return self._annotation_dict(row)
