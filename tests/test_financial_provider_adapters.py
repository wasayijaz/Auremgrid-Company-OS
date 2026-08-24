from __future__ import annotations

import unittest

from auremgrid.connectors.financial import CRMReadOnlyAdapter, GoogleAdsReadOnlyAdapter, MetaAdsReadOnlyAdapter, StripeReadOnlyAdapter
from auremgrid.domain.errors import ValidationError


class FinancialProviderAdapterTests(unittest.TestCase):
    def test_unconfigured_provider_is_truthfully_not_connected(self) -> None:
        self.assertEqual(StripeReadOnlyAdapter().status, "not_connected")
        self.assertEqual(MetaAdsReadOnlyAdapter().pull("campaigns", None, "acct", {"acct": "ws"}).records, ())
        self.assertEqual(GoogleAdsReadOnlyAdapter().pull("metrics", None, "acct", {"acct": "ws"}).records, ())
        self.assertEqual(CRMReadOnlyAdapter().pull("contacts", None, "acct", {"acct": "ws"}).records, ())

    def test_stripe_page_normalizes_and_dedupes(self) -> None:
        adapter = StripeReadOnlyAdapter(lambda **_: {"data": [
            {"id": "in_1", "amount_paid": 1200, "currency": "usd", "created": 1700000000},
            {"id": "in_1", "amount_paid": 1200, "currency": "usd", "created": 1700000000},
        ], "next_cursor": "page-2"})
        page = adapter.pull("invoices", None, "acct", {"acct": "ws"})
        self.assertEqual(len(page.records), 1)
        self.assertEqual(page.records[0].amount, 1200.0)
        self.assertEqual(page.next_cursor, "page-2")

    def test_conflicting_duplicate_is_quarantined(self) -> None:
        adapter = MetaAdsReadOnlyAdapter(lambda **_: {"data": [
            {"id": "camp_1", "spend": 10, "date_start": "2026-08-01"},
            {"id": "camp_1", "spend": 20, "date_start": "2026-08-01"},
        ]})
        page = adapter.pull("insights", None, "acct", {"acct": "ws"})
        self.assertEqual(len(page.records), 1)
        self.assertEqual(page.quarantined[0]["reason"], "conflicting_duplicate")

    def test_unmapped_account_fails_closed(self) -> None:
        with self.assertRaises(ValidationError):
            StripeReadOnlyAdapter().pull("invoices", None, "acct", {})

    def test_google_ads_metrics_normalize_nested_fields_and_cost_micros(self) -> None:
        adapter = GoogleAdsReadOnlyAdapter(lambda **_: {"data": [{
            "campaign": {"id": "123", "name": "Search Leads"},
            "segments": {"date": "2026-08-20"},
            "metrics": {"costMicros": 2500000, "clicks": 20, "impressions": 1000, "conversions": 3},
            "customer": {"currencyCode": "usd"},
        }], "next_cursor": "gads-next"})
        page = adapter.pull("metrics", None, "acct", {"acct": "ws"})
        self.assertEqual(page.records[0].provider, "google_ads")
        self.assertEqual(page.records[0].object_type, "metrics")
        self.assertEqual(page.records[0].external_id, "campaign:123:date:2026-08-20")
        self.assertEqual(page.records[0].amount, 2.5)
        self.assertEqual(page.records[0].currency, "USD")
        self.assertEqual(page.next_cursor, "gads-next")

    def test_crm_contacts_and_opportunities_normalize_without_provider_writes(self) -> None:
        calls: list[dict[str, object]] = []

        def transport(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"data": [{
                "contact": {"id": "person_1", "name": "Avery Chen", "title": "COO"},
                "account": {"name": "Northwind"},
                "updated_at": "2026-08-20T10:00:00Z",
                "currency": "usd",
            }], "next_cursor": "crm-next"}

        page = CRMReadOnlyAdapter(transport).pull("contacts", None, "acct", {"acct": "ws"})
        self.assertEqual(calls, [{"resource": "contacts", "cursor": None, "account_id": "acct"}])
        self.assertEqual(page.records[0].provider, "crm")
        self.assertEqual(page.records[0].object_type, "contacts")
        self.assertEqual(page.records[0].external_id, "person_1")
        self.assertEqual(page.records[0].currency, "USD")
        self.assertEqual(page.next_cursor, "crm-next")

        opportunity = CRMReadOnlyAdapter(lambda **_: {"data": [{
            "deal": {"id": "deal_1", "amount": "4200", "currency": "usd", "stage": "qualified"},
            "type": "scope_expansion",
        }]})
        record = opportunity.pull("opportunities", None, "acct", {"acct": "ws"}).records[0]
        self.assertEqual(record.external_id, "deal_1")
        self.assertEqual(record.amount, 4200.0)
        self.assertEqual(record.status, "qualified")


if __name__ == "__main__":
    unittest.main()
