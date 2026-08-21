from __future__ import annotations

import json
import os as environment
import unittest
from unittest.mock import patch

from auremgrid.connectors.fireflies import FIREFLIES_REQUIRED_SCOPES
from auremgrid.connectors.http import ConnectorTransportError, HttpResponse
from auremgrid.domain.errors import ValidationError
from auremgrid.connectors.google_auth import ConnectorSourceEvent, RouteLifecycleMutation
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class FirefliesApiTransport:
    """Exact provider-shaped transport used by the service wiring test."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def request(self, method, url, headers, body=None):
        self.calls.append(url)
        if url.endswith("/auth/profile"):
            payload = {"id": "user-1", "email": "owner@fireflies.test"}
        elif url.startswith("https://api.fireflies.ai/v2/transcripts"):
            payload = {
                "transcripts": [{
                    "id": "meeting-1",
                    "title": "Client kickoff",
                    "date": "2026-08-19T00:00:00Z",
                    "duration": 1800,
                    "participants": [{"name": "Alice"}],
                    "summary": {"short": "Kickoff notes"},
                }],
            }
        else:
            raise AssertionError(f"unexpected Fireflies request: {method} {url}")
        return HttpResponse(200, {}, json.dumps(payload).encode())


class FirefliesIntegrationWiringTests(unittest.TestCase):
    def setUp(self):
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency", "org_fireflies")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client", "ws_fireflies")
        self.person = self.os.create_person(self.org.id, "Owner", "owner@fireflies.test", role="owner", person_id="person_fireflies")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.os.create_actor(self.ws.id, "Fireflies", "admin", "actor_fireflies")
        _, self.identity = issue_identity(self.os, self.org.id, self.person.id, self.ws.id, "actor_fireflies")
        environment.environ["AUREMGRID_TEST_FIREFLIES"] = "fireflies-key"

    def tearDown(self):
        environment.environ.pop("AUREMGRID_TEST_FIREFLIES", None)
        self.os.close()

    def test_single_account_lifecycle_cursor_and_idempotent_restart(self):
        permissions = sorted(FIREFLIES_REQUIRED_SCOPES)
        integration = self.os.integrations.configure(
            self.identity, "fireflies", "user-1", {"account:user-1": self.ws.id}, permissions,
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Fireflies key", "env:AUREMGRID_TEST_FIREFLIES",
            ["connector:fireflies", *permissions],
        )
        state = {"seen": False}

        def factory(mode, _source, _secret, *args):
            if mode == "verify":
                return {"account_id": "user-1", "account_name": "Owner", "granted_permissions": permissions}
            route, workspace, cursor, _runtime = args
            if state["seen"]:
                return [], cursor, False, {"lifecycle_mutations": ()}
            state["seen"] = True
            dedupe = "meeting-1:v1"
            event = ConnectorSourceEvent(
                dedupe, "fireflies/meetings/meeting-1", "transcript", "fireflies/meetings/meeting-1",
                "https://fireflies.test/rec/1", "transcript body",
                {"route_keys": [route], "workspace_ids": [workspace]}, "2026-08-19T00:00:00+00:00",
            )
            mutation = RouteLifecycleMutation(
                "fireflies/meetings/meeting-1", route, workspace, "upsert", "2026-08-19T00:00:00Z", dedupe,
            )
            return [event], '{"v":1,"meeting_id":"meeting-1","provider_date":"2026-08-19T00:00:00Z"}', False, {"lifecycle_mutations": (mutation,)}

        self.os.integrations.connector_factory = factory
        with patch("auremgrid.services.integration_ops.LIVE_SOURCES", frozenset({"fireflies"})):
            self.os.integrations.verify(self.identity, integration["id"])
            self.os.integrations.sync(self.identity, integration["id"])
            first = self.os.integrations.get(self.identity, integration["id"])
            self.assertEqual(first["object_count"], 1)
            self.os.integrations.sync(self.identity, integration["id"])
            self.assertEqual(self.os.integrations.get(self.identity, integration["id"])["object_count"], 1)

        rows = self.os.store.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE workspace_id=? AND source_key=?",
            (self.ws.id, "fireflies/meetings/meeting-1"),
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(
            self.os.store.conn.execute(
                "SELECT COUNT(*) FROM provider_object_routes WHERE connector='fireflies' AND status='active'",
            ).fetchone()[0],
            1,
        )

    def test_requires_transcripts_read_permission_exactly(self):
        with self.assertRaises(ValidationError):
            self.os.integrations.configure(self.identity, "fireflies", "user-1", {"account:user-1": self.ws.id}, [])
        with self.assertRaises(ValidationError):
            self.os.integrations.configure(
                self.identity, "fireflies", "user-1", {"account:user-1": self.ws.id},
                ["transcripts:read", "extra:scope"],
            )

    def test_rejects_multiple_or_malformed_account_mappings(self):
        with self.assertRaises(ValidationError):
            self.os.integrations.configure(
                self.identity, "fireflies", "user-1",
                {"account:user-1": self.ws.id, "account:user-2": self.ws.id},
                sorted(FIREFLIES_REQUIRED_SCOPES),
            )
        with self.assertRaises(ValidationError):
            self.os.integrations.configure(
                self.identity, "fireflies", "user-1", {"team:user-1": self.ws.id},
                sorted(FIREFLIES_REQUIRED_SCOPES),
            )

    def test_real_adapter_uses_provider_shape_and_advances_cursor(self):
        permissions = sorted(FIREFLIES_REQUIRED_SCOPES)
        integration = self.os.integrations.configure(
            self.identity, "fireflies", "user-1", {"account:user-1": self.ws.id}, permissions,
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Fireflies key", "env:AUREMGRID_TEST_FIREFLIES",
            ["connector:fireflies", *permissions],
        )
        transport = FirefliesApiTransport()

        with patch("auremgrid.connectors.fireflies.HttpTransport", return_value=transport):
            verified = self.os.integrations.verify(self.identity, integration["id"])
            self.assertEqual(verified["integration"]["status"], "authorized")
            self.os.integrations.sync(self.identity, integration["id"])
            self.os.integrations.sync(self.identity, integration["id"])

        self.assertTrue(any(url.startswith("https://api.fireflies.ai/v2/transcripts") for url in transport.calls))
        source_rows = self.os.store.conn.execute(
            "SELECT workspace_id,source_key FROM sources ORDER BY source_key",
        ).fetchall()
        self.assertEqual(
            [(row["workspace_id"], row["source_key"]) for row in source_rows],
            [(self.ws.id, "fireflies/meetings/meeting-1")],
        )
        self.assertEqual(self.os.integrations.get(self.identity, integration["id"])["object_count"], 1)

    def test_cross_workspace_meeting_overlap_quarantines_without_workspace_evidence(self):
        # Fireflies allows exactly one account:<id> mapping, so an overlap can
        # only be forged by a misbehaving/compromised transport emitting an
        # event whose workspace does not match the owned route.
        permissions = sorted(FIREFLIES_REQUIRED_SCOPES)
        integration = self.os.integrations.configure(
            self.identity, "fireflies", "user-1", {"account:user-1": self.ws.id}, permissions,
        )
        self.os.integrations.bind_credential(
            self.identity, integration["id"], "Fireflies key", "env:AUREMGRID_TEST_FIREFLIES",
            ["connector:fireflies", *permissions],
        )

        def factory(mode, _source, _secret, *args):
            if mode == "verify":
                return {"account_id": "user-1", "account_name": "Owner", "granted_permissions": permissions}
            route, workspace, _cursor, _runtime = args
            dedupe = "meeting-x:overlap"
            event = ConnectorSourceEvent(
                dedupe, "fireflies/meetings/meeting-x", "transcript", "fireflies/meetings/meeting-x",
                "https://fireflies.test/rec/x", "must not be ingested",
                {"route_keys": [route], "workspace_ids": ["not-the-mapped-workspace"]},
            )
            mutation = RouteLifecycleMutation("fireflies/meetings/meeting-x", route, workspace, "upsert", "1", dedupe)
            return [event], None, False, {"lifecycle_mutations": (mutation,)}

        self.os.integrations.connector_factory = factory
        self.os.integrations.verify(self.identity, integration["id"])
        with self.assertRaises(ConnectorTransportError):
            self.os.integrations.sync(self.identity, integration["id"])
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM connector_source_events").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
        quarantine = self.os.store.conn.execute("SELECT * FROM provider_sync_quarantines").fetchall()
        self.assertEqual(len(quarantine), 1)
        self.assertNotIn("must not be ingested", str(dict(quarantine[0])))


if __name__ == "__main__":
    unittest.main()

