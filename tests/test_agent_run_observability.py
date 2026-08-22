from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, NotFoundError
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

        run = self.os.agent_ops.claim_next_task(self.org.id, self.agent["id"])
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
        run = self.os.agent_ops.start_run(self.org.id, self.agent["id"], task["id"])
        self.os.agent_ops.complete_run(self.org.id, self.agent["id"], run["id"], "Done")

        self.assertEqual(self.os.agent_ops.list_runs(self.org.id, self.viewer.id), [])
        with self.assertRaises(AuthorizationError):
            self.os.agent_ops.list_runs(
                self.org.id, self.viewer.id, workspace_id=self.hidden.id
            )
        with self.assertRaises(NotFoundError):
            self.os.agent_ops.run_detail(self.org.id, self.viewer.id, run["id"])


if __name__ == "__main__":
    unittest.main()
