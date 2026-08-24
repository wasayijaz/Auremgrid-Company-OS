from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.workflow_catalog import load_workflow_catalog
from auremgrid.services.workflow_ops import WorkflowOperations


def template() -> dict:
    return {
        "key": "client_launch",
        "name": "Client launch",
        "version": "1",
        "stages": [
            {
                "key": "brief",
                "name": "Brief",
                "sequence": 1,
                "assignee": {"wing": "strategy", "role": "lead"},
                "required_evidence": ["brief"],
                "handoff_to": {"wing": "creative", "role": "designer"},
                "handoff_contract": "approved brief and source links",
            },
            {
                "key": "creative",
                "name": "Creative",
                "sequence": 2,
                "assignee": {"wing": "creative", "role": "designer"},
                "depends_on": ["brief"],
                "required_evidence": ["preview"],
                "requires_approval": True,
            },
        ],
    }


class WorkflowOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client Workspace", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.worker = self.os.create_person(self.org.id, "Worker")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.worker.id, "operator")
        self.os.client_ops.create_client_roster(
            self.org.id,
            self.ws.id,
            self.owner.id,
            [
                {"role_key": "client_success_dri", "person_id": self.owner.id},
                {"role_key": "client_success_backup", "person_id": self.worker.id},
                {"role_key": "wing_lead", "wing": "strategy", "person_id": self.owner.id},
                {"role_key": "wing_lead", "wing": "Client Strategy/Marketing", "person_id": self.owner.id},
                {"role_key": "wing_executive", "wing": "Client Strategy/Marketing", "person_id": self.owner.id},
                {"role_key": "wing_executive", "wing": "creative", "person_id": self.worker.id},
                {"role_key": "wing_executive", "wing": "Product & Engineering", "person_id": self.worker.id},
                {"role_key": "wing_executive", "wing": "Paid Media", "person_id": self.worker.id},
                {"role_key": "wing_executive", "wing": "Design", "person_id": self.worker.id},
                {"role_key": "wing_executive", "wing": "Operations", "person_id": self.worker.id},
            ],
        )
        self.ops = WorkflowOperations(self.os.store.conn, new_id, self.os._require_person_access)

    def tearDown(self) -> None:
        self.os.close()

    def test_happy_path_completes_run_with_progress(self) -> None:
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template())
        self.ops.start_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        self.ops.submit_evidence(
            self.org.id,
            self.ws.id,
            self.owner.id,
            run["id"],
            "brief",
            "brief",
            object_type="source",
            object_id="src_1",
            locator="drive://brief",
            content_hash="hash1",
        )
        self.ops.complete_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        self.ops.acknowledge_handoff(
            self.org.id,
            self.ws.id,
            self.worker.id,
            run["id"],
            "brief",
            "creative",
            "approved brief and source links",
        )
        self.ops.start_stage(self.org.id, self.ws.id, self.worker.id, run["id"], "creative")
        self.ops.submit_evidence(
            self.org.id, self.ws.id, self.worker.id, run["id"], "creative", "preview", uri="https://example.test/ad"
        )
        approval = self._canonical_approval(run["id"], "creative")
        self.ops.request_approval(
            self.org.id, self.ws.id, self.worker.id, run["id"], "creative", "ready", approval["id"]
        )
        self.os.agency_ops.decide_approval(self.org.id, self.owner.id, approval["id"], True, "ship it")
        self.ops.decide_approval(
            self.org.id, self.ws.id, self.owner.id, run["id"], "creative", "approve", "ship it", approval["id"]
        )
        self.ops.complete_stage(self.org.id, self.ws.id, self.worker.id, run["id"], "creative")
        summary = self.ops.summary(self.org.id, self.ws.id, self.owner.id, run["id"])
        self.assertEqual(summary["run"]["status"], "completed")
        self.assertEqual(summary["progress"]["completed"], 2)
        self.assertEqual(summary["progress"]["percent"], 1)

    def test_run_can_be_created_from_luna_catalog_dataclass(self) -> None:
        catalog_template = load_workflow_catalog().get("campaign_launch")
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, catalog_template)
        summary = self.ops.summary(self.org.id, self.ws.id, self.owner.id, run["id"])
        self.assertEqual(run["definition_key"], "campaign_launch")
        self.assertEqual(len(summary["stages"]), len(catalog_template.stages))
        self.assertEqual(summary["stages"][0]["assignee_wing"], catalog_template.stages[0].owner_wing)
        self.assertTrue(summary["stages"][0]["requires_approval"])

    def test_rejected_gate_returns_stage_to_rework(self) -> None:
        run = self._ready_for_creative_approval()
        approval = self._canonical_approval(run["id"], "creative")
        self.ops.request_approval(
            self.org.id, self.ws.id, self.worker.id, run["id"], "creative", "ready", approval["id"]
        )
        self.os.agency_ops.decide_approval(self.org.id, self.owner.id, approval["id"], False, "tighten copy")
        decision = self.ops.decide_approval(
            self.org.id, self.ws.id, self.owner.id, run["id"], "creative", "request_changes", "tighten copy", approval["id"]
        )
        summary = self.ops.summary(self.org.id, self.ws.id, self.owner.id, run["id"])
        creative = next(stage for stage in summary["stages"] if stage["stage_key"] == "creative")
        self.assertEqual(decision["decision"], "request_changes")
        self.assertEqual(creative["status"], "in_progress")

    def test_rejected_gate_reopens_explicit_revision_target_and_renews_handoff(self) -> None:
        rework_template = template()
        rework_template["key"] = "client_launch_with_rework"
        rework_template["stages"][1]["on_reject_stage_key"] = "brief"
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, rework_template)
        self.ops.start_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        self.ops.submit_evidence(self.org.id, self.ws.id, self.owner.id, run["id"], "brief", "brief", text="brief")
        self.ops.complete_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        self.ops.acknowledge_handoff(
            self.org.id, self.ws.id, self.worker.id, run["id"], "brief", "creative", "approved brief and source links"
        )
        self.ops.start_stage(self.org.id, self.ws.id, self.worker.id, run["id"], "creative")
        self.ops.submit_evidence(
            self.org.id, self.ws.id, self.worker.id, run["id"], "creative", "preview", text="preview"
        )
        approval = self._canonical_approval(run["id"], "creative")
        self.ops.request_approval(
            self.org.id, self.ws.id, self.worker.id, run["id"], "creative", "ready", approval["id"]
        )
        self.os.agency_ops.decide_approval(self.org.id, self.owner.id, approval["id"], False, "revise brief")
        self.ops.decide_approval(
            self.org.id, self.ws.id, self.owner.id, run["id"], "creative", "request_changes", "revise brief", approval["id"]
        )
        summary = self.ops.summary(self.org.id, self.ws.id, self.owner.id, run["id"])
        stages = {stage["stage_key"]: stage for stage in summary["stages"]}
        self.assertEqual(stages["brief"]["status"], "in_progress")
        self.assertEqual(stages["creative"]["status"], "blocked")
        self.ops.complete_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief", "revised")
        with self.assertRaises(ValidationError):
            self.ops.start_stage(self.org.id, self.ws.id, self.worker.id, run["id"], "creative")
        self.ops.acknowledge_handoff(
            self.org.id, self.ws.id, self.worker.id, run["id"], "brief", "creative", "revised brief and source links"
        )
        restarted = self.ops.start_stage(self.org.id, self.ws.id, self.worker.id, run["id"], "creative")
        self.assertEqual(restarted["status"], "in_progress")

    def test_gate_cannot_complete_without_required_evidence(self) -> None:
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template())
        self.ops.start_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        with self.assertRaises(ValidationError):
            self.ops.complete_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")

    def test_gate_requires_canonical_approval_request(self) -> None:
        run = self._ready_for_creative_approval()
        with self.assertRaises(ValidationError):
            self.ops.request_approval(
                self.org.id, self.ws.id, self.worker.id, run["id"], "creative", "ready"
            )

    def test_dependencies_block_premature_start(self) -> None:
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template())
        with self.assertRaises(ValidationError):
            self.ops.start_stage(self.org.id, self.ws.id, self.worker.id, run["id"], "creative")

    def test_handoff_acknowledgement_is_required_before_target_start(self) -> None:
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template())
        self.ops.start_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        self.ops.submit_evidence(self.org.id, self.ws.id, self.owner.id, run["id"], "brief", "brief", text="brief")
        self.ops.complete_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        with self.assertRaises(ValidationError):
            self.ops.start_stage(self.org.id, self.ws.id, self.worker.id, run["id"], "creative")

    def test_idempotency_reuses_create_and_transition_results(self) -> None:
        first = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template(), idempotency_key="external-1")
        second = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template(), idempotency_key="external-1")
        self.assertEqual(first["id"], second["id"])
        start1 = self.ops.start_stage(
            self.org.id, self.ws.id, self.owner.id, first["id"], "brief", idempotency_key="external-start"
        )
        start2 = self.ops.start_stage(
            self.org.id, self.ws.id, self.owner.id, first["id"], "brief", idempotency_key="external-start"
        )
        self.assertEqual(start1["id"], start2["id"])
        self.assertEqual(start1["version"], start2["version"])

    def test_overdue_escalation_query_returns_runs_and_stages(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template(), due_at=past)
        overdue = self.ops.overdue_escalations(self.org.id, self.ws.id, self.owner.id)
        self.assertEqual(overdue["runs"][0]["id"], run["id"])
        self.assertTrue(any(stage["run_id"] == run["id"] for stage in overdue["stages"]))

    def test_illegal_transition_and_stale_version_are_rejected(self) -> None:
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template())
        with self.assertRaises(ValidationError):
            self.ops.complete_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        stage = self.ops.start_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        with self.assertRaises(ValidationError):
            self.ops.block_stage(
                self.org.id,
                self.ws.id,
                self.owner.id,
                run["id"],
                "brief",
                "blocked with stale version",
                expected_version=stage["version"] - 1,
            )

    def test_transition_history_is_append_only_and_auditable(self) -> None:
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template())
        self.ops.start_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        self.ops.submit_evidence(self.org.id, self.ws.id, self.owner.id, run["id"], "brief", "brief", text="brief")
        history = self.ops.history(self.org.id, self.ws.id, self.owner.id, run["id"])
        self.assertEqual([item["action"] for item in history], ["create_run", "start_stage", "submit_evidence"])
        with self.assertRaises(Exception):
            self.os.store.conn.execute("UPDATE workflow_transition_history SET action='tamper'")

    def test_definition_versions_and_run_snapshot_are_immutable(self) -> None:
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template())
        with self.assertRaises(Exception):
            self.os.store.conn.execute(
                "UPDATE workflow_definition_versions SET snapshot='{}' WHERE id=?",
                (run["definition_version_id"],),
            )
        with self.assertRaises(Exception):
            self.os.store.conn.execute(
                "UPDATE workflow_runs SET template_snapshot='{}' WHERE id=?",
                (run["id"],),
            )

    def _ready_for_creative_approval(self) -> dict:
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template())
        self.ops.start_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        self.ops.submit_evidence(self.org.id, self.ws.id, self.owner.id, run["id"], "brief", "brief", text="brief")
        self.ops.complete_stage(self.org.id, self.ws.id, self.owner.id, run["id"], "brief")
        self.ops.acknowledge_handoff(
            self.org.id, self.ws.id, self.worker.id, run["id"], "brief", "creative", "approved brief and source links"
        )
        self.ops.start_stage(self.org.id, self.ws.id, self.worker.id, run["id"], "creative")
        self.ops.submit_evidence(
            self.org.id, self.ws.id, self.worker.id, run["id"], "creative", "preview", uri="https://example.test/ad"
        )
        return run

    def _canonical_approval(self, run_id: str, stage_key: str) -> dict:
        return self.os.agency_ops.request_approval(
            self.org.id,
            "person",
            self.worker.id,
            f"workflow:{run_id}:{stage_key}",
            "workflow_stage_approval",
            {"run_id": run_id, "stage_key": stage_key},
            "workflow gate",
            "human",
            self.ws.id,
            self.owner.id,
        )


if __name__ == "__main__":
    unittest.main()
