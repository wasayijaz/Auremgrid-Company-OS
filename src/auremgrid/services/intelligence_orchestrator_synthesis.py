"""Synthesis, disagreement, and runbook gate helpers."""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from auremgrid.services.intelligence_orchestrator_shared import MAX_ITEMS, MAX_ITERATIONS, MAX_TEXT, _json, _ref_id, _score, validate_expert_result


class IntelligenceOrchestratorSynthesisMixin:
    def _synthesize(self, situation: Mapping[str, Any], specialists: list[dict[str, Any]], contradictions: list[dict[str, Any]], errors: list[str], allowed_refs: set[str]) -> dict[str, Any]:
        if not specialists:
            return validate_expert_result({
                "finding": "No specialist produced a bounded result.", "evidence_for": [], "evidence_against": [],
                "assumptions": [], "unknowns": ["Specialist output unavailable."], "hypothesis": "No hypothesis established.",
                "confidence": 0.0, "analogues": [], "risks": errors, "options": [], "recommendation": {"summary": "Review available evidence manually."},
                "expected_impact": {"level": "unknown"}, "needs_review": True, "dissent": [],
            }, allowed_refs=allowed_refs)
        def collect(field: str) -> list[Any]:
            return [item for specialist in ordered for item in specialist.get(field, [])][: self.limits.max_items]

        def distinct_values(field: str) -> list[Any]:
            values = [specialist.get(field) for specialist in ordered if specialist.get(field) not in (None, "", {}, [])]
            unique: list[Any] = []
            seen: set[str] = set()
            for value in values:
                marker = json.dumps(_json(value), sort_keys=True, separators=(",", ":"))
                if marker not in seen:
                    seen.add(marker)
                    unique.append(value)
            return unique

        ranked = sorted(
            enumerate(specialists),
            key=lambda item: (-self._specialist_evidence_weight(item[1], situation, allowed_refs), item[0]),
        )
        ordered = [item for _, item in ranked]
        weights = [self._specialist_evidence_weight(item, situation, allowed_refs) for item in ordered]
        weight_total = sum(weights) or float(len(ordered))
        findings = distinct_values("finding")
        hypotheses = distinct_values("hypothesis")
        recommendations = distinct_values("recommendation")
        impacts = distinct_values("expected_impact")
        combined = {
            "finding": " | ".join(str(value) for value in findings)[:MAX_TEXT] or "No finding returned.",
            "evidence_for": collect("evidence_for"),
            "evidence_against": collect("evidence_against"),
            "assumptions": [item for specialist in ordered for item in specialist.get("assumptions", [])][: self.limits.max_items],
            "unknowns": [item for specialist in ordered for item in specialist.get("unknowns", [])][: self.limits.max_items],
            "hypothesis": hypotheses[0] if len(hypotheses) == 1 else ("Competing specialist hypotheses: " + " | ".join(str(value) for value in hypotheses))[:MAX_TEXT],
            "confidence": round(sum(float(item.get("confidence", 0.0)) * weight for item, weight in zip(ordered, weights)) / weight_total, 3),
            "analogues": collect("analogues"),
            "risks": collect("risks"),
            "options": collect("options"),
            "recommendation": recommendations[0] if len(recommendations) == 1 else {"summary": "Review the synthesized specialist perspectives before acting.", "alternatives": recommendations[: self.limits.max_items]},
            "expected_impact": impacts[0] if len(impacts) == 1 else {"perspectives": impacts[: self.limits.max_items]},
            "needs_review": any(bool(item.get("needs_review")) for item in ordered) or bool(contradictions or errors),
            "dissent": collect("dissent"),
        }
        if contradictions:
            combined["unknowns"] = list(combined.get("unknowns", [])) + ["Independent specialists disagree on the visible signal."]
        return validate_expert_result(combined, allowed_refs=allowed_refs)

    @staticmethod
    def _bounded_iterations(value: Any, default: int = MAX_ITERATIONS) -> int:
        try:
            return max(1, min(MAX_ITERATIONS, int(value)))
        except (TypeError, ValueError):
            return default

    def _reality_check(self, result: dict[str, Any], situation: Mapping[str, Any], allowed_refs: set[str]) -> dict[str, Any]:
        checked = validate_expert_result(result, allowed_refs=allowed_refs)
        if not checked["evidence_for"] and situation.get("status") == "insufficient_evidence":
            checked["needs_review"] = True
        return checked

    @staticmethod
    def _contradictions(specialists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        contradictions = []
        for left_index, left in enumerate(specialists):
            for right in specialists[left_index + 1:]:
                left_hypothesis = str(left.get("hypothesis") or "").strip()
                right_hypothesis = str(right.get("hypothesis") or "").strip()
                if (
                    not left_hypothesis or not right_hypothesis
                    or left_hypothesis.casefold() == right_hypothesis.casefold()
                    or float(left.get("confidence") or 0) < 0.55
                    or float(right.get("confidence") or 0) < 0.55
                ):
                    continue
                reason = IntelligenceOrchestratorSynthesisMixin._material_contradiction_reason(left, right)
                if reason:
                    contradictions.append({
                        "left": left.get("profile"), "right": right.get("profile"),
                        "left_hypothesis": left_hypothesis, "right_hypothesis": right_hypothesis,
                        "left_evidence": left.get("evidence_for", [])[:8], "right_evidence": right.get("evidence_for", [])[:8],
                        "reason": reason,
                    })
        return contradictions[:MAX_ITEMS]

    @staticmethod
    def _material_contradiction_reason(left: Mapping[str, Any], right: Mapping[str, Any]) -> str | None:
        left_for = {_ref_id(item) for item in left.get("evidence_for", []) or []}
        right_for = {_ref_id(item) for item in right.get("evidence_for", []) or []}
        left_against = {_ref_id(item) for item in left.get("evidence_against", []) or []}
        right_against = {_ref_id(item) for item in right.get("evidence_against", []) or []}
        if (left_for & right_against) or (right_for & left_against):
            return "Specialists cite opposing evidence for the same claim."
        left_level = IntelligenceOrchestratorSynthesisMixin._impact_level(left.get("expected_impact"))
        right_level = IntelligenceOrchestratorSynthesisMixin._impact_level(right.get("expected_impact"))
        if left_level is not None and right_level is not None and abs(left_level - right_level) >= 3:
            return "Specialists materially disagree on expected impact severity."
        left_action = IntelligenceOrchestratorSynthesisMixin._recommendation_direction(left.get("recommendation"))
        right_action = IntelligenceOrchestratorSynthesisMixin._recommendation_direction(right.get("recommendation"))
        if left_action and right_action and left_action != right_action:
            return "Specialists recommend incompatible action directions."
        return None

    @staticmethod
    def _impact_level(value: Any) -> int | None:
        if not isinstance(value, Mapping):
            return None
        raw = str(value.get("level") or value.get("severity") or "").strip().lower()
        return {
            "none": 0, "low": 1, "minor": 1, "medium": 2, "moderate": 2,
            "high": 4, "critical": 5, "severe": 5,
        }.get(raw)

    @staticmethod
    def _recommendation_direction(value: Any) -> str | None:
        text = json.dumps(_json(value), sort_keys=True).lower() if isinstance(value, Mapping) else str(value or "").lower()
        if any(word in text for word in ("stop", "pause", "defer", "block", "reject", "do not")):
            return "hold"
        if any(word in text for word in ("start", "execute", "approve", "accelerate", "ship", "expand")):
            return "act"
        return None

    @staticmethod
    def _disagreement_summary(
        specialists: Sequence[Mapping[str, Any]],
        contradictions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return an inspectable, confidence-weighted specialist debate.

        A weighted majority is only a tie-breaker for the read model.  A close
        margin remains explicitly contested so callers cannot mistake a slim
        vote for consensus.  Profile identifiers are retained for human
        review and no specialist is silently discarded.
        """
        entries = [
            {
                "specialist_id": str(item.get("specialist_id") or ""),
                "profile": item.get("profile"),
                "hypothesis": str(item.get("hypothesis") or "").strip(),
                "confidence": round(max(0.0, min(1.0, float(item.get("confidence") or 0.0))), 3),
            }
            for item in specialists
            if str(item.get("hypothesis") or "").strip()
        ]
        if not entries:
            return {
                "status": "insufficient",
                "specialist_count": len(specialists),
                "hypothesis_count": 0,
                "weighted_confidence": 0.0,
                "confidence_margin": 0.0,
                "majority_hypothesis": None,
                "minority": [],
                "resolution": "human_review",
            }
        buckets: dict[str, dict[str, Any]] = {}
        for entry in entries:
            key = entry["hypothesis"].casefold()
            bucket = buckets.setdefault(key, {"hypothesis": entry["hypothesis"], "weight": 0.0, "members": []})
            bucket["weight"] += entry["confidence"]
            bucket["members"].append(entry)
        ranked = sorted(buckets.values(), key=lambda item: (-item["weight"], item["hypothesis"].casefold()))
        total_weight = sum(float(item["weight"]) for item in ranked)
        top = ranked[0]
        second_weight = float(ranked[1]["weight"]) if len(ranked) > 1 else 0.0
        margin = round((float(top["weight"]) - second_weight) / max(total_weight, 1e-9), 3)
        status = "contested" if contradictions else "consensus"
        if len(ranked) > 1 and margin < 0.15:
            status = "contested"
        lens_disagreements = []
        if len(ranked) > 1 and not contradictions:
            lens_disagreements = [
                {
                    "hypothesis": bucket["hypothesis"],
                    "weight": round(float(bucket["weight"]), 3),
                    "specialists": [member["specialist_id"] for member in bucket["members"]],
                    "reason": "different_profile_lens",
                }
                for bucket in ranked
            ][:MAX_ITEMS]
        minority = [
            {"hypothesis": bucket["hypothesis"], "weight": round(float(bucket["weight"]), 3),
             "specialists": [member["specialist_id"] for member in bucket["members"]]}
            for bucket in ranked[1:]
        ]
        return {
            "status": status,
            "specialist_count": len(specialists),
            "hypothesis_count": len(ranked),
            "weighted_confidence": round(float(top["weight"]) / max(len(top["members"]), 1), 3),
            "confidence_margin": margin,
            "majority_hypothesis": top["hypothesis"],
            "majority_specialists": [member["specialist_id"] for member in top["members"]],
            "minority": minority[:MAX_ITEMS],
            "lens_disagreements": lens_disagreements,
            "resolution": "human_review" if status == "contested" else "weighted_consensus",
        }

    @staticmethod
    def _historical_learning(analogues: Any) -> dict[str, Any]:
        """Summarize prior analogue outcomes without inferring absent data."""
        rows = [item for item in (analogues or []) if isinstance(item, Mapping)]
        resolved = 0.0
        weighted = 0.0
        known_outcomes = 0
        for item in rows[:MAX_ITEMS]:
            stats = item.get("outcome_stats") if isinstance(item.get("outcome_stats"), Mapping) else {}
            similarity = _score(item.get("similarity"), _score(item.get("confidence"), 0.0))
            rate = stats.get("resolution_rate")
            if rate is None:
                continue
            try:
                rate = max(0.0, min(1.0, float(rate)))
            except (TypeError, ValueError):
                continue
            weighted += similarity
            resolved += similarity * rate
            known_outcomes += 1
        rate = round(resolved / weighted, 3) if weighted else None
        return {
            "status": "available" if rows else "insufficient_evidence",
            "analogue_count": len(rows),
            "outcome_observation_count": known_outcomes,
            "weighted_resolution_rate": rate,
            "recommendation_signal": (
                "Prior comparable interventions resolved the signal; reuse cautiously."
                if rate is not None and rate >= 0.6 else
                "Prior analogues are mixed or unresolved; require a reversible review."
                if rate is not None else
                "No measured analogue outcome is available; do not generalize."
            ),
        }

    @staticmethod
    def _scenario_analysis(inputs: Any) -> dict[str, Any]:
        values = dict(inputs) if isinstance(inputs, Mapping) else {}
        retained = values.get("retained_inputs") if isinstance(values.get("retained_inputs"), Mapping) else values
        projection = values.get("projection") if isinstance(values.get("projection"), Mapping) else {}
        unknowns = [str(item) for item in (values.get("constraints") or []) if item]
        return {
            "status": "available" if retained or projection else "insufficient_evidence",
            "retained_inputs": _json(retained),
            "projection": _json(projection),
            "constraint_count": len(unknowns),
            "constraints": unknowns[:MAX_ITEMS],
            "sensitivity": "bounded_inputs_only" if retained or projection else "none",
        }

    def _runbook_gates(
        self,
        runbook: Any,
        situation: Mapping[str, Any],
        specialists: Sequence[Mapping[str, Any]],
        contradictions: Sequence[Mapping[str, Any]],
        errors: Sequence[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Evaluate runbook gates as bounded, read-only orchestration stages."""
        if runbook is None:
            return [], False
        activation = list(self._field(runbook, "activation_sequence") or [])
        handoff = list(self._field(runbook, "handoff_gates") or [])
        quality = list(self._field(runbook, "quality_gates") or [])
        scenario_policy = self._field(runbook, "scenario_policy")
        events: list[dict[str, Any]] = []
        review = False
        activation_status = "completed" if activation else "degraded"
        if not activation:
            review = True
        handoff_ok = bool(specialists) and not errors
        review |= not handoff_ok and bool(handoff)
        quality_ok = bool(specialists) and all(item.get("evidence_for") or item.get("unknowns") for item in specialists)
        review |= not quality_ok and bool(quality)
        contradiction_ok = not contradictions
        review |= not contradiction_ok
        scenario_ok = not scenario_policy or isinstance(situation.get("context", {}).get("scenario_inputs"), Mapping)
        review |= not scenario_ok
        events.append({
            "stage": "runbook_gates",
            "status": "review" if review else activation_status,
            "activation": {"status": activation_status, "steps": activation[:8]},
            "handoff": {"status": "completed" if handoff_ok else "degraded", "gates": handoff[:8]},
            "quality": {"status": "completed" if quality_ok else "review", "gates": quality[:8]},
            "contradiction": {"status": "completed" if contradiction_ok else "review", "count": len(contradictions)},
            "scenario": {"status": "completed" if scenario_ok else "review", "policy": scenario_policy},
        })
        return events, review

