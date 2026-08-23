from __future__ import annotations

import unittest

from auremgrid.connectors.financial import MetaAdsReadOnlyAdapter, StripeReadOnlyAdapter
from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS


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
