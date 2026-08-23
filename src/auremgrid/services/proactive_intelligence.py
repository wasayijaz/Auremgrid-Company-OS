from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity


SNAPSHOT_TYPES = {"executive", "workspace"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json_dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_load(value: str | None, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    return json.loads(str(value))


class ProactiveIntelligenceService:
    """Persist read-only intelligence projections for proactive surfaces."""

    def __init__(self, os: Any) -> None:
        self.os = os
        self.conn = os.store.conn

    def enqueue_refresh(
        self,
        identity: AuthenticatedIdentity,
        snapshot_type: str = "executive",
        workspace_id: str | None = None,
        idempotency_key: str | None = None,
        priority: int = 0,
        runbook_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot_type = self._snapshot_type(snapshot_type)
        if snapshot_type == "workspace" and not workspace_id:
            raise ValidationError("workspace_id is required for workspace snapshots")
        if snapshot_type == "executive" and workspace_id is not None:
            raise ValidationError("executive snapshots are organization-scoped")
        scoped = identity
        if workspace_id is not None:
            scoped = self.os.auth.scope_identity(identity, workspace_id)
        scoped.require("brain_read")
        key = idempotency_key or self._default_idempotency_key(
            scoped.organization_id, scoped.person_id, snapshot_type, workspace_id
        )
        return self.os.jobs.enqueue_job(
            scoped.organization_id,
            workspace_id,
            scoped.principal_id,
            "proactive_intelligence.refresh",
            {"snapshot_type": snapshot_type, "workspace_id": workspace_id, "runbook_id": runbook_id},
            priority=int(priority),
            max_attempts=3,
            idempotency_key=key,
        )

    def refresh_snapshot(
        self,
        organization_id: str,
        person_id: str,
        snapshot_type: str = "executive",
        workspace_id: str | None = None,
        actor_id: str | None = None,
        as_of: datetime | None = None,
        runbook_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot_type = self._snapshot_type(snapshot_type)
        if snapshot_type == "executive":
            if workspace_id is not None:
                raise ValidationError("executive snapshots are organization-scoped")
            payload = self.os.intelligence.executive_brief(
                organization_id, person_id, actor_id=actor_id, as_of=as_of,
                use_reasoning_provider=False,
            )
        else:
            if workspace_id is None:
                raise ValidationError("workspace_id is required for workspace snapshots")
            payload = self.os.intelligence.workspace(
                organization_id, workspace_id, person_id, actor_id=actor_id, as_of=as_of,
                use_reasoning_provider=False,
            )
            if runbook_id and hasattr(self.os, "intelligence_orchestrator"):
                orchestration = self.os.intelligence_orchestrator.run(
                    organization_id, workspace_id, person_id, actor_id=actor_id,
                    runbook_id=runbook_id, as_of=as_of,
                )
                payload["orchestration"] = orchestration
                payload["trace_id"] = orchestration.get("trace_id")
                payload["recommendation_id"] = orchestration.get("recommendation_id")
                payload["runbook_route"] = orchestration.get("runbook_route")
        generated_at = str(payload.get("generated_at") or _now().isoformat())
        status = self._snapshot_status(payload)
        degraded_reason = payload.get("degraded_reason")
        evidence_refs = self._evidence_refs(payload)
        projection_fingerprint = self._projection_fingerprint(payload)
        latest = self.latest_snapshot(organization_id, person_id, snapshot_type, workspace_id)
        if latest is not None and latest.get("projection_fingerprint") == projection_fingerprint:
            latest["unchanged"] = True
            return latest
        version = self._next_version(organization_id, workspace_id, person_id, snapshot_type)
        snapshot = {
            "id": self.os.jobs.new_id("intelshot"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "person_id": person_id,
            "snapshot_type": snapshot_type,
            "version": version,
            "status": status,
            "degraded_reason": degraded_reason,
            "projection_fingerprint": projection_fingerprint,
            "generated_at": generated_at,
            "payload": payload,
            "evidence_refs": evidence_refs,
            "created_at": _now().isoformat(),
            "unchanged": False,
        }
        attention = self._attention_items(snapshot, payload)
        with self.conn:
            self.conn.execute(
                """INSERT INTO proactive_intelligence_snapshots(
                    id, organization_id, workspace_id, person_id, snapshot_type, version,
                    status, degraded_reason, projection_fingerprint, generated_at, payload_json,
                    evidence_refs_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot["id"], organization_id, workspace_id, person_id, snapshot_type,
                    version, status, degraded_reason, projection_fingerprint, generated_at,
                    _json_dump(payload), _json_dump(evidence_refs), snapshot["created_at"],
                ),
            )
            for item in attention:
                self.conn.execute(
                    """INSERT INTO proactive_intelligence_attention_items(
                        id, snapshot_id, organization_id, workspace_id, person_id, rank,
                        title, narrative, status, evidence_refs_json, generated_at, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item["id"], snapshot["id"], organization_id, item.get("workspace_id"),
                        person_id, item["rank"], item["title"], item["narrative"],
                        item["status"], _json_dump(item["evidence_refs"]), generated_at,
                        snapshot["created_at"],
                    ),
                    )
            self._sync_attention_lifecycle(snapshot, attention, payload)
        snapshot["attention"] = attention
        return snapshot

    def _sync_attention_lifecycle(self, snapshot: dict[str, Any], attention: list[dict[str, Any]], payload: dict[str, Any]) -> None:
        """Dedupe attention across snapshots while preserving lifecycle state."""
        trace_id = payload.get("trace_id") or (payload.get("deliberation") or {}).get("trace_id")
        recommendation_id = payload.get("recommendation_id")
        for item in attention:
            fingerprint = hashlib.sha256(_json_dump({
                "title": item["title"], "narrative": item["narrative"], "evidence": item["evidence_refs"],
            }).encode("utf-8")).hexdigest()
            prior = self.conn.execute(
                "SELECT * FROM proactive_intelligence_attention_lifecycle WHERE organization_id=? AND workspace_id IS ? AND person_id=? AND fingerprint=?",
                (snapshot["organization_id"], snapshot.get("workspace_id"), snapshot["person_id"], fingerprint),
            ).fetchone()
            status = "resurfaced" if prior and prior["status"] in {"resolved", "dismissed"} else (prior["status"] if prior else "new")
            now = _now().isoformat()
            if prior is None:
                self.conn.execute(
                    "INSERT INTO proactive_intelligence_attention_lifecycle VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.os.jobs.new_id("intelcycle"), snapshot["organization_id"], snapshot.get("workspace_id"), snapshot["person_id"], fingerprint, snapshot["id"], item["id"], status, trace_id, recommendation_id, _json_dump(item.get("action_descriptor") or {}), None, "new projection", now, now),
                )
            else:
                self.conn.execute(
                    "UPDATE proactive_intelligence_attention_lifecycle SET snapshot_id=?,attention_item_id=?,status=?,trace_id=?,recommendation_id=?,updated_at=? WHERE id=?",
                    (snapshot["id"], item["id"], status, trace_id, recommendation_id, now, prior["id"]),
                )

    def update_attention_status(self, identity: AuthenticatedIdentity, fingerprint: str, status: str, reason: str = "") -> dict[str, Any]:
        if status not in {"acknowledged", "acted_on", "resolved", "dismissed"}:
            raise ValidationError("unsupported attention lifecycle status")
        identity.require("brain_read")
        row = self.conn.execute(
            "SELECT * FROM proactive_intelligence_attention_lifecycle WHERE organization_id=? AND person_id=? AND fingerprint=?",
            (identity.organization_id, identity.person_id, fingerprint),
        ).fetchone()
        if row is None:
            raise NotFoundError("attention lifecycle item not found")
        if row["workspace_id"] is not None:
            self.os.auth.scope_identity(identity, row["workspace_id"]).require("brain_read")
        now = _now().isoformat()
        self.conn.execute("UPDATE proactive_intelligence_attention_lifecycle SET status=?,reason=?,updated_at=? WHERE id=?", (status, reason, now, row["id"]))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM proactive_intelligence_attention_lifecycle WHERE id=?", (row["id"],)).fetchone())

    def attention_lifecycle(self, identity: AuthenticatedIdentity, workspace_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        scoped = identity if workspace_id is None else self.os.auth.scope_identity(identity, workspace_id)
        scoped.require("brain_read")
        rows = self.conn.execute(
            "SELECT * FROM proactive_intelligence_attention_lifecycle WHERE organization_id=? AND person_id=? AND workspace_id IS ? ORDER BY updated_at DESC LIMIT ?",
            (scoped.organization_id, scoped.person_id, workspace_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_action_acted_on(self, identity: AuthenticatedIdentity, fingerprint: str, approval_request_id: str, reason: str = "approved action") -> dict[str, Any]:
        identity.require("brain_read")
        row = self.conn.execute(
            "SELECT * FROM proactive_intelligence_attention_lifecycle WHERE organization_id=? AND person_id=? AND fingerprint=?",
            (identity.organization_id, identity.person_id, fingerprint),
        ).fetchone()
        if row is None:
            raise NotFoundError("attention lifecycle item not found")
        approval = self.conn.execute(
            "SELECT * FROM approval_requests WHERE organization_id=? AND id=? AND workspace_id IS ?",
            (identity.organization_id, approval_request_id, row["workspace_id"]),
        ).fetchone()
        if approval is None or approval["status"] != "approved":
            raise AuthorizationError("approved canonical action required")
        if row["workspace_id"] is not None:
            self.os.auth.scope_identity(identity, row["workspace_id"]).require("workspace_write")
        now = _now().isoformat()
        self.conn.execute("UPDATE proactive_intelligence_attention_lifecycle SET status='acted_on',approval_request_id=?,reason=?,updated_at=? WHERE id=?", (approval_request_id, reason, now, row["id"]))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM proactive_intelligence_attention_lifecycle WHERE id=?", (row["id"],)).fetchone())

    def latest_snapshot(
        self,
        organization_id: str,
        person_id: str,
        snapshot_type: str = "executive",
        workspace_id: str | None = None,
    ) -> dict[str, Any] | None:
        snapshot_type = self._snapshot_type(snapshot_type)
        row = self.conn.execute(
            """SELECT * FROM proactive_intelligence_snapshots
               WHERE organization_id=? AND workspace_id IS ? AND person_id=? AND snapshot_type=?
               ORDER BY generated_at DESC, version DESC LIMIT 1""",
            (organization_id, workspace_id, person_id, snapshot_type),
        ).fetchone()
        if row is None:
            return None
        return self._decode_snapshot(row)

    def require_latest_snapshot(
        self,
        organization_id: str,
        person_id: str,
        snapshot_type: str = "executive",
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot = self.latest_snapshot(organization_id, person_id, snapshot_type, workspace_id)
        if snapshot is None:
            raise NotFoundError("proactive intelligence snapshot not found")
        return snapshot

    def attention_queue(
        self,
        organization_id: str,
        person_id: str,
        workspace_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValidationError("limit must be positive")
        rows = self.conn.execute(
            """SELECT * FROM proactive_intelligence_attention_items
               WHERE snapshot_id=(
                   SELECT id FROM proactive_intelligence_snapshots
                   WHERE organization_id=? AND workspace_id IS ? AND person_id=?
                   ORDER BY generated_at DESC, version DESC LIMIT 1
               )
               ORDER BY rank ASC, id ASC LIMIT ?""",
            (organization_id, workspace_id, person_id, int(limit)),
        ).fetchall()
        return [self._decode_attention(row) for row in rows]

    def authorize_read(
        self,
        identity: AuthenticatedIdentity,
        organization_id: str,
        person_id: str,
        workspace_id: str | None = None,
    ) -> AuthenticatedIdentity:
        if identity.organization_id != organization_id or identity.person_id != person_id:
            raise AuthorizationError("identity scope mismatch")
        scoped = identity
        if workspace_id is not None:
            scoped = self.os.auth.scope_identity(identity, workspace_id)
        scoped.require("brain_read")
        return scoped

    def refresh_status(
        self,
        identity: AuthenticatedIdentity,
        snapshot_type: str = "executive",
        workspace_id: str | None = None,
        stale_after: timedelta = timedelta(hours=24),
    ) -> dict[str, Any]:
        """Describe the durable refresh lifecycle without pretending a worker is running."""
        snapshot_type = self._snapshot_type(snapshot_type)
        if snapshot_type == "workspace" and not workspace_id:
            raise ValidationError("workspace_id is required for workspace snapshots")
        if snapshot_type == "executive" and workspace_id is not None:
            raise ValidationError("executive snapshots are organization-scoped")
        scoped = identity if workspace_id is None else self.os.auth.scope_identity(identity, workspace_id)
        scoped.require("brain_read")
        snapshot = self.latest_snapshot(
            scoped.organization_id, scoped.person_id, snapshot_type, workspace_id
        )
        jobs = [
            job for job in self.os.jobs.list_jobs(scoped.organization_id, workspace_id)
            if job.get("type") == "proactive_intelligence.refresh"
            and (job.get("payload") or {}).get("snapshot_type") == snapshot_type
            and (job.get("payload") or {}).get("workspace_id") == workspace_id
        ]
        latest_job = jobs[0] if jobs else None
        job_status = str((latest_job or {}).get("status") or "")
        if job_status in {"queued", "retry_wait"}:
            status = "queued"
        elif job_status in {"leased", "running"}:
            status = "running"
        elif job_status in {"failed", "dead_letter"}:
            status = "failed"
        elif snapshot is None:
            status = "no_snapshot"
        else:
            generated = datetime.fromisoformat(str(snapshot["generated_at"]).replace("Z", "+00:00"))
            status = "stale" if _now() - generated.astimezone(timezone.utc) > stale_after else "ready"
        worker_parts = [
            "python scripts/auremgrid.py worker-once",
            "--db <database-path>",
            f"--organization {scoped.organization_id}",
        ]
        if workspace_id:
            worker_parts.append(f"--workspace {workspace_id}")
        worker_parts.append("--worker-id local-worker-1")
        job_view = None
        if latest_job is not None:
            job_view = {
                key: latest_job.get(key)
                for key in (
                    "id", "status", "progress", "attempts", "max_attempts", "error",
                    "created_at", "updated_at", "started_at", "completed_at", "version",
                )
            }
        snapshot_view = None
        if snapshot is not None:
            snapshot_view = {
                "id": snapshot["id"],
                "version": snapshot["version"],
                "status": snapshot["status"],
                "generated_at": snapshot["generated_at"],
                "attention_count": len(snapshot.get("attention") or []),
                "degraded_reason": snapshot.get("degraded_reason"),
            }
        return {
            "status": status,
            "snapshot_type": snapshot_type,
            "workspace_id": workspace_id,
            "worker_required": status in {"no_snapshot", "queued", "failed", "stale"},
            "worker_command": " ".join(worker_parts),
            "latest_job": job_view,
            "latest_snapshot": snapshot_view,
        }

    def _decode_snapshot(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = _json_load(item.pop("payload_json"), {})
        item["evidence_refs"] = _json_load(item.pop("evidence_refs_json"), {})
        item["attention"] = [
            self._decode_attention(attention)
            for attention in self.conn.execute(
                """SELECT * FROM proactive_intelligence_attention_items
                   WHERE snapshot_id=? ORDER BY rank ASC, id ASC""",
                (item["id"],),
            ).fetchall()
        ]
        return item

    def _decode_attention(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["evidence_refs"] = _json_load(item.pop("evidence_refs_json"), {})
        return item

    def _snapshot_type(self, value: str) -> str:
        snapshot_type = str(value or "").strip()
        if snapshot_type not in SNAPSHOT_TYPES:
            raise ValidationError("unsupported proactive intelligence snapshot type")
        return snapshot_type

    def _next_version(self, organization_id: str, workspace_id: str | None, person_id: str, snapshot_type: str) -> int:
        row = self.conn.execute(
            """SELECT MAX(version) FROM proactive_intelligence_snapshots
               WHERE organization_id=? AND workspace_id IS ? AND person_id=? AND snapshot_type=?""",
            (organization_id, workspace_id, person_id, snapshot_type),
        ).fetchone()
        return int((row[0] if row else 0) or 0) + 1

    def _snapshot_status(self, payload: dict[str, Any]) -> str:
        status = str(payload.get("status") or "ready")
        if status in {"ready", "degraded", "insufficient_evidence"}:
            return status
        if status == "ok":
            return "ready"
        return "degraded"

    def _evidence_refs(self, payload: dict[str, Any]) -> dict[str, list[str]]:
        refs: dict[str, set[str]] = {"sources": set(), "documents": set(), "facts": set(), "objects": set()}

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                object_ref = value.get("object_ref")
                if object_ref is not None:
                    refs["objects"].add(str(object_ref))
                citation = value.get("citation")
                if isinstance(citation, dict):
                    for key, target in (("source_id", "sources"), ("document_id", "documents"), ("fact_id", "facts")):
                        if citation.get(key):
                            refs[target].add(str(citation[key]))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload.get("sections", {}))
        visit(payload.get("findings", []))
        return {key: sorted(values) for key, values in refs.items() if values}

    def _projection_fingerprint(self, payload: dict[str, Any]) -> str:
        normalized = self._without_volatile_metadata(payload)
        return hashlib.sha256(_json_dump(normalized).encode("utf-8")).hexdigest()

    def _without_volatile_metadata(self, value: Any) -> Any:
        volatile_keys = {
            "generated_at",
            "created_at",
            "updated_at",
            "as_of",
            "recorded_at",
            "detected_at",
            "provider_metadata",
            "context_hash",
            "output_hash",
            "fallback_reason",
        }
        if isinstance(value, dict):
            return {
                key: self._without_volatile_metadata(child)
                for key, child in value.items()
                if key not in volatile_keys
            }
        if isinstance(value, list):
            return [self._without_volatile_metadata(child) for child in value]
        return value

    def _attention_items(self, snapshot: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        if snapshot["snapshot_type"] == "executive":
            source_items = payload.get("sections", {}).get("top_three") or payload.get("sections", {}).get("attention", [])[:3]
        else:
            source_items = payload.get("findings", [])[:3]
        status = self._attention_status(snapshot["status"])
        items: list[dict[str, Any]] = []
        for rank, item in enumerate(source_items[:3], start=1):
            title = str(item.get("title") or item.get("what_changed") or "Intelligence attention item")
            narrative = self._narrative(item)
            item_workspace_id = item.get("workspace_id") or snapshot.get("workspace_id")
            evidence_refs = self._evidence_refs({"sections": {"item": item}, "findings": [item]})
            items.append({
                "id": self.os.jobs.new_id("intelattn"),
                "snapshot_id": snapshot["id"],
                "organization_id": snapshot["organization_id"],
                "workspace_id": str(item_workspace_id) if item_workspace_id is not None else None,
                "person_id": snapshot["person_id"],
                "rank": rank,
                "title": title,
                "narrative": narrative,
                "status": status,
                "action_descriptor": (item.get("action_descriptors") or item.get("actions") or [None])[0],
                "evidence_refs": evidence_refs,
                "generated_at": snapshot["generated_at"],
                "created_at": snapshot["created_at"],
            })
        return items

    def _attention_status(self, snapshot_status: str) -> str:
        if snapshot_status == "insufficient_evidence":
            return "insufficient_evidence"
        if snapshot_status == "degraded":
            return "degraded"
        return "open"

    def _narrative(self, item: dict[str, Any]) -> str:
        impact = item.get("impact") if isinstance(item.get("impact"), dict) else {}
        recommendation = item.get("recommendation") if isinstance(item.get("recommendation"), dict) else {}
        parts = [
            item.get("what_changed") or item.get("summary") or item.get("title"),
            item.get("why_it_matters") or impact.get("summary"),
            item.get("next_step") or recommendation.get("summary"),
        ]
        return " ".join(str(part).strip() for part in parts if str(part or "").strip())

    def _default_idempotency_key(
        self,
        organization_id: str,
        person_id: str,
        snapshot_type: str,
        workspace_id: str | None,
    ) -> str:
        workspace = workspace_id or "portfolio"
        return f"proactive-intelligence:{snapshot_type}:{organization_id}:{person_id}:{workspace}:{self.os.jobs.new_id('refresh')}"
