"""Human-labelled agency intelligence benchmark, metrics, and release gates.

This module provides pure-function metrics, adapters, schema validation, and
production release gate checks for evaluating agency intelligence runs against
hand-authored gold-standard cases.

All functions are pure transformations over dictionary/mapping payloads and do
not depend on live orchestrator execution or database writes.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


BENCHMARK_CASES_FILE = (
    Path(__file__).resolve().parent.parent / "benchmarks" / "agency_intelligence_cases.json"
)

IMPACT_LEVEL_MAP: dict[str, int] = {
    "none": 0,
    "low": 1,
    "minor": 1,
    "medium": 2,
    "moderate": 2,
    "high": 4,
    "critical": 5,
    "severe": 5,
}


def _extract_ref_id(item: Any) -> str | None:
    """Extract citation identifier from diverse ref/evidence representations."""
    if isinstance(item, str):
        cleaned = item.strip()
        return cleaned if cleaned else None
    if isinstance(item, Mapping):
        ref = item.get("ref") or item.get("object_ref") or item.get("source") or item.get("id")
        if isinstance(ref, Mapping):
            ref = ref.get("id") or ref.get("ref")
        if ref not in (None, ""):
            cleaned = str(ref).strip()
            return cleaned if cleaned else None
    return None


def extract_evidence_refs(payload: Any) -> set[str]:
    """Extract all citation identifiers from an evidence list or result dict."""
    refs: set[str] = set()
    if payload is None:
        return refs

    if isinstance(payload, Mapping):
        for key in ("evidence_for", "evidence_against", "analogues", "historical_analogues", "evidence"):
            val = payload.get(key)
            if isinstance(val, (list, tuple)):
                for item in val:
                    ref_id = _extract_ref_id(item)
                    if ref_id:
                        refs.add(ref_id)
        # Check direct refs field
        if "refs" in payload and isinstance(payload["refs"], (list, tuple)):
            for item in payload["refs"]:
                ref_id = _extract_ref_id(item)
                if ref_id:
                    refs.add(ref_id)
        # Check specialists list if present
        if "specialists" in payload and isinstance(payload["specialists"], (list, tuple)):
            for spec in payload["specialists"]:
                refs.update(extract_evidence_refs(spec))
        # Check claims if present
        if "claims" in payload and isinstance(payload["claims"], (list, tuple)):
            for claim in payload["claims"]:
                if isinstance(claim, Mapping):
                    for ref_key in ("evidence_refs", "refs", "citations"):
                        for r in claim.get(ref_key, ()) or ():
                            rid = _extract_ref_id(r)
                            if rid:
                                refs.add(rid)
        return refs

    if isinstance(payload, (list, tuple, set)):
        for item in payload:
            ref_id = _extract_ref_id(item)
            if ref_id:
                refs.add(ref_id)
            elif isinstance(item, Mapping):
                refs.update(extract_evidence_refs(item))

    return refs


def compute_attention_precision_recall(
    selected_refs: Iterable[str],
    relevant_refs: Iterable[str],
) -> dict[str, float]:
    """Compute precision, recall, and F1 of selected evidence refs against relevant refs."""
    selected = {str(r).strip() for r in selected_refs if str(r).strip()}
    relevant = {str(r).strip() for r in relevant_refs if str(r).strip()}

    if not selected and not relevant:
        return {
            "precision": 1.0,
            "recall": 1.0,
            "f1": 1.0,
            "selected_count": 0,
            "relevant_count": 0,
            "overlap_count": 0,
        }
    if not selected:
        return {
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "selected_count": 0,
            "relevant_count": len(relevant),
            "overlap_count": 0,
        }
    if not relevant:
        return {
            "precision": 0.0,
            "recall": 1.0,
            "f1": 0.0,
            "selected_count": len(selected),
            "relevant_count": 0,
            "overlap_count": 0,
        }

    overlap = selected & relevant
    precision = len(overlap) / len(selected)
    recall = len(overlap) / len(relevant)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0.0 else 0.0

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "selected_count": len(selected),
        "relevant_count": len(relevant),
        "overlap_count": len(overlap),
    }


def compute_unsupported_claim_rate(
    claims: Sequence[Mapping[str, Any]],
    resolvable_refs: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Compute rate of claims lacking resolvable citation references.

    A claim is unsupported if:
    1. It has no citations/evidence_refs declared, OR
    2. None of its declared refs resolve within the allowed/resolvable reference set.
    """
    if not claims:
        return {
            "unsupported_claim_rate": 0.0,
            "citation_rate": 1.0,
            "total_claims": 0,
            "unsupported_claims": 0,
            "supported_claims": 0,
        }

    resolvable_set = {str(r).strip() for r in resolvable_refs} if resolvable_refs is not None else None
    unsupported_count = 0
    supported_count = 0

    for claim in claims:
        if not isinstance(claim, Mapping):
            unsupported_count += 1
            continue

        raw_refs: list[Any] = []
        for ref_key in ("evidence_refs", "citations", "refs", "evidence"):
            val = claim.get(ref_key)
            if isinstance(val, (list, tuple)):
                raw_refs.extend(val)

        extracted_refs = {_extract_ref_id(r) for r in raw_refs}
        valid_refs = {r for r in extracted_refs if r is not None}

        if not valid_refs:
            unsupported_count += 1
        elif resolvable_set is not None:
            if any(r in resolvable_set for r in valid_refs):
                supported_count += 1
            else:
                unsupported_count += 1
        else:
            supported_count += 1

    total = unsupported_count + supported_count
    unsupported_rate = (unsupported_count / total) if total > 0 else 0.0
    citation_rate = (supported_count / total) if total > 0 else 1.0

    return {
        "unsupported_claim_rate": round(unsupported_rate, 4),
        "citation_rate": round(citation_rate, 4),
        "total_claims": total,
        "unsupported_claims": unsupported_count,
        "supported_claims": supported_count,
    }


