from __future__ import annotations

import os as environment
import unittest

from auremgrid.connectors.catalog import connector_catalog
from auremgrid.api.mcp import McpToolRouter
from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.integration_ops import (
    GMAIL_READ_SCOPE,
    GOOGLE_DRIVE_READ_SCOPE,
)
from tests.auth_support import issue_identity


class GoogleIntegrationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org_google_integration")
        self.ws = self.os.create_organization_workspace(
            self.org.id, "Client", "client", "ws_google_integration"
        )
        self.person = self.os.create_person(
            self.org.id,
            "Owner",
            "owner@google-integration.test",
            role="owner",
            person_id="person_google_integration",
        )
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.os.create_actor(self.ws.id, "Connector actor", "admin", "actor_google_integration")
        _, self.identity = issue_identity(
            self.os, self.org.id, self.person.id, self.ws.id, "actor_google_integration"
        )
        environment.environ["AUREMGRID_TEST_GOOGLE_TOKEN"] = "google-secret-sentinel"

    def tearDown(self) -> None:
        environment.environ.pop("AUREMGRID_TEST_GOOGLE_TOKEN", None)
        self.os.close()

    def test_google_minimum_permissions_are_enforced_server_side(self) -> None:
        with self.assertRaisesRegex(ValidationError, "drive.readonly"):
            self.os.integrations.configure(
                self.identity,
                "google_drive",
                "permission-id-1",
                {"folder:folder-1": self.ws.id},
                [],
            )
        with self.assertRaisesRegex(ValidationError, "gmail.readonly"):
            self.os.integrations.configure(
                self.identity,
                "gmail",
                "permission-id-1",
                {"label:INBOX": self.ws.id},
                [],
            )

    def test_catalog_does_not_advertise_google_as_live_before_adapter_gate(self) -> None:
        catalog = {item["source"]: item for item in connector_catalog()}
        self.assertTrue(catalog["slack"]["live_enabled"])
        self.assertTrue(catalog["clickup"]["live_enabled"])
        self.assertFalse(catalog["google_drive"]["live_enabled"])
        self.assertFalse(catalog["gmail"]["live_enabled"])

    def test_google_mapping_contract_rejects_unbounded_or_ambiguous_keys(self) -> None:
        with self.assertRaisesRegex(ValidationError, "folder:<id>"):
            self.os.integrations.configure(
                self.identity,
                "google_drive",
                "permission-id-1",
                {"folder-1": self.ws.id},
                [GOOGLE_DRIVE_READ_SCOPE],
            )
        with self.assertRaisesRegex(ValidationError, "label:<id>"):
            self.os.integrations.configure(
                self.identity,
                "gmail",
                "account@example.test",
                {"*": self.ws.id},
                [GMAIL_READ_SCOPE],
            )

    def test_google_configuration_is_truthful_and_cannot_verify_or_enqueue_while_live_is_disabled(self) -> None:
        integration = self.os.integrations.configure(
            self.identity,
            "google_drive",
            "permission-id-1",
            {"folder:folder-1": self.ws.id},
            [GOOGLE_DRIVE_READ_SCOPE],
        )
        self.assertFalse(integration["live_enabled"])
        self.assertEqual(integration["status"], "not_connected")
        self.os.integrations.bind_credential(
            self.identity,
            integration["id"],
            "Google read credential",
            "env:AUREMGRID_TEST_GOOGLE_TOKEN",
            ["connector:google_drive", GOOGLE_DRIVE_READ_SCOPE],
        )
        with self.assertRaisesRegex(ValidationError, "verification is not enabled"):
            self.os.integrations.verify(self.identity, integration["id"])
        with self.assertRaisesRegex(ValidationError, "not enabled for live"):
            self.os.integrations.enqueue_sync(self.identity, integration["id"])
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
        )
        self.assertNotIn("google-secret-sentinel", "\n".join(self.os.store.conn.iterdump()))
        current = self.os.integrations.get(self.identity, integration["id"])
        self.assertEqual(current["status"], "not_connected")
        self.assertEqual(current["credential"]["status"], "unverified")

    def test_each_google_mapping_will_snapshot_an_immutable_workspace_stream(self) -> None:
        integration = self.os.integrations.configure(
            self.identity,
            "gmail",
            "account@example.test",
            {"label:INBOX": self.ws.id, "label:Label_Projects": self.ws.id},
            [GMAIL_READ_SCOPE],
        )
        first = self.os.integrations._mapping_hash("gmail", "label:INBOX", self.ws.id)
        second = self.os.integrations._mapping_hash("gmail", "label:Label_Projects", self.ws.id)
        self.assertNotEqual(first, second)
        self.assertEqual(
            integration["workspace_mappings"],
            {"label:INBOX": self.ws.id, "label:Label_Projects": self.ws.id},
        )

    def test_mcp_status_exposes_the_closed_live_gate(self) -> None:
        integration = self.os.integrations.configure(
            self.identity,
            "gmail",
            "account@example.test",
            {"label:INBOX": self.ws.id},
            [GMAIL_READ_SCOPE],
        )
        result = McpToolRouter(self.os, self.identity).call(
            "integrations.list", {"organization_id": self.org.id}
        )
        current = next(item for item in result["integrations"] if item["id"] == integration["id"])
        self.assertFalse(current["live_enabled"])
        self.assertEqual(current["status"], "not_connected")

    def test_configuration_persists_only_canonical_trimmed_values(self) -> None:
        integration = self.os.integrations.configure(
            self.identity,
            " GMAIL ",
            " User@Example.Test ",
            {" label:INBOX ": f" {self.ws.id} "},
            [f" {GMAIL_READ_SCOPE} ", GMAIL_READ_SCOPE],
        )
        self.assertEqual(integration["source"], "gmail")
        self.assertEqual(integration["expected_account_id"], "user@example.test")
        self.assertEqual(integration["workspace_mappings"], {"label:INBOX": self.ws.id})
        self.assertEqual(integration["permissions"], [GMAIL_READ_SCOPE])

    def test_mapping_keys_must_be_unique_after_trimming(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unique after normalization"):
            self.os.integrations.configure(
                self.identity,
                "google_drive",
                "permission-id-1",
                {"folder:one": self.ws.id, " folder:one ": self.ws.id},
                [GOOGLE_DRIVE_READ_SCOPE],
            )

    def test_gmail_requires_nonempty_canonical_label_and_supported_scope(self) -> None:
        with self.assertRaisesRegex(ValidationError, "label:<id>"):
            self.os.integrations.configure(
                self.identity,
                "gmail",
                "user@example.test",
                {"label:  ": self.ws.id},
                [GMAIL_READ_SCOPE],
            )
        with self.assertRaisesRegex(ValidationError, "gmail.readonly"):
            self.os.integrations.configure(
                self.identity,
                "gmail",
                "user@example.test",
                {"label:INBOX": self.ws.id},
                ["https://www.googleapis.com/auth/gmail.metadata"],
            )

    def test_broader_actual_google_grants_satisfy_required_read_scope(self) -> None:
        self.assertEqual(
            self.os.integrations._missing_permissions(
                "google_drive",
                {GOOGLE_DRIVE_READ_SCOPE},
                {"https://www.googleapis.com/auth/drive", "extra"},
            ),
            set(),
        )
        self.assertEqual(
            self.os.integrations._missing_permissions(
                "gmail",
                {GMAIL_READ_SCOPE},
                {"https://www.googleapis.com/auth/gmail.modify", "extra"},
            ),
            set(),
        )
        self.assertEqual(
            self.os.integrations._missing_permissions(
                "gmail", {GMAIL_READ_SCOPE, "custom-required"}, {GMAIL_READ_SCOPE, "extra"}
            ),
            {"custom-required"},
        )

    def test_drive_expected_identity_must_be_stable_permission_id(self) -> None:
        with self.assertRaisesRegex(ValidationError, "permissionId"):
            self.os.integrations.configure(
                self.identity,
                "google_drive",
                "user@example.test",
                {"folder:one": self.ws.id},
                [GOOGLE_DRIVE_READ_SCOPE],
            )


if __name__ == "__main__":
    unittest.main()
