from __future__ import annotations

import sqlite3
import unittest

from auremgrid.services.agent_execution import (
    AGENT_THINK_JOB_TYPE,
    AgentExecutionWorker,
    AgentThinkJobHandler,
    ModelEntry,
    ModelRegistry,
    ProviderRegistry,
    SQLiteAgentExecutionStore,
    ThinkingBudget,
    agent_think_available,
    scope_context_packet,
)


class ThinkProvider:
    name = "test-provider"
    model = "test-model"
    version = "1"

    def __init__(self) -> None:
        self.contexts: list[dict] = []

    def deliberate(self, context):
        self.contexts.append(dict(context))
        return {
            "plan": {"action": "send", "target": "operator"},
            "action_descriptors": [{"action": "send", "requires_approval": True}],
            "trace": {"span": "think-1"},
            "citations": [{"source_id": "source-1"}],
            "usage": {"input_tokens": 7, "output_tokens": 11},
        }


class AgentThinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.store = SQLiteAgentExecutionStore(self.conn)
        self.store.initialize()
        self.provider = ThinkProvider()
        self.providers = ProviderRegistry()
        self.providers.register("test-provider", self.provider)
        self.models = ModelRegistry()
        self.models.register(ModelEntry("test-model", "test-provider", 1, 0.01, 100))
        self.models.set_fallback_chain(1, ["test-model"])

    def tearDown(self) -> None:
        self.conn.close()

    def test_happy_path_persists_result_attempt_usage_trace_and_citations(self) -> None:
        result = AgentThinkJobHandler(self.store, self.providers, self.models)({
            "organization_id": "org-1", "workspace_id": "ws-1", "principal_id": "principal-1",
            "type": AGENT_THINK_JOB_TYPE,
            "payload": {"agent_id": "agent-1", "run_id": "run-1", "person_id": "person-1", "context": {"workspace_id": "ws-1", "question": "status"}},
        })
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["thinking"]["input_tokens"], 7)
        self.assertEqual(result["thinking"]["citations"], [{"source_id": "source-1"}])
        self.assertEqual(result["thinking"]["attempts"][0]["trace"], {"span": "think-1"})
        self.assertTrue(result["thinking"]["result_json"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_thinking_results").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_thinking_attempts").fetchone()[0], 1)

    def test_no_provider_is_not_available(self) -> None:
        providers = ProviderRegistry()
        self.assertFalse(agent_think_available(providers, self.models))

    def test_context_packet_drops_other_workspace_and_organization_records(self) -> None:
        packet = scope_context_packet({
            "organization_id": "org-1", "workspace_id": "ws-1", "records": [
                {"workspace_id": "ws-1", "value": "visible"},
                {"workspace_id": "ws-other", "value": "hidden"},
                {"organization_id": "org-other", "value": "hidden-org"},
            ],
        }, organization_id="org-1", workspace_id="ws-1", person_id=None)
        self.assertEqual([item["value"] for item in packet["records"]], ["visible"])
        self.assertNotIn("hidden", repr(packet))
        self.assertNotIn("hidden-org", repr(packet))

    def test_action_descriptors_block_execution_without_approval(self) -> None:
        task = AgentThinkJobHandler(self.store, self.providers, self.models)
        task({
            "organization_id": "org-1", "workspace_id": "ws-1",
            "payload": {"agent_id": "agent-1", "run_id": "run-approval", "context": {}},
        })
        calls: list[dict] = []
        worker = AgentExecutionWorker(self.store, self.providers, self.models, lambda plan, run: calls.append(plan) or {"ok": True})
        response = worker.execute(
            {"organization_id": "org-1", "workspace_id": "ws-1", "agent_id": "agent-1", "run_id": "run-approval"},
            {}, tier=1, budget=ThinkingBudget(100, 1.0),
        )
        self.assertEqual(response["status"], "approval_required")
        self.assertEqual(calls, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_executor_actions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
