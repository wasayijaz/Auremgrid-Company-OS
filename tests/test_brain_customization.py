from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone

from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class BrainCustomizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.alpha = self.os.create_organization_workspace(self.org.id, "Alpha", "client")
        self.beta = self.os.create_organization_workspace(self.org.id, "Beta", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.viewer = self.os.create_person(self.org.id, "Viewer", role="member")
        self.os.add_person_to_workspace(self.org.id, self.alpha.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.beta.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.alpha.id, self.viewer.id, "viewer")
        _, self.owner_alpha = issue_identity(self.os, self.org.id, self.owner.id, self.alpha.id)
        _, self.owner_beta = issue_identity(self.os, self.org.id, self.owner.id, self.beta.id)
        _, self.viewer_alpha = issue_identity(self.os, self.org.id, self.viewer.id, self.alpha.id)

        self.other_org = self.os.create_organization("Other")
        self.other_ws = self.os.create_organization_workspace(self.other_org.id, "Other WS", "client")
        self.other_owner = self.os.create_person(self.other_org.id, "Other Owner", role="owner")
        self.os.add_person_to_workspace(self.other_org.id, self.other_ws.id, self.other_owner.id, "admin")
        _, self.other_identity = issue_identity(self.os, self.other_org.id, self.other_owner.id, self.other_ws.id)

    def tearDown(self) -> None:
        self.os.close()

    def test_scoped_active_reads_do_not_cross_workspace_or_organization(self) -> None:
        org_version = self.os.brain_customizations.create_version(
            self.owner_alpha, self.org.id, "organization", "instructions",
            "Agency voice", "Use the approved agency voice.", reason="baseline",
        )
        alpha_version = self.os.brain_customizations.create_version(
            self.owner_alpha, self.org.id, "workspace", "policy",
            "Alpha policy", "Never reveal Alpha margins.", workspace_id=self.alpha.id, reason="client rule",
        )
        self.os.brain_customizations.activate_version(self.owner_alpha, self.org.id, org_version["id"], "go live")
        self.os.brain_customizations.activate_version(self.owner_alpha, self.org.id, alpha_version["id"], "go live")

        alpha = self.os.brain_customizations.active(self.owner_alpha, self.org.id, self.alpha.id)
        beta = self.os.brain_customizations.active(self.owner_beta, self.org.id, self.beta.id)
        self.assertEqual({item["body"] for item in alpha["effective"]}, {"Use the approved agency voice.", "Never reveal Alpha margins."})
        self.assertEqual([item["body"] for item in beta["effective"]], ["Use the approved agency voice."])
        self.assertNotIn("Alpha margins", repr(beta))
        with self.assertRaises(AuthorizationError):
            self.os.brain_customizations.active(self.other_identity, self.org.id, self.alpha.id)

    def test_management_is_acl_fenced_and_dashboard_surface_is_truthful(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.brain_customizations.create_version(
                self.viewer_alpha, self.org.id, "workspace", "settings",
                "Viewer attempt", "temperature=1", workspace_id=self.alpha.id, reason="not allowed",
            )
        settings = self.os.dashboard.settings(self.viewer_alpha, self.org.id, self.alpha.id)
        self.assertFalse(settings["brain_customization"]["can_manage"])
        self.assertEqual(settings["brain_customization"]["allowed_actions"], [])

        version = self.os.brain_customizations.create_version(
            self.owner_alpha, self.org.id, "workspace", "settings",
            "Alpha settings", "Prefer concise responses.", {"tone": "concise"}, self.alpha.id, "owner setting",
        )
        self.os.brain_customizations.activate_version(self.owner_alpha, self.org.id, version["id"], "activate")
        owner_settings = self.os.dashboard.settings(self.owner_alpha, self.org.id, self.alpha.id)
        self.assertEqual(owner_settings["brain_customization"]["status"], "configured")
        self.assertTrue(owner_settings["brain_customization"]["can_manage"])
        self.assertEqual(owner_settings["brain_customization"]["active"][0]["payload"], {"tone": "concise"})

    def test_history_and_rollback_are_activation_events_not_mutations(self) -> None:
        v1 = self.os.brain_customizations.create_version(
            self.owner_alpha, self.org.id, "workspace", "instructions",
            "Alpha v1", "Instruction v1", workspace_id=self.alpha.id, reason="first",
        )
        self.os.brain_customizations.activate_version(self.owner_alpha, self.org.id, v1["id"], "activate v1")
        after_v1 = datetime.now(timezone.utc)
        time.sleep(0.02)
        v2 = self.os.brain_customizations.create_version(
            self.owner_alpha, self.org.id, "workspace", "instructions",
            "Alpha v2", "Instruction v2", workspace_id=self.alpha.id, reason="second",
        )
        self.os.brain_customizations.activate_version(self.owner_alpha, self.org.id, v2["id"], "activate v2")
        after_v2 = datetime.now(timezone.utc)
        time.sleep(0.02)
        rollback = self.os.brain_customizations.rollback(self.owner_alpha, self.org.id, v1["id"], "restore v1")

        self.assertEqual(rollback["body"], "Instruction v1")
        self.assertEqual(rollback["activation_action"], "rolled_back")
        self.assertEqual(
            self.os.brain_customizations.active(self.owner_alpha, self.org.id, self.alpha.id, "instructions", after_v1)["effective"][0]["body"],
            "Instruction v1",
        )
        self.assertEqual(
            self.os.brain_customizations.active(self.owner_alpha, self.org.id, self.alpha.id, "instructions", after_v2)["effective"][0]["body"],
            "Instruction v2",
        )
        actions = [row["action"] for row in self.os.store.conn.execute(
            "SELECT action FROM brain_customization_events WHERE organization_id=? AND workspace_id=? ORDER BY event_sequence",
            (self.org.id, self.alpha.id),
        ).fetchall()]
        self.assertEqual(actions, ["created", "activated", "created", "activated", "rolled_back"])
        audit_rows = self.os.store.conn.execute(
            "SELECT action,entity_type,entity_id FROM ledger_audit WHERE organization_id=? AND workspace_id=? AND entity_type='brain_customization'",
            (self.org.id, self.alpha.id),
        ).fetchall()
        self.assertGreaterEqual(len(audit_rows), 5)
        with self.assertRaises(Exception):
            self.os.store.conn.execute("UPDATE brain_customization_events SET reason='rewrite'")


if __name__ == "__main__":
    unittest.main()
