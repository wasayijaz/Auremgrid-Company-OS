from __future__ import annotations

import os as environment
import json
import unittest
from unittest.mock import patch

from auremgrid.connectors.catalog import connector_catalog
from auremgrid.api.mcp import McpToolRouter
from auremgrid.connectors.http import ConnectorTransportError
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

    def test_google_credential_bundle_is_strict_and_never_echoed_on_failure(self) -> None:
        sentinel = "refresh-secret-sentinel"
        raw = json.dumps({
            "client_id": "client-id",
            "client_secret": "client-secret-sentinel",
            "refresh_token": sentinel,
        })
        parsed = self.os.integrations._parse_google_credential_bundle(raw)
        self.assertEqual(parsed["refresh_token"], sentinel)
        for invalid in (
            "not-json",
            json.dumps({"client_id": "id", "client_secret": "secret"}),
            json.dumps({"client_id": "id", "client_secret": "secret", "refresh_token": sentinel, "extra": "x"}),
            json.dumps({"client_id": " id ", "client_secret": "secret", "refresh_token": sentinel}),
        ):
            with self.assertRaises(ValidationError) as raised:
                self.os.integrations._parse_google_credential_bundle(invalid)
            self.assertNotIn(sentinel, str(raised.exception))

    def test_google_refresh_uses_actual_broader_grant_and_keeps_bundle_in_memory(self) -> None:
        sentinel = "refresh-secret-sentinel"
        raw = json.dumps({
            "client_id": "client-id",
            "client_secret": "client-secret-sentinel",
            "refresh_token": sentinel,
        })
        calls = []
        def factory(mode, source, secret, integration):
            calls.append((mode, source, secret, integration["source"]))
            return {
                "access_token": "ephemeral-access-token",
                "expires_at": "2026-08-19T13:00:00+00:00",
                "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
            }
        self.os.integrations.connector_factory = factory
        token, scopes = self.os.integrations._refresh_google_access(
            {"source": "gmail", "permissions": [GMAIL_READ_SCOPE]}, raw
        )
        self.assertEqual(token, "ephemeral-access-token")
        self.assertEqual(scopes, ("https://www.googleapis.com/auth/gmail.modify",))
        self.assertEqual(calls[0][:2], ("refresh", "gmail"))
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM secret_bindings"
        ).fetchone()[0], 0)

    def test_google_refresh_fails_closed_when_provider_omits_grant_evidence(self) -> None:
        raw = json.dumps({
            "client_id": "client-id",
            "client_secret": "client-secret-sentinel",
            "refresh_token": "refresh-secret-sentinel",
        })
        self.os.integrations.connector_factory = lambda *_args: {
            "access_token": "ephemeral-access-token", "scopes": []
        }
        with self.assertRaises(ConnectorTransportError) as raised:
            self.os.integrations._refresh_google_access(
                {"source": "google_drive", "permissions": [GOOGLE_DRIVE_READ_SCOPE]}, raw
            )
        self.assertEqual(raised.exception.status, 403)
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("refresh-secret-sentinel", str(raised.exception))

    def test_internal_google_path_refreshes_for_verify_and_every_sync_without_persisting_tokens(self) -> None:
        bundle = json.dumps({
            "client_id": "client-id-sentinel",
            "client_secret": "client-secret-sentinel",
            "refresh_token": "refresh-token-sentinel",
        })
        environment.environ["AUREMGRID_TEST_GOOGLE_TOKEN"] = bundle
        integration = self.os.integrations.configure(
            self.identity, "gmail", "user@example.test",
            {"label:INBOX": self.ws.id}, [GMAIL_READ_SCOPE],
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Google bundle",
            "env:AUREMGRID_TEST_GOOGLE_TOKEN", ["connector:gmail", GMAIL_READ_SCOPE],
        )
        calls: list[str] = []
        def factory(mode, source, secret, *args):
            calls.append(mode)
            if mode == "refresh":
                self.assertIn("refresh-token-sentinel", secret)
                return {
                    "access_token": "ephemeral-access-sentinel",
                    "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                }
            self.assertEqual(secret, "ephemeral-access-sentinel")
            if mode == "verify":
                return {
                    "account_id": "User@Example.Test",
                    "account_name": "User@Example.Test",
                    "granted_permissions": ["https://www.googleapis.com/auth/gmail.modify"],
                }
            return [], '{"v":1,"phase":"history","checkpoint":"101","page_token":null}', False
        self.os.integrations.connector_factory = factory
        with patch(
            "auremgrid.services.integration_ops.LIVE_SOURCES",
            frozenset({"slack", "clickup", "gmail"}),
        ):
            verified = self.os.integrations.verify(self.identity, integration["id"])
            self.assertEqual(verified["integration"]["status"], "authorized")
            result = self.os.integrations.sync(self.identity, integration["id"])
            self.assertEqual(result["status"], "completed")
        self.assertEqual(calls.count("refresh"), 2)
        persisted = "\n".join(self.os.store.conn.iterdump())
        for sentinel in (
            "client-id-sentinel", "client-secret-sentinel",
            "refresh-token-sentinel", "ephemeral-access-sentinel",
        ):
            self.assertNotIn(sentinel, persisted)

    def test_refresh_revocation_sets_reauthentication_state_before_any_provider_page(self) -> None:
        environment.environ["AUREMGRID_TEST_GOOGLE_TOKEN"] = json.dumps({
            "client_id": "client-id", "client_secret": "client-secret",
            "refresh_token": "refresh-token",
        })
        integration = self.os.integrations.configure(
            self.identity, "gmail", "user@example.test",
            {"label:INBOX": self.ws.id}, [GMAIL_READ_SCOPE],
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Google bundle",
            "env:AUREMGRID_TEST_GOOGLE_TOKEN", ["connector:gmail", GMAIL_READ_SCOPE],
        )
        state = {"revoked": False, "pulls": 0}
        def factory(mode, source, secret, *args):
            if mode == "refresh":
                if state["revoked"]:
                    return {
                        "access_token": None, "scopes": [], "error": "sanitized",
                        "error_code": "authorization_required", "retryable": False,
                    }
                return {"access_token": "access", "scopes": [GMAIL_READ_SCOPE]}
            if mode == "verify":
                return {
                    "account_id": "user@example.test", "account_name": "Mailbox",
                    "granted_permissions": [GMAIL_READ_SCOPE],
                }
            state["pulls"] += 1
            return [], None, False
        self.os.integrations.connector_factory = factory
        with patch(
            "auremgrid.services.integration_ops.LIVE_SOURCES",
            frozenset({"slack", "clickup", "gmail"}),
        ):
            self.os.integrations.verify(self.identity, integration["id"])
            state["revoked"] = True
            with self.assertRaises(ConnectorTransportError):
                self.os.integrations.sync(self.identity, integration["id"])
        current = self.os.integrations.get(self.identity, integration["id"])
        self.assertEqual(current["status"], "reauth_required")
        self.assertEqual(current["health"], "degraded")
        self.assertEqual(state["pulls"], 0)
        self.assertEqual(self.os.store.conn.execute(
            "SELECT COUNT(*) FROM connector_ingest_batches"
        ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
