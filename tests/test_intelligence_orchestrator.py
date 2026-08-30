from __future__ import annotations

import unittest
import time
import tempfile
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
        shared_ref = {"object_ref": {"type": "work_item", "id": "shared-work"}, "summary": "Shared delivery evidence."}
        def handler(i):
            def run(_ctx):
                value = _result(f"hypothesis-{i}")
                value["evidence_for"] = [shared_ref]
                value["expected_impact"] = {"level": "critical" if i == 0 else "low"}
                return value
            return run
        handlers = {f"expert-{i}": handler(i) for i in range(20)}
        orchestrator = IntelligenceOrchestrator(self.os, contracts, specialist_handlers=handlers)
        result = orchestrator.run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        self.assertTrue(result["needs_review"])
        self.assertTrue(result["contradictions"])
        self.assertLessEqual(len(result["profiles"]), 13)
        self.assertLessEqual(len(result["trace"]), 10)

    def test_lens_disagreement_is_not_material_contradiction_without_conflicting_evidence(self):
        contracts = _Contracts(2)
        handlers = {
            "expert-0": lambda _ctx: _result("delivery lens says review queue"),
            "expert-1": lambda _ctx: _result("capacity lens says rebalance work"),
        }
        result = IntelligenceOrchestrator(self.os, contracts, specialist_handlers=handlers).run(
            "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin"
        )
        self.assertEqual(result["contradictions"], [])
        self.assertEqual(result["disagreement"]["resolution"], "human_review")
        self.assertTrue(result["disagreement"]["lens_disagreements"])

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

    def test_orchestrator_result_survives_restart_and_keeps_specialists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orchestrator.sqlite"
            first_os = CompanyOS(path)
            first_os.seed_demo(FIXTURES)
            contracts = _Contracts(13)
            result = IntelligenceOrchestrator(first_os, contracts).run(
                "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin",
                profile_ids=[f"expert-{i}" for i in range(13)],
            )
            self.assertEqual(len(result["specialists"]), 13)
            trace_id = result["trace_id"]
            first_os.close()
            second_os = CompanyOS(path)
            try:
                fetched = IntelligenceOrchestrator(second_os, contracts).get_run(
                    trace_id, "org_demo", "ws_alpha", "person_demo_owner"
                )
                self.assertIsNotNone(fetched)
                self.assertEqual(fetched["trace_id"], trace_id)
                self.assertEqual(len(fetched["specialists"]), 13)
            finally:
                second_os.close()

    def test_specialist_provider_receives_profile_context_for_each_profile(self):
        calls = []
        class Provider:
            name = "fixture"
            model = "fixture"
            version = "1"
            def deliberate(self, context):
                calls.append(context["profile"]["id"])
                return _result(context["profile"]["id"])
        contracts = _Contracts(13)
        result = IntelligenceOrchestrator(self.os, contracts, specialist_provider=Provider()).run(
            "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin",
            profile_ids=[f"expert-{i}" for i in range(13)],
        )
        self.assertEqual(sorted(calls, key=lambda value: int(value.rsplit("-", 1)[1])), [f"expert-{i}" for i in range(13)])
        self.assertEqual({item["specialist_id"] for item in result["specialists"]}, set(calls))

    def test_profile_context_is_domain_scoped_and_trace_correlated(self):
        contracts = _Contracts(1)
        contracts.profiles[0].update({"domains": ["work"]})
        seen = {}
        def handler(context):
            seen.update(context)
            return _result()
        result = IntelligenceOrchestrator(
            self.os, contracts, specialist_handlers={"expert-0": handler}
        ).run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        self.assertNotIn("finance_amount_delta", seen.get("scenario_inputs", {}))
        self.assertTrue(all(event.get("trace_id") == result["trace_id"] for event in result["trace"]))

    def test_deterministic_specialists_choose_domain_relevant_evidence(self):
        contracts = _Contracts(2)
        contracts.profiles = [
            {"id": "performance_analyst", "version": "1", "name": "Performance", "domains": ["performance"]},
            {"id": "capacity_planner", "version": "1", "name": "Capacity", "domains": ["capacity"]},
        ]
        orchestrator = IntelligenceOrchestrator(self.os, contracts)
        context = {
            "findings": [{
                "title": "Cross-domain signal",
                "evidence": [
                    {"object_ref": {"type": "campaign_metric_snapshot", "id": "campaign-1"}, "summary": "ROAS fell"},
                    {"object_ref": {"type": "capacity_snapshot", "id": "capacity-1"}, "summary": "Capacity is negative"},
                ],
            }],
            "historical_analogues": [], "decision_action_outcome_learning": [], "scenario_inputs": {},
        }
        specialists, errors = orchestrator._run_specialists(
            contracts.profiles, context, {"campaign-1", "capacity-1"}
        )
        self.assertEqual(errors, [])
        by_profile = {item["specialist_id"]: item for item in specialists}
        self.assertEqual(by_profile["performance_analyst"]["evidence_for"][0]["object_ref"]["id"], "campaign-1")
        self.assertEqual(by_profile["capacity_planner"]["evidence_for"][0]["object_ref"]["id"], "capacity-1")

    def test_runbook_gates_are_evaluated_read_only(self):
        contracts = _Contracts(1)
        contracts.runbooks[0].update({
            "handoff_gates": ["owner named"],
            "quality_gates": ["evidence cited"],
            "scenario_policy": "bounded",
        })

        def compliant_result(_ctx):
            result = _result()
            result["unknowns"] = ["fixture evidence unavailable"]
            return result

        result = IntelligenceOrchestrator(
            self.os, contracts, specialist_handlers={"expert-0": compliant_result}
        ).run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        gate = next(item for item in result["trace"] if item["stage"] == "runbook_gates")
        self.assertEqual(gate["handoff"]["status"], "completed")
        self.assertEqual(gate["quality"]["status"], "completed")
        self.assertEqual(gate["scenario"]["status"], "completed")

    def test_specialist_fanout_is_bounded_parallel(self):
        def slow(_ctx):
            time.sleep(0.08)
            return _result()
        started = time.monotonic()
        result = IntelligenceOrchestrator(
            self.os, _Contracts(3), specialist_handlers={f"expert-{i}": slow for i in range(3)},
        ).run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin", profile_ids=["expert-0", "expert-1", "expert-2"])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2)
        self.assertEqual([item["specialist_id"] for item in result["specialists"]], ["expert-0", "expert-1", "expert-2"])

    def test_synthesis_balances_all_specialists_and_honors_profile_iteration_caps(self):
        contracts = _Contracts(2)
        contracts.profiles[0]["max_iterations"] = 1
        contracts.profiles[1]["max_iterations"] = 2
        contracts.runbooks[0]["max_iterations"] = 3
        calls = {"expert-0": 0, "expert-1": 0}
        def handler(key):
            def run(_ctx):
                calls[key] += 1
                value = _result(f"hypothesis-{key}")
                value["finding"] = f"finding-{key}"
                value["confidence"] = 0.2 if key == "expert-0" else 0.9
                value["recommendation"] = {"summary": f"recommend-{key}"}
                return value
            return run
        result = IntelligenceOrchestrator(
            self.os, contracts, specialist_handlers={key: handler(key) for key in calls},
        ).run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin", profile_ids=list(calls), iterations=3)
        self.assertEqual(calls, {"expert-0": 1, "expert-1": 2})
        self.assertIn("finding-expert-0", result["finding"])
        self.assertIn("finding-expert-1", result["finding"])
        self.assertIn("hypothesis-expert-0", result["hypothesis"])
        self.assertIn("hypothesis-expert-1", result["hypothesis"])
        self.assertEqual(result["confidence"], 0.55)
        self.assertEqual(len(result["recommendation"]["alternatives"]), 2)

    def test_runbook_iteration_cap_limits_all_profiles(self):
        contracts = _Contracts(2)
        contracts.runbooks[0]["max_iterations"] = 1
        calls = {"expert-0": 0, "expert-1": 0}
        def handler(key):
            def run(_ctx):
                calls[key] += 1
                return _result()
            return run
        IntelligenceOrchestrator(
            self.os, contracts, specialist_handlers={key: handler(key) for key in calls},
        ).run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin", profile_ids=list(calls), iterations=3)
        self.assertEqual(calls, {"expert-0": 1, "expert-1": 1})

    def test_default_specialists_use_distinct_profile_methods(self):
        contracts = _Contracts(3)
        contracts.profiles[0].update({"specialty": "Growth", "domains": ["growth"], "reasoning_method": "objective map"})
        contracts.profiles[1].update({"specialty": "Risk", "domains": ["risk"], "reasoning_method": "blast radius"})
        contracts.profiles[2].update({"specialty": "QA", "domains": ["quality"], "reasoning_method": "acceptance checklist"})
        result = IntelligenceOrchestrator(self.os, contracts).run(
            "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin",
            profile_ids=["expert-0", "expert-1", "expert-2"],
        )
        findings = [item["finding"] for item in result["specialists"]]
        hypotheses = [item["hypothesis"] for item in result["specialists"]]
        self.assertEqual(len(set(findings)), 3)
        self.assertEqual(len(set(hypotheses)), 3)

    def test_context_budget_overflow_is_explicit(self):
        contracts = _Contracts(1)
        contracts.profiles[0].update({"max_context": 128, "domains": ["work"]})
        result = IntelligenceOrchestrator(self.os, contracts).run(
            "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin",
            profile_ids=["expert-0"],
        )
        specialist = result["specialists"][0]
        self.assertEqual(specialist["context_budget"]["status"], "overflow")
        self.assertEqual(result["context_budget"]["status"], "overflow")
        self.assertTrue(any(event["stage"] == "context_budget" for event in result["trace"]))

    def test_per_run_token_cap_degrades_without_invoking_remaining_specialists(self):
        calls = {"count": 0}
        def counted(_ctx):
            calls["count"] += 1
            return _result()
        limits = __import__("auremgrid.services.intelligence_orchestrator", fromlist=["OrchestrationLimits"]).OrchestrationLimits(max_tokens=1)
        result = IntelligenceOrchestrator(
            self.os, _Contracts(2), limits=limits,
            specialist_handlers={"expert-0": counted, "expert-1": counted},
        ).run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin")
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["evaluation_safety"]["cap_reason"], "token_cap")

    def test_open_evaluation_circuit_skips_specialists(self):
        self.os.intelligence_evaluation_safety.configure_policy(
            "org_demo", "person_demo_owner", "reasoning", breaker_threshold=1,
        )
        safety = self.os.intelligence_evaluation_safety
        run = safety.start("org_demo", "person_demo_owner", "reasoning", workspace_id="ws_alpha")
        safety.complete("org_demo", "person_demo_owner", run["id"], workspace_id="ws_alpha", input_tokens=999999)
        result = IntelligenceOrchestrator(self.os, _Contracts(1)).run(
            "org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin"
        )
        self.assertEqual(result["evaluation_safety"]["status"], "circuit_open")
        self.assertEqual(result["specialists"], [])

    def test_tiny_context_overflow_keeps_cited_anchor(self):
        contracts = _Contracts(1)
        contracts.profiles[0].update({"max_context": 1})
        context = {
            "findings": [{
                "summary": "Visible work evidence should survive residual overflow.",
                "evidence": [{
                    "object_ref": {"type": "work_item", "id": "visible-work"},
                    "summary": "The delivery item moved past its review date.",
                }],
            }],
            "historical_analogues": [],
            "decision_action_outcome_learning": [],
            "scenario_inputs": {},
        }
        specialists, errors = IntelligenceOrchestrator(self.os, contracts)._run_specialists(
            contracts.profiles, context, {"visible-work"}
        )
        self.assertEqual(errors, [])
        specialist = specialists[0]
        self.assertEqual(specialist["context_budget"]["status"], "overflow")
        self.assertEqual(specialist["evidence_for"][0]["object_ref"]["id"], "visible-work")

    def test_mixed_raw_specialist_evidence_drops_invalid_items_only(self):
        contracts = _Contracts(1)
        valid = {"object_ref": {"type": "work_item", "id": "visible-work"}, "summary": "Visible work signal."}

        def mixed(_ctx):
            value = _result()
            value["evidence_for"] = [valid, {"summary": "uncited"}, {"ref": "not-visible"}]
            return value

        specialists, errors = IntelligenceOrchestrator(
            self.os, contracts, specialist_handlers={"expert-0": mixed}
        )._run_specialists(contracts.profiles, {"findings": []}, {"visible-work"})
        self.assertEqual(errors, [])
        self.assertEqual(specialists[0]["status"], "degraded")
        self.assertEqual(specialists[0]["evidence_for"], [valid])
        self.assertTrue(specialists[0]["needs_review"])
        self.assertTrue(any("dropped" in item for item in specialists[0]["unknowns"]))

    def test_historical_analogue_wire_alias_is_normalized(self):
        from auremgrid.services.intelligence_orchestrator import validate_expert_result
        value = _result()
        value.pop("analogues")
        value["historical_analogues"] = [{"ref": "visible"}]
        normalized = validate_expert_result(value)
        self.assertEqual(normalized["analogues"], normalized["historical_analogues"])

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

    def test_disagreement_historical_and_scenario_summaries_are_explicit(self):
        contracts = _Contracts(2)
        def handler(key):
            def run(ctx):
                value = _result(f"hypothesis-{key}")
                value["confidence"] = .7
                value["analogues"] = ctx.get("historical_analogues", [])[:1]
                return value
            return run
        result = IntelligenceOrchestrator(
            self.os, contracts,
            specialist_handlers={"expert-0": handler("expert-0"), "expert-1": handler("expert-1")},
        ).run("org_demo", "ws_alpha", "person_demo_owner", actor_id="act_alpha_admin", profile_ids=["expert-0", "expert-1"])
        self.assertEqual(result["disagreement"]["status"], "contested")
        self.assertEqual(result["disagreement"]["resolution"], "human_review")
        self.assertIn("status", result["historical_learning"])
        self.assertIn("status", result["scenario_analysis"])


if __name__ == "__main__":
    unittest.main()