def compute_calibration(
    predictions: Sequence[tuple[float, int | bool] | Mapping[str, Any]],
    num_bins: int = 5,
) -> dict[str, Any]:
    """Compute Brier score, bucketed calibration, and expected calibration error (ECE)."""
    parsed: list[tuple[float, int]] = []
    for item in predictions:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            conf = max(0.0, min(1.0, float(item[0])))
            correct = 1 if bool(item[1]) else 0
            parsed.append((conf, correct))
        elif isinstance(item, Mapping):
            conf = max(0.0, min(1.0, float(item.get("confidence", item.get("predicted_confidence", 0.0)))))
            correct = 1 if bool(item.get("correct", item.get("is_correct", False))) else 0
            parsed.append((conf, correct))

    if not parsed:
        return {
            "brier_score": 0.0,
            "ece": 0.0,
            "bins": [],
            "total_predictions": 0,
        }

    n = len(parsed)
    brier_score = sum((conf - y) ** 2 for conf, y in parsed) / n

    bins: list[dict[str, Any]] = []
    ece = 0.0

    for i in range(num_bins):
        in_bin = [p for p in parsed if min(num_bins - 1, int(round(p[0], 6) * num_bins)) == i]

        count = len(in_bin)
        if count > 0:
            avg_conf = sum(p[0] for p in in_bin) / count
            accuracy = sum(p[1] for p in in_bin) / count
            cal_err = abs(avg_conf - accuracy)
            ece += (count / n) * cal_err
        else:
            avg_conf = 0.0
            accuracy = 0.0
            cal_err = 0.0

        lower = i / num_bins
        upper = (i + 1) / num_bins

        bins.append({
            "bin_index": i,
            "range": [round(lower, 2), round(upper, 2)],
            "count": count,
            "avg_confidence": round(avg_conf, 4),
            "empirical_accuracy": round(accuracy, 4),
            "calibration_error": round(cal_err, 4),
        })

    return {
        "brier_score": round(brier_score, 4),
        "ece": round(ece, 4),
        "bins": bins,
        "total_predictions": n,
    }


def normalize_recommendation_direction(rec: Any) -> str | None:
    """Classify recommendation direction into canonical agency action directions."""
    if rec is None:
        return None
    if isinstance(rec, Mapping):
        direct = rec.get("direction")
        if direct and str(direct).strip():
            return str(direct).strip().lower()
        text = json.dumps(rec, sort_keys=True).lower()
    else:
        text = str(rec).lower()

    if any(w in text for w in ("stop", "pause", "defer", "block", "reject", "do not", "hold")):
        return "hold"
    if any(w in text for w in ("start", "execute", "approve", "accelerate", "ship", "expand", "act", "scale", "revert")):
        return "act"
    if any(w in text for w in ("audit", "review", "investigate", "reconcile", "analyze", "examine")):
        return "audit"
    return None


