from __future__ import annotations

import sqlite3
import unittest

from auremgrid.connectors.financial import (
    CRMReadOnlyAdapter,
    GA4AnalyticsReadOnlyAdapter,
    GoogleAdsReadOnlyAdapter,
    MetaAdsReadOnlyAdapter,
    SearchConsoleReadOnlyAdapter,
    StripeReadOnlyAdapter,
)
from auremgrid.domain.errors import ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS


class ConnectorSubstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Connector Substance")
        self.ws = self.os.create_organization_workspace(self.org.id, "Workspace", "client")
        self.other_ws = self.os.create_organization_workspace(self.org.id, "Other", "client")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.other_ws.id, self.person.id, "admin")
        self.os.agency_ops.connect_finance(self.org.id, self.person.id, "stripe_accounting")
        self.identity = AuthenticatedIdentity(
            "p",
            self.org.id,
            self.person.id,
            "service",
            frozenset({"integration_sync", "workspace_write"}),
            workspace_id=self.ws.id,
        )

    def tearDown(self) -> None:
        self.os.close()

    def allow_analytics_import_records(self) -> None:
        self.os.store.conn.executescript(
            """
            DROP TRIGGER IF EXISTS provider_import_records_no_update;
            DROP TRIGGER IF EXISTS provider_import_records_no_delete;
            ALTER TABLE provider_import_records RENAME TO provider_import_records_before_analytics_test;
            CREATE TABLE provider_import_records (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                provider TEXT NOT NULL CHECK(provider IN ('stripe_accounting','meta_ads','google_ads','crm','ga4_analytics','search_console')),
                object_type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                occurred_at TEXT,
                amount REAL,
                currency TEXT,
                payload_hash TEXT NOT NULL,
                source TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                UNIQUE(organization_id, provider, object_type, external_id)
            );
            INSERT INTO provider_import_records(
                id,organization_id,workspace_id,provider,object_type,external_id,account_id,
                occurred_at,amount,currency,payload_hash,source,imported_at
            )
            SELECT id,organization_id,workspace_id,provider,object_type,external_id,account_id,
                   occurred_at,amount,currency,payload_hash,source,imported_at
            FROM provider_import_records_before_analytics_test;
            DROP TABLE provider_import_records_before_analytics_test;
            CREATE INDEX IF NOT EXISTS idx_provider_import_records_scope
                ON provider_import_records(organization_id, workspace_id, provider, object_type, imported_at);
            CREATE TRIGGER IF NOT EXISTS provider_import_records_no_update BEFORE UPDATE ON provider_import_records BEGIN
                SELECT RAISE(ABORT, 'provider import records are append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS provider_import_records_no_delete BEFORE DELETE ON provider_import_records BEGIN
                SELECT RAISE(ABORT, 'provider import records are append-only');
            END;
            """
        )

    def test_meta_ads_bad_payload_quarantines_and_reconcile_counts(self) -> None:
        campaign = self.os.agency_ops.create_campaign(self.org.id, self.ws.id, self.person.id, "Meta Leads", "leads", "meta")
        adapter = MetaAdsReadOnlyAdapter(lambda **_: {
            "provider": "meta_ads",
            "account_id": "act_1",
            "scopes": ["ads_read"],
            "data": [
                {"id": "ins_1", "canonical_campaign_id": campaign["id"], "spend": 25, "conversions": 2, "date_start": "2026-08-01"},
                {"id": "ins_1", "canonical_campaign_id": campaign["id"], "spend": 25, "conversions": 2, "date_start": "2026-08-01"},
                {"spend": 9, "date_start": "2026-08-01"},
            ],
            "next_cursor": "meta-cursor-2",
        })
        result = self.os.provider_imports.pull(self.identity, "meta_ads", "act_1", {"act_1": self.ws.id}, "insights", adapter=adapter)
        self.assertTrue(result["verification"]["read_scope_verified"])
        self.assertEqual(result["baseline"], {"provider_count": 3, "record_count": 1, "quarantine_count": 1})
        self.assertEqual(result["reconcile"]["duplicates_on_page"], 1)
        self.assertEqual(result["reconcile"]["canonical_written"], 1)
        self.assertEqual(result["cursor_after"], "meta-cursor-2")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM campaign_metric_snapshots WHERE campaign_id=?", (campaign["id"],)).fetchone()[0], 1)
        quarantine = self.os.store.conn.execute("SELECT reason,quarantine_details FROM provider_import_quarantines WHERE provider='meta_ads'").fetchone()
        self.assertEqual(quarantine["reason"], "invalid_record")
        self.assertIn("provider record id is required", quarantine["quarantine_details"])

    def test_google_ads_scope_and_account_fences_fail_closed_before_import(self) -> None:
        with self.assertRaises(ValidationError):
            GoogleAdsReadOnlyAdapter(lambda **_: {
                "provider": "google_ads",
                "account_id": "wrong",
                "scopes": ["google_ads.readonly"],
                "data": [],
            }).pull("metrics", None, "acct", {"acct": self.ws.id})
        with self.assertRaises(ValidationError):
            GoogleAdsReadOnlyAdapter(lambda **_: {
                "provider": "google_ads",
                "account_id": "acct",
                "scopes": [],
                "data": [],
            }).pull("metrics", None, "acct", {"acct": self.ws.id})

    def test_ga4_metrics_without_campaign_mapping_do_not_fabricate_canonical_values(self) -> None:
        self.allow_analytics_import_records()
        adapter = GA4AnalyticsReadOnlyAdapter(lambda **_: {
            "provider": "ga4_analytics",
            "account_id": "property_1",
            "scopes": ["analytics.readonly"],
            "data": [{"id": "ga4_1", "date": "2026-08-02", "totalRevenue": 300, "sessions": 44}],
        })
        result = self.os.provider_imports.pull(
            self.identity, "ga4_analytics", "property_1", {"property_1": self.ws.id}, "metrics", adapter=adapter
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["unsupported"], 1)
        self.assertEqual(result["canonical_written"], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM campaign_metric_snapshots").fetchone()[0], 0)
        row = self.os.store.conn.execute("SELECT reason FROM provider_import_quarantines WHERE provider='ga4_analytics'").fetchone()
        self.assertEqual(row["reason"], "unsupported_without_campaign_mapping")

    def test_ga4_cursor_replay_dedupes_and_keeps_single_canonical_metric(self) -> None:
        self.allow_analytics_import_records()
        campaign = self.os.agency_ops.create_campaign(self.org.id, self.ws.id, self.person.id, "GA4 Leads", "leads", "ga4")
        adapter = GA4AnalyticsReadOnlyAdapter(lambda **_: {
            "provider": "ga4_analytics",
            "account_id": "property_1",
            "scopes": ["analytics.readonly"],
            "data": [{"id": "ga4_metric_1", "canonical_campaign_id": campaign["id"], "date": "2026-08-02", "totalRevenue": 300, "sessions": 44, "conversions": 5}],
            "next_cursor": "ga4-next",
        })
        first = self.os.provider_imports.pull(
            self.identity, "ga4_analytics", "property_1", {"property_1": self.ws.id}, "metrics", adapter=adapter
        )
        replay = self.os.provider_imports.pull(
            self.identity, "ga4_analytics", "property_1", {"property_1": self.ws.id}, "metrics", cursor=first["cursor_after"], adapter=adapter
        )
        self.assertEqual(first["imported"], 1)
        self.assertEqual(first["canonical_written"], 1)
        self.assertEqual(replay["duplicates"], 1)
        self.assertEqual(replay["canonical_written"], 0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM campaign_metric_snapshots WHERE campaign_id=?", (campaign["id"],)).fetchone()[0], 1)
        cursor = self.os.store.conn.execute("SELECT cursor_value,status FROM provider_import_cursors WHERE provider='ga4_analytics'").fetchone()
        self.assertEqual(cursor["cursor_value"], "ga4-next")
        self.assertEqual(cursor["status"], "configured")

    def test_finance_crm_and_search_console_keep_fences_and_reconcile_totals(self) -> None:
        stripe = StripeReadOnlyAdapter(lambda **_: {
            "provider": "stripe_accounting",
            "account_id": "acct",
            "data": [{"id": "charge_1", "amount": 125, "currency": "usd", "created": "2026-08-01T00:00:00Z"}],
        })
        finance = self.os.provider_imports.pull(self.identity, "stripe_accounting", "acct", {"acct": self.ws.id}, "charges", adapter=stripe)
        self.assertEqual(finance["fence"], {"account_id": "acct", "workspace_id": self.ws.id, "resource": "charges"})
        self.assertEqual(finance["reconcile"]["imported"], 1)

        crm = CRMReadOnlyAdapter(lambda **_: {
            "provider": "crm",
            "account_id": "crm_acct",
            "scopes": ["crm.read"],
            "data": [{"id": "contact_1", "name": "Avery Chen", "company": "Northwind", "role": "COO"}],
        })
        crm_result = self.os.provider_imports.pull(self.identity, "crm", "crm_acct", {"crm_acct": self.ws.id}, "contacts", adapter=crm)
        self.assertEqual(crm_result["reconcile"]["canonical_written"], 1)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)

        self.allow_analytics_import_records()
        campaign = self.os.agency_ops.create_campaign(self.org.id, self.ws.id, self.person.id, "Organic Search", "search", "search_console")
        search = SearchConsoleReadOnlyAdapter(lambda **_: {
            "provider": "search_console",
            "account_id": "site_1",
            "scopes": ["webmasters.readonly"],
            "data": [{"id": "query_1", "canonical_campaign_id": campaign["id"], "date": "2026-08-03", "clicks": 12, "impressions": 400}],
        })
        search_result = self.os.provider_imports.pull(
            self.identity, "search_console", "site_1", {"site_1": self.ws.id}, "queries", adapter=search
        )
        self.assertEqual(search_result["reconcile"]["canonical_written"], 1)
        metric = self.os.store.conn.execute("SELECT clicks,impressions,source FROM campaign_metric_snapshots WHERE campaign_id=?", (campaign["id"],)).fetchone()
        self.assertEqual(metric["clicks"], 12)
        self.assertEqual(metric["impressions"], 400)
        self.assertEqual(metric["source"], "search_console:queries:query_1")

    def test_import_record_fence_quarantines_cross_workspace_reuse(self) -> None:
        adapter = StripeReadOnlyAdapter(lambda **_: {
            "provider": "stripe_accounting",
            "account_id": "acct",
            "data": [{"id": "charge_same", "amount": 125, "currency": "usd", "created": "2026-08-01T00:00:00Z"}],
        })
        self.os.provider_imports.pull(self.identity, "stripe_accounting", "acct", {"acct": self.ws.id}, "charges", adapter=adapter)
        other_identity = AuthenticatedIdentity(
            "p2",
            self.org.id,
            self.person.id,
            "service",
            frozenset({"integration_sync", "workspace_write"}),
            workspace_id=self.other_ws.id,
        )
        other_adapter = StripeReadOnlyAdapter(lambda **_: {
            "provider": "stripe_accounting",
            "account_id": "acct_other",
            "data": [{"id": "charge_same", "amount": 125, "currency": "usd", "created": "2026-08-01T00:00:00Z"}],
        })
        result = self.os.provider_imports.pull(
            other_identity, "stripe_accounting", "acct_other", {"acct_other": self.other_ws.id}, "charges", adapter=other_adapter
        )
        self.assertEqual(result["quarantined"], 1)
        row = self.os.store.conn.execute("SELECT reason FROM provider_import_quarantines WHERE reason='fence_violation'").fetchone()
        self.assertEqual(row["reason"], "fence_violation")


if __name__ == "__main__":
    unittest.main()
