from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.billing_read import BillingReadService


class BillingReadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Path(self.tempdir.name) / "billing.json"
        self.storage.write_text(json.dumps({
            "clients": [
                {"organization_id": "org-1", "id": "client-a", "name": "Acme Co"},
                {"organization_id": "org-1", "id": "client-b", "name": "Beta Studio"},
                {"organization_id": "org-2", "id": "client-x", "name": "Other Org"},
            ],
            "invoices": [
                {"organization_id": "org-1", "client_id": "client-a", "id": "inv-1", "due_date": "2026-08-28", "balance_due": "100.00", "status": "open"},
                {"organization_id": "org-1", "client_id": "client-a", "id": "inv-2", "due_date": "2026-07-24", "balance_due": "200.00", "status": "open"},
                {"organization_id": "org-1", "client_id": "client-a", "id": "inv-3", "due_date": "2026-06-09", "amount": "300.00", "paid_amount": "25.00", "status": "open"},
                {"organization_id": "org-1", "client_id": "client-a", "id": "inv-4", "due_date": "2026-05-01", "balance_due": "400.00", "status": "open"},
                {"organization_id": "org-1", "client_id": "client-a", "id": "inv-paid", "due_date": "2026-08-01", "balance_due": "999.00", "status": "paid"},
                {"organization_id": "org-1", "client_id": "client-b", "id": "inv-b", "due_date": "2026-09-20", "balance_due": "50.00", "status": "open"},
                {"organization_id": "org-2", "client_id": "client-x", "id": "inv-x", "due_date": "2026-08-01", "balance_due": "777.00", "status": "open"},
            ],
            "payments": [
                {"organization_id": "org-1", "client_id": "client-a", "invoice_id": "inv-1", "id": "pay-old", "paid_at": "2026-08-30T10:00:00Z", "amount": "40.00", "method": "ach"},
                {"organization_id": "org-1", "client_id": "client-a", "invoice_id": "inv-2", "id": "pay-new", "paid_at": "2026-09-05T10:00:00Z", "amount": "60.50", "method": "card"},
                {"organization_id": "org-1", "client_id": "client-b", "invoice_id": "inv-b", "id": "pay-b", "paid_at": "2026-09-01T10:00:00Z", "amount": "25.00", "method": "ach"},
                {"organization_id": "org-2", "client_id": "client-x", "invoice_id": "inv-x", "id": "pay-x", "paid_at": "2026-09-06T10:00:00Z", "amount": "999.00", "method": "wire"},
            ],
        }), encoding="utf-8")
        self.service = BillingReadService(
            self.storage,
            clock=lambda: datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_receivables_summary_returns_org_scoped_aging_per_client(self) -> None:
        summaries = self.service.receivables_summary("org-1")
        self.assertEqual([summary["client_id"] for summary in summaries], ["client-a", "client-b"])
        self.assertEqual(summaries[0]["open_balance"], 975.0)
        self.assertEqual(summaries[0]["overdue_count"], 4)
        self.assertEqual(summaries[0]["aging_buckets"], {
            "0-30": 100.0,
            "31-60": 200.0,
            "61-90": 275.0,
            "90+": 400.0,
        })
        self.assertEqual(summaries[1]["open_balance"], 50.0)
        self.assertEqual(summaries[1]["overdue_count"], 0)

    def test_payment_history_is_newest_first_and_client_org_fenced(self) -> None:
        payments = self.service.payment_history("org-1", "client-a")
        self.assertEqual([payment["id"] for payment in payments], ["pay-new", "pay-old"])
        self.assertEqual(payments[0]["amount"], 60.5)
        self.assertEqual({payment["organization_id"] for payment in payments}, {"org-1"})
        with self.assertRaises(AuthorizationError):
            self.service.payment_history("org-1", "client-x")

    def test_revenue_by_month_rolls_up_org_scoped_payments(self) -> None:
        revenue = self.service.revenue_by_month("org-1")
        self.assertEqual(revenue, [
            {"organization_id": "org-1", "month": "2026-08", "revenue": 40.0},
            {"organization_id": "org-1", "month": "2026-09", "revenue": 85.5},
        ])

    def test_read_validation_fails_closed_for_bad_storage_and_missing_client(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.receivables_summary("")
        with self.assertRaises(NotFoundError):
            self.service.payment_history("org-1", "missing")
        self.storage.write_text(json.dumps({"clients": [], "invoices": {}, "payments": []}), encoding="utf-8")
        with self.assertRaises(ValidationError):
            self.service.receivables_summary("org-1")

    def test_service_exposes_no_mutation_methods(self) -> None:
        public_methods = {name for name in dir(self.service) if not name.startswith("_") and callable(getattr(self.service, name))}
        self.assertFalse(public_methods & {"create_invoice", "update_invoice", "delete_invoice", "sync", "write"})


if __name__ == "__main__":
    unittest.main()
