from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS, new_id
from auremgrid.services.workflow_ops import WorkflowOperations


class WorkflowRosterAssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client")
        self.ws_other = self.os.create_organization_workspace(self.org.id, "Other client", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.people = {
            name: self.os.create_person(self.org.id, name, role="member")
            for name in ("Lead", "Executive", "Creative", "Other lead", "Other executive", "Viewer")
        }
        for workspace_id in (self.ws.id, self.ws_other.id):
            self.os.add_person_to_workspace(self.org.id, workspace_id, self.owner.id, "admin")
        for person in self.people.values():
            self.os.add_person_to_workspace(self.org.id, self.ws.id, person.id, "operator")
        self.os.store.conn.execute(
            "UPDATE workspace_memberships SET role='viewer' WHERE workspace_id=? AND person_id=?",
            (self.ws.id, self.people["Viewer"].id),
        )
        self.os.store.conn.commit()
        for person in (self.people["Lead"], self.people["Executive"]):
            self.os.add_person_to_workspace(self.org.id, self.ws_other.id, person.id, "operator")
        self.ops = WorkflowOperations(self.os.store.conn, new_id, self.os._require_person_access)

    def tearDown(self) -> None:
        self.os.close()

    def _template(self, *, explicit: str | None = None, handoff: object | None = None) -> dict:
        first = {
            "key": "brief",
            "name": "Brief",
            "sequence": 1,
            "assignee": {"wing": "STRATEGY", "role": "Team Lead"},
            "required_evidence": ["brief"],
            "expected_duration_hours": 5,
        }
        if explicit is not None:
            first["assignee_person_id"] = explicit
        if handoff is not None:
            first["handoff_to"] = handoff
            first["handoff_contract"] = "brief package"
        return {
            "key": "roster_workflow",
            "name": "Roster workflow",
            "version": "1",
            "stages": [
                first,
                {
                    "key": "deliver",
                    "name": "Deliver",
                    "sequence": 2,
                    "assignee": {"wing": "strategy", "role": "Executive"},
                    "depends_on": ["brief"],
                    "required_evidence": ["deliverable"],
                },
            ],
        }

    def _create_roster(self, *, lead=None, executive=None, wing="strategy", extra=None, effective_at=None) -> dict:
        lead = lead or self.people["Lead"]
        executive = executive or self.people["Executive"]
        roles = [
            {"role_key": "client_success_dri", "person_id": self.owner.id},
            {"role_key": "client_success_backup", "person_id": self.people["Creative"].id},
            {"role_key": "wing_lead", "wing": wing, "person_id": lead.id},
            {"role_key": "wing_executive", "wing": wing, "person_id": executive.id},
        ]
        if extra:
            roles.extend(extra)
        return self.os.client_ops.create_client_roster(
            self.org.id, self.ws.id, self.owner.id, roles, effective_at=effective_at
        )

    def test_no_roster_rejects_before_any_run_write(self) -> None:
        before = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        with self.assertRaisesRegex(ValidationError, "active client roster is required"):
            self.ops.create_run(self.org.id, self.ws.id, self.owner.id, self._template())
        after = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        self.assertEqual(before, after)

    def test_agent_roster_owner_resolves_and_scope_fail_closed(self) -> None:
        agent = self.os.agent_ops.seed_primary_agents(self.org.id, self.owner.id)[0]
        self.os.agent_ops.configure_agent(self.org.id, self.owner.id, agent["id"], "test", [], [self.ws.id], [])
        self.os.store.conn.execute("UPDATE agents SET capability_tags='[\"workflow_run\"]' WHERE id=?", (agent["id"],)); self.os.store.conn.commit()
        self._create_roster(lead=self.people["Lead"], extra=[{"role_key": "wing_executive", "wing": "creative", "principal_type": "agent", "agent_id": agent["id"]}])
        template = self._template(); template["stages"][1]["assignee"] = {"wing": "creative", "role": "Executive"}
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template)
        self.assertEqual(run["template_snapshot"]["stages"][1]["assignee_principal_type"], "agent")
        self.os.store.conn.execute("UPDATE agents SET status='error' WHERE id=?", (agent["id"],)); self.os.store.conn.commit()
        with self.assertRaises(ValidationError): self.ops.create_run(self.org.id, self.ws.id, self.owner.id, template)

    def test_active_roster_resolves_lead_executive_and_structured_handoff(self) -> None:
        roster = self._create_roster(
            extra=[{"role_key": "wing_executive", "wing": "creative", "person_id": self.people["Creative"].id}]
        )
        run = self.ops.create_run(
            self.org.id,
            self.ws.id,
            self.owner.id,
            self._template(handoff={"wing": "creative", "role": "Designer"}),
        )
        summary = self.ops.summary(self.org.id, self.ws.id, self.owner.id, run["id"])
        stages = {stage["stage_key"]: stage for stage in summary["stages"]}
        self.assertEqual(stages["brief"]["assignee_person_id"], self.people["Lead"].id)
        self.assertEqual(stages["deliver"]["assignee_person_id"], self.people["Executive"].id)
        self.assertEqual(stages["brief"]["handoff_to_person_id"], self.people["Creative"].id)
        self.assertEqual(run["template_snapshot"]["client_roster_id"], roster["id"])
        self.assertEqual(run["template_snapshot"]["client_roster_version"], roster["version"])
        self.assertEqual(run["template_snapshot"]["stages"][0]["expected_duration_hours"], 5)

    def test_explicit_assignee_mismatch_rejects_before_any_run_write(self) -> None:
        self._create_roster()
        before = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        with self.assertRaises(ValidationError):
            self.ops.create_run(
                self.org.id,
                self.ws.id,
                self.owner.id,
                self._template(explicit=self.people["Creative"].id),
            )
        after = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        self.assertEqual(before, after)

    def test_roster_isolation_rejects_workspace_without_roster(self) -> None:
        self._create_roster()
        with self.assertRaisesRegex(ValidationError, "active client roster is required"):
            self.ops.create_run(self.org.id, self.ws_other.id, self.owner.id, self._template())

    def test_wrong_wing_roster_rejects_before_any_run_write(self) -> None:
        self._create_roster(wing="creative")
        before = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        with self.assertRaisesRegex(ValidationError, "active client roster has 0 matches"):
            self.ops.create_run(self.org.id, self.ws.id, self.owner.id, self._template())
        after = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        self.assertEqual(before, after)

    def test_inactive_roster_owner_rejects_before_any_run_write(self) -> None:
        self._create_roster()
        self.os.store.conn.execute("UPDATE people SET status='inactive' WHERE id=?", (self.people["Lead"].id,))
        self.os.store.conn.commit()
        before = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        with self.assertRaisesRegex(ValidationError, "stage brief owner must be an active workspace member"):
            self.ops.create_run(self.org.id, self.ws.id, self.owner.id, self._template())
        after = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        self.assertEqual(before, after)

    def test_viewer_roster_owner_rejects_before_any_run_write(self) -> None:
        self._create_roster(lead=self.people["Viewer"])
        before = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        with self.assertRaisesRegex(ValidationError, "stage brief owner must have workflow_run capability"):
            self.ops.create_run(self.org.id, self.ws.id, self.owner.id, self._template())
        after = self.os.store.conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0]
        self.assertEqual(before, after)

    def test_start_stage_rejects_if_persisted_stage_loses_named_owner(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.os.store.conn.execute(
            """INSERT INTO workflow_definitions(id,organization_id,key,name,created_at,updated_at)
               VALUES (?,?,?,?,?,?)""",
            ("wdef_legacy_owner", self.org.id, "legacy_owner", "Legacy owner", now, now),
        )
        self.os.store.conn.execute(
            """INSERT INTO workflow_definition_versions(id,definition_id,version,snapshot,created_by_person_id,created_at)
               VALUES (?,?,?,?,?,?)""",
            ("wdefver_legacy_owner", "wdef_legacy_owner", "1", '{"stages":[]}', self.owner.id, now),
        )
        run_id = "wrun_legacy_owner"
        self.os.store.conn.execute(
            """INSERT INTO workflow_runs(
                id,organization_id,workspace_id,definition_id,definition_version_id,definition_key,
                definition_version,definition_name,template_snapshot,status,created_by_person_id,
                idempotency_key,due_at,sla_minutes,escalation_at,blocked_reason,created_at,updated_at,
                started_at,completed_at,cancelled_at,version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, self.org.id, self.ws.id, "wdef_legacy_owner", "wdefver_legacy_owner",
                "legacy_owner", "1", "Legacy owner", '{"stages":[]}', "pending", self.owner.id,
                None, None, None, None, None, now, now, None, None, None, 1,
            ),
        )
        self.os.store.conn.execute(
            """INSERT INTO workflow_stage_runs(
                id,run_id,stage_key,name,sequence,status,assignee_wing,assignee_role,assignee_person_id,
                required_evidence,requires_approval,handoff_to_wing,handoff_to_role,handoff_to_person_id,
                on_reject_stage_key,due_at,blocked_reason,created_at,updated_at,started_at,completed_at,
                cancelled_at,version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "wstage_legacy_owner", run_id, "brief", "Brief", 1, "pending", "strategy", "lead",
                None, "[]", 0, None, None, None, None, None, None, now, now, None, None, None, 1,
            ),
        )
        self.os.store.conn.commit()
        with self.assertRaisesRegex(ValidationError, "workflow stage cannot start without an active client roster owner"):
            self.ops.start_stage(self.org.id, self.ws.id, self.owner.id, run_id, "brief")

    def test_roster_update_affects_new_runs_but_not_old_run(self) -> None:
        anchor = datetime.now(timezone.utc)
        first_roster = self._create_roster(effective_at=anchor - timedelta(seconds=2))
        old = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, self._template())
        second_roster = self._create_roster(
            lead=self.people["Other lead"],
            executive=self.people["Other executive"],
            effective_at=anchor - timedelta(seconds=1),
        )
        new = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, self._template())
        old_summary = self.ops.summary(self.org.id, self.ws.id, self.owner.id, old["id"])
        new_summary = self.ops.summary(self.org.id, self.ws.id, self.owner.id, new["id"])
        old_stage = next(stage for stage in old_summary["stages"] if stage["stage_key"] == "brief")
        new_stage = next(stage for stage in new_summary["stages"] if stage["stage_key"] == "brief")
        self.assertEqual(old_stage["assignee_person_id"], self.people["Lead"].id)
        self.assertEqual(new_stage["assignee_person_id"], self.people["Other lead"].id)
        self.assertEqual(old["template_snapshot"]["client_roster_id"], first_roster["id"])
        self.assertEqual(new["template_snapshot"]["client_roster_id"], second_roster["id"])

    def test_two_workspace_rosters_do_not_conflict_definition_versions(self) -> None:
        first = self._create_roster()
        second = self.os.client_ops.create_client_roster(
            self.org.id,
            self.ws_other.id,
            self.owner.id,
            [
                {"role_key": "client_success_dri", "person_id": self.owner.id},
                {"role_key": "client_success_backup", "person_id": self.people["Lead"].id},
                {"role_key": "wing_lead", "wing": "strategy", "person_id": self.people["Lead"].id},
                {"role_key": "wing_executive", "wing": "strategy", "person_id": self.people["Executive"].id},
            ],
        )
        run_one = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, self._template())
        run_two = self.ops.create_run(self.org.id, self.ws_other.id, self.owner.id, self._template())
        self.assertEqual(run_one["template_snapshot"]["client_roster_id"], first["id"])
        self.assertEqual(run_two["template_snapshot"]["client_roster_id"], second["id"])


if __name__ == "__main__":
    unittest.main()