def compute_top3_usefulness(
    recommendations: Sequence[Mapping[str, Any] | str],
    case: Mapping[str, Any],
) -> dict[str, Any]:
    """Score usefulness of up to top-3 ranked candidate recommendations against case ground truth."""
    if not recommendations:
        return {
            "top3_usefulness": 0.0,
            "scores": [],
            "recommendations_evaluated": 0,
        }

    expected_rec = case.get("expected_recommendation") or {}
    acceptable = expected_rec.get("acceptable_directions") or {}
    default_dir = str(expected_rec.get("direction") or "").lower()
    default_usefulness = float(expected_rec.get("usefulness", 1.0))

    scores: list[float] = []
    top_recs = list(recommendations)[:3]

    for rec in top_recs:
        direction = normalize_recommendation_direction(rec)
        if direction is not None:
            if direction in acceptable:
                scores.append(float(acceptable[direction]))
            elif direction == default_dir:
                scores.append(default_usefulness)
            else:
                scores.append(0.0)
        else:
            scores.append(0.0)

    mean_usefulness = (sum(scores) / len(scores)) if scores else 0.0
    return {
        "top3_usefulness": round(mean_usefulness, 4),
        "scores": [round(s, 4) for s in scores],
        "recommendations_evaluated": len(scores),
    }


def compute_evidence_leakage(
    selected_refs: Iterable[str],
    allowed_refs: Iterable[str],
) -> dict[str, Any]:
    """Detect citations referencing evidence outside the allowed ACL-scoped set."""
    selected = {str(r).strip() for r in selected_refs if str(r).strip()}
    allowed = {str(r).strip() for r in allowed_refs if str(r).strip()}
    leaked = selected - allowed
    return {
        "leakage_count": len(leaked),
        "leaked_refs": sorted(leaked),
        "is_clean": len(leaked) == 0,
    }


def parse_impact_level(impact_val: Any) -> int:
    """Parse expected impact severity level (0 to 5 scale)."""
    if impact_val is None:
        return 0
    if isinstance(impact_val, int):
        return max(0, min(5, impact_val))
    if isinstance(impact_val, Mapping):
        raw = str(impact_val.get("level") or impact_val.get("severity") or impact_val.get("tier") or "").strip().lower()
    else:
        raw = str(impact_val).strip().lower()
    return IMPACT_LEVEL_MAP.get(raw, 0)


