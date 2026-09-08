from __future__ import annotations

from typing import Any

from auremgrid.domain.errors import ValidationError
from auremgrid.services.intelligence_shared import _now


# Fixed starter catalog. Keys are stable contract identifiers; the question
# text is the canonical phrasing stored alongside every answer.
SUCCESS_QUESTIONS: tuple[dict[str, str], ...] = (
    {"key": "success_outcome", "question": "What does a successful client outcome look like?"},
    {"key": "success_metrics", "question": "Which 2-3 numbers define success?"},
    {"key": "hard_constraints", "question": "What constraints must never be crossed?"},
    {"key": "communication_standard", "question": "What does good communication look like?"},
    {"key": "human_consult_triggers", "question": "When should a human be consulted?"},
)

_QUESTION_BY_KEY = {item["key"]: item for item in SUCCESS_QUESTIONS}


class IntelligenceIntakeStore:
    """Fixed success-definition catalog with one persisted answer per question key."""

    def __init__(self, conn: Any, new_id: Any) -> None:
        self.conn = conn
        self.new_id = new_id

    def answers(self, organization_id: str) -> list[dict[str, Any]]:
        """Answered questions only; missing answers stay absent."""
        return [
            dict(row) for row in self.conn.execute(
                """SELECT question_key, question, answer, answered_by, updated_at
                   FROM intelligence_success_definitions
                   WHERE organization_id=? AND workspace_id IS NULL
                   ORDER BY question_key""",
                (organization_id,),
            ).fetchall()
        ]

    def questions(self, organization_id: str) -> dict[str, Any]:
        """Full catalog with the current answer attached; unanswered stays null."""
        answered = {row["question_key"]: row for row in self.answers(organization_id)}
        return {
            "organization_id": organization_id,
            "questions": [
                {
                    "key": item["key"],
                    "question": item["question"],
                    "answer": answered[item["key"]]["answer"] if item["key"] in answered else None,
                    "answered_by": answered[item["key"]]["answered_by"] if item["key"] in answered else None,
                    "updated_at": answered[item["key"]]["updated_at"] if item["key"] in answered else None,
                }
                for item in SUCCESS_QUESTIONS
            ],
        }

    def answer_question(self, organization_id: str, question_key: str, answer: str, answered_by: str) -> dict[str, Any]:
        """Owner-scoped upsert: one row per organization and question key."""
        key = str(question_key or "").strip()
        question = _QUESTION_BY_KEY.get(key)
        if question is None:
            raise ValidationError("unknown question_key")
        text = str(answer or "").strip()
        if not text:
            raise ValidationError("answer must not be empty")
        now = _now().isoformat()
        existing = self.conn.execute(
            """SELECT id FROM intelligence_success_definitions
               WHERE organization_id=? AND workspace_id IS NULL AND question_key=?""",
            (organization_id, key),
        ).fetchone()
        if existing is not None:
            self.conn.execute(
                """UPDATE intelligence_success_definitions
                   SET answer=?, answered_by=?, updated_at=? WHERE id=?""",
                (text, answered_by, now, existing["id"]),
            )
        else:
            self.conn.execute(
                """INSERT INTO intelligence_success_definitions(
                       id, organization_id, workspace_id, question_key, question,
                       answer, answered_by, created_at, updated_at
                   ) VALUES (?,?,NULL,?,?,?,?,?,?)""",
                (self.new_id("isuc"), organization_id, key, question["question"], text, answered_by, now, now),
            )
        self.conn.commit()
        row = self.conn.execute(
            """SELECT question_key, question, answer, answered_by, created_at, updated_at
               FROM intelligence_success_definitions
               WHERE organization_id=? AND workspace_id IS NULL AND question_key=?""",
            (organization_id, key),
        ).fetchone()
        return dict(row)
