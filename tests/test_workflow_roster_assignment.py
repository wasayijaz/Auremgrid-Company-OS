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
            for name in ("Lead", "Executive", "Creative", "Other lead", "Other executive")
        }
        for workspace_id in (self.ws.id, self.ws_other.id):
            self.os.add_person_to_workspace(self.org.id, workspace_id, self.owner.id, "admin")
        for person in self.people.values():
            self.os.add_person_to_workspace(self.org.id, self.ws.id, person.id, "operator")
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

    def _create_roster(self, *, lead=None, executive=None, extra=None, effective_at=None) -> dict:
        lead = lead or self.people["Lead"]
        executive = executive or self.people["Executive"]
        roles = [
            {"role_key": "client_success_dri", "person_id": self.owner.id},
            {"role_key": "client_success_backup", "person_id": self.people["Creative"].id},
            {"role_key": "wing_lead", "wing": "strategy", "person_id": lead.id},
            {"role_key": "wing_executive", "wing": "strategy", "person_id": executive.id},
        ]
        if extra:
            roles.extend(extra)
        return self.os.client_ops.create_client_roster(
            self.org.id, self.ws.id, self.owner.id, roles, effective_at=effective_at
        )

    def test_no_roster_preserves_existing_behavior(self) -> None:
        run = self.ops.create_run(self.org.id, self.ws.id, self.owner.id, self._template())
        summary = self.ops.summary(self.org.id, self.ws.id, self.owner.id, run["id"])
        self.assertIsNone(summary["stages"][0]["assignee_person_id"])
        self.assertNotIn("client_roster_id", run["template_snapshot"])

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

    def test_roster_is_isolated_to_workspace(self) -> None:
        self._create_roster()
        run = self.ops.create_run(self.org.id, self.ws_other.id, self.owner.id, self._template())
        self.assertIsNone(run["template_snapshot"]["stages"][0]["assignee_person_id"])
        self.assertNotIn("client_roster_id", run["template_snapshot"])

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
