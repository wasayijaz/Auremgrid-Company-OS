from __future__ import annotations

import sqlite3
import threading
import time
import unittest

from auremgrid.services.agent_execution import (
    AGENT_EXECUTION_DDL,
    AgentExecutionWorker,
    ModelEntry,
    ModelRegistry,
    ProviderRegistry,
    SQLiteAgentExecutionStore,
    ThinkingBudget,
    ThinkingTask,
)


class RaisingProvider:
    name = "raising"
    model = "raising-model"
    version = "1"

    def deliberate(self, context):
        raise RuntimeError("first model unavailable")


class SuccessProvider:
    name = "success"
    model = "success-model"
    version = "1"

    def deliberate(self, context):
        return {
            "plan": {"action": "record", "value": context["value"]},
            "usage": {"input_tokens": 10, "output_tokens": 15},
        }


class SlowProvider:
    name = "slow"
    model = "slow-model"
    version = "1"

    def deliberate(self, context):
        time.sleep(10)
        return {"plan": {"action": "late"}}


class AgentExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(AGENT_EXECUTION_DDL)
        self.store = SQLiteAgentExecutionStore(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _registries(self) -> tuple[ProviderRegistry, ModelRegistry]:
        providers = ProviderRegistry()
        providers.register("raising", RaisingProvider())
        providers.register("success", SuccessProvider())
        providers.register("slow", SlowProvider())
        models = ModelRegistry()
        models.register(ModelEntry("fast-bad", "raising", 1, 0.01, 100))
        models.register(ModelEntry("fast-good", "success", 1, 0.01, 100))
        models.register(ModelEntry("too-expensive", "success", 1, 10.0, 1000))
        models.register(ModelEntry("slow-model", "slow", 1, 0.01, 100))
        return providers, models

    def test_fallback_chain_records_failure_then_success(self) -> None:
        providers, models = self._registries()
        models.set_fallback_chain(1, ["fast-bad", "fast-good"])

        result = ThinkingTask(
            providers,
            models,
            self.store,
            organization_id="org",
            workspace_id="ws",
            run_id="run-fallback",
        ).execute({"value": "ok"}, tier=1, budget=ThinkingBudget(max_tokens=250, max_cost=1.0))

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.model_id, "fast-good")
        rows = self.conn.execute(
            "SELECT provider_name,model_id,ok,error FROM agent_thinking_attempts WHERE run_id=? ORDER BY created_at",
            ("run-fallback",),
        ).fetchall()
        self.assertEqual([(row["provider_name"], row["model_id"], row["ok"]) for row in rows], [("raising", "fast-bad", 0), ("success", "fast-good", 1)])
        self.assertIn("provider_call_failed", rows[0]["error"])

    def test_budget_exhaustion_stops_before_unaffordable_model(self) -> None:
        providers, models = self._registries()
        models.set_fallback_chain(1, ["too-expensive", "fast-good"])

        result = ThinkingTask(
            providers,
            models,
            self.store,
            organization_id="org",
            workspace_id=None,
            run_id="run-budget",
        ).execute({"value": "ok"}, tier=1, budget=ThinkingBudget(max_tokens=50, max_cost=0.01))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempts, ())
        count = self.conn.execute("SELECT COUNT(*) FROM agent_thinking_attempts WHERE run_id=?", ("run-budget",)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_durable_resume_completes_action_once_without_rerunning_think(self) -> None:
        providers, models = self._registries()
        models.set_fallback_chain(1, ["fast-good"])
        calls: list[dict] = []

        def action_executor(plan, run):
            calls.append(dict(plan))
            return {"applied": True, "value": plan["plan"]["value"]}

        worker = AgentExecutionWorker(self.store, providers, models, action_executor)
        run = {
            "organization_id": "org",
            "workspace_id": "ws",
            "agent_id": "agent",
            "run_id": "run-durable",
            "task_id": "task",
        }
        with self.assertRaises(RuntimeError):
            worker.execute(
                run,
                {"value": "persisted"},
                tier=1,
                budget=ThinkingBudget(max_tokens=100, max_cost=1.0),
                crash_after_think=True,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_thinking_results WHERE run_id='run-durable'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_executor_actions WHERE run_id='run-durable'").fetchone()[0], 0)

        first_resume = worker.execute(run, {"value": "changed"}, tier=1, budget=ThinkingBudget(max_tokens=100, max_cost=1.0))
        second_resume = worker.execute(run, {"value": "changed-again"}, tier=1, budget=ThinkingBudget(max_tokens=100, max_cost=1.0))

        self.assertEqual(first_resume["status"], "done")
        self.assertEqual(second_resume["status"], "done")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["plan"]["value"], "persisted")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_thinking_attempts WHERE run_id='run-durable'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM agent_executor_actions WHERE run_id='run-durable' AND status='done'").fetchone()[0], 1)

    def test_cancel_kills_slow_provider_subprocess(self) -> None:
        providers, models = self._registries()
        models.set_fallback_chain(1, ["slow-model"])
        task = ThinkingTask(
            providers,
            models,
            self.store,
            organization_id="org",
            workspace_id=None,
            run_id="run-cancel",
        )
        result_holder = {}
        thread = threading.Thread(
            target=lambda: result_holder.setdefault(
                "result",
                task.execute({"value": "wait"}, tier=1, budget=ThinkingBudget(max_tokens=100, max_cost=1.0), timeout_seconds=30),
            )
        )
        thread.start()
        deadline = time.time() + 3
        while (
            (task._current_process is None or task._current_process.pid is None)
            and time.time() < deadline
        ):
            time.sleep(0.01)
        self.assertIsNotNone(task._current_process)
        pid = task._current_process.pid
        task.cancel()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(pid)
        result = result_holder["result"]
        self.assertEqual(result.status, "cancelled")
        row = self.conn.execute("SELECT ok,error FROM agent_thinking_attempts WHERE run_id='run-cancel'").fetchone()
        self.assertEqual(row["ok"], 0)
        self.assertEqual(row["error"], "cancelled")


if __name__ == "__main__":
    unittest.main()
