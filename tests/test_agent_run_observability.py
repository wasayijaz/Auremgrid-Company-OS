from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS


class AgentRunObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.primary = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.hidden = self.os.create_organization_workspace(self.org.id, "Hidden", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.viewer = self.os.create_person(self.org.id, "Viewer")
        self.os.add_person_to_workspace(self.org.id, self.primary.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.hidden.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.primary.id, self.viewer.id, "viewer")
        self.agent = next(
            item for item in self.os.agent_ops.seed_primary_agents(self.org.id, self.owner.id)
            if item["name"] == "Luna"
        )
        self.os.agent_ops.configure_agent(
            self.org.id,
            self.owner.id,
            self.agent["id"],
            "local",
            ["work.list"],
            [self.primary.id, self.hidden.id],
            ["domain.write"],
        )

    def tearDown(self) -> None:
        self.os.close()

    def test_worker_claim_trace_tool_and_completed_run_are_inspectable(self) -> None:
        task = self.os.agent_ops.enqueue_task(
            self.org.id,
            self.owner.id,
            self.agent["id"],
            "Inspect work",
            "List open work",
            self.primary.id,
            priority=9,
        )

        run = self.os.agent_ops.claim_next_task(self.org.id, self.owner.id, self.agent["id"])
        self.assertIsNotNone(run)
        self.assertEqual(run["task_id"], task["id"])
        first = self.os.agent_ops.record_trace(
            self.org.id, self.agent["id"], run["id"], "plan", "Inspect canonical work"
        )
        second = self.os.agent_ops.record_trace(
            self.org.id, self.agent["id"], run["id"], "result", "No blockers found", {"count": 0}
        )
        self.assertEqual((first["sequence"], second["sequence"]), (1, 2))
        self.os.agent_ops.record_tool_call(
            self.org.id,
            self.agent["id"],
            run["id"],
            "work.list",
            {"workspace_id": self.primary.id},
            "0 items",
        )
        with self.assertRaises(AuthorizationError):
            self.os.agent_ops.record_tool_call(
                self.org.id,
                self.agent["id"],
                run["id"],
                "work.list",
                {"workspace_id": self.hidden.id},
            )
        self.os.agent_ops.complete_run(
            self.org.id, self.agent["id"], run["id"], "No open work", 10, 5, 0.01
        )

        listed = self.os.agent_ops.list_runs(
            self.org.id, self.viewer.id, workspace_id=self.primary.id
        )
        self.assertEqual([item["id"] for item in listed], [run["id"]])
        detail = self.os.agent_ops.run_detail(self.org.id, self.viewer.id, run["id"])
        self.assertEqual(detail["output"]["content"], "No open work")
        self.assertEqual([item["kind"] for item in detail["traces"]], ["plan", "result"])
        self.assertEqual(detail["tool_calls"][0]["tool_name"], "work.list")

    def test_run_visibility_is_limited_to_workspace_membership(self) -> None:
        task = self.os.agent_ops.enqueue_task(
            self.org.id,
            self.owner.id,
            self.agent["id"],
            "Hidden audit",
            "Inspect hidden client",
            self.hidden.id,
        )
        run = self.os.agent_ops.start_run(self.org.id, self.owner.id, self.agent["id"], task["id"])
        self.os.agent_ops.complete_run(self.org.id, self.agent["id"], run["id"], "Done")

        self.assertEqual(self.os.agent_ops.list_runs(self.org.id, self.viewer.id), [])
        with self.assertRaises(AuthorizationError):
            self.os.agent_ops.list_runs(
                self.org.id, self.viewer.id, workspace_id=self.hidden.id
            )
        with self.assertRaises(NotFoundError):
            self.os.agent_ops.run_detail(self.org.id, self.viewer.id, run["id"])

    def test_viewer_cannot_claim_or_start_a_hidden_workspace_task(self) -> None:
        task = self.os.agent_ops.enqueue_task(
            self.org.id, self.owner.id, self.agent["id"], "Hidden claim", "Inspect hidden work", self.hidden.id
        )
        self.assertIsNone(self.os.agent_ops.claim_next_task(self.org.id, self.viewer.id, self.agent["id"]))
        with self.assertRaises(AuthorizationError):
            self.os.agent_ops.start_run(self.org.id, self.viewer.id, self.agent["id"], task["id"])

    def test_queue_priority_is_bounded_to_dashboard_contract(self) -> None:
        with self.assertRaisesRegex(ValidationError, "between 0 and 100"):
            self.os.agent_ops.enqueue_task(
                self.org.id, self.owner.id, self.agent["id"], "Invalid priority", "Do not queue", self.primary.id,
                priority=101,
            )

    def test_dashboard_agent_surfaces_are_workspace_isolated(self) -> None:
        primary_task = self.os.agent_ops.enqueue_task(
            self.org.id, self.owner.id, self.agent["id"], "Primary task", "Read primary work", self.primary.id
        )
        primary_run = self.os.agent_ops.start_run(self.org.id, self.owner.id, self.agent["id"], primary_task["id"])
        self.os.agent_ops.complete_run(self.org.id, self.agent["id"], primary_run["id"], "Primary result")

        hidden_task = self.os.agent_ops.enqueue_task(
            self.org.id, self.owner.id, self.agent["id"], "Hidden task", "Read hidden work", self.hidden.id
        )
        hidden_run = self.os.agent_ops.start_run(self.org.id, self.owner.id, self.agent["id"], hidden_task["id"])
        self.os.agent_ops.complete_run(self.org.id, self.agent["id"], hidden_run["id"], "Hidden result")

        center = self.os.agent_ops.command_center(self.org.id, self.viewer.id)
        self.assertEqual([row["id"] for row in center["recent_runs"]], [primary_run["id"]])
        command = self.os.dashboard.command(self.org.id, self.viewer.id)
        dashboard_agent = next(row for row in command["agents"] if row["id"] == self.agent["id"])
        self.assertEqual(dashboard_agent["runtime"]["runs_total"], 1)
        self.assertEqual(dashboard_agent["allowed_workspace_ids"], f'["{self.primary.id}"]')

        detail = self.os.dashboard.agent_detail(self.org.id, self.viewer.id, self.agent["id"])
        self.assertEqual([row["id"] for row in detail["runs"]], [primary_run["id"]])
        self.assertEqual([row["workspace_id"] for row in detail["tasks"]], [self.primary.id])
        self.assertEqual([row["task_id"] for row in detail["queue"]], [primary_task["id"]])
        self.assertEqual(detail["agent"]["allowed_workspace_ids"], [self.primary.id])

        hidden_only = next(
            item for item in self.os.agent_ops.seed_primary_agents(self.org.id, self.owner.id)
            if item["name"] == "Terra"
        )
        self.os.agent_ops.configure_agent(
            self.org.id, self.owner.id, hidden_only["id"], "local", ["work.list"], [self.hidden.id], []
        )
        with self.assertRaises(NotFoundError):
            self.os.dashboard.agent_detail(self.org.id, self.viewer.id, hidden_only["id"])


if __name__ == "__main__":
    unittest.main()
