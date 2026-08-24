from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.connectors.financial import CRMReadOnlyAdapter, GoogleAdsReadOnlyAdapter, MetaAdsReadOnlyAdapter, StripeReadOnlyAdapter
from auremgrid.domain.errors import AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS
from tests.auth_support import LATEST_SCHEMA_VERSION, issue_identity


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
                "contacts",
                "opportunities",
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

    def test_google_ads_preview_normalizes_without_persisting_or_claiming_connection(self) -> None:
        before = self.state_counts()
        adapter = GoogleAdsReadOnlyAdapter(lambda **_: {"data": [{
            "campaign": {"id": "123", "name": "Search Leads"},
            "segments": {"date": "2026-08-20"},
            "metrics": {"costMicros": 2500000, "clicks": 20, "impressions": 1000, "conversions": 3},
            "customer": {"currencyCode": "usd"},
        }], "next_cursor": "next-google"})
        result = self.os.provider_imports.preview(
            self.identity, "google_ads", "acct", {"acct": self.ws.id}, "metrics", adapter=adapter
        )
        self.assertEqual(result["status"], "preview_degraded")
        self.assertFalse(result["persisted"])
        self.assertEqual(result["would_import"], 1)
        self.assertEqual(result["canonical_would_write"], 0)
        self.assertEqual(result["unsupported"], 1)
        self.assertEqual(result["records"][0]["provider"], "google_ads")
        self.assertEqual(result["records"][0]["amount"], 2.5)
        self.assertEqual(result["cursor_after"], "next-google")
        self.assertEqual(self.state_counts(), before)

    def test_google_ads_unconfigured_sync_records_not_connected_cursor_only(self) -> None:
        result = self.os.provider_imports.pull(
            self.identity, "google_ads", "acct", {"acct": self.ws.id}, "metrics"
        )
        self.assertEqual(result["status"], "not_connected")
        row = self.os.store.conn.execute(
            "SELECT provider,status FROM provider_import_cursors WHERE provider='google_ads'"
        ).fetchone()
        self.assertEqual(row["status"], "not_connected")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_import_records WHERE provider='google_ads'").fetchone()[0], 0)

    def test_google_ads_metrics_require_canonical_campaign_mapping_for_canonical_write(self) -> None:
        campaign = self.os.agency_ops.create_campaign(
            self.org.id, self.ws.id, self.person.id, "Search Leads", "leads", "google_ads"
        )
        adapter = GoogleAdsReadOnlyAdapter(lambda **_: {"data": [{
            "id": "metric_1",
            "canonical_campaign_id": campaign["id"],
            "segments": {"date": "2026-08-20"},
            "metrics": {"costMicros": 2500000, "clicks": 20, "impressions": 1000, "conversions": 3},
            "customer": {"currencyCode": "usd"},
        }]})
        result = self.os.provider_imports.pull(
            self.identity, "google_ads", "acct", {"acct": self.ws.id}, "metrics", adapter=adapter
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["canonical_written"], 1)
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM provider_import_records WHERE provider='google_ads'").fetchone()[0], 1
        )
        metric = self.os.store.conn.execute(
            "SELECT spend,clicks,impressions,leads,source FROM campaign_metric_snapshots WHERE campaign_id=?",
            (campaign["id"],),
        ).fetchone()
        self.assertEqual(metric["spend"], 2.5)
        self.assertEqual(metric["clicks"], 20)
        self.assertEqual(metric["impressions"], 1000)
        self.assertEqual(metric["leads"], 3)
        self.assertEqual(metric["source"], "google_ads:metrics:metric_1")

    def test_google_ads_metrics_without_canonical_campaign_mapping_are_unsupported(self) -> None:
        adapter = GoogleAdsReadOnlyAdapter(lambda **_: {"data": [{
            "id": "metric_no_mapping",
            "segments": {"date": "2026-08-20"},
            "metrics": {"costMicros": 2500000},
        }]})
        result = self.os.provider_imports.pull(
            self.identity, "google_ads", "acct", {"acct": self.ws.id}, "metrics", adapter=adapter
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["unsupported"], 1)
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM campaign_metric_snapshots").fetchone()[0], 0
        )
        row = self.os.store.conn.execute("SELECT reason FROM provider_import_quarantines WHERE provider='google_ads'").fetchone()
        self.assertEqual(row["reason"], "unsupported_without_campaign_mapping")

    def test_crm_preview_maps_contacts_without_persisting(self) -> None:
        before = self.state_counts()
        adapter = CRMReadOnlyAdapter(lambda **_: {"data": [{
            "id": "contact_1",
            "name": "Avery Chen",
            "company": "Northwind",
            "role": "COO",
            "influence": "high",
            "decision_power": "final",
            "preferences": ["email"],
        }], "next_cursor": "crm-preview"})
        result = self.os.provider_imports.preview(
            self.identity, "crm", "acct", {"acct": self.ws.id}, "contacts", adapter=adapter
        )
        self.assertEqual(result["status"], "preview_valid")
        self.assertEqual(result["would_import"], 1)
        self.assertEqual(result["canonical_would_write"], 1)
        self.assertFalse(result["persisted"])
        self.assertEqual(result["records"][0]["provider"], "crm")
        self.assertEqual(result["cursor_after"], "crm-preview")
        self.assertEqual(self.state_counts(), before)

    def test_crm_sync_contact_replay_does_not_duplicate_canonical_contact(self) -> None:
        adapter = CRMReadOnlyAdapter(lambda **_: {"data": [{
            "id": "contact_1",
            "name": "Avery Chen",
            "company": "Northwind",
            "role": "COO",
        }]})
        first = self.os.provider_imports.pull(
            self.identity, "crm", "acct", {"acct": self.ws.id}, "contacts", adapter=adapter
        )
        replay = self.os.provider_imports.pull(
            self.identity, "crm", "acct", {"acct": self.ws.id}, "contacts", adapter=adapter
        )
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["canonical_written"], 1)
        self.assertEqual(replay["duplicates"], 1)
        self.assertEqual(replay["canonical_written"], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_import_records WHERE provider='crm'").fetchone()[0], 1)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)

    def test_crm_sync_opportunity_writes_existing_sales_record(self) -> None:
        adapter = CRMReadOnlyAdapter(lambda **_: {"data": [{
            "id": "deal_1",
            "type": "scope_expansion",
            "name": "Expand analytics retainer",
            "recommendation": "Prepare expansion proposal",
            "amount": 4500,
            "stage": "qualified",
        }]})
        result = self.os.provider_imports.pull(
            self.identity, "crm", "acct", {"acct": self.ws.id}, "opportunities", adapter=adapter
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["canonical_written"], 1)
        row = self.os.store.conn.execute("SELECT type,estimated_value,reason,evidence,recommendation FROM opportunities").fetchone()
        self.assertEqual(row["type"], "scope_expansion")
        self.assertEqual(row["estimated_value"], 4500)
        self.assertEqual(row["reason"], "Expand analytics retainer")
        self.assertIn("crm:opportunities:deal_1", row["evidence"])
        self.assertEqual(row["recommendation"], "Prepare expansion proposal")

    def test_crm_invalid_canonical_contact_is_quarantined_without_contact_write(self) -> None:
        adapter = CRMReadOnlyAdapter(lambda **_: {"data": [{
            "id": "contact_missing_company",
            "name": "Avery Chen",
            "role": "COO",
        }]})
        result = self.os.provider_imports.pull(
            self.identity, "crm", "acct", {"acct": self.ws.id}, "contacts", adapter=adapter
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["canonical_written"], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)
        row = self.os.store.conn.execute("SELECT reason,quarantine_details FROM provider_import_quarantines WHERE provider='crm'").fetchone()
        self.assertEqual(row["reason"], "canonical_write_rejected")
        self.assertIn("crm contact requires company", row["quarantine_details"])

    def test_crm_unconfigured_sync_records_not_connected_cursor_only(self) -> None:
        result = self.os.provider_imports.pull(
            self.identity, "crm", "acct", {"acct": self.ws.id}, "contacts"
        )
        self.assertEqual(result["status"], "not_connected")
        row = self.os.store.conn.execute("SELECT status FROM provider_import_cursors WHERE provider='crm'").fetchone()
        self.assertEqual(row["status"], "not_connected")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM provider_import_records WHERE provider='crm'").fetchone()[0], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 0)

    def test_provider_import_migration_replay_preserves_rows_and_allows_google_ads_and_crm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "imports.sqlite"
            first = CompanyOS(path)
            org = first.create_organization("Replay Imports")
            ws = first.create_organization_workspace(org.id, "Workspace", "client")
            person = first.create_person(org.id, "Owner", role="owner")
            first.add_person_to_workspace(org.id, ws.id, person.id, "admin")
            identity = AuthenticatedIdentity(
                "p", org.id, person.id, "service", frozenset({"integration_sync", "workspace_write"}), workspace_id=ws.id
            )
            stripe = StripeReadOnlyAdapter(lambda **_: {
                "data": [{"id": "in_replay", "amount": 100, "currency": "usd", "created": 1700000000, "due_at": "2023-11-20T22:13:20+00:00"}]
            })
            first.provider_imports.pull(identity, "stripe_accounting", "acct", {"acct": ws.id}, "invoices", adapter=stripe)
            first.close()

            conn = sqlite3.connect(path)
            conn.execute("DELETE FROM schema_migrations WHERE version IN (52,53)")
            conn.commit()
            conn.close()

            second = CompanyOS(path)
            self.assertEqual(second.store.schema_version, LATEST_SCHEMA_VERSION)
            self.assertEqual(
                second.store.conn.execute("SELECT COUNT(*) FROM provider_import_records WHERE provider='stripe_accounting'").fetchone()[0],
                1,
            )
            campaign = second.agency_ops.create_campaign(org.id, ws.id, person.id, "Search Leads", "leads", "google_ads")
            google = GoogleAdsReadOnlyAdapter(lambda **_: {"data": [{
                "id": "metric_replay",
                "canonical_campaign_id": campaign["id"],
                "segments": {"date": "2026-08-20"},
                "metrics": {"costMicros": 1000000},
            }]})
            result = second.provider_imports.pull(identity, "google_ads", "acct", {"acct": ws.id}, "metrics", adapter=google)
            self.assertEqual(result["imported"], 1)
            self.assertEqual(
                second.store.conn.execute("SELECT COUNT(*) FROM provider_import_records WHERE provider='google_ads'").fetchone()[0],
                1,
            )
            crm = CRMReadOnlyAdapter(lambda **_: {"data": [{
                "id": "contact_replay",
                "name": "Avery Chen",
                "company": "Northwind",
                "role": "COO",
            }]})
            crm_result = second.provider_imports.pull(identity, "crm", "acct", {"acct": ws.id}, "contacts", adapter=crm)
            self.assertEqual(crm_result["imported"], 1)
            self.assertEqual(
                second.store.conn.execute("SELECT COUNT(*) FROM provider_import_records WHERE provider='crm'").fetchone()[0],
                1,
            )
            second.close()


if __name__ == "__main__":
    unittest.main()