def compute_false_critical_rate(
    evaluations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Calculate the proportion of critical recommendations that are contradicted or ungrounded.

    false-critical rate <= 5% (critical recommendations contradicted by labels).
    """
    critical_count = 0
    false_critical_count = 0

    for ev in evaluations:
        is_candidate_critical = bool(ev.get("candidate_is_critical", False))
        if is_candidate_critical:
            critical_count += 1
            if bool(ev.get("is_contradicted", False)):
                false_critical_count += 1

    rate = (false_critical_count / critical_count) if critical_count > 0 else 0.0
    return {
        "false_critical_rate": round(rate, 4),
        "critical_count": critical_count,
        "false_critical_count": false_critical_count,
    }


def adapt_run_result(
    run_result: Mapping[str, Any],
    case: Mapping[str, Any],
) -> dict[str, Any]:
    """Pure adapter translating an orchestrator run-result (or stub) into standardized metric inputs."""
    selected_refs = extract_evidence_refs(run_result)
    allowed_refs = set(case.get("allowed_evidence_refs") or ())
    relevant_refs = set(case.get("relevant_evidence_refs") or ())

    # Extract claims
    claims: list[dict[str, Any]] = []
    if "claims" in run_result and isinstance(run_result["claims"], (list, tuple)):
        for c in run_result["claims"]:
            if isinstance(c, Mapping):
                claims.append(dict(c))
    else:
        # Synthesize claims from result findings if no explicit claims list provided
        finding_text = str(run_result.get("finding") or "").strip()
        if finding_text and finding_text != "No finding returned.":
            claims.append({
                "statement": finding_text,
                "evidence_refs": list(selected_refs),
            })
        hypo = str(run_result.get("hypothesis") or "").strip()
        if hypo and hypo != "No causal hypothesis established.":
            claims.append({
                "statement": hypo,
                "evidence_refs": list(selected_refs),
            })

    # Extract recommendations
    recommendations: list[Any] = []
    primary_rec = run_result.get("recommendation")
    if primary_rec:
        if isinstance(primary_rec, Mapping) and "alternatives" in primary_rec:
            recommendations.append(primary_rec)
            for alt in primary_rec.get("alternatives", []):
                recommendations.append(alt)
        else:
            recommendations.append(primary_rec)
    for opt in run_result.get("options", ()) or ():
        recommendations.append(opt)

    # Predicted confidence
    try:
        predicted_confidence = float(run_result.get("confidence", 0.0))
    except (TypeError, ValueError):
        predicted_confidence = 0.0

    # Determine recommendation direction and correctness against expected direction
    expected_rec = case.get("expected_recommendation") or {}
    expected_dir = str(expected_rec.get("direction") or "").lower()
    acceptable_dirs = set((expected_rec.get("acceptable_directions") or {}).keys())
    if not acceptable_dirs and expected_dir:
        acceptable_dirs.add(expected_dir)

    top_dir = normalize_recommendation_direction(recommendations[0]) if recommendations else None
    is_correct = bool(top_dir and (top_dir in acceptable_dirs or top_dir == expected_dir))

    # Impact assessment
    expected_tier = str(case.get("expected_impact_tier") or "").lower()
    expected_level = parse_impact_level(expected_tier)

    cand_impact = run_result.get("expected_impact")
    cand_level = parse_impact_level(cand_impact)
    candidate_is_critical = (cand_level >= 5)

    # A critical recommendation is contradicted if:
    # 1. Candidate recommended critical action but expected impact is low/none (level difference >= 3)
    # 2. Candidate direction opposed the ground truth direction (e.g. act vs hold) when labeled critical
    is_contradicted = False
    if candidate_is_critical:
        if expected_level <= 2:  # labeled low or none
            is_contradicted = True
        elif top_dir and expected_dir and top_dir != expected_dir:
            # opposing directions
            is_contradicted = True

    return {
        "case_id": case.get("case_id"),
        "selected_refs": sorted(selected_refs),
        "allowed_refs": sorted(allowed_refs),
        "relevant_refs": sorted(relevant_refs),
        "claims": claims,
        "recommendations": recommendations,
        "predicted_confidence": predicted_confidence,
        "is_correct": is_correct,
        "candidate_is_critical": candidate_is_critical,
        "candidate_level": cand_level,
        "expected_level": expected_level,
        "is_contradicted": is_contradicted,
    }


def evaluate_case(
    run_result: Mapping[str, Any],
    case: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate a single run-result against its corresponding ground-truth case."""
    adapted = adapt_run_result(run_result, case)

    attention = compute_attention_precision_recall(
        adapted["selected_refs"],
        adapted["relevant_refs"],
    )
    leakage = compute_evidence_leakage(
        adapted["selected_refs"],
        adapted["allowed_refs"],
    )
    claims_metric = compute_unsupported_claim_rate(
        adapted["claims"],
        resolvable_refs=adapted["allowed_refs"],
    )
    usefulness = compute_top3_usefulness(
        adapted["recommendations"],
        case,
    )

    return {
        "case_id": adapted["case_id"],
        "attention": attention,
        "leakage": leakage,
        "claims": claims_metric,
        "usefulness": usefulness,
        "predicted_confidence": adapted["predicted_confidence"],
        "is_correct": adapted["is_correct"],
        "candidate_is_critical": adapted["candidate_is_critical"],
        "is_contradicted": adapted["is_contradicted"],
    }


def compute_benchmark_metrics(
    evaluations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate per-case evaluation outputs into candidate metrics for release gating."""
    if not evaluations:
        return {
            "attention_precision": 0.0,
            "attention_recall": 0.0,
            "attention_f1": 0.0,
            "citation_rate": 1.0,
            "unsupported_claim_rate": 0.0,
            "evidence_leakage": 0,
            "top3_usefulness": 0.0,
            "brier_score": 0.0,
            "ece": 0.0,
            "calibration": {"brier_score": 0.0, "ece": 0.0, "bins": []},
            "false_critical_rate": 0.0,
            "case_count": 0,
        }

    n = len(evaluations)
    avg_precision = sum(e["attention"]["precision"] for e in evaluations) / n
    avg_recall = sum(e["attention"]["recall"] for e in evaluations) / n
    avg_f1 = sum(e["attention"]["f1"] for e in evaluations) / n

    total_claims = sum(e["claims"]["total_claims"] for e in evaluations)
    total_unsupported = sum(e["claims"]["unsupported_claims"] for e in evaluations)
    total_supported = sum(e["claims"]["supported_claims"] for e in evaluations)

    unsupported_claim_rate = (total_unsupported / total_claims) if total_claims > 0 else 0.0
    citation_rate = (total_supported / total_claims) if total_claims > 0 else 1.0

    total_leakage = sum(e["leakage"]["leakage_count"] for e in evaluations)
    avg_usefulness = sum(e["usefulness"]["top3_usefulness"] for e in evaluations) / n

    # Calibration over all cases
    pairs = [(e["predicted_confidence"], e["is_correct"]) for e in evaluations]
    calibration = compute_calibration(pairs)

    # False critical rate
    fc_result = compute_false_critical_rate(evaluations)

    return {
        "attention_precision": round(avg_precision, 4),
        "attention_recall": round(avg_recall, 4),
        "attention_f1": round(avg_f1, 4),
        "citation_rate": round(citation_rate, 4),
        "unsupported_claim_rate": round(unsupported_claim_rate, 4),
        "evidence_leakage": total_leakage,
        "top3_usefulness": round(avg_usefulness, 4),
        "brier_score": calibration["brier_score"],
        "ece": calibration["ece"],
        "calibration": calibration,
        "false_critical_rate": fc_result["false_critical_rate"],
        "critical_count": fc_result["critical_count"],
        "false_critical_count": fc_result["false_critical_count"],
        "case_count": n,
    }


def gate(candidate_metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate candidate metrics against production release gates.

    Release gate policy:
    1. citation rate == 100% (1.0)
    2. evidence leakage == 0 (no refs outside allowed set)
    3. top-3 usefulness >= 0.8
    4. false-critical rate <= 5% (0.05)
    """
    citation_rate = float(candidate_metrics.get("citation_rate", 0.0))
    leakage = int(candidate_metrics.get("evidence_leakage", 0))
    usefulness = float(candidate_metrics.get("top3_usefulness", 0.0))
    false_critical = float(candidate_metrics.get("false_critical_rate", 0.0))

    failures: list[str] = []

    # Gate 1: citation rate 100%
    gate_citation_passed = citation_rate >= 1.0 - 1e-6
    if not gate_citation_passed:
        failures.append(f"citation rate must be 100% (got {citation_rate:.1%})")

    # Gate 2: evidence leakage 0
    gate_leakage_passed = leakage <= 0
    if not gate_leakage_passed:
        failures.append(f"evidence leakage must be 0 (got {leakage})")

    # Gate 3: top-3 usefulness >= 0.8
    gate_usefulness_passed = usefulness >= 0.80 - 1e-6
    if not gate_usefulness_passed:
        failures.append(f"top-3 usefulness must be >= 0.80 (got {usefulness:.2f})")

    # Gate 4: false-critical rate <= 5%
    gate_false_critical_passed = false_critical <= 0.05 + 1e-6
    if not gate_false_critical_passed:
        failures.append(f"false-critical rate must be <= 5% (got {false_critical:.1%})")

    passed = len(failures) == 0
    return {
        "passed": passed,
        "failures": failures,
        "gates": {
            "citation_rate": {
                "passed": gate_citation_passed,
                "value": citation_rate,
                "target": "100%",
            },
            "evidence_leakage": {
                "passed": gate_leakage_passed,
                "value": leakage,
                "target": "0",
            },
            "top3_usefulness": {
                "passed": gate_usefulness_passed,
                "value": usefulness,
                "target": ">= 0.80",
            },
            "false_critical_rate": {
                "passed": gate_false_critical_passed,
                "value": false_critical,
                "target": "<= 5%",
            },
        },
    }


def load_benchmark_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load and validate the ground-truth benchmark cases from disk."""
    filepath = Path(path) if path else BENCHMARK_CASES_FILE
    if not filepath.exists():
        raise FileNotFoundError(f"Benchmark cases file not found: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        cases = json.load(f)

    if not isinstance(cases, list):
        raise ValueError("Benchmark cases file must contain a JSON array of case objects.")

    for i, c in enumerate(cases):
        if not isinstance(c, dict):
            raise ValueError(f"Case index {i} is not a valid JSON object.")
        for field in (
            "case_id",
            "situation",
            "allowed_evidence_refs",
            "relevant_evidence_refs",
            "labelled_claims",
            "expected_recommendation",
            "expected_impact_tier",
        ):
            if field not in c:
                raise ValueError(f"Case {c.get('case_id', i)} missing required field: {field}")

    return cases


def run_benchmark(
    run_results_by_case_id: Mapping[str, Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute full benchmark evaluation across provided run results and compute release gates."""
    benchmark_cases = list(cases) if cases is not None else load_benchmark_cases()
    evaluations: list[dict[str, Any]] = []

    for case in benchmark_cases:
        cid = case["case_id"]
        run_res = run_results_by_case_id.get(cid)
        if run_res is None:
            # If no run result provided for this case, treat as empty degraded run
            run_res = {
                "finding": "No finding returned.",
                "evidence_for": [],
                "recommendation": {"direction": "hold", "summary": "No recommendation"},
                "confidence": 0.0,
            }
        ev = evaluate_case(run_res, case)
        evaluations.append(ev)

    metrics = compute_benchmark_metrics(evaluations)
    gate_decision = gate(metrics)

    return {
        "passed": gate_decision["passed"],
        "metrics": metrics,
        "gate": gate_decision,
        "evaluations": evaluations,
    }
