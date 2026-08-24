from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import NotFoundError, ValidationError


HYPOTHESIS_STATUSES = {"proposed", "supported", "challenged", "refuted", "resolved", "retired"}
GENERATOR_TYPES = {"person", "agent", "expert_profile", "runbook", "model", "system"}
LIFECYCLE_EVENTS = {"accepted", "rejected", "chosen", "evaluated"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(f"{field} must be a list")
    return value


def _text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field} is required")
    return text


def _confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("confidence must be numeric") from exc
    if confidence < 0 or confidence > 1:
        raise ValidationError("confidence must be between 0 and 1")
    return confidence


def _parse_time(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _row_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in (
        "evidence_for_refs_json",
        "evidence_against_refs_json",
        "assumptions_json",
        "outcome_json",
        "profile_contributors_json",
        "options_json",
        "evidence_refs_json",
        "measured_outcomes_json",
    ):
        if key in result:
            target = key.removesuffix("_json")
            result[target] = json.loads(result.pop(key) or "null")
    return result


class IntelligenceLearningService:
    def __init__(self, os: Any, new_id: Callable[[str], str]) -> None:
        self.os = os
        self.conn = os.store.conn
        self.new_id = new_id

    def record_hypothesis(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        text: str,
        *,
        evidence_for_refs: list[dict[str, Any]] | None = None,
        evidence_against_refs: list[dict[str, Any]] | None = None,
        status: str = "proposed",
        confidence: float = 0.5,
        assumptions: list[str] | None = None,
        generated_by: dict[str, str] | None = None,
        resolution: str | None = None,
        outcome: dict[str, Any] | None = None,
        supersedes_hypothesis_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.os._require_person_access(organization_id, workspace_id, person_id, write=True)
        status = _text(status, "status")
        if status not in HYPOTHESIS_STATUSES:
            raise ValidationError("invalid hypothesis status")
        generated = self._generated_by(generated_by, person_id)
        evidence_for = self._validate_evidence_refs(organization_id, workspace_id, _list(evidence_for_refs, "evidence_for_refs"))
        evidence_against = self._validate_evidence_refs(organization_id, workspace_id, _list(evidence_against_refs, "evidence_against_refs"))
        assumptions_list = [str(item).strip() for item in _list(assumptions, "assumptions") if str(item).strip()]
        if supersedes_hypothesis_id:
            self._hypothesis(organization_id, workspace_id, supersedes_hypothesis_id)
        payload = {
            "text": _text(text, "text"),
            "evidence_for_refs": evidence_for,
            "evidence_against_refs": evidence_against,
            "status": status,
            "confidence": _confidence(confidence),
            "assumptions": assumptions_list,
            "generated_by": generated,
            "resolution": resolution,
            "outcome": outcome or None,
            "supersedes_hypothesis_id": supersedes_hypothesis_id,
        }
        cached = self._idempotent(organization_id, workspace_id, idempotency_key, "intelligence.hypothesis.record", payload)
        if cached is not None:
            return cached
        now = _now()
        item = {
            "id": self.new_id("ihyp"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "text": payload["text"],
            "evidence_for_refs_json": _json(evidence_for),
            "evidence_against_refs_json": _json(evidence_against),
            "status": status,
            "confidence": payload["confidence"],
            "assumptions_json": _json(assumptions_list),
            "generated_by_type": generated["type"],
            "generated_by_id": generated["id"],
            "recorded_by_person_id": person_id,
            "supersedes_hypothesis_id": supersedes_hypothesis_id,
            "resolution": resolution.strip() if isinstance(resolution, str) and resolution.strip() else None,
            "outcome_json": _json(outcome) if outcome else None,
            "created_at": now,
        }
        self.conn.execute(
            """INSERT INTO intelligence_hypotheses(
                id,organization_id,workspace_id,text,evidence_for_refs_json,evidence_against_refs_json,
                status,confidence,assumptions_json,generated_by_type,generated_by_id,recorded_by_person_id,
                supersedes_hypothesis_id,resolution,outcome_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        result = _row_dict(item)
        self._save_idempotency(organization_id, workspace_id, idempotency_key, "intelligence.hypothesis.record", payload, result, now)
        self.conn.commit()
        return result

    def record_recommendation(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        summary: str,
        *,
        runbook_id: str,
        runbook_version: int,
        profile_contributors: list[dict[str, Any]],
        confidence: float,
        options: list[dict[str, Any]],
        recommended_option_id: str | None,
        evidence_refs: list[dict[str, Any]],
        evaluation_window_start: str,
        evaluation_window_end: str,
        generated_by: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.os._require_person_access(organization_id, workspace_id, person_id, write=True)
        runbook_id = _text(runbook_id, "runbook_id")
        self._runbook(runbook_id, int(runbook_version))
        contributors = self._profile_contributors(_list(profile_contributors, "profile_contributors"))
        option_list = self._options(_list(options, "options"), recommended_option_id)
        evidence = self._validate_evidence_refs(organization_id, workspace_id, _list(evidence_refs, "evidence_refs"))
        start = _parse_time(evaluation_window_start, "evaluation_window_start")
        end = _parse_time(evaluation_window_end, "evaluation_window_end")
        if end <= start:
            raise ValidationError("evaluation window end must be after start")
        generated = self._generated_by(generated_by, person_id)
        payload = {
            "summary": _text(summary, "summary"),
            "runbook_id": runbook_id,
            "runbook_version": int(runbook_version),
            "profile_contributors": contributors,
            "confidence": _confidence(confidence),
            "options": option_list,
            "recommended_option_id": recommended_option_id,
            "evidence_refs": evidence,
            "evaluation_window_start": start.isoformat(),
            "evaluation_window_end": end.isoformat(),
            "generated_by": generated,
        }
        cached = self._idempotent(organization_id, workspace_id, idempotency_key, "intelligence.recommendation.record", payload)
        if cached is not None:
            return cached
        now = _now()
        item = {
            "id": self.new_id("irec"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "summary": payload["summary"],
            "runbook_id": runbook_id,
            "runbook_version": int(runbook_version),
            "profile_contributors_json": _json(contributors),
            "confidence": payload["confidence"],
            "options_json": _json(option_list),
            "recommended_option_id": recommended_option_id,
            "evidence_refs_json": _json(evidence),
            "generated_by_type": generated["type"],
            "generated_by_id": generated["id"],
            "recorded_by_person_id": person_id,
            "evaluation_window_start": start.isoformat(),
            "evaluation_window_end": end.isoformat(),
            "created_at": now,
        }
        self.conn.execute(
            """INSERT INTO intelligence_recommendations(
                id,organization_id,workspace_id,summary,runbook_id,runbook_version,profile_contributors_json,
                confidence,options_json,recommended_option_id,evidence_refs_json,generated_by_type,generated_by_id,
                recorded_by_person_id,evaluation_window_start,evaluation_window_end,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        result = _row_dict(item)
        self._save_idempotency(organization_id, workspace_id, idempotency_key, "intelligence.recommendation.record", payload, result, now)
        self.conn.commit()
        return result

    def append_recommendation_event(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        recommendation_id: str,
        event_type: str,
        *,
        chosen_option_id: str | None = None,
        measured_outcomes: list[dict[str, Any]] | None = None,
        score: float | None = None,
        lessons: str = "",
        evidence_refs: list[dict[str, Any]] | None = None,
        evaluation_window_start: str | None = None,
        evaluation_window_end: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.os._require_person_access(organization_id, workspace_id, person_id, write=True)
        recommendation = self._recommendation(organization_id, workspace_id, recommendation_id)
        event_type = _text(event_type, "event_type")
        if event_type not in LIFECYCLE_EVENTS:
            raise ValidationError("invalid recommendation lifecycle event")
        if event_type == "chosen":
            self._validate_option_choice(recommendation, chosen_option_id)
        elif chosen_option_id is not None:
            raise ValidationError("chosen_option_id is only valid for chosen events")
        evidence = self._validate_evidence_refs(organization_id, workspace_id, _list(evidence_refs, "evidence_refs"))
        outcomes = _list(measured_outcomes, "measured_outcomes")
        if event_type == "evaluated":
            evaluation_window_start, evaluation_window_end = self._validate_recommendation_outcomes(
                recommendation, outcomes, evidence, evaluation_window_start, evaluation_window_end
            )
        elif outcomes:
            raise ValidationError("measured_outcomes are only valid for evaluated events")
        normalized_score = None if score is None else _confidence(score)
        if normalized_score is not None and event_type != "evaluated":
            raise ValidationError("score is only valid for evaluated events")
        payload = {
            "recommendation_id": recommendation_id,
            "event_type": event_type,
            "chosen_option_id": chosen_option_id,
            "measured_outcomes": outcomes,
            "score": normalized_score,
            "lessons": lessons.strip(),
            "evidence_refs": evidence,
            "evaluation_window_start": evaluation_window_start,
            "evaluation_window_end": evaluation_window_end,
        }
        cached = self._idempotent(organization_id, workspace_id, idempotency_key, "intelligence.recommendation.lifecycle", payload)
        if cached is not None:
            return cached
        now = _now()
        item = {
            "id": self.new_id("ircl"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "recommendation_id": recommendation_id,
            "event_type": event_type,
            "accepted": 1 if event_type == "accepted" else 0,
            "rejected": 1 if event_type == "rejected" else 0,
            "chosen_option_id": chosen_option_id,
            "evaluation_window_start": evaluation_window_start,
            "evaluation_window_end": evaluation_window_end,
            "measured_outcomes_json": _json(outcomes),
            "score": normalized_score,
            "lessons": lessons.strip(),
            "evidence_refs_json": _json(evidence),
            "recorded_by_person_id": person_id,
            "created_at": now,
        }
        self.conn.execute(
            """INSERT INTO intelligence_recommendation_lifecycle(
                id,organization_id,workspace_id,recommendation_id,event_type,accepted,rejected,chosen_option_id,
                evaluation_window_start,evaluation_window_end,measured_outcomes_json,score,lessons,evidence_refs_json,
                recorded_by_person_id,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        result = _row_dict(item)
        self._save_idempotency(organization_id, workspace_id, idempotency_key, "intelligence.recommendation.lifecycle", payload, result, now)
        self.conn.commit()
        return result

    def workspace_learning(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.os._require_person_access(organization_id, workspace_id, person_id)
        hypotheses = [
            _row_dict(row)
            for row in self.conn.execute(
                """SELECT * FROM intelligence_hypotheses
                   WHERE organization_id=? AND workspace_id=?
                   ORDER BY created_at,id""",
                (organization_id, workspace_id),
            ).fetchall()
        ]
        recommendations = [
            _row_dict(row)
            for row in self.conn.execute(
                """SELECT * FROM intelligence_recommendations
                   WHERE organization_id=? AND workspace_id=?
                   ORDER BY created_at,id""",
                (organization_id, workspace_id),
            ).fetchall()
        ]
        lifecycle = [
            _row_dict(row)
            for row in self.conn.execute(
                """SELECT * FROM intelligence_recommendation_lifecycle
                   WHERE organization_id=? AND workspace_id=?
                   ORDER BY created_at,id""",
                (organization_id, workspace_id),
            ).fetchall()
        ]
        return {
            "scope": {"organization_id": organization_id, "workspace_id": workspace_id},
            "hypotheses": hypotheses,
            "recommendations": recommendations,
            "recommendation_lifecycle": lifecycle,
        }

    def recommendation_quality(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        *,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Return an evidence-scoped, read-only recommendation correctness aggregate.

        Only the latest evaluated lifecycle event for each recommendation is eligible.
        An event must retain a score, measured outcomes, and evidence references; all
        other recommendations remain explicitly pending or insufficient rather than
        being inferred as successes or failures.
        """
        self.os._require_person_access(organization_id, workspace_id, person_id)
        as_of_value = _parse_time(as_of, "as_of").isoformat() if as_of else None
        recommendation_sql = """SELECT * FROM intelligence_recommendations
            WHERE organization_id=? AND workspace_id=?"""
        recommendation_params: list[Any] = [organization_id, workspace_id]
        if as_of_value:
            recommendation_sql += " AND created_at<=?"
            recommendation_params.append(as_of_value)
        recommendation_sql += " ORDER BY created_at,id"
        recommendations = self.conn.execute(recommendation_sql, tuple(recommendation_params)).fetchall()
        recommendation_ids = {row["id"] for row in recommendations}

        lifecycle_sql = """SELECT * FROM intelligence_recommendation_lifecycle
            WHERE organization_id=? AND workspace_id=?"""
        lifecycle_params: list[Any] = [organization_id, workspace_id]
        if as_of_value:
            lifecycle_sql += " AND created_at<=?"
            lifecycle_params.append(as_of_value)
        lifecycle_sql += " ORDER BY created_at,id"
        evaluated_by_recommendation: dict[str, dict[str, Any]] = {}
        for row in self.conn.execute(lifecycle_sql, tuple(lifecycle_params)).fetchall():
            if row["recommendation_id"] not in recommendation_ids or row["event_type"] != "evaluated":
                continue
            evaluated_by_recommendation[row["recommendation_id"]] = _row_dict(row)

        correct = 0
        incorrect = 0
        insufficient = 0
        pending = 0
        measured_outcome_count = 0
        evidence_ref_count = 0
        window_starts: list[str] = []
        window_ends: list[str] = []
        for recommendation in recommendations:
            evaluated = evaluated_by_recommendation.get(recommendation["id"])
            if evaluated is None:
                pending += 1
                continue
            outcomes = evaluated.get("measured_outcomes") or []
            evidence_refs = evaluated.get("evidence_refs") or []
            score = evaluated.get("score")
            if score is None or not outcomes or not evidence_refs:
                insufficient += 1
                continue
            measured_outcome_count += len(outcomes)
            evidence_ref_count += len(evidence_refs)
            window_start = evaluated.get("evaluation_window_start")
            window_end = evaluated.get("evaluation_window_end")
            if window_start:
                window_starts.append(str(window_start))
            if window_end:
                window_ends.append(str(window_end))
            if float(score) >= 0.5:
                correct += 1
            else:
                incorrect += 1

        denominator = correct + incorrect
        status = "ready" if denominator else ("pending" if pending else "insufficient_evidence")
        mean_score = ((sum(
            float(item.get("score"))
            for item in evaluated_by_recommendation.values()
            if item.get("score") is not None
            and item.get("measured_outcomes")
            and item.get("evidence_refs")
        ) / denominator) if denominator else None)
        return {
            "scope": {"organization_id": organization_id, "workspace_id": workspace_id},
            "status": status,
            "correctness_rate": (correct / denominator) if denominator else None,
            "mean_score": mean_score,
            "correct_count": correct,
            "incorrect_count": incorrect,
            "denominator": denominator,
            "evaluated_count": len(evaluated_by_recommendation),
            "recommendation_count": len(recommendations),
            "pending_count": pending,
            "insufficient_count": insufficient,
            "evaluation_window": {
                "start": min(window_starts) if window_starts else None,
                "end": max(window_ends) if window_ends else None,
            },
            "evidence_scope": {
                "measured_outcome_count": measured_outcome_count,
                "evidence_ref_count": evidence_ref_count,
            },
            "method": {
                "score_threshold": 0.5,
                "unit": "latest evaluated recommendation",
                "eligible_when": "score, measured outcomes, and evidence references are all present",
            },
        }

    def _generated_by(self, generated_by: dict[str, str] | None, person_id: str) -> dict[str, str]:
        generated = generated_by or {"type": "person", "id": person_id}
        generator_type = _text(generated.get("type"), "generated_by.type")
        generator_id = _text(generated.get("id"), "generated_by.id")
        if generator_type not in GENERATOR_TYPES:
            raise ValidationError("invalid generated_by type")
        if generator_type == "expert_profile" and not self.conn.execute(
            "SELECT 1 FROM expert_profiles WHERE id=? AND status='active' LIMIT 1", (generator_id,)
        ).fetchone():
            raise NotFoundError("expert profile not found")
        if generator_type == "runbook" and not self.conn.execute(
            "SELECT 1 FROM intelligence_runbooks WHERE id=? AND status='active' LIMIT 1", (generator_id,)
        ).fetchone():
            raise NotFoundError("intelligence runbook not found")
        return {"type": generator_type, "id": generator_id}

    def _runbook(self, runbook_id: str, version: int) -> None:
        if version <= 0:
            raise ValidationError("runbook_version must be positive")
        if not self.conn.execute(
            "SELECT 1 FROM intelligence_runbooks WHERE id=? AND version=? AND status='active'",
            (runbook_id, version),
        ).fetchone():
            raise NotFoundError("intelligence runbook not found")

    def _profile_contributors(self, contributors: list[Any]) -> list[dict[str, Any]]:
        if not contributors:
            raise ValidationError("profile_contributors are required")
        normalized = []
        for contributor in contributors:
            if not isinstance(contributor, dict):
                raise ValidationError("profile contributor must be an object")
            profile_id = _text(contributor.get("profile_id") or contributor.get("id"), "profile_contributor.profile_id")
            version = int(contributor.get("version") or 1)
            if not self.conn.execute(
                "SELECT 1 FROM expert_profiles WHERE id=? AND version=? AND status='active'",
                (profile_id, version),
            ).fetchone():
                raise NotFoundError("expert profile not found")
            normalized.append({"profile_id": profile_id, "version": version, "role": str(contributor.get("role") or "").strip()})
        return normalized

    @staticmethod
    def _options(options: list[Any], recommended_option_id: str | None) -> list[dict[str, Any]]:
        if not options:
            raise ValidationError("options are required")
        normalized = []
        option_ids = set()
        for option in options:
            if not isinstance(option, dict):
                raise ValidationError("recommendation option must be an object")
            option_id = _text(option.get("id"), "option.id")
            if option_id in option_ids:
                raise ValidationError("recommendation option ids must be unique")
            option_ids.add(option_id)
            normalized.append({**option, "id": option_id})
        if recommended_option_id is not None and recommended_option_id not in option_ids:
            raise ValidationError("recommended_option_id must match an option")
        return normalized

    def _validate_option_choice(self, recommendation: dict[str, Any], chosen_option_id: str | None) -> None:
        chosen = _text(chosen_option_id, "chosen_option_id")
        options = json.loads(recommendation["options_json"])
        if chosen not in {str(option.get("id")) for option in options}:
            raise ValidationError("chosen option must belong to the recommendation")

    def _validate_evidence_refs(self, organization_id: str, workspace_id: str, refs: list[Any]) -> list[dict[str, Any]]:
        normalized = []
        seen = set()
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValidationError("evidence refs must be objects")
            ref_type = _text(ref.get("type"), "evidence_ref.type")
            ref_id = _text(ref.get("id"), "evidence_ref.id")
            key = (ref_type, ref_id)
            if key in seen:
                continue
            seen.add(key)
            if not self._evidence_exists(organization_id, workspace_id, ref_type, ref_id):
                raise NotFoundError("evidence ref not found in workspace scope")
            normalized.append({"type": ref_type, "id": ref_id})
        return normalized

    def _evidence_exists(self, organization_id: str, workspace_id: str, ref_type: str, ref_id: str) -> bool:
        scoped_tables = {
            "source": "sources",
            "document": "documents",
            "fact": "facts",
            "decision": "decisions",
            "risk": "risks",
            "signal": "signals",
            "work_item": "work_items",
            "work_event": "work_events",
            "workflow_run": "workflow_runs",
            "workflow_evidence": "workflow_evidence",
            "feedback_event": "feedback_events",
            "performance_insight": "performance_insights",
        }
        table = scoped_tables.get(ref_type)
        if table is None:
            raise ValidationError("unsupported evidence ref type")
        if table in {"sources", "documents", "facts", "work_items", "work_events"}:
            sql = f"SELECT 1 FROM {table} WHERE workspace_id=? AND id=?"
            values = (workspace_id, ref_id)
        else:
            sql = f"SELECT 1 FROM {table} WHERE organization_id=? AND workspace_id=? AND id=?"
            values = (organization_id, workspace_id, ref_id)
        return self.conn.execute(sql, values).fetchone() is not None

    def _hypothesis(self, organization_id: str, workspace_id: str, hypothesis_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM intelligence_hypotheses WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, hypothesis_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("hypothesis not found")
        return dict(row)

    def _recommendation(self, organization_id: str, workspace_id: str, recommendation_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM intelligence_recommendations WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, recommendation_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("recommendation not found")
        return dict(row)

    def _validate_recommendation_outcomes(
        self,
        recommendation: dict[str, Any],
        outcomes: list[Any],
        evidence_refs: list[dict[str, Any]],
        window_start: str | None,
        window_end: str | None,
    ) -> tuple[str, str]:
        if not outcomes:
            raise ValidationError("evaluated recommendations require measured_outcomes")
        if not evidence_refs:
            raise ValidationError("evaluated recommendations require evidence refs")
        start = _parse_time(window_start or recommendation["evaluation_window_start"], "evaluation_window_start")
        end = _parse_time(window_end or recommendation["evaluation_window_end"], "evaluation_window_end")
        if end <= start:
            raise ValidationError("evaluation window end must be after start")
        original_start = _parse_time(recommendation["evaluation_window_start"], "recommendation evaluation_window_start")
        original_end = _parse_time(recommendation["evaluation_window_end"], "recommendation evaluation_window_end")
        if start < original_start or end > original_end:
            raise ValidationError("outcome attribution must stay inside the recommendation evaluation window")
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                raise ValidationError("measured outcome must be an object")
            outcome_type = _text(outcome.get("type"), "measured_outcome.type")
            outcome_id = _text(outcome.get("id"), "measured_outcome.id")
            occurred_at = _parse_time(outcome.get("occurred_at"), "measured_outcome.occurred_at")
            if occurred_at < start or occurred_at > end:
                raise ValidationError("measured outcome is outside the evaluation window")
            if not self._evidence_exists(recommendation["organization_id"], recommendation["workspace_id"], outcome_type, outcome_id):
                raise NotFoundError("measured outcome not found in recommendation scope")
            if not any(ref["type"] == outcome_type and ref["id"] == outcome_id for ref in evidence_refs):
                raise ValidationError("measured outcome requires matching evidence ref")
        return start.isoformat(), end.isoformat()

    def _idempotent(
        self,
        organization_id: str,
        workspace_id: str,
        key: str | None,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not key:
            return None
        payload_hash = _payload_hash(payload)
        row = self.conn.execute(
            """SELECT payload_hash,response FROM intelligence_learning_idempotency_keys
               WHERE organization_id=? AND workspace_id=? AND key=? AND operation=?""",
            (organization_id, workspace_id, key, operation),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise ValidationError("idempotency key was already used with a different payload")
        return json.loads(row["response"])

    def _save_idempotency(
        self,
        organization_id: str,
        workspace_id: str,
        key: str | None,
        operation: str,
        payload: dict[str, Any],
        response: dict[str, Any],
        created_at: str,
    ) -> None:
        if not key:
            return
        self.conn.execute(
            """INSERT INTO intelligence_learning_idempotency_keys(
                organization_id,workspace_id,key,operation,payload_hash,response,created_at
            ) VALUES (?,?,?,?,?,?,?)""",
            (organization_id, workspace_id, key, operation, _payload_hash(payload), _json(response), created_at),
        )
