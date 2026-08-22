from __future__ import annotations

import json
import math
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


_SEMANTIC_CONCEPTS: tuple[tuple[str, frozenset[str]], ...] = (
    ("natural-human-texture", frozenset({
        "ai", "artificial", "generated", "human", "natural", "person", "face", "skin",
        "polished", "retouched", "smooth", "smoothed", "synthetic", "texture",
    })),
    ("concise-copy", frozenset({
        "brief", "concise", "short", "shorten", "shorter", "tight", "wordy", "verbose",
    })),
)


def _semantic_key(category: str, raw: str) -> str:
    """Return a conservative concept key while preserving unknown feedback literally.

    This is intentionally deterministic and auditable.  It captures only
    established agency preference concepts; ambiguous feedback stays on the
    literal path instead of being over-clustered by an opaque similarity score.
    """
    literal = _normalize_key(raw)
    tokens = frozenset("".join(char if char.isalnum() else " " for char in literal).split())
    for concept, vocabulary in _SEMANTIC_CONCEPTS:
        if concept == "natural-human-texture":
            artificial_markers = {"ai", "artificial", "generated", "polished", "retouched", "smooth", "smoothed", "synthetic"}
            appearance_markers = {"face", "human", "person", "skin", "texture"}
            matched = bool(tokens & artificial_markers) or ("natural" in tokens and bool(tokens & appearance_markers))
        else:
            matched = bool(tokens & vocabulary)
        if matched:
            if concept == "natural-human-texture" and category != "design":
                continue
            if concept == "concise-copy" and category != "copy":
                continue
            return f"semantic:{concept}"
    return literal


class FeedbackOperations:
    def __init__(self, conn: Any, new_id: Callable[[str], str], authorize: Callable[..., Any], embedding_provider: Any | None = None) -> None:
        self.conn, self.new_id, self.authorize, self.embedding_provider = conn, new_id, authorize, embedding_provider

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        dot = sum(a * b for a, b in zip(left, right))
        scale = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
        return dot / scale if scale else 0.0

    def _semantic_existing(self, organization_id: str, workspace_id: str, category: str, raw_feedback: str) -> Any | None:
        if self.embedding_provider is None:
            return None
        rows = self.conn.execute(
            "SELECT id,pattern_key,occurrence_count,sample_evidence,preference_status FROM feedback_patterns "
            "WHERE organization_id=? AND workspace_id=? AND category=?",
            (organization_id, workspace_id, category),
        ).fetchall()
        if not rows:
            return None
        representatives = [json.loads(row["sample_evidence"])[-1] for row in rows]
        try:
            vectors = self.embedding_provider.embed([raw_feedback, *representatives])
        except Exception:
            return None
        if len(vectors) != len(rows) + 1:
            return None
        scored = [(self._cosine(list(vectors[0]), list(vector)), row) for vector, row in zip(vectors[1:], rows)]
        score, row = max(scored, key=lambda item: item[0])
        return row if score >= 0.84 else None

    def record_feedback(self, organization_id: str, workspace_id: str, person_id: str,
        category: str, raw_feedback: str, source_type: str, source_id: str | None = None) -> dict[str, Any]:
        self.authorize(organization_id, workspace_id, person_id, write=True)
        if category not in VALID_CATEGORIES:
            raise ValidationError(f"invalid category: {category}")
        now = _now().isoformat()
        if not raw_feedback.strip():
            raise ValidationError("raw feedback is required")
        pattern_key = _semantic_key(category, raw_feedback)
        existing = self.conn.execute(
            "SELECT id, pattern_key, occurrence_count, sample_evidence, preference_status FROM feedback_patterns WHERE organization_id=? AND workspace_id=? AND category=? AND pattern_key=?",
            (organization_id, workspace_id, category, pattern_key),
        ).fetchone()
        existing = existing or self._semantic_existing(organization_id, workspace_id, category, raw_feedback)
        if existing:
            pid = existing["id"]
            pattern_key = existing["pattern_key"]
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
