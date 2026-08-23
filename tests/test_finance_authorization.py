from __future__ import annotations

import math
import unittest

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS


class FinanceAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws_one = self.os.create_organization_workspace(self.org.id, "Prime", "client")
        self.ws_two = self.os.create_organization_workspace(self.org.id, "Second", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.admin = self.os.create_person(self.org.id, "Admin", role="admin")
        self.member = self.os.create_person(self.org.id, "Member", role="member")
        for person in (self.owner, self.admin, self.member):
            self.os.add_person_to_workspace(self.org.id, self.ws_one.id, person.id, "operator")
        self.os.add_person_to_workspace(self.org.id, self.ws_two.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws_two.id, self.admin.id, "operator")
        self.os.agency_ops.connect_finance(self.org.id, self.owner.id, "test-ledger")

    def tearDown(self) -> None:
        self.os.close()

    def _org_mutations(self, person_id: str):
        return (
            lambda: self.os.agency_ops.record_revenue(
                self.org.id, None, person_id, 100, "2026-08-01", "ledger"
            ),
            lambda: self.os.agency_ops.record_cost(
                self.org.id, None, person_id, 100, "labor", "2026-08-01", "ledger"
            ),
            lambda: self.os.agency_ops.record_budget(
                self.org.id, None, person_id, 100, "2026-08-01", "2026-08-31"
            ),
            lambda: self.os.agency_ops.record_software_cost(
                self.org.id, None, person_id, "Suite", 100, "2026-08-01", "ledger"
            ),
            lambda: self.os.agency_ops.record_ai_usage_cost(
                self.org.id, None, person_id, "provider", "model", 10, 100,
                "2026-08-01", "ledger",
            ),
        )

    def test_org_level_finance_writes_require_owner_or_admin(self) -> None:
        for index, mutation in enumerate(self._org_mutations(self.member.id)):
            with self.subTest(mutation=index):
                with self.assertRaisesRegex(AuthorizationError, "organization admin required"):
                    mutation()

        for person_id in (self.owner.id, self.admin.id):
            for index, mutation in enumerate(self._org_mutations(person_id)):
                with self.subTest(person=person_id, mutation=index):
                    item = mutation()
                    self.assertIsNone(item["workspace_id"])

        counts = {
            table: self.os.store.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NULL").fetchone()[0]
            for table in ("revenues", "costs", "budgets", "software_costs", "ai_usage_costs")
        }
        self.assertEqual(counts, {table: 2 for table in counts})

    def test_workspace_finance_writes_keep_workspace_scope(self) -> None:
        mutations = (
            lambda: self.os.agency_ops.record_revenue(
                self.org.id, self.ws_one.id, self.member.id, 100, "2026-08-01", "ledger"
            ),
            lambda: self.os.agency_ops.record_cost(
                self.org.id, self.ws_one.id, self.member.id, 100, "labor", "2026-08-01", "ledger"
            ),
            lambda: self.os.agency_ops.record_budget(
                self.org.id, self.ws_one.id, self.member.id, 100, "2026-08-01", "2026-08-31"
            ),
            lambda: self.os.agency_ops.record_software_cost(
                self.org.id, self.ws_one.id, self.member.id, "Suite", 100, "2026-08-01", "ledger"
            ),
            lambda: self.os.agency_ops.record_ai_usage_cost(
                self.org.id, self.ws_one.id, self.member.id, "provider", "model", 10, 100,
                "2026-08-01", "ledger",
            ),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=index):
                self.assertEqual(mutation()["workspace_id"], self.ws_one.id)

        cross_workspace = (
            lambda: self.os.agency_ops.record_revenue(
                self.org.id, self.ws_two.id, self.member.id, 100, "2026-08-01", "ledger"
            ),
            lambda: self.os.agency_ops.record_cost(
                self.org.id, self.ws_two.id, self.member.id, 100, "labor", "2026-08-01", "ledger"
            ),
            lambda: self.os.agency_ops.record_budget(
                self.org.id, self.ws_two.id, self.member.id, 100, "2026-08-01", "2026-08-31"
            ),
            lambda: self.os.agency_ops.record_software_cost(
                self.org.id, self.ws_two.id, self.member.id, "Suite", 100, "2026-08-01", "ledger"
            ),
            lambda: self.os.agency_ops.record_ai_usage_cost(
                self.org.id, self.ws_two.id, self.member.id, "provider", "model", 10, 100,
                "2026-08-01", "ledger",
            ),
        )
        for index, mutation in enumerate(cross_workspace):
            with self.subTest(cross_workspace_mutation=index):
                with self.assertRaises(AuthorizationError):
                    mutation()

    def test_invalid_finance_amounts_are_rejected_for_every_mutation(self) -> None:
        for amount in (-1, math.nan, math.inf, "not-a-number"):
            mutations = (
                lambda: self.os.agency_ops.record_revenue(
                    self.org.id, None, self.owner.id, amount, "2026-08-01", "ledger"
                ),
                lambda: self.os.agency_ops.record_cost(
                    self.org.id, None, self.owner.id, amount, "labor", "2026-08-01", "ledger"
                ),
                lambda: self.os.agency_ops.record_budget(
                    self.org.id, None, self.owner.id, amount, "2026-08-01", "2026-08-31"
                ),
                lambda: self.os.agency_ops.record_software_cost(
                    self.org.id, None, self.owner.id, "Suite", amount, "2026-08-01", "ledger"
                ),
                lambda: self.os.agency_ops.record_ai_usage_cost(
                    self.org.id, None, self.owner.id, "provider", "model", 10, amount,
                    "2026-08-01", "ledger",
                ),
            )
            for index, mutation in enumerate(mutations):
                with self.subTest(amount=amount, mutation=index):
                    with self.assertRaises(ValidationError):
                        mutation()

    def test_finance_dates_status_sources_and_projects_are_validated(self) -> None:
        with self.assertRaisesRegex(ValidationError, "cost incurred date"):
            self.os.agency_ops.record_cost(self.org.id, None, self.owner.id, 100, "labor", " ", "ledger")
        with self.assertRaisesRegex(ValidationError, "budget period start"):
            self.os.agency_ops.record_budget(self.org.id, None, self.owner.id, 100, "", "2026-08-31")
        with self.assertRaisesRegex(ValidationError, "software cost period start"):
            self.os.agency_ops.record_software_cost(self.org.id, None, self.owner.id, "Suite", 100, "", "ledger")
        with self.assertRaisesRegex(ValidationError, "AI usage date"):
            self.os.agency_ops.record_ai_usage_cost(
                self.org.id, None, self.owner.id, "provider", "model", 10, 100, "", "ledger"
            )
        with self.assertRaisesRegex(ValidationError, "invalid invoice status"):
            self.os.agency_ops.record_invoice(
                self.org.id, self.ws_one.id, self.owner.id, 100,
                "2026-08-01", "2026-08-31", "ledger", status="unknown",
            )
        with self.assertRaisesRegex(ValidationError, "finance currency and source"):
            self.os.agency_ops.record_revenue(self.org.id, None, self.owner.id, 100, "2026-08-01", " ")
        with self.assertRaises(NotFoundError):
            self.os.agency_ops.record_budget(
                self.org.id, None, self.owner.id, 100, "2026-08-01", "2026-08-31", project_id="project"
            )


if __name__ == "__main__":
    unittest.main()
