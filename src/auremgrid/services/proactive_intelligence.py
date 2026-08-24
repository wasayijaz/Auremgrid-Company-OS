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
        detectors = self._proactive_detectors(organization_id, person_id, snapshot_type, workspace_id, as_of)
        payload["proactive_detectors"] = detectors
        if snapshot_type == "executive":
            payload.setdefault("sections", {})["proactive_detectors"] = detectors
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

    def _proactive_detectors(
        self,
        organization_id: str,
        person_id: str,
        snapshot_type: str,
        workspace_id: str | None,
        as_of: datetime | None,
    ) -> list[dict[str, Any]]:
        as_of_dt = as_of or _now()
        workspace_ids = self._visible_workspace_ids(organization_id, person_id, workspace_id)
        detectors = (
            ("health", self._detect_health),
            ("overdue_commitment", self._detect_overdue_commitment),
            ("scope", self._detect_scope),
            ("margin", self._detect_margin),
            ("stalled_review", self._detect_stalled_review),
            ("capacity", self._detect_capacity),
            ("campaign_anomaly", self._detect_campaign_anomaly),
            ("feedback", self._detect_feedback),
            ("renewal", self._detect_renewal),
            ("expansion", self._detect_expansion),
        )
        items: list[dict[str, Any]] = []
        for detector_type, detector in detectors:
            try:
                items.append(detector(organization_id, person_id, workspace_ids, as_of_dt))
            except Exception as exc:  # defensive: a broken source should degrade the detector, not the snapshot
                items.append(self._degraded_detector(detector_type, workspace_id, str(exc)))
        return items

    def _visible_workspace_ids(self, organization_id: str, person_id: str, workspace_id: str | None) -> list[str]:
        if workspace_id is not None:
            row = self.conn.execute(
                """SELECT 1 FROM workspace_memberships wm
                   JOIN workspace_organization wo ON wo.workspace_id=wm.workspace_id
                   WHERE wo.organization_id=? AND wm.person_id=? AND wm.workspace_id=?""",
                (organization_id, person_id, workspace_id),
            ).fetchone()
            return [workspace_id] if row else []
        rows = self.conn.execute(
            """SELECT wm.workspace_id FROM workspace_memberships wm
               JOIN workspace_organization wo ON wo.workspace_id=wm.workspace_id
               WHERE wo.organization_id=? AND wm.person_id=?
               ORDER BY wm.workspace_id""",
            (organization_id, person_id),
        ).fetchall()
        return [str(row["workspace_id"]) for row in rows]

    def _detect_health(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        rows = self._latest_workspace_rows(
            "client_health_snapshots",
            organization_id,
            workspace_ids,
            "calculated_at",
        )
        if not rows:
            return self._insufficient_detector("health", None, "No client health snapshots are available for visible workspaces.")
        worst = min(rows, key=lambda row: float(row["overall"]))
        overall = float(worst["overall"])
        previous = worst["previous_score"]
        drop = float(previous) - overall if previous is not None else 0.0
        trend = str(worst["trend"] or "").lower()
        if overall < 80 or drop >= 5 or trend in {"down", "declining", "negative"}:
            return self._detector_item(
                "health",
                "Client health needs review",
                f"{worst['workspace_id']} health is {overall:.0f}"
                + (f", down {drop:.0f} points from the prior score" if drop >= 5 else f" with {trend or 'recorded'} trend"),
                worst["workspace_id"],
                [self._detector_evidence("client_health_snapshots", worst, str(worst["explanation"]))],
                recommendation="Review the latest health explanation and confirm the recovery owner.",
                severity="high" if overall < 70 else "medium",
                confidence=0.86,
            )
        return self._detector_item(
            "health",
            "Client health source is current",
            f"{len(rows)} visible workspace health snapshot(s) are present and none crossed the alert threshold.",
            None,
            [self._detector_evidence("client_health_snapshots", worst, str(worst["explanation"]))],
            status="ready",
            recommendation="Keep monitoring the next calculated health snapshot.",
            severity="low",
            confidence=0.72,
        )

    def _detect_overdue_commitment(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        overdue: list[dict[str, Any]] = []
        for workspace_id in workspace_ids:
            overdue.extend(dict(row) for row in self.conn.execute(
                """SELECT * FROM work_items
                   WHERE workspace_id=?
                     AND status NOT IN ('shipped','done','completed','closed','archived','cancelled')
                     AND COALESCE(needed_by, deadline) IS NOT NULL
                     AND date(COALESCE(needed_by, deadline)) < date(?)
                   ORDER BY date(COALESCE(needed_by, deadline)) ASC, id ASC LIMIT 1""",
                (workspace_id, as_of.date().isoformat()),
            ).fetchall())
        if overdue:
            row = overdue[0]
            due = row["needed_by"] or row["deadline"]
            return self._detector_item(
                "overdue_commitment",
                "Commitment is overdue",
                f"{row['title']} was due by {due} and is still {row['status']}.",
                row["workspace_id"],
                [self._detector_evidence("work_items", row, f"{row['title']} due {due}")],
                recommendation="Confirm whether the commitment is still valid, blocked, or already shipped.",
                severity="high",
                confidence=0.9,
            )
        open_count = sum(
            int(self.conn.execute(
                """SELECT COUNT(*) FROM work_items
                   WHERE workspace_id=?
                     AND status NOT IN ('shipped','done','completed','closed','archived','cancelled')""",
                (workspace_id,),
            ).fetchone()[0] or 0)
            for workspace_id in workspace_ids
        )
        if open_count == 0:
            return self._insufficient_detector("overdue_commitment", None, "No open commitments are tracked for visible workspaces.")
        return self._ready_detector("overdue_commitment", "No overdue commitments found", f"{open_count} open commitment(s) have no past due date.")

    def _detect_scope(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        contract_count = self._count_for_workspaces(
            "SELECT COUNT(*) FROM contracts WHERE organization_id=? AND workspace_id=? AND status='active'",
            organization_id,
            workspace_ids,
        )
        if contract_count == 0:
            return self._insufficient_detector("scope", None, "No active contracts are available for visible workspaces.")
        rows = self._rows_for_workspaces(
            """SELECT u.*,a.service_category,a.included_quantity,a.included_hours
               FROM scope_usage u
               JOIN scope_allowances a ON a.id=u.allowance_id
               JOIN contracts c ON c.id=u.contract_id
               WHERE u.organization_id=? AND u.workspace_id=? AND c.status='active'
               ORDER BY u.calculated_at DESC,u.period_start DESC,u.id DESC""",
            organization_id,
            workspace_ids,
        )
        percentages: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            percentage = self._scope_percentage(row)
            if percentage is not None:
                percentages.append((percentage, row))
        if not percentages:
            return self._insufficient_detector("scope", None, "Active contracts exist, but allowance usage has not been recorded.")
        percentage, row = max(percentages, key=lambda item: item[0])
        if percentage > 90:
            return self._detector_item(
                "scope",
                "Scope allowance is near or over limit",
                f"{row['service_category']} is at {percentage:.0f}% of the recorded allowance.",
                row["workspace_id"],
                [self._detector_evidence("scope_usage", row, f"{row['service_category']} usage at {percentage:.0f}%")],
                recommendation="Review the allowance before more delivery work is accepted.",
                severity="high" if percentage > 100 else "medium",
                confidence=0.88,
            )
        return self._ready_detector("scope", "Scope usage is within allowance", f"Highest recorded allowance usage is {percentage:.0f}%.")

    def _detect_margin(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        rows = self._latest_workspace_rows("client_economics", organization_id, workspace_ids, "calculated_at")
        rows = [row for row in rows if row["margin"] is not None]
        if not rows:
            return self._insufficient_detector("margin", None, "No client economics rows with margin are available.")
        worst = min(rows, key=lambda row: float(row["margin"]))
        margin = float(worst["margin"])
        if margin < 0.25:
            return self._detector_item(
                "margin",
                "Margin pressure detected",
                f"{worst['workspace_id']} margin is {margin:.0%} for period starting {worst['period_start']}.",
                worst["workspace_id"],
                [self._detector_evidence("client_economics", worst, f"margin {margin:.0%}")],
                recommendation="Review labor, software, AI, and other recorded costs before the next account decision.",
                severity="high" if margin < 0 else "medium",
                confidence=0.87,
            )
        return self._ready_detector("margin", "Margins are above pressure threshold", f"Lowest recorded margin is {margin:.0%}.")

    def _detect_stalled_review(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        cutoff = (as_of - timedelta(hours=48)).isoformat()
        rows = self._rows_for_workspaces(
            """SELECT * FROM reviews
               WHERE organization_id=? AND workspace_id=?
                 AND opened_at IS NOT NULL
                 AND opened_at < ?
                 AND closed_at IS NULL
                 AND status NOT IN ('approved','rejected','closed','completed')
               ORDER BY opened_at ASC,id ASC LIMIT 1""",
            organization_id,
            workspace_ids,
            cutoff,
        )
        if rows:
            row = rows[0]
            return self._detector_item(
                "stalled_review",
                "Review has stalled",
                f"Review {row['id']} has been open since {row['opened_at']}.",
                row["workspace_id"],
                [self._detector_evidence("reviews", row, f"review opened {row['opened_at']}")],
                recommendation="Ask the reviewer for a decision or unblocker before more revisions pile up.",
                severity="medium",
                confidence=0.86,
            )
        review_count = self._count_for_workspaces("SELECT COUNT(*) FROM reviews WHERE organization_id=? AND workspace_id=?", organization_id, workspace_ids)
        if review_count == 0:
            return self._insufficient_detector("stalled_review", None, "No review records are available for visible workspaces.")
        return self._ready_detector("stalled_review", "No stalled reviews found", "Tracked reviews are closed or inside the 48-hour window.")

    def _detect_capacity(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        rows = [
            dict(row) for row in self.conn.execute(
                """SELECT * FROM capacity_snapshots
                   WHERE organization_id=?
                   ORDER BY overloaded DESC, remaining_hours ASC, calculated_at DESC,id DESC LIMIT 1""",
                (organization_id,),
            ).fetchall()
        ]
        if not rows:
            return self._insufficient_detector("capacity", None, "No team capacity snapshots are available.")
        row = rows[0]
        remaining = float(row["remaining_hours"])
        if int(row["overloaded"]) or remaining < 0:
            return self._detector_item(
                "capacity",
                "Capacity is overbooked",
                f"{row['person_id']} has {remaining:.1f} remaining hours for week {row['week_start']}.",
                None,
                [self._detector_evidence("capacity_snapshots", row, f"remaining hours {remaining:.1f}")],
                recommendation="Rebalance work before assigning new commitments.",
                severity="high",
                confidence=0.9,
            )
        return self._ready_detector("capacity", "Capacity is not overloaded", f"Latest capacity snapshot has {remaining:.1f} remaining hours.")

    def _detect_campaign_anomaly(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        rows = self._rows_for_workspaces(
            """SELECT a.*,c.workspace_id,c.name campaign_name
               FROM campaign_anomalies a
               JOIN campaigns c ON c.id=a.campaign_id
               WHERE c.organization_id=? AND c.workspace_id=?
                 AND a.status IN ('open','proposed','new','observing')
               ORDER BY CASE a.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                        a.detected_at DESC,a.id DESC LIMIT 1""",
            organization_id,
            workspace_ids,
        )
        if rows:
            row = rows[0]
            return self._detector_item(
                "campaign_anomaly",
                "Campaign anomaly needs review",
                f"{row['campaign_name']} has a {row['severity']} {row['metric']} anomaly: {row['explanation']}",
                row["workspace_id"],
                [self._detector_evidence("campaign_anomalies", row, str(row["evidence"]))],
                recommendation="Review the underlying campaign metric snapshot before changing spend or creative.",
                severity=str(row["severity"] or "medium"),
                confidence=0.84,
            )
        metric_count = self._count_for_workspaces("SELECT COUNT(*) FROM campaign_metric_snapshots WHERE organization_id=? AND workspace_id=?", organization_id, workspace_ids)
        if metric_count == 0:
            return self._insufficient_detector("campaign_anomaly", None, "No campaign metric snapshots are available for anomaly detection.")
        return self._ready_detector("campaign_anomaly", "No open campaign anomalies", f"{metric_count} campaign metric snapshot(s) are available and no open anomaly is recorded.")

    def _detect_feedback(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        rows = self._rows_for_workspaces(
            """SELECT * FROM feedback_patterns
               WHERE organization_id=? AND workspace_id=?
                 AND occurrence_count >= 2
                 AND preference_status IN ('observing','proposed')
               ORDER BY occurrence_count DESC,last_seen_at DESC,id DESC LIMIT 1""",
            organization_id,
            workspace_ids,
        )
        if rows:
            row = rows[0]
            return self._detector_item(
                "feedback",
                "Repeated feedback pattern detected",
                f"{row['pattern_key']} appeared {row['occurrence_count']} times and is still {row['preference_status']}.",
                row["workspace_id"],
                [self._detector_evidence("feedback_patterns", row, str(row["sample_evidence"]))],
                recommendation="Decide whether this pattern should become an approved client preference.",
                severity="medium",
                confidence=0.82,
            )
        source_count = self._count_for_workspaces("SELECT COUNT(*) FROM feedback_patterns WHERE organization_id=? AND workspace_id=?", organization_id, workspace_ids)
        if source_count == 0:
            source_count = self._count_for_workspaces("SELECT COUNT(*) FROM feedback_events WHERE organization_id=? AND workspace_id=?", organization_id, workspace_ids)
        if source_count == 0:
            return self._insufficient_detector("feedback", None, "No feedback events or patterns are available.")
        return self._ready_detector("feedback", "No repeated unresolved feedback pattern", f"{source_count} feedback source record(s) are available.")

    def _detect_renewal(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        rows = self._rows_for_workspaces(
            """SELECT * FROM contracts
               WHERE organization_id=? AND workspace_id=? AND status='active'
                 AND COALESCE(renewal_date,end_date) IS NOT NULL
               ORDER BY date(COALESCE(renewal_date,end_date)) ASC,id ASC""",
            organization_id,
            workspace_ids,
        )
        if not rows:
            contract_count = self._count_for_workspaces("SELECT COUNT(*) FROM contracts WHERE organization_id=? AND workspace_id=? AND status='active'", organization_id, workspace_ids)
            if contract_count == 0:
                return self._insufficient_detector("renewal", None, "No active contracts are available for renewal detection.")
            return self._insufficient_detector("renewal", None, "Active contracts exist, but renewal/end dates are not recorded.")
        due_rows = []
        for row in rows:
            due_date = self._date_from_iso(row["renewal_date"] or row["end_date"])
            if due_date is not None and 0 <= (due_date - as_of.date()).days <= 60:
                due_rows.append((due_date, row))
        if due_rows:
            due_date, row = min(due_rows, key=lambda item: item[0])
            return self._detector_item(
                "renewal",
                "Renewal window is approaching",
                f"{row['workspace_id']} contract renewal/end date is {due_date.isoformat()}.",
                row["workspace_id"],
                [self._detector_evidence("contracts", row, f"renewal/end date {due_date.isoformat()}")],
                recommendation="Prepare the renewal position before the client conversation.",
                severity="medium",
                confidence=0.86,
            )
        next_date = self._date_from_iso(rows[0]["renewal_date"] or rows[0]["end_date"])
        return self._ready_detector("renewal", "No near-term renewal window", f"Next recorded renewal/end date is {next_date.isoformat() if next_date else 'not parseable'}.")

    def _detect_expansion(self, organization_id: str, person_id: str, workspace_ids: list[str], as_of: datetime) -> dict[str, Any]:
        rows = self._rows_for_workspaces(
            """SELECT * FROM opportunities
               WHERE organization_id=? AND workspace_id=?
                 AND status IN ('open','proposed')
                 AND lower(type) IN ('scope_expansion','expansion','upsell','cross_sell','cross-sell','account_expansion')
               ORDER BY COALESCE(estimated_value,0) DESC,created_at DESC,id DESC LIMIT 1""",
            organization_id,
            workspace_ids,
        )
        if rows:
            row = rows[0]
            value = f" worth {float(row['estimated_value']):.0f}" if row["estimated_value"] is not None else ""
            return self._detector_item(
                "expansion",
                "Expansion opportunity is open",
                f"{row['type']} opportunity{value}: {row['reason']}",
                row["workspace_id"],
                [self._detector_evidence("opportunities", row, str(row["evidence"]))],
                recommendation=str(row["recommendation"] or "Review the expansion evidence."),
                severity="medium",
                confidence=0.84,
            )
        source_count = self._count_for_workspaces("SELECT COUNT(*) FROM opportunities WHERE organization_id=? AND workspace_id=?", organization_id, workspace_ids)
        if source_count == 0:
            return self._insufficient_detector("expansion", None, "No opportunity records are available for visible workspaces.")
        return self._ready_detector("expansion", "No open expansion opportunity", f"{source_count} opportunity record(s) are available, with no open expansion item.")

    def _latest_workspace_rows(self, table: str, organization_id: str, workspace_ids: list[str], time_column: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for workspace_id in workspace_ids:
            row = self.conn.execute(
                f"""SELECT * FROM {table}
                    WHERE organization_id=? AND workspace_id=?
                    ORDER BY {time_column} DESC,id DESC LIMIT 1""",
                (organization_id, workspace_id),
            ).fetchone()
            if row is not None:
                rows.append(dict(row))
        return rows

    def _rows_for_workspaces(self, sql: str, organization_id: str, workspace_ids: list[str], *extra: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for workspace_id in workspace_ids:
            rows.extend(dict(row) for row in self.conn.execute(sql, (organization_id, workspace_id, *extra)).fetchall())
        return rows

    def _count_for_workspaces(self, sql: str, organization_id: str, workspace_ids: list[str]) -> int:
        return sum(int(self.conn.execute(sql, (organization_id, workspace_id)).fetchone()[0] or 0) for workspace_id in workspace_ids)

    def _scope_percentage(self, row: dict[str, Any]) -> float | None:
        included_hours = row.get("included_hours")
        included_quantity = row.get("included_quantity")
        if included_hours not in (None, 0):
            return (float(row.get("used_hours") or 0) / float(included_hours)) * 100
        if included_quantity not in (None, 0):
            used = float(row.get("delivered_quantity") or 0) + float(row.get("in_review_quantity") or 0) + float(row.get("requested_quantity") or 0)
            return (used / float(included_quantity)) * 100
        return None

    def _date_from_iso(self, value: str | None) -> Any:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            return None

    def _ready_detector(self, detector_type: str, title: str, summary: str) -> dict[str, Any]:
        return self._detector_item(detector_type, title, summary, None, [], status="ready", recommendation="Keep monitoring this detector.", severity="low", confidence=0.7)

    def _insufficient_detector(self, detector_type: str, workspace_id: str | None, reason: str) -> dict[str, Any]:
        return self._detector_item(
            detector_type,
            f"{detector_type.replace('_', ' ').title()} detector needs source data",
            reason,
            workspace_id,
            [],
            status="insufficient_evidence",
            recommendation="Connect or record the missing source data before relying on this detector.",
            severity="unknown",
            confidence=0.0,
        )

    def _degraded_detector(self, detector_type: str, workspace_id: str | None, reason: str) -> dict[str, Any]:
        return self._detector_item(
            detector_type,
            f"{detector_type.replace('_', ' ').title()} detector degraded",
            f"Detector could not read its source cleanly: {reason}",
            workspace_id,
            [],
            status="degraded",
            recommendation="Retry after checking the underlying canonical source.",
            severity="unknown",
            confidence=0.0,
        )

    def _detector_item(
        self,
        detector_type: str,
        title: str,
        summary: str,
        workspace_id: str | None,
        evidence: list[dict[str, Any]],
        status: str = "open",
        recommendation: str = "Review the cited source record.",
        severity: str = "medium",
        confidence: float = 0.75,
    ) -> dict[str, Any]:
        return {
            "type": detector_type,
            "detector": detector_type,
            "status": status,
            "severity": severity,
            "title": title,
            "summary": summary,
            "what_changed": title,
            "why_it_matters": summary,
            "next_step": recommendation,
            "recommendation": {"summary": recommendation},
            "workspace_id": workspace_id,
            "confidence": confidence,
            "evidence": evidence,
        }

    def _detector_evidence(self, table: str, row: dict[str, Any], summary: str, confidence: float = 0.8) -> dict[str, Any]:
        row_id = str(row.get("id") or "")
        return {
            "object_ref": {"type": table, "id": row_id},
            "citation": {
                "source_key": f"canonical://{table}/{row_id}",
                "locator": f"{table}:{row_id}",
                "evidence_span": summary,
                "confidence": confidence,
            },
        }

    def _attention_items(self, snapshot: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        detector_items = [
            item for item in payload.get("proactive_detectors", [])
            if item.get("status") in {"open", "degraded"}
        ]
        if snapshot["snapshot_type"] == "executive":
            existing_items = payload.get("sections", {}).get("top_three") or payload.get("sections", {}).get("attention", [])
        else:
            existing_items = payload.get("findings", [])
        source_items = (detector_items + existing_items)[:3]
        status = self._attention_status(snapshot["status"])
        items: list[dict[str, Any]] = []
        for rank, item in enumerate(source_items[:3], start=1):
            title = str(item.get("title") or item.get("what_changed") or "Intelligence attention item")
            narrative = self._narrative(item)
            item_workspace_id = item.get("workspace_id") or snapshot.get("workspace_id")
            evidence_refs = self._evidence_refs({"sections": {"item": item}, "findings": [item]})
            item_status = item.get("status") if item.get("status") in {"degraded", "insufficient_evidence"} else status
            items.append({
                "id": self.os.jobs.new_id("intelattn"),
                "snapshot_id": snapshot["id"],
                "organization_id": snapshot["organization_id"],
                "workspace_id": str(item_workspace_id) if item_workspace_id is not None else None,
                "person_id": snapshot["person_id"],
                "rank": rank,
                "title": title,
                "narrative": narrative,
                "status": item_status,
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
