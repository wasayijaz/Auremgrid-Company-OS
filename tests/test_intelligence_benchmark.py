"""Tests for human-labelled agency intelligence benchmark, metrics, and release gates."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from auremgrid.services.intelligence_benchmark import (
    adapt_run_result,
    compute_attention_precision_recall,
    compute_benchmark_metrics,
    compute_calibration,
    compute_evidence_leakage,
    compute_false_critical_rate,
    compute_top3_usefulness,
    compute_unsupported_claim_rate,
    evaluate_case,
    extract_evidence_refs,
    gate,
    load_benchmark_cases,
    normalize_recommendation_direction,
    run_benchmark,
)


class BenchmarkCaseSchemaValidationTests(unittest.TestCase):
    """Schema and completeness validation for hand-authored benchmark cases."""

    def test_all_24_cases_load_and_parse(self):
        cases = load_benchmark_cases()
        self.assertEqual(len(cases), 24, f"Expected exactly 24 cases, got {len(cases)}")

    def test_case_schema_and_domain_distribution(self):
        cases = load_benchmark_cases()
        case_ids = set()
        domains = set()

        for case in cases:
            cid = case.get("case_id")
            self.assertIsInstance(cid, str)
            self.assertTrue(cid.startswith("case-"), f"Invalid case_id format: {cid}")
            self.assertNotIn(cid, case_ids, f"Duplicate case_id: {cid}")
            case_ids.add(cid)

            # Situation text
            sit = case.get("situation")
            self.assertIsInstance(sit, str)
            self.assertGreater(len(sit.strip()), 20, f"Situation text too short in {cid}")

            # Evidence sets
            allowed = case.get("allowed_evidence_refs")
            relevant = case.get("relevant_evidence_refs")
            self.assertIsInstance(allowed, list)
            self.assertIsInstance(relevant, list)
            self.assertGreater(len(allowed), 0, f"Allowed refs empty in {cid}")
            self.assertGreater(len(relevant), 0, f"Relevant refs empty in {cid}")

            # Relevant must be a strict subset of allowed
            allowed_set = set(allowed)
            self.assertEqual(len(allowed), len(allowed_set), f"Duplicate allowed refs in {cid}")
            for r in relevant:
                self.assertIn(r, allowed_set, f"Relevant ref {r} not in allowed set in {cid}")

            # Labelled claims
            claims = case.get("labelled_claims")
            self.assertIsInstance(claims, list)
            self.assertGreaterEqual(len(claims), 2, f"Expected at least 2 claims in {cid}")
            for cl in claims:
                self.assertIn("statement", cl)
                self.assertIn("supported", cl)
                self.assertIsInstance(cl["supported"], bool)
                self.assertIn("evidence_refs", cl)
                if cl["supported"]:
                    self.assertGreater(len(cl["evidence_refs"]), 0, f"Supported claim missing refs in {cid}")
                    for ref in cl["evidence_refs"]:
                        self.assertIn(ref, allowed_set, f"Claim ref {ref} not in allowed set in {cid}")
                else:
                    self.assertEqual(len(cl["evidence_refs"]), 0, f"Unsupported claim has refs in {cid}")

            # Expected recommendation
            rec = case.get("expected_recommendation")
            self.assertIsInstance(rec, dict)
            self.assertIn("direction", rec)
            self.assertIn(rec["direction"], ("act", "hold", "audit"))
            self.assertIn("usefulness", rec)
            self.assertGreaterEqual(rec["usefulness"], 0.8)
            self.assertLessEqual(rec["usefulness"], 1.0)
            self.assertIn("acceptable_directions", rec)

            # Expected impact tier
            tier = case.get("expected_impact_tier")
            self.assertIn(tier, ("none", "low", "medium", "high", "critical"))

            domain = case.get("domain")
            self.assertIsNotNone(domain)
            domains.add(domain)

        # Ensure realistic agency domains are covered
        expected_domains = {"performance_review", "budget_decision", "churn_risk", "campaign_pacing"}
        self.assertTrue(expected_domains.issubset(domains), f"Missing domains: {expected_domains - domains}")


class MetricMathHandComputedFixtureTests(unittest.TestCase):
    """Exact hand-computed fixtures for attention, unsupported-claims, calibration, and usefulness."""

    def test_attention_precision_recall_exact_fixture(self):
        # Fixture: 4 selected refs, 3 relevant refs, 2 overlapping
        selected = ["ev1", "ev2", "ev3", "ev4"]
        relevant = ["ev2", "ev4", "ev5"]

        # Expected:
        # overlap = {"ev2", "ev4"} (len 2)
        # precision = 2 / 4 = 0.5000
        # recall = 2 / 3 = 0.6667
        # f1 = 2 * (0.5 * 2/3) / (0.5 + 2/3) = (2/3) / (7/6) = 4/7 = 0.5714
        result = compute_attention_precision_recall(selected, relevant)
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual(result["recall"], 0.6667)
        self.assertEqual(result["f1"], 0.5714)
        self.assertEqual(result["selected_count"], 4)
        self.assertEqual(result["relevant_count"], 3)
        self.assertEqual(result["overlap_count"], 2)

    def test_attention_precision_recall_boundary_conditions(self):
        # Empty selected and relevant
        both_empty = compute_attention_precision_recall([], [])
        self.assertEqual(both_empty["precision"], 1.0)
        self.assertEqual(both_empty["recall"], 1.0)
        self.assertEqual(both_empty["f1"], 1.0)

        # None selected, but relevant exists
        none_selected = compute_attention_precision_recall([], ["ev1", "ev2"])
        self.assertEqual(none_selected["precision"], 0.0)
        self.assertEqual(none_selected["recall"], 0.0)
        self.assertEqual(none_selected["f1"], 0.0)

        # Perfect match
        perfect = compute_attention_precision_recall(["ev1", "ev2"], ["ev1", "ev2"])
        self.assertEqual(perfect["precision"], 1.0)
        self.assertEqual(perfect["recall"], 1.0)
        self.assertEqual(perfect["f1"], 1.0)

    def test_unsupported_claim_rate_exact_fixture(self):
        # Fixture: 4 claims with allowed refs {"ev1", "ev2"}
        # claim 1: refs=["ev1"] -> supported (resolves in allowed)
        # claim 2: refs=["ev2"] -> supported (resolves in allowed)
        # claim 3: refs=[] -> unsupported (no refs)
        # claim 4: refs=["ev999"] -> unsupported (not in allowed set)
        allowed = ["ev1", "ev2"]
        claims = [
            {"statement": "ROAS dropped", "evidence_refs": ["ev1"]},
            {"statement": "CTR is down", "evidence_refs": ["ev2"]},
            {"statement": "Shopify crashed", "evidence_refs": []},
            {"statement": "Alien invasion", "evidence_refs": ["ev999"]},
        ]

        # Expected:
        # total = 4, supported = 2, unsupported = 2
        # unsupported_claim_rate = 2/4 = 0.5000
        # citation_rate = 2/4 = 0.5000
        result = compute_unsupported_claim_rate(claims, resolvable_refs=allowed)
        self.assertEqual(result["total_claims"], 4)
        self.assertEqual(result["supported_claims"], 2)
        self.assertEqual(result["unsupported_claims"], 2)
        self.assertEqual(result["unsupported_claim_rate"], 0.5)
        self.assertEqual(result["citation_rate"], 0.5)

    def test_unsupported_claim_rate_perfect_and_empty(self):
        # All supported
        claims = [{"statement": "Stat A", "refs": ["ev1"]}]
        res = compute_unsupported_claim_rate(claims, resolvable_refs=["ev1"])
        self.assertEqual(res["citation_rate"], 1.0)
        self.assertEqual(res["unsupported_claim_rate"], 0.0)

        # Empty claims
        empty = compute_unsupported_claim_rate([])
        self.assertEqual(empty["citation_rate"], 1.0)
        self.assertEqual(empty["unsupported_claim_rate"], 0.0)

    def test_calibration_and_brier_score_exact_fixture(self):
        # Hand-computed fixture: 4 predictions
        # (0.9, 1) -> (0.9 - 1)^2 = 0.01
        # (0.8, 1) -> (0.8 - 1)^2 = 0.04
        # (0.6, 0) -> (0.6 - 0)^2 = 0.36
        # (0.3, 0) -> (0.3 - 0)^2 = 0.09
        # Sum = 0.01 + 0.04 + 0.36 + 0.09 = 0.50
        # N = 4 -> Brier score = 0.50 / 4 = 0.125
        predictions = [
            (0.9, 1),
            (0.8, 1),
            (0.6, 0),
            (0.3, 0),
        ]
        cal = compute_calibration(predictions, num_bins=5)
        self.assertEqual(cal["brier_score"], 0.125)
        self.assertEqual(cal["total_predictions"], 4)

        # Binning breakdown for 5 bins:
        # [0.0, 0.2): count = 0
        # [0.2, 0.4): p=0.3, y=0 -> count=1, avg_conf=0.3, acc=0.0, err=0.3
        # [0.4, 0.6): count = 0
        # [0.6, 0.8): p=0.6, y=0 -> count=1, avg_conf=0.6, acc=0.0, err=0.6
        # [0.8, 1.0]: p=0.8,0.9, y=1,1 -> count=2, avg_conf=0.85, acc=1.0, err=0.15
        # Expected ECE = (1/4)*0.3 + (1/4)*0.6 + (2/4)*0.15 = 0.075 + 0.15 + 0.075 = 0.30
        self.assertEqual(cal["ece"], 0.3)
        self.assertEqual(len(cal["bins"]), 5)
        self.assertEqual(cal["bins"][1]["count"], 1)
        self.assertEqual(cal["bins"][3]["count"], 1)
        self.assertEqual(cal["bins"][4]["count"], 2)

    def test_top3_usefulness_exact_fixture(self):
        # Case ground truth: direction "act" (0.95), acceptable: act:0.95, audit:0.80, hold:0.20
        case = {
            "expected_recommendation": {
                "direction": "act",
                "usefulness": 0.95,
                "acceptable_directions": {
                    "act": 0.95,
                    "audit": 0.80,
                    "hold": 0.20,
                },
            }
        }

        # Recommendations: [act, audit, hold] -> scores: [0.95, 0.80, 0.20]
        # Mean usefulness = (0.95 + 0.80 + 0.20) / 3 = 1.95 / 3 = 0.6500
        recs = [
            {"direction": "act", "summary": "scale campaigns"},
            {"direction": "audit", "summary": "check logs"},
            {"direction": "hold", "summary": "pause non-brand"},
        ]
        result = compute_top3_usefulness(recs, case)
        self.assertEqual(result["top3_usefulness"], 0.65)
        self.assertEqual(result["scores"], [0.95, 0.80, 0.20])
        self.assertEqual(result["recommendations_evaluated"], 3)

    def test_evidence_leakage_detection(self):
        allowed = ["ev-01", "ev-02", "ev-03"]
        clean_selected = ["ev-01", "ev-02"]
        leaked_selected = ["ev-01", "ev-02", "ev-secret-external"]

        clean = compute_evidence_leakage(clean_selected, allowed)
        self.assertTrue(clean["is_clean"])
        self.assertEqual(clean["leakage_count"], 0)

        leaked = compute_evidence_leakage(leaked_selected, allowed)
        self.assertFalse(leaked["is_clean"])
        self.assertEqual(leaked["leakage_count"], 1)
        self.assertEqual(leaked["leaked_refs"], ["ev-secret-external"])

    def test_false_critical_rate_exact_fixture(self):
        # 4 critical evaluations: 2 true critical, 2 false critical
        evaluations = [
            {"candidate_is_critical": True, "is_contradicted": False},
            {"candidate_is_critical": True, "is_contradicted": False},
            {"candidate_is_critical": True, "is_contradicted": True},
            {"candidate_is_critical": True, "is_contradicted": True},
            {"candidate_is_critical": False, "is_contradicted": True},  # not critical, ignored in denom
        ]
        res = compute_false_critical_rate(evaluations)
        self.assertEqual(res["critical_count"], 4)
        self.assertEqual(res["false_critical_count"], 2)
        self.assertEqual(res["false_critical_rate"], 0.5)


class ReleaseGateTests(unittest.TestCase):
    """Rigorous pass/fail testing for release gates, including individual gate failures."""

    def test_release_gate_all_pass(self):
        candidate_metrics = {
            "citation_rate": 1.0,
            "evidence_leakage": 0,
            "top3_usefulness": 0.92,
            "false_critical_rate": 0.02,
        }
        decision = gate(candidate_metrics)
        self.assertTrue(decision["passed"])
        self.assertEqual(len(decision["failures"]), 0)
        self.assertTrue(decision["gates"]["citation_rate"]["passed"])
        self.assertTrue(decision["gates"]["evidence_leakage"]["passed"])
        self.assertTrue(decision["gates"]["top3_usefulness"]["passed"])
        self.assertTrue(decision["gates"]["false_critical_rate"]["passed"])

    def test_gate_citation_rate_fails_when_below_100_pct(self):
        candidate_metrics = {
            "citation_rate": 0.96,  # fails 100% gate
            "evidence_leakage": 0,
            "top3_usefulness": 0.92,
            "false_critical_rate": 0.0,
        }
        decision = gate(candidate_metrics)
        self.assertFalse(decision["passed"])
        self.assertEqual(len(decision["failures"]), 1)
        self.assertIn("citation rate must be 100%", decision["failures"][0])
        self.assertFalse(decision["gates"]["citation_rate"]["passed"])
        self.assertTrue(decision["gates"]["evidence_leakage"]["passed"])

    def test_gate_evidence_leakage_fails_when_nonzero(self):
        candidate_metrics = {
            "citation_rate": 1.0,
            "evidence_leakage": 2,  # fails 0 leakage gate
            "top3_usefulness": 0.90,
            "false_critical_rate": 0.0,
        }
        decision = gate(candidate_metrics)
        self.assertFalse(decision["passed"])
        self.assertEqual(len(decision["failures"]), 1)
        self.assertIn("evidence leakage must be 0", decision["failures"][0])
        self.assertFalse(decision["gates"]["evidence_leakage"]["passed"])

    def test_gate_top3_usefulness_fails_when_below_threshold(self):
        candidate_metrics = {
            "citation_rate": 1.0,
            "evidence_leakage": 0,
            "top3_usefulness": 0.76,  # fails >= 0.80 gate
            "false_critical_rate": 0.0,
        }
        decision = gate(candidate_metrics)
        self.assertFalse(decision["passed"])
        self.assertEqual(len(decision["failures"]), 1)
        self.assertIn("top-3 usefulness must be >= 0.80", decision["failures"][0])
        self.assertFalse(decision["gates"]["top3_usefulness"]["passed"])

    def test_gate_false_critical_rate_fails_when_above_5_pct(self):
        candidate_metrics = {
            "citation_rate": 1.0,
            "evidence_leakage": 0,
            "top3_usefulness": 0.85,
            "false_critical_rate": 0.08,  # fails <= 5% gate
        }
        decision = gate(candidate_metrics)
        self.assertFalse(decision["passed"])
        self.assertEqual(len(decision["failures"]), 1)
        self.assertIn("false-critical rate must be <= 5%", decision["failures"][0])
        self.assertFalse(decision["gates"]["false_critical_rate"]["passed"])

    def test_gate_multiple_simultaneous_failures(self):
        candidate_metrics = {
            "citation_rate": 0.80,
            "evidence_leakage": 3,
            "top3_usefulness": 0.50,
            "false_critical_rate": 0.25,
        }
        decision = gate(candidate_metrics)
        self.assertFalse(decision["passed"])
        self.assertEqual(len(decision["failures"]), 4)


class AdapterAndEndToEndBenchmarkTests(unittest.TestCase):
    """Adapter tests converting orchestrator payloads and full benchmark execution."""

    def test_adapt_run_result_from_stub_payload(self):
        case = {
            "case_id": "case-001",
            "allowed_evidence_refs": ["ev-1", "ev-2", "ev-3"],
            "relevant_evidence_refs": ["ev-1", "ev-2"],
            "expected_recommendation": {
                "direction": "act",
                "usefulness": 0.95,
                "acceptable_directions": {"act": 0.95, "audit": 0.80},
            },
            "expected_impact_tier": "high",
        }

        run_result = {
            "finding": "Prospecting CPA increased drastically.",
            "evidence_for": [{"ref": "ev-1", "summary": "Meta analytics"}, {"source": "ev-2"}],
            "evidence_against": [],
            "confidence": 0.88,
            "recommendation": {
                "direction": "act",
                "summary": "Pause bleeding ads and reallocate budget.",
            },
            "options": [
                {"direction": "audit", "summary": "Audit audience reach."},
            ],
            "expected_impact": {"level": "high"},
            "claims": [
                {"statement": "Prospecting CPA doubled.", "evidence_refs": ["ev-1"]},
                {"statement": "Audience reach expanded.", "evidence_refs": ["ev-2"]},
            ],
        }

        adapted = adapt_run_result(run_result, case)
        self.assertEqual(adapted["case_id"], "case-001")
        self.assertEqual(adapted["selected_refs"], ["ev-1", "ev-2"])
        self.assertEqual(adapted["allowed_refs"], ["ev-1", "ev-2", "ev-3"])
        self.assertEqual(adapted["relevant_refs"], ["ev-1", "ev-2"])
        self.assertEqual(adapted["predicted_confidence"], 0.88)
        self.assertTrue(adapted["is_correct"])
        self.assertFalse(adapted["candidate_is_critical"])
        self.assertFalse(adapted["is_contradicted"])

        ev = evaluate_case(run_result, case)
        self.assertEqual(ev["attention"]["precision"], 1.0)
        self.assertEqual(ev["attention"]["recall"], 1.0)
        self.assertEqual(ev["claims"]["citation_rate"], 1.0)
        self.assertEqual(ev["leakage"]["leakage_count"], 0)
        self.assertGreaterEqual(ev["usefulness"]["top3_usefulness"], 0.8)

    def test_run_benchmark_end_to_end_all_pass(self):
        cases = load_benchmark_cases()
        run_results_by_case = {}

        # Synthesize matching gold-standard runs for all 24 benchmark cases
        for c in cases:
            cid = c["case_id"]
            relevant = c["relevant_evidence_refs"]
            allowed = c["allowed_evidence_refs"]
            expected_rec = c["expected_recommendation"]

            run_results_by_case[cid] = {
                "finding": f"Synthesized findings for {cid}",
                "evidence_for": [{"ref": r} for r in relevant],
                "evidence_against": [],
                "confidence": 0.85,
                "recommendation": {
                    "direction": expected_rec["direction"],
                    "summary": expected_rec.get("summary", "Execute recommended strategy"),
                },
                "options": [],
                "expected_impact": {"level": c.get("expected_impact_tier", "medium")},
                "claims": [
                    {
                        "statement": cl["statement"],
                        "evidence_refs": list(relevant),
                    }
                    for cl in c.get("labelled_claims", [])
                ],
            }

        benchmark_res = run_benchmark(run_results_by_case, cases=cases)
        self.assertTrue(benchmark_res["passed"])
        metrics = benchmark_res["metrics"]
        self.assertEqual(metrics["case_count"], 24)
        self.assertEqual(metrics["citation_rate"], 1.0)
        self.assertEqual(metrics["evidence_leakage"], 0)
        self.assertGreaterEqual(metrics["top3_usefulness"], 0.8)
        self.assertLessEqual(metrics["false_critical_rate"], 0.05)


if __name__ == "__main__":
    unittest.main()

