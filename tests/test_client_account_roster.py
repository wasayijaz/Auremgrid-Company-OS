from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.storage.migrations import migrate


class ClientAccountRosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.client = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.other_client = self.os.create_organization_workspace(self.org.id, "Other", "client")
        self.internal = self.os.create_organization_workspace(self.org.id, "HQ", "internal")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner", person_id="person_owner")
        self.dri = self.os.create_person(self.org.id, "DRI", person_id="person_dri")
        self.backup = self.os.create_person(self.org.id, "Backup", person_id="person_backup")
        self.lead = self.os.create_person(self.org.id, "Lead", person_id="person_lead")
        self.exec = self.os.create_person(self.org.id, "Executive", person_id="person_exec")
        self.operator = self.os.create_person(self.org.id, "Operator", person_id="person_operator")
        for person in (self.owner, self.dri, self.backup, self.lead, self.exec):
            self.os.add_person_to_workspace(self.org.id, self.client.id, person.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.client.id, self.operator.id, "operator")
        self.os.add_person_to_workspace(self.org.id, self.other_client.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.internal.id, self.owner.id, "admin")

    def tearDown(self) -> None:
        self.os.close()

    def _roles(self, *, facilitator: str | None = None, note_taker: str | None = None) -> list[dict[str, str]]:
        roles = [
            {"role_key": "client_success_dri", "person_id": self.dri.id},
            {"role_key": "client_success_backup", "person_id": self.backup.id},
            {"role_key": "account_lead", "person_id": self.lead.id},
            {"role_key": "account_executive", "person_id": self.exec.id},
            {"role_key": "wing_lead", "wing": "paid media", "person_id": self.lead.id},
        ]
        if facilitator:
            roles.append({"role_key": "default_meeting_facilitator", "person_id": facilitator})
        if note_taker:
            roles.append({"role_key": "default_meeting_note_taker", "person_id": note_taker})
        return roles

    def test_rosters_are_workspace_isolated_and_client_only(self) -> None:
        roster = self.os.client_ops.create_client_roster(
            self.org.id,
            self.client.id,
            self.owner.id,
            self._roles(),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        nonmember = self.os.create_person(self.org.id, "Nonmember", person_id="person_nonmember")
        self.assertEqual(roster["version"], 1)
        self.assertEqual(self.os.client_ops.get_client_roster(self.org.id, self.other_client.id, self.owner.id), None)
        with self.assertRaises(AuthorizationError):
            self.os.client_ops.get_client_roster(self.org.id, self.client.id, nonmember.id)
        with self.assertRaises(ValidationError):
            self.os.client_ops.create_client_roster(
                self.org.id,
                self.internal.id,
                self.owner.id,
                self._roles(),
                datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

    def test_as_of_selects_immutable_roster_versions(self) -> None:
        first = self.os.client_ops.create_client_roster(
            self.org.id, self.client.id, self.owner.id, self._roles(),
            datetime(2026, 1, 1, tzinfo=timezone.utc), "launch roster",
        )
        second = self.os.client_ops.create_client_roster(
            self.org.id, self.client.id, self.owner.id,
            [
                {"role_key": "client_success_dri", "person_id": self.lead.id},
                {"role_key": "client_success_backup", "person_id": self.backup.id},
                {"role_key": "account_lead", "person_id": self.dri.id},
            ],
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            "handoff roster",
        )
        old = self.os.client_ops.get_client_roster(
            self.org.id, self.client.id, self.owner.id, as_of=datetime(2026, 1, 15, tzinfo=timezone.utc)
        )
        current = self.os.client_ops.get_client_roster(
            self.org.id, self.client.id, self.owner.id, as_of=datetime(2026, 3, 1, tzinfo=timezone.utc)
        )
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(old["id"], first["id"])
        self.assertEqual(current["id"], second["id"])
        self.assertEqual(
            self.os.client_ops.resolve_account_role(
                self.org.id, self.client.id, self.owner.id, "wing_lead", wing="Paid Media",
                as_of=datetime(2026, 1, 15, tzinfo=timezone.utc),
            )["person_id"],
            self.lead.id,
        )
        with self.assertRaises(ValidationError):
            self.os.client_ops.create_client_roster(
                self.org.id, self.client.id, self.owner.id, self._roles(),
                datetime(2026, 2, 1, tzinfo=timezone.utc),
            )

    def test_invalid_people_roles_and_singletons_are_rejected(self) -> None:
        outsider = self.os.create_person(self.org.id, "Outsider", person_id="person_outsider")
        inactive = self.os.create_person(self.org.id, "Inactive", person_id="person_inactive")
        self.os.add_person_to_workspace(self.org.id, self.client.id, inactive.id, "viewer")
        self.os.store.conn.execute("UPDATE people SET status='disabled' WHERE id=?", (inactive.id,))
        self.os.store.conn.commit()
        cases = [
            [{"role_key": "client_success_dri", "person_id": self.dri.id}],
            [
                {"role_key": "client_success_dri", "person_id": self.dri.id},
                {"role_key": "client_success_backup", "person_id": self.dri.id},
            ],
            [
                {"role_key": "client_success_dri", "person_id": self.dri.id},
                {"role_key": "client_success_backup", "person_id": self.backup.id},
                {"role_key": "not_a_role", "person_id": self.lead.id},
            ],
            [
                {"role_key": "client_success_dri", "person_id": self.dri.id},
                {"role_key": "client_success_backup", "person_id": self.backup.id},
                {"role_key": "wing_lead", "person_id": self.lead.id},
            ],
            [
                {"role_key": "client_success_dri", "person_id": self.dri.id},
                {"role_key": "client_success_backup", "person_id": self.backup.id},
                {"role_key": "account_lead", "wing": "strategy", "person_id": self.lead.id},
            ],
            [
                {"role_key": "client_success_dri", "person_id": self.dri.id},
                {"role_key": "client_success_backup", "person_id": self.backup.id},
                {"role_key": "account_lead", "person_id": outsider.id},
            ],
            [
                {"role_key": "client_success_dri", "person_id": self.dri.id},
                {"role_key": "client_success_backup", "person_id": self.backup.id},
                {"role_key": "account_lead", "person_id": inactive.id},
            ],
            [
                {"role_key": "client_success_dri", "person_id": self.dri.id},
                {"role_key": "client_success_backup", "person_id": self.backup.id},
                {"role_key": "account_lead", "person_id": self.lead.id},
                {"role_key": "account_lead", "person_id": self.exec.id},
            ],
        ]
        for index, roles in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ValidationError):
                    self.os.client_ops.create_client_roster(
                        self.org.id,
                        self.client.id,
                        self.owner.id,
                        roles,
                        datetime(2026, 3, index + 1, tzinfo=timezone.utc),
                    )

    def test_roster_and_meeting_responsibility_writes_require_workspace_admin(self) -> None:
        roster = self.os.client_ops.create_client_roster(
            self.org.id,
            self.client.id,
            self.owner.id,
            self._roles(facilitator=self.lead.id),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            self.os.client_ops.get_client_roster(self.org.id, self.client.id, self.operator.id)["id"],
            roster["id"],
        )
        with self.assertRaises(AuthorizationError):
            self.os.client_ops.create_client_roster(
                self.org.id,
                self.client.id,
                self.operator.id,
                self._roles(),
                datetime(2026, 1, 2, tzinfo=timezone.utc),
            )
        meeting = self.os.client_ops.create_meeting(
            self.org.id, self.client.id, self.owner.id, "Weekly", datetime(2026, 1, 5, tzinfo=timezone.utc)
        )
        with self.assertRaises(AuthorizationError):
            self.os.client_ops.set_meeting_responsibilities(
                self.org.id,
                self.client.id,
                self.operator.id,
                meeting.id,
                facilitator_person_id=self.exec.id,
            )

    def test_meeting_responsibilities_default_then_explicit_override(self) -> None:
        roster = self.os.client_ops.create_client_roster(
            self.org.id,
            self.client.id,
            self.owner.id,
            self._roles(facilitator=self.lead.id, note_taker=self.backup.id),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        meeting = self.os.client_ops.create_meeting(
            self.org.id, self.client.id, self.owner.id, "Weekly", datetime(2026, 1, 5, tzinfo=timezone.utc)
        )
        defaults = self.os.client_ops.get_meeting_responsibilities(self.org.id, self.client.id, self.owner.id, meeting.id)
        self.assertEqual(defaults["roster_id"], roster["id"])
        self.assertEqual(defaults["facilitator_person_id"], self.lead.id)
        self.assertEqual(defaults["note_taker_person_id"], self.backup.id)
        self.assertEqual(defaults["source"], {"facilitator": "default", "note_taker": "default"})
        override = self.os.client_ops.set_meeting_responsibilities(
            self.org.id,
            self.client.id,
            self.owner.id,
            meeting.id,
            facilitator_person_id=self.exec.id,
            reason="client escalation",
        )
        self.assertEqual(override["facilitator_person_id"], self.exec.id)
        self.assertEqual(override["note_taker_person_id"], self.backup.id)
        self.assertEqual(override["source"], {"facilitator": "explicit", "note_taker": "default"})
        self.assertEqual(override["event_ids"]["facilitator"], override["event_id"])

    def test_raw_update_delete_guards_preserve_append_only_ledgers(self) -> None:
        roster = self.os.client_ops.create_client_roster(
            self.org.id, self.client.id, self.owner.id, self._roles(facilitator=self.lead.id),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        meeting = self.os.client_ops.create_meeting(
            self.org.id, self.client.id, self.owner.id, "Weekly", datetime(2026, 1, 5, tzinfo=timezone.utc)
        )
        event = self.os.client_ops.set_meeting_responsibilities(
            self.org.id, self.client.id, self.owner.id, meeting.id, facilitator_person_id=self.exec.id
        )
        role_id = roster["roles"][0]["id"]
        guarded = [
            ("UPDATE client_account_rosters SET note='changed' WHERE id=?", (roster["id"],)),
            ("DELETE FROM client_account_rosters WHERE id=?", (roster["id"],)),
            ("UPDATE client_account_roster_roles SET person_id=? WHERE id=?", (self.exec.id, role_id)),
            ("DELETE FROM client_account_roster_roles WHERE id=?", (role_id,)),
            ("UPDATE meeting_responsibility_events SET reason='changed' WHERE id=?", (event["event_id"],)),
            ("DELETE FROM meeting_responsibility_events WHERE id=?", (event["event_id"],)),
        ]
        for sql, params in guarded:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.os.store.conn.execute(sql, params)

    def test_same_tick_meeting_responsibilities_follow_append_sequence(self) -> None:
        self.os.client_ops.create_client_roster(
            self.org.id, self.client.id, self.owner.id, self._roles(),
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        meeting = self.os.client_ops.create_meeting(
            self.org.id, self.client.id, self.owner.id, "Weekly", datetime(2026, 1, 5, tzinfo=timezone.utc)
        )
        fixed = datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc)
        with patch("auremgrid.services.client_ops._now", return_value=fixed):
            first = self.os.client_ops.set_meeting_responsibilities(
                self.org.id, self.client.id, self.owner.id, meeting.id,
                facilitator_person_id=self.lead.id,
            )
            second = self.os.client_ops.set_meeting_responsibilities(
                self.org.id, self.client.id, self.owner.id, meeting.id,
                facilitator_person_id=self.exec.id,
            )
        resolved = self.os.client_ops.get_meeting_responsibilities(
            self.org.id, self.client.id, self.owner.id, meeting.id, as_of=fixed
        )
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(resolved["facilitator_person_id"], self.exec.id)
        self.assertEqual(resolved["event_ids"]["facilitator"], second["event_id"])

    def test_schema_20_replays_safely_when_ledger_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "replay.db"
            first = CompanyOS(path)
            org = first.create_organization("Agency")
            ws = first.create_organization_workspace(org.id, "Prime", "client")
            owner = first.create_person(org.id, "Owner", role="owner")
            dri = first.create_person(org.id, "DRI")
            backup = first.create_person(org.id, "Backup")
            for person in (owner, dri, backup):
                first.add_person_to_workspace(org.id, ws.id, person.id, "admin")
            first.client_ops.create_client_roster(
                org.id,
                ws.id,
                owner.id,
                [
                    {"role_key": "client_success_dri", "person_id": dri.id},
                    {"role_key": "client_success_backup", "person_id": backup.id},
                ],
                datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            first.store.conn.execute("DELETE FROM schema_migrations WHERE version=20")
            first.store.conn.commit()
            self.assertEqual(migrate(first.store.conn), 22)
            first.close()
            second = CompanyOS(path)
            try:
                self.assertEqual(second.store.schema_version, 22)
                roster = second.client_ops.get_client_roster(org.id, ws.id, owner.id)
                self.assertEqual(roster["version"], 1)
            finally:
                second.close()


if __name__ == "__main__":
    unittest.main()
