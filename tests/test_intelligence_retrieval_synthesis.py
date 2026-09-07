from __future__ import annotations

import unittest

from auremgrid.services.intelligence_orchestrator import IntelligenceOrchestrator, validate_expert_result


def _specialist(**overrides):
    value = {
        "finding": "visible finding",
        "evidence_for": [],
        "evidence_against": [],
        "assumptions": [],
        "unknowns": [],
        "hypothesis": "same",
        "confidence": 0.8,
        "analogues": [],
        "risks": [],
        "options": [],
        "recommendation": {"summary": "review"},
        "expected_impact": {"level": "medium"},
        "needs_review": False,
    }
    value.update(overrides)
    return validate_expert_result(value)


class IntelligenceRetrievalSynthesisTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = IntelligenceOrchestrator(os=None)
        self.context = {
            "findings": [{
                "title": "Cross-domain signal",
                "evidence": [
                    {
                        "object_ref": {"type": "campaign_metric_snapshot", "id": "campaign-1"},
                        "summary": "ROAS fell",
                        "quality": 0.9,
                        "timestamp": "2026-09-01T00:00:00+00:00",
                    },
                    {
                        "object_ref": {"type": "capacity_snapshot", "id": "capacity-1"},
                        "summary": "Capacity is negative",
                        "quality": 0.8,
                        "timestamp": "2026-08-01T00:00:00+00:00",
                    },
                ],
            }],
            "historical_analogues": [],
            "decision_action_outcome_learning": [],
            "scenario_inputs": {},
        }
        self.performance = {
            "id": "performance_analyst",
            "name": "Performance",
            "domains": ["performance"],
            "required_evidence": ["campaign_metric_snapshot"],
        }
        self.capacity = {
            "id": "capacity_planner",
            "name": "Capacity",
            "domains": ["capacity"],
            "required_evidence": ["capacity_snapshot"],
        }

    def test_two_profiles_produce_different_retrieval_plans(self):
        performance_plan = self.orchestrator._build_retrieval_plan(self.performance, self.context, domain="performance")
        capacity_plan = self.orchestrator._build_retrieval_plan(self.capacity, self.context, domain="capacity")
        self.assertEqual(performance_plan["domains"], ["performance"])
        self.assertEqual(capacity_plan["domains"], ["capacity"])
        self.assertNotEqual(performance_plan["evidence_types"], capacity_plan["evidence_types"])
        self.assertEqual(performance_plan["evidence_refs"], ["campaign-1"])
        self.assertEqual(capacity_plan["evidence_refs"], ["capacity-1"])
        performance_context = self.orchestrator._restrict_profile_context(self.performance, self.context)
        capacity_context = self.orchestrator._restrict_profile_context(self.capacity, self.context)
        performance_ids = {
            item.get("object_ref", {}).get("id")
            for finding in performance_context["findings"]
            for item in finding.get("evidence", [])
        }
        capacity_ids = {
            item.get("object_ref", {}).get("id")
            for finding in capacity_context["findings"]
            for item in finding.get("evidence", [])
        }
        self.assertEqual(performance_ids, {"campaign-1"})
        self.assertEqual(capacity_ids, {"capacity-1"})
        self.assertNotEqual(performance_context["retrieval_plan"]["evidence_refs"], capacity_context["retrieval_plan"]["evidence_refs"])

    def test_evidence_rich_finding_outranks_confidence_only(self):
        allowed_refs = {"campaign-1", "campaign-2", "campaign-3"}
        rich_evidence = [
            {"object_ref": {"type": "campaign_metric_snapshot", "id": "campaign-1"}, "quality": 1.0, "timestamp": "2026-09-06T00:00:00+00:00"},
            {"object_ref": {"type": "campaign_metric_snapshot", "id": "campaign-2"}, "quality": 0.9, "timestamp": "2026-09-05T00:00:00+00:00"},
            {"object_ref": {"type": "campaign_metric_snapshot", "id": "campaign-3"}, "quality": 0.8, "timestamp": "2026-09-04T00:00:00+00:00"},
        ]
        situation = {
            "findings": [{"title": "Campaign drop", "domain": "performance", "evidence": rich_evidence}],
        }
        rich = _specialist(
            finding="evidence-backed decline",
            hypothesis="spend efficiency dropped",
            confidence=0.3,
            evidence_for=rich_evidence,
            recommendation={"summary": "pause the inefficient campaign"},
            profile={"id": "performance_analyst", "domains": ["performance"]},
        )
        loud = _specialist(
            finding="confidence-only optimism",
            hypothesis="the dip is noise",
            confidence=0.95,
            evidence_for=[],
            recommendation={"summary": "keep spending"},
            profile={"id": "account_strategist", "domains": ["client_success"]},
        )
        result = self.orchestrator._synthesize(situation, [loud, rich], [], [], allowed_refs)
        self.assertTrue(result["finding"].startswith("evidence-backed decline"))
        self.assertIn("spend efficiency dropped", result["hypothesis"])
        self.assertLess(result["finding"].find("evidence-backed decline"), result["finding"].find("confidence-only optimism"))
        simple_average = round((0.3 + 0.95) / 2, 3)
        self.assertLess(result["confidence"], simple_average)
        self.assertGreater(result["confidence"], 0.3)
        self.assertEqual(result["recommendation"]["alternatives"][0]["summary"], "pause the inefficient campaign")

    def test_allowed_refs_still_enforced(self):
        allowed_refs = {"campaign-1"}
        mixed = validate_expert_result(
            {
                "finding": "mixed citations",
                "evidence_for": [
                    {"object_ref": {"type": "campaign_metric_snapshot", "id": "campaign-1"}},
                    {"ref": "secret-workspace-record"},
                    {"summary": "uncited claim"},
                ],
                "evidence_against": [],
                "assumptions": [],
                "unknowns": [],
                "hypothesis": "only visible evidence counts",
                "confidence": 0.7,
                "analogues": [{"ref": "not-visible"}],
                "risks": [],
                "options": [],
                "recommendation": {"summary": "review"},
                "expected_impact": {"level": "medium"},
                "needs_review": False,
            },
            allowed_refs=allowed_refs,
        )
        self.assertEqual(mixed["evidence_for"][0]["object_ref"]["id"], "campaign-1")
        self.assertEqual(len(mixed["evidence_for"]), 1)
        self.assertTrue(mixed["needs_review"])
        self.assertTrue(any("dropped" in item for item in mixed["unknowns"]))
        synthesized = self.orchestrator._synthesize(
            {"findings": [{"evidence": [{"object_ref": {"type": "campaign_metric_snapshot", "id": "campaign-1"}}]}]},
            [mixed],
            [],
            [],
            allowed_refs,
        )
        self.assertEqual(len(synthesized["evidence_for"]), 1)
        self.assertEqual(synthesized["evidence_for"][0]["object_ref"]["id"], "campaign-1")
        self.assertEqual(synthesized["analogues"], [])
        leaked = validate_expert_result(
            {
                "finding": "leaked",
                "evidence_for": [{"ref": "secret-workspace-record"}],
                "evidence_against": [{"object_ref": {"id": "not-visible"}}],
                "assumptions": [],
                "unknowns": [],
                "hypothesis": "unsupported",
                "confidence": 0.9,
                "analogues": [{"ref": "also-secret"}],
                "risks": [],
                "options": [],
                "recommendation": {"summary": "act"},
                "expected_impact": {"level": "high"},
                "needs_review": False,
            },
            allowed_refs=allowed_refs,
        )
        self.assertEqual(leaked["evidence_for"], [])
        self.assertEqual(leaked["evidence_against"], [])
        self.assertEqual(leaked["analogues"], [])
        self.assertTrue(leaked["needs_review"])


if __name__ == "__main__":
    unittest.main()
