from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


VALID_CATEGORIES = ("design", "copy", "approval", "stakeholder", "process", "other")
_PROMOTE_THRESHOLD = 3
_MAX_EVIDENCE = 5


def _normalize_key(raw: str) -> str:
    k = " ".join(str(raw).strip().lower().split())
    return k[:200] if len(k) > 200 else k


class FeedbackOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any]) -> None:
        self.conn, self.new_id, self.authorize = conn, new_id, authorize

    def record_feedback(self, organization_id: str, workspace_id: str, person_id: str,
        category: str, raw_feedback: str, source_type: str, source_id: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        if category not in VALID_CATEGORIES:
            raise ValidationError(f"invalid category: {category}")
        now = _now().isoformat()
        pattern_key = _normalize_key(raw_feedback)
        existing = self.conn.execute(
            "SELECT id, occurrence_count, sample_evidence, preference_status FROM feedback_patterns WHERE organization_id=? AND workspace_id=? AND category=? AND pattern_key=?",
            (organization_id, workspace_id, category, pattern_key),
        ).fetchone()
        if existing:
            pid = existing["id"]
            count = existing["occurrence_count"] + 1
            evidence = json.loads(existing["sample_evidence"])
            evidence.append(raw_feedback[:300])
            evidence = evidence[-_MAX_EVIDENCE:]
            status = existing["preference_status"]
            if count >= _PROMOTE_THRESHOLD and status == "observing":
                status = "proposed"
            self.conn.execute(
                "UPDATE feedback_patterns SET occurrence_count=?, last_seen_at=?, sample_evidence=?, preference_status=?, updated_at=? WHERE id=?",
                (count, now, json.dumps(evidence), status, now, pid),
            )
        else:
            pid = self.new_id("fp")
            status = "observing"
            self.conn.execute(
                "INSERT INTO feedback_patterns (id, organization_id, workspace_id, category, pattern_key, occurrence_count, first_seen_at, last_seen_at, sample_evidence, preference_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, organization_id, workspace_id, category, pattern_key, 1, now, now, json.dumps([raw_feedback[:300]]), status, now, now),
            )
        event_id = self.new_id("fe")
        self.conn.execute(
            "INSERT INTO feedback_events (id, organization_id, workspace_id, pattern_id, category, raw_feedback, source_type, source_id, recorded_by_person_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, organization_id, workspace_id, pid, category, raw_feedback, source_type, source_id, person_id, now),
        )
        self.conn.commit()
        return {"id": event_id, "pattern_id": pid, "pattern_key": pattern_key, "occurrence_count": count if existing else 1, "preference_status": status}

    def list_patterns(self, organization_id: str, workspace_id: str, person_id: str,
        category: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        self.authorize(organization_id, workspace_id, person_id)
        sql = "SELECT * FROM feedback_patterns WHERE organization_id=? AND workspace_id=?"
        params: list[Any] = [organization_id, workspace_id]
        if category:
            sql += " AND category=?"; params.append(category)
        if status:
            sql += " AND preference_status=?"; params.append(status)
        sql += " ORDER BY last_seen_at DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def promote_pattern(self, organization_id: str, workspace_id: str, person_id: str, pattern_id: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        row = self.conn.execute(
            "SELECT * FROM feedback_patterns WHERE id=? AND organization_id=? AND workspace_id=?",
            (pattern_id, organization_id, workspace_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("pattern not found")
        if row["preference_status"] != "observing":
            raise ValidationError("can only promote observing patterns")
        now = _now().isoformat()
        self.conn.execute("UPDATE feedback_patterns SET preference_status='proposed', updated_at=? WHERE id=?", (now, pattern_id))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM feedback_patterns WHERE id=?", (pattern_id,)).fetchone())

    def decide_pattern(self, organization_id: str, workspace_id: str, person_id: str, pattern_id: str, decision: str) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        if decision not in ("approved", "rejected"):
            raise ValidationError("decision must be approved or rejected")
        row = self.conn.execute(
            "SELECT * FROM feedback_patterns WHERE id=? AND organization_id=? AND workspace_id=?",
            (pattern_id, organization_id, workspace_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("pattern not found")
        now = _now().isoformat()
        self.conn.execute("UPDATE feedback_patterns SET preference_status=?, updated_at=? WHERE id=?", (decision, now, pattern_id))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM feedback_patterns WHERE id=?", (pattern_id,)).fetchone())
