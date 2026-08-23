from __future__ import annotations

import unittest
import time
from pathlib import Path

from auremgrid.services.brain import CompanyOS
from auremgrid.services.intelligence_orchestrator import IntelligenceOrchestrator


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class _Contracts:
    def __init__(self, count: int = 2):
        self.profiles = [{"id": f"expert-{i}", "version": "1", "name": f"Expert {i}"} for i in range(count)]
        self.runbooks = [{"id": "ops", "version": "1", "name": "Ops", "profile_ids": [p["id"] for p in self.profiles], "domains": ["work"], "activation_sequence": ["work"]}]

    def list_profiles(self, **_kwargs):
        return self.profiles

    def list_runbooks(self, **_kwargs):
        return self.runbooks


def _result(hypothesis: str = "same"):
    return {
        "finding": "visible finding", "evidence_for": [], "evidence_against": [],
        "assumptions": [], "unknowns": [], "hypothesis": hypothesis,
        "confidence": .8, "analogues": [], "risks": [], "options": [],
        "recommendation": {"summary": "review"}, "expected_impact": {"level": "medium"},
        "needs_review": False,
    }


class IntelligenceOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)

    def tearDown(self):
        self.os.close()

    def test_malformed_specialist_degrades_and_preserves_required_result(self):
        orchestrator = IntelligenceOrchestrator(
            self.os, _Contracts(), specialist_handlers={"expert-0": lambda _ctx: {"bad": True}}
        )
        result = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["needs_review"])
        self.assertTrue(set(("finding", "evidence_for", "evidence_against", "assumptions", "unknowns", "hypothesis", "confidence", "analogues", "risks", "options", "recommendation", "expected_impact", "needs_review")) <= result.keys())

    def test_acl_leaking_refs_are_removed(self):
        def leak(_ctx):
            value = _result()
            value["evidence_for"] = [{"ref": "secret-workspace-record"}]
            return value
        orchestrator = IntelligenceOrchestrator(self.os, _Contracts(1), specialist_handlers={"expert-0": leak})
        result = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        self.assertEqual(result["evidence_for"], [])

    def test_contradictions_force_review_and_specialists_are_bounded(self):
        contracts = _Contracts(20)
        handlers = {f"expert-{i}": (lambda _ctx, i=i: _result(f"hypothesis-{i}")) for i in range(20)}
        orchestrator = IntelligenceOrchestrator(self.os, contracts, specialist_handlers=handlers)
        result = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        self.assertTrue(result["needs_review"])
        self.assertLessEqual(len(result["profiles"]), 8)
        self.assertLessEqual(len(result["trace"]), 8)

    def test_missing_specialist_is_deterministic_degraded(self):
        orchestrator = IntelligenceOrchestrator(self.os, _Contracts(1), specialist_handlers={"expert-0": lambda _ctx: (_ for _ in ()).throw(TimeoutError())})
        first = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        second = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        self.assertEqual(first["status"], "degraded")
        self.assertEqual(first["recommendation"], second["recommendation"])
        self.assertNotEqual(first["trace_id"], second["trace_id"])

    def test_uncited_evidence_is_dropped_and_requires_review(self):
        def uncited(_ctx):
            value = _result()
            value["evidence_for"] = [{"summary": "no citation"}, {"ref": "not-visible"}]
            value["evidence_against"] = [{"object_ref": {"id": "not-visible"}}]
            value["analogues"] = [{"summary": "no citation"}]
            return value
        result = IntelligenceOrchestrator(self.os, _Contracts(1), specialist_handlers={"expert-0": uncited}).run(
            "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin"
        )
        self.assertEqual(result["evidence_for"], [])
        self.assertEqual(result["evidence_against"], [])
        self.assertEqual(result["analogues"], [])
        self.assertTrue(result["needs_review"])

    def test_specialist_timeout_is_enforced(self):
        def slow(_ctx):
            time.sleep(0.15)
            return _result()
        limits = __import__("auremgrid.services.intelligence_orchestrator", fromlist=["OrchestrationLimits"]).OrchestrationLimits(timeout_seconds=0.02)
        orchestrator = IntelligenceOrchestrator(self.os, _Contracts(1), limits=limits, specialist_handlers={"expert-0": slow})
        started = time.monotonic()
        result = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        self.assertLess(time.monotonic() - started, 0.12)
        self.assertTrue(any("timeout" in error for event in result["trace"] for error in event.get("errors", [])))

    def test_iteration_budget_caps_refinement_passes(self):
        calls = {"count": 0}
        def counted(_ctx):
            calls["count"] += 1
            return _result()
        limits = __import__("auremgrid.services.intelligence_orchestrator", fromlist=["OrchestrationLimits"]).OrchestrationLimits(max_iterations=2)
        orchestrator = IntelligenceOrchestrator(self.os, _Contracts(1), limits=limits, specialist_handlers={"expert-0": counted})
        orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin", iterations=99)
        self.assertEqual(calls["count"], 2)

    def test_get_run_requires_matching_scope(self):
        orchestrator = IntelligenceOrchestrator(self.os, _Contracts(1), specialist_handlers={"expert-0": lambda _ctx: _result()})
        result = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        self.assertIsNotNone(orchestrator.get_run(result["trace_id"], "org_demo", "ws_alpha", "person_demo_owner"))
        with self.assertRaises(Exception):
            orchestrator.get_run(result["trace_id"], "org_demo", "ws_alpha", "person_demo_viewer")

    def test_default_runbook_uses_trigger_match_and_reports_no_match(self):
        contracts = _Contracts(1)
        contracts.runbooks = [
            {"id": "alpha", "version": "1", "name": "Alpha", "profile_ids": ["expert-0"], "domains": ["finance"], "activation_sequence": ["revenue"], "intent": "revenue planning"},
            {"id": "workbook", "version": "1", "name": "Workbook", "profile_ids": ["expert-0"], "domains": ["work"], "activation_sequence": ["work"]},
        ]
        orchestrator = IntelligenceOrchestrator(self.os, contracts, specialist_handlers={"expert-0": lambda _ctx: _result()})
        matched = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin", query="work")
        self.assertEqual(matched["runbook"]["id"], "workbook")
        no_match = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin", query="unrelated phrase")
        self.assertEqual(no_match["runbook_route"]["status"], "no_match")
        self.assertEqual(no_match["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
