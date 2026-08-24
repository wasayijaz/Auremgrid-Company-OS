from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.brain import CompanyOS


class OperatorReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Operator Readiness")
        self.ws = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.other_ws = self.os.create_organization_workspace(self.org.id, "Other", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.member = self.os.create_person(self.org.id, "Member")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.other_ws.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.member.id, "operator")
        self.agent = next(
            agent for agent in self.os.agent_ops.seed_primary_agents(self.org.id, self.owner.id)
            if agent["name"] == "Luna"
        )
        self.os.agent_ops.configure_agent(
            self.org.id,
            self.owner.id,
            self.agent["id"],
            "local",
            ["work.list"],
            [self.ws.id, self.other_ws.id],
            ["domain.write"],
        )

    def tearDown(self) -> None:
        self.os.close()

    def test_supervised_export_is_bounded_redacted_and_workspace_scoped(self) -> None:
        scheduler = self.os.scheduler(self.org.id, self.ws.id, "worker-readiness", poll_seconds=0.01)
        scheduler.run_once()
        task = self.os.agent_ops.enqueue_task(
            self.org.id, self.owner.id, self.agent["id"], "Inspect", "List work", self.ws.id
        )
        run = self.os.agent_ops.start_run(self.org.id, self.owner.id, self.agent["id"], task["id"])
        self.os.agent_ops.record_trace(
            self.org.id, self.agent["id"], run["id"], "plan", "Inspect safely", {"token": "trace-secret"}
        )
        self.os.agent_ops.record_tool_call(
            self.org.id,
            self.agent["id"],
            run["id"],
            "work.list",
            {"workspace_id": self.ws.id, "api_key": "tool-secret"},
            "Bearer abc.def",
        )
        self.os.agent_ops.complete_run(
            self.org.id, self.agent["id"], run["id"], "Completed with Bearer xyz.secret"
        )
        hidden_task = self.os.agent_ops.enqueue_task(
            self.org.id, self.owner.id, self.agent["id"], "Other", "List other work", self.other_ws.id
        )
        hidden_run = self.os.agent_ops.start_run(self.org.id, self.owner.id, self.agent["id"], hidden_task["id"])
        self.os.agent_ops.complete_run(self.org.id, self.agent["id"], hidden_run["id"], "Other workspace result")
        automation = self.os.agent_ops.create_automation(
            self.org.id,
            self.owner.id,
            "Silence risk",
            "client_silence",
            [{"field": "days", "operator": "gt", "value": 5}],
            [{"type": "risk.create", "config": {"workspace_id": self.ws.id, "type": "relationship"}}],
            "auto",
        )
        auto_run = self.os.agent_ops.trigger_automations(
            self.org.id, "client_silence", {"days": 6, "workspace_id": self.ws.id, "token": "payload-secret"}
        )[0]

        exported = self.os.operator_readiness.supervised_operations_export(
            self.org.id, self.owner.id, self.ws.id
        )

        self.assertEqual(exported["_meta"]["workspace_id"], self.ws.id)
        self.assertEqual([item["id"] for item in exported["agents"]["runs"]], [run["id"]])
        self.assertEqual(exported["scheduler"]["heartbeats"][0]["worker_id"], "worker-readiness")
        tool_call = exported["agents"]["runs"][0]["tool_calls"][0]
        self.assertEqual(tool_call["arguments"]["api_key"], "[REDACTED]")
        self.assertEqual(tool_call["result_preview"], "Bearer [REDACTED]")
        trace = exported["agents"]["runs"][0]["traces"][0]
        self.assertEqual(trace["metadata"]["token"], "[REDACTED]")
        self.assertEqual(exported["agents"]["runs"][0]["output"]["content"], "Completed with Bearer [REDACTED]")
        self.assertEqual(exported["automations"]["definitions"][0]["id"], automation["id"])
        self.assertEqual(exported["automations"]["runs"][0]["id"], auto_run["run_id"])
        self.assertEqual(exported["automations"]["runs"][0]["trigger_payload"]["token"], "[REDACTED]")

    def test_supervised_export_requires_org_admin(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.operator_readiness.supervised_operations_export(
                self.org.id, self.member.id, self.ws.id
            )

    def test_readiness_report_surfaces_training_checkpoint_and_scheduler_state(self) -> None:
        self.os.scheduler(self.org.id, self.ws.id, "worker-ready", poll_seconds=0.01).run_once()
        self.os.agent_ops.create_automation(
            self.org.id,
            self.owner.id,
            "Silence risk",
            "client_silence",
            [{"field": "days", "operator": "gt", "value": 5}],
            [{"type": "notification.create", "config": {"workspace_id": self.ws.id}}],
            "auto",
        )
        self.os.agent_ops.trigger_automations(
            self.org.id, "client_silence", {"days": 6, "workspace_id": self.ws.id}
        )

        report = self.os.operator_readiness.readiness_report(self.org.id, self.owner.id, self.ws.id)

        self.assertEqual(report["status"], "ready")
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["scheduler_heartbeat_present"]["status"], "pass")
        self.assertEqual(checks["automation_training_checkpoints_visible"]["status"], "pass")

    def test_postgres_portability_assessment_is_explicitly_not_ready(self) -> None:
        assessment = self.os.operator_readiness.postgres_portability_assessment()
        finding_ids = {finding["id"] for finding in assessment["findings"]}

        self.assertEqual(assessment["status"], "not_ready")
        self.assertGreater(assessment["summary"]["blockers"], 0)
        self.assertIn("companyos_sqlite_store_runtime", finding_ids)
        self.assertIn("postgres_migration_runner_missing", finding_ids)
        self.assertIn("sqlite_virtual_table_fts5", finding_ids)
        self.assertIn("sqlite_trigger_raise", finding_ids)
        self.assertFalse(assessment["opened_connections"])


if __name__ == "__main__":
    unittest.main()
