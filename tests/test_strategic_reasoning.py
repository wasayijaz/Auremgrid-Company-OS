from __future__ import annotations

import json
import unittest
from pathlib import Path

from auremgrid.services.brain import CompanyOS
from auremgrid.adapters.reasoning import HttpStrategicReasoningProvider, strategic_reasoning_provider_from_config


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class FakeProvider:
    name = "fake"
    model = "fake-model"
    version = "v1"

    def __init__(self, result):
        self.result = result
        self.context = None

    def deliberate(self, context):
        self.context = context
        return self.result


def result():
    return {
        "hypotheses": [{"text": "Visible evidence indicates a delivery constraint.", "confidence": 0.7}],
        "options": [{"title": "Triage", "summary": "Review the owner and deadline.", "tradeoffs": ["Consumes review time"]}],
        "scenarios": [{"name": "bounded", "assumptions": ["Evidence remains current"], "mitigations": ["Recheck tomorrow"]}],
        "recommendation": {"summary": "Review the cited blocker before changing the plan.", "rationale": "The recommendation is reversible."},
        "confidence": 0.64,
        "dissent": [{"text": "The evidence may be stale."}],
    }


class StrategicReasoningTests(unittest.TestCase):
    def _workspace(self, provider):
        os = CompanyOS(":memory:", strategic_reasoning_provider=provider)
        os.seed_demo(FIXTURES)
        return os

    def test_provider_gets_acl_scoped_context_and_structured_result(self):
        provider = FakeProvider(result())
        os = self._workspace(provider)
        try:
            output = os.intelligence.workspace("org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin")
            self.assertEqual(output["deliberation"]["mode"], "model_backed")
            self.assertEqual(output["deliberation"]["hypotheses"][0]["confidence"]["score"], 0.7)
            self.assertEqual(output["deliberation"]["provider_metadata"]["status"], "used")
            self.assertTrue(provider.context["evidence"])
            self.assertNotIn("store", provider.context)
            self.assertTrue(all(item["object_ref"] for item in provider.context["evidence"]))
            detail = os.store.conn.execute(
                "SELECT detail FROM audit_events WHERE action='intelligence.deliberate'"
            ).fetchone()[0]
            self.assertNotIn("Visible evidence indicates", detail)
            self.assertIn("context_hash", json.loads(detail))
        finally:
            os.close()

    def test_malformed_output_falls_back_without_canonical_mutation(self):
        provider = FakeProvider({"hypotheses": []})
        os = self._workspace(provider)
        try:
            before = os.store.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            output = os.intelligence.workspace("org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin")
            after = os.store.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(output["deliberation"]["mode"], "deterministic_evidence_review")
            self.assertEqual(output["deliberation"]["provider_metadata"]["status"], "fallback")
        finally:
            os.close()

    def test_provider_failure_falls_back_honestly(self):
        provider = FakeProvider(result())
        def fail(_context):
            raise RuntimeError("private provider detail")
        provider.deliberate = fail
        os = self._workspace(provider)
        try:
            output = os.intelligence.workspace("org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin")
            metadata = output["deliberation"]["provider_metadata"]
            self.assertEqual(metadata["status"], "fallback")
            self.assertEqual(metadata["fallback_reason"], "provider_call_failed")
            self.assertNotIn("private provider detail", json.dumps(metadata))
        finally:
            os.close()

    def test_no_provider_keeps_deterministic_reads_audit_free(self):
        os = self._workspace(None)
        try:
            os.intelligence.workspace("org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin")
            count = os.store.conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action='intelligence.deliberate'"
            ).fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            os.close()

    def test_identical_provider_result_is_audited_once(self):
        provider = FakeProvider(result())
        os = self._workspace(provider)
        try:
            for _ in range(2):
                os.intelligence.workspace("org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin")
            count = os.store.conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action='intelligence.deliberate'"
            ).fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            os.close()

    def test_http_provider_requires_safe_endpoint_and_configures_from_environment(self):
        self.assertIsNone(strategic_reasoning_provider_from_config(environ={}))
        with self.assertRaises(ValueError):
            HttpStrategicReasoningProvider("http://example.invalid/reason")
        with self.assertRaises(ValueError):
            strategic_reasoning_provider_from_config(environ={"AUREMGRID_REASONING_ENDPOINT": "not-a-url"})
        provider = strategic_reasoning_provider_from_config(environ={
            "AUREMGRID_REASONING_ENDPOINT": "http://127.0.0.1:8787/reason",
            "AUREMGRID_REASONING_MODEL": "local-model",
            "AUREMGRID_REASONING_TIMEOUT": "500",
        })
        self.assertIsNotNone(provider)
        self.assertEqual(provider.model, "local-model")
        self.assertEqual(provider.timeout, 60.0)


if __name__ == "__main__":
    unittest.main()
