from __future__ import annotations

import json
import threading
import unittest
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.connectors.financial import MetaAdsReadOnlyAdapter, StripeReadOnlyAdapter
from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class ProviderImportsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Imports")
        self.ws = self.os.create_organization_workspace(self.org.id, "Workspace", "client")
        self.other_ws = self.os.create_organization_workspace(self.org.id, "Other", "client")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.other_ws.id, self.person.id, "admin")
        self.os.agency_ops.connect_finance(self.org.id, self.person.id, "stripe_accounting")
        self.identity = AuthenticatedIdentity(
            "p", self.org.id, self.person.id, "service",
            frozenset({"integration_sync", "workspace_write"}), workspace_id=self.ws.id,
        )

    def tearDown(self) -> None:
        self.os.close()

    def state_counts(self) -> dict[str, int]:
        return {
            table: self.os.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "provider_import_cursors",
                "provider_import_records",
                "provider_import_quarantines",
                "invoices",
                "campaign_metric_snapshots",
                "ledger_audit",
            )
        }

    def test_preview_normalizes_without_persisting_provider_or_canonical_state(self) -> None:
        before = self.state_counts()
        adapter = StripeReadOnlyAdapter(lambda **_: {
            "data": [{"id": "in_preview", "amount": 100, "currency": "usd", "created": 1700000000, "due_at": "2023-11-20T22:13:20+00:00"}],
            "next_cursor": "next-preview",
        })
        result = self.os.provider_imports.preview(self.identity, "stripe_accounting", "acct", {"acct": self.ws.id}, "invoices", adapter=adapter)
        self.assertEqual(result["status"], "preview_valid")
        self.assertFalse(result["persisted"])
        self.assertEqual(result["would_import"], 1)
        self.assertEqual(result["canonical_would_write"], 1)
        self.assertEqual(result["cursor_after"], "next-preview")
        self.assertEqual(result["records"][0]["external_id"], "in_preview")
        self.assertEqual(self.state_counts(), before)

    def test_preview_quarantine_details_are_returned_not_persisted(self) -> None:
        before = self.state_counts()
        adapter = StripeReadOnlyAdapter(lambda **_: {"data": [{"amount": 1, "currency": "usd"}]})
        result = self.os.provider_imports.preview(self.identity, "stripe_accounting", "acct", {"acct": self.ws.id}, "invoices", adapter=adapter)
        self.assertEqual(result["status"], "preview_degraded")
        self.assertEqual(result["quarantined"], 1)
        self.assertIn("provider record id is required", result["quarantine_details"][0]["error"])
        self.assertEqual(self.state_counts(), before)

    def test_http_preview_without_injected_transport_does_not_create_cursor(self) -> None:
        self.os.create_actor(self.ws.id, "Import admin", "admin", "actor_import_admin")
        token, _ = issue_identity(self.os, self.org.id, self.person.id, self.ws.id, "actor_import_admin")
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            connection = HTTPConnection(host, port, timeout=5)
            payload = json.dumps({
                "provider": "stripe_accounting",
                "account_id": "acct",
                "workspace_mappings": {"acct": self.ws.id},
                "resource": "invoices",
            })
            connection.request("POST", "/provider-imports/preview", body=payload, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            })
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "preview_not_connected")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_import_cursors").fetchone()[0], 0)

    def test_import_replay_and_conflict_quarantine(self) -> None:
        pages = [{"data": [{"id": "in_1", "amount": 100, "currency": "usd", "created": 1700000000, "due_at": "2023-11-20T22:13:20+00:00"}]}]
        adapter = StripeReadOnlyAdapter(lambda **_: pages[0])
        first = self.os.provider_imports.pull(self.identity, "stripe_accounting", "acct", {"acct": self.ws.id}, "invoices", adapter=adapter)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["canonical_written"], 1)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 1)
        replay = self.os.provider_imports.pull(self.identity, "stripe_accounting", "acct", {"acct": self.ws.id}, "invoices", adapter=adapter)
        self.assertEqual(replay["duplicates"], 1)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 1)
        conflict = StripeReadOnlyAdapter(lambda **_: {"data": [{"id": "in_1", "amount": 999, "currency": "usd", "created": 1700000000, "due_at": "2023-11-20T22:13:20+00:00"}]})
        result = self.os.provider_imports.pull(self.identity, "stripe_accounting", "acct", {"acct": self.ws.id}, "invoices", adapter=conflict)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_import_quarantines").fetchone()[0], 1)

    def test_unverified_sync_persists_not_connected_without_claiming_configured(self) -> None:
        result = self.os.provider_imports.pull(self.identity, "stripe_accounting", "acct", {"acct": self.ws.id}, "invoices")
        self.assertEqual(result["status"], "not_connected")
        row = self.os.store.conn.execute("SELECT status FROM provider_import_cursors").fetchone()
        self.assertEqual(row["status"], "not_connected")

    def test_malformed_adapter_quarantine_details_persist(self) -> None:
        adapter = StripeReadOnlyAdapter(lambda **_: {"data": [{"amount": 1, "currency": "usd"}]})
        result = self.os.provider_imports.pull(self.identity, "stripe_accounting", "acct", {"acct": self.ws.id}, "invoices", adapter=adapter)
        self.assertEqual(result["quarantined"], 1)
        row = self.os.store.conn.execute("SELECT quarantine_details FROM provider_import_quarantines").fetchone()
        self.assertIn("provider record id is required", row["quarantine_details"])

    def test_workspace_scoped_identity_cannot_import_other_workspace_mapping(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.provider_imports.pull(self.identity, "stripe_accounting", "acct", {"acct": self.other_ws.id}, "invoices")

    def test_meta_insights_without_canonical_campaign_mapping_are_unsupported(self) -> None:
        adapter = MetaAdsReadOnlyAdapter(lambda **_: {"data": [{"id": "insight_1", "spend": 25, "date_start": "2026-08-01"}]})
        result = self.os.provider_imports.pull(self.identity, "meta_ads", "acct", {"acct": self.ws.id}, "insights", adapter=adapter)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["unsupported"], 1)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM campaign_metric_snapshots").fetchone()[0], 0)
        row = self.os.store.conn.execute("SELECT reason FROM provider_import_quarantines").fetchone()
        self.assertEqual(row["reason"], "unsupported_without_campaign_mapping")


if __name__ == "__main__":
    unittest.main()
