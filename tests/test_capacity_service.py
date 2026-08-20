from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS


WEEK = "2026-01-05"


class CapacityServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.client = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.other = self.os.create_organization_workspace(self.org.id, "Other", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner", person_id="person_owner")
        self.worker = self.os.create_person(self.org.id, "Worker", person_id="person_worker")
        self.backup = self.os.create_person(self.org.id, "Backup", person_id="person_backup")
        self.other_admin = self.os.create_person(self.org.id, "Other Admin", role="admin", person_id="person_other_admin")
        for person in (self.owner, self.worker, self.backup):
            self.os.add_person_to_workspace(self.org.id, self.client.id, person.id, "admin")
        for person in (self.worker, self.other_admin):
            self.os.add_person_to_workspace(self.org.id, self.other.id, person.id, "admin")

    def tearDown(self) -> None:
        self.os.close()

    def _availability(self, person_id: str, hours: float) -> None:
        self.os.store.conn.execute(
            "INSERT INTO availability VALUES (?,?,?,?,?)",
            (f"avail_{person_id}", self.org.id, person_id, WEEK, hours),
        )
        self.os.store.conn.commit()

    def _leave(self, person_id: str, start: str, end: str, hours: float, status: str = "approved") -> None:
        self.os.store.conn.execute(
            "INSERT INTO leave_records VALUES (?,?,?,?,?,?,?)",
            (f"leave_{person_id}_{start}", self.org.id, person_id, start, end, hours, status),
        )
        self.os.store.conn.commit()

    def _person(self, board: dict, person_id: str) -> dict:
        return next(item for item in board["people"] if item["person_id"] == person_id)

    def _account(self, board: dict, workspace_id: str) -> dict:
        return next(item for item in board["accounts"] if item["workspace_id"] == workspace_id)

    def test_leave_is_allocated_across_overlapping_weekdays(self) -> None:
        self._availability(self.worker.id, 40)
        self._leave(self.worker.id, "2026-01-08", "2026-01-13", 24)
        board = self.os.capacity.weekly_board(self.org.id, self.owner.id, WEEK, self.client.id)
        worker = self._person(board, self.worker.id)
        self.assertEqual(worker["available_hours"], 40)
        self.assertEqual(worker["leave_hours"], 12)
        self.assertEqual(worker["net_available_hours"], 28)
        self.assertEqual(worker["remaining_hours"], 28)
        self.assertEqual(board["metadata"]["historical_inputs"], "current_configuration")

    def test_work_remaining_and_booked_time_use_as_of_cutoff(self) -> None:
        self._availability(self.worker.id, 20)
        with patch("auremgrid.services.work_ops._now", return_value=datetime(2026, 1, 5, 9, tzinfo=timezone.utc)):
            item = self.os.work_ops.create(
                self.org.id, self.client.id, self.owner.id, "Landing", "Build", "Client", estimate_hours=10
            )
        with patch("auremgrid.services.work_ops._now", return_value=datetime(2026, 1, 5, 9, 1, tzinfo=timezone.utc)):
            self.os.work_ops.assign(self.org.id, self.client.id, self.owner.id, item.id, self.worker.id)
        with patch("auremgrid.services.work_ops._now", return_value=datetime(2026, 1, 6, 12, tzinfo=timezone.utc)):
            self.os.work_ops.log_time(
                self.org.id,
                self.client.id,
                self.worker.id,
                item.id,
                datetime(2026, 1, 6, 9, tzinfo=timezone.utc),
                datetime(2026, 1, 6, 12, tzinfo=timezone.utc),
            )
        with patch("auremgrid.services.work_ops._now", return_value=datetime(2026, 1, 9, 12, tzinfo=timezone.utc)):
            self.os.work_ops.log_time(
                self.org.id,
                self.client.id,
                self.worker.id,
                item.id,
                datetime(2026, 1, 9, 10, tzinfo=timezone.utc),
                datetime(2026, 1, 9, 12, tzinfo=timezone.utc),
            )
        board = self.os.capacity.weekly_board(
            self.org.id, self.owner.id, WEEK, self.client.id, as_of=datetime(2026, 1, 7, tzinfo=timezone.utc)
        )
        worker = self._person(board, self.worker.id)
        self.assertEqual(worker["booked_hours"], 3)
        self.assertEqual(worker["work_remaining_hours"], 7)
        self.assertEqual(worker["remaining_hours"], 10)

    def test_time_entry_is_clipped_at_the_as_of_boundary(self) -> None:
        self._availability(self.worker.id, 20)
        with patch("auremgrid.services.work_ops._now", return_value=datetime(2026, 1, 5, 8, tzinfo=timezone.utc)):
            item = self.os.work_ops.create(
                self.org.id, self.client.id, self.owner.id, "Timed", "Timed", "Client", estimate_hours=10
            )
            self.os.work_ops.assign(self.org.id, self.client.id, self.owner.id, item.id, self.worker.id)
        with patch("auremgrid.services.work_ops._now", return_value=datetime(2026, 1, 5, 15, tzinfo=timezone.utc)):
            self.os.work_ops.log_time(
                self.org.id, self.client.id, self.worker.id, item.id,
                datetime(2026, 1, 5, 9, tzinfo=timezone.utc),
                datetime(2026, 1, 5, 15, tzinfo=timezone.utc),
            )
        board = self.os.capacity.weekly_board(
            self.org.id, self.owner.id, WEEK, self.client.id,
            as_of=datetime(2026, 1, 5, 12, tzinfo=timezone.utc),
        )
        worker = self._person(board, self.worker.id)
        self.assertEqual(worker["booked_hours"], 3)
        self.assertEqual(worker["work_remaining_hours"], 7)

    def test_workflow_and_roster_rollups_use_persisted_stage_duration(self) -> None:
        self._availability(self.worker.id, 12)
        with patch("auremgrid.services.client_ops._now", return_value=datetime(2026, 1, 1, tzinfo=timezone.utc)):
            roster = self.os.client_ops.create_client_roster(
                self.org.id,
                self.client.id,
                self.owner.id,
                [
                    {"role_key": "client_success_dri", "person_id": self.owner.id},
                    {"role_key": "client_success_backup", "person_id": self.backup.id},
                    {"role_key": "wing_lead", "wing": "strategy", "person_id": self.worker.id},
                ],
                datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        created_at = datetime(2026, 1, 5, 10, tzinfo=timezone.utc).isoformat()
        snapshot = {
            "key": "launch",
            "name": "Launch",
            "version": "1",
            "stages": [
                {
                    "key": "strategy",
                    "name": "Strategy",
                    "assignee_wing": "strategy",
                    "assignee_role": "Team Lead",
                    "assignee_person_id": self.worker.id,
                    "expected_duration_hours": 6,
                }
            ],
        }
        self.os.store.conn.execute(
            """INSERT INTO workflow_runs(
                id,organization_id,workspace_id,definition_id,definition_version_id,definition_key,
                definition_version,definition_name,template_snapshot,status,created_by_person_id,
                idempotency_key,due_at,sla_minutes,escalation_at,blocked_reason,created_at,updated_at,
                started_at,completed_at,cancelled_at,version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "wrun_capacity",
                self.org.id,
                self.client.id,
                "wdef_capacity",
                "wver_capacity",
                "launch",
                "1",
                "Launch",
                json.dumps(snapshot, sort_keys=True),
                "pending",
                self.owner.id,
                None,
                None,
                None,
                None,
                None,
                created_at,
                created_at,
                None,
                None,
                None,
                1,
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
                "wstage_capacity",
                "wrun_capacity",
                "strategy",
                "Strategy",
                1,
                "pending",
                "strategy",
                "Team Lead",
                self.worker.id,
                "[]",
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                created_at,
                created_at,
                None,
                None,
                None,
                1,
            ),
        )
        self.os.store.conn.commit()
        board = self.os.capacity.weekly_board(
            self.org.id, self.owner.id, WEEK, self.client.id, as_of=datetime(2026, 1, 6, tzinfo=timezone.utc)
        )
        worker = self._person(board, self.worker.id)
        account = self._account(board, self.client.id)
        self.assertEqual(worker["workflow_hours"], 6)
        self.assertEqual(account["workflow_hours"], 6)
        self.assertEqual(account["roster"]["id"], roster["id"])
        self.assertEqual(board["wings"], [{"wing": "strategy", "workflow_hours": 6, "workflow_unestimated_stage_count": 0, "assigned_person_ids": [self.worker.id]}])

        self.os.store.conn.execute(
            "UPDATE workflow_stage_runs SET status='completed',completed_at=?,updated_at=? WHERE id='wstage_capacity'",
            (datetime(2026, 1, 6, 12, tzinfo=timezone.utc).isoformat(),) * 2,
        )
        self.os.store.conn.execute(
            """INSERT INTO workflow_transition_history(
                id,run_id,stage_run_id,actor_person_id,action,from_status,to_status,reason,
                metadata,idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "whist_capacity", "wrun_capacity", "wstage_capacity", self.owner.id,
                "complete", "pending", "completed", "done", "{}", None,
                datetime(2026, 1, 6, 12, tzinfo=timezone.utc).isoformat(),
            ),
        )
        self.os.store.conn.commit()
        historical = self.os.capacity.weekly_board(
            self.org.id, self.owner.id, WEEK, self.client.id,
            as_of=datetime(2026, 1, 5, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(self._person(historical, self.worker.id)["workflow_hours"], 6)

    def test_workspace_filter_does_not_disclose_inaccessible_accounts(self) -> None:
        with patch("auremgrid.services.work_ops._now", return_value=datetime(2026, 1, 5, 9, tzinfo=timezone.utc)):
            visible = self.os.work_ops.create(
                self.org.id, self.client.id, self.owner.id, "Visible", "Visible", "Client", estimate_hours=4
            )
            hidden = self.os.work_ops.create(
                self.org.id, self.other.id, self.other_admin.id, "Hidden", "Hidden", "Client", estimate_hours=9
            )
        with patch("auremgrid.services.work_ops._now", return_value=datetime(2026, 1, 5, 10, tzinfo=timezone.utc)):
            self.os.work_ops.assign(self.org.id, self.client.id, self.owner.id, visible.id, self.worker.id)
            self.os.work_ops.assign(self.org.id, self.other.id, self.other_admin.id, hidden.id, self.worker.id)
        board = self.os.capacity.weekly_board(
            self.org.id, self.owner.id, WEEK, as_of=datetime(2026, 1, 6, tzinfo=timezone.utc)
        )
        self.assertEqual([account["workspace_id"] for account in board["accounts"]], [self.client.id])
        self.assertNotIn(self.other_admin.id, [person["person_id"] for person in board["people"]])
        self.assertEqual(self._person(board, self.worker.id)["work_remaining_hours"], 4)

    def test_role_and_title_do_not_grant_workspace_or_wing_access(self) -> None:
        titled = self.os.create_person(
            self.org.id, "Self Declared Lead", title="Paid Media Lead", role="owner", person_id="person_title"
        )
        with self.assertRaises(AuthorizationError):
            self.os.capacity.weekly_board(self.org.id, titled.id, WEEK, self.client.id)
        board = self.os.capacity.weekly_board(self.org.id, self.owner.id, WEEK, self.client.id)
        self.assertEqual(board["wings"], [])

    def test_week_start_must_be_iso_monday(self) -> None:
        with self.assertRaises(ValidationError):
            self.os.capacity.weekly_board(self.org.id, self.owner.id, "2026-01-06", self.client.id)


if __name__ == "__main__":
    unittest.main()
