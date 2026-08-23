from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS


class RevenueOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Pipeline", "internal")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.member = self.os.create_person(self.org.id, "Member")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.member.id, "operator")

    def tearDown(self) -> None:
        self.os.close()

    def test_conversion_is_idempotent_and_creates_client_contract(self) -> None:
        prospect = self.os.revenue.create_prospect(self.org.id, self.ws.id, self.owner.id, "Ava", "Acme")
        proposal = self.os.revenue.create_proposal(self.org.id, self.ws.id, self.owner.id, prospect["id"], "Retainer", 1200)
        first = self.os.revenue.convert_to_client(self.org.id, self.ws.id, self.owner.id, proposal["id"], "Acme", idempotency_key="k1")
        second = self.os.revenue.convert_to_client(self.org.id, self.ws.id, self.owner.id, proposal["id"], "Acme", idempotency_key="k1")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["contract"]["workspace_id"], first["client_workspace"]["id"])
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM sales_events").fetchone()[0], 3)

    def test_campaign_pacing_is_honest_when_spend_missing(self) -> None:
        campaign = self.os.agency_ops.create_campaign(self.org.id, self.ws.id, self.owner.id, "Demand", "Leads", "meta", budget=1000)
        signal = self.os.revenue.campaign_budget_pacing(self.org.id, self.ws.id, self.owner.id)[0]
        self.assertEqual(signal["status"], "insufficient_data")
        self.os.agency_ops.record_campaign_metrics(self.org.id, self.ws.id, self.owner.id, campaign["id"], "manual", spend=1100)
        self.assertEqual(self.os.revenue.campaign_budget_pacing(self.org.id, self.ws.id, self.owner.id)[0]["status"], "over_paced")

    def test_forecast_renewal_uses_workspace_scoped_contract_dates(self) -> None:
        end = (datetime.now(timezone.utc) + timedelta(days=20)).date().isoformat()
        self.os.client_ops.create_contract(self.org.id, self.ws.id, self.owner.id, "retainer", "monthly", datetime.now(timezone.utc).date().isoformat(), 500, end_date=end)
        rows = self.os.forecasts.generate_forecasts(self.org.id, self.owner.id, "client_renewal")
        self.assertEqual(rows[0]["forecast_type"], "client_renewal")

    def test_scope_authorization_blocks_outsider(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.revenue.list_prospects(self.org.id, self.ws.id, "outsider")

    def test_conversion_requires_idempotency_key(self) -> None:
        p = self.os.revenue.create_prospect(self.org.id, self.ws.id, self.owner.id, "Ava", "Acme")
        q = self.os.revenue.create_proposal(self.org.id, self.ws.id, self.owner.id, p["id"], "Retainer", 1)
        with self.assertRaises(ValidationError):
            self.os.revenue.convert_to_client(self.org.id, self.ws.id, self.owner.id, q["id"], "Acme", idempotency_key="")


if __name__ == "__main__":
    unittest.main()
