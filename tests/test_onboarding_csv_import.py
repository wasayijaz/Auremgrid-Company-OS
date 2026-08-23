from __future__ import annotations

import unittest

from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS


class OnboardingCsvImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Northwind", "org_northwind")
        self.ws = self.os.create_organization_workspace(self.org.id, "HQ", "internal", "ws_hq")
        self.owner = self.os.create_person(self.org.id, "Ava Owner", "ava@example.test", role="owner", person_id="person_ava")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")

    def tearDown(self) -> None:
        self.os.close()

    def test_preview_quarantines_invalid_rows_and_commit_requires_checkpoint(self) -> None:
        csv_text = "name,workspace_id,kind\nAcme,ws_acme,client\nBad,,internal\n"
        preview = self.os.onboarding.preview_csv_import(
            self.org.id, None, self.owner.id, "client_workspaces", csv_text, "preview-clients-1"
        )

        self.assertEqual(preview["batch"]["status"], "commit_required")
        self.assertEqual(preview["batch"]["valid_rows"], 1)
        self.assertEqual(preview["batch"]["invalid_rows"], 1)
        self.assertEqual(preview["rows"][1]["status"], "quarantined")
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM workspaces WHERE id='ws_acme'").fetchone()[0],
            0,
        )

        committed = self.os.onboarding.commit_csv_import(
            self.org.id, preview["batch"]["id"], self.owner.id, "commit-clients-1"
        )
        self.assertEqual(committed["batch"]["status"], "committed")
        self.assertEqual(
            self.os.store.conn.execute("SELECT name FROM workspaces WHERE id='ws_acme'").fetchone()[0],
            "Acme",
        )
        self.assertIsNotNone(self.os.company.workspace_membership("ws_acme", self.owner.id))

    def test_preview_and_commit_are_idempotent_for_same_keys(self) -> None:
        csv_text = "name,objective,platform,budget\nDemand,Leads,meta,1000\n"
        preview = self.os.onboarding.preview_csv_import(
            self.org.id, self.ws.id, self.owner.id, "campaigns", csv_text, "preview-campaigns-1"
        )
        replayed = self.os.onboarding.preview_csv_import(
            self.org.id, self.ws.id, self.owner.id, "campaigns", csv_text, "preview-campaigns-1"
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(preview["batch"]["id"], replayed["batch"]["id"])

        committed = self.os.onboarding.commit_csv_import(
            self.org.id, preview["batch"]["id"], self.owner.id, "commit-campaigns-1"
        )
        replayed_commit = self.os.onboarding.commit_csv_import(
            self.org.id, preview["batch"]["id"], self.owner.id, "commit-campaigns-1"
        )
        self.assertTrue(replayed_commit["replayed"])
        self.assertEqual(committed["batch"]["status"], "committed")
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM campaigns WHERE workspace_id=?", (self.ws.id,)).fetchone()[0],
            1,
        )

        with self.assertRaisesRegex(ValidationError, "different import content"):
            self.os.onboarding.preview_csv_import(
                self.org.id, self.ws.id, self.owner.id, "campaigns",
                "name,objective,platform\nOther,Leads,meta\n", "preview-campaigns-1",
            )

    def test_campaign_metrics_preview_validates_existing_campaign_and_sources(self) -> None:
        campaign = self.os.agency_ops.create_campaign(
            self.org.id, self.ws.id, self.owner.id, "Demand", "Leads", "meta"
        )
        csv_text = (
            "campaign_id,source,spend,revenue,leads,impressions,clicks\n"
            f"{campaign['id']},manual import,100,400,10,10000,200\n"
            "missing,manual import,1,,,,\n"
            f"{campaign['id']},,1,,,,\n"
        )

        preview = self.os.onboarding.preview_csv_import(
            self.org.id, self.ws.id, self.owner.id, "campaign_metrics", csv_text, "preview-metrics-1"
        )
        self.assertEqual(preview["batch"]["valid_rows"], 1)
        self.assertEqual(preview["batch"]["invalid_rows"], 2)

        committed = self.os.onboarding.commit_csv_import(
            self.org.id, preview["batch"]["id"], self.owner.id, "commit-metrics-1"
        )
        self.assertEqual(committed["batch"]["status"], "committed")
        self.assertEqual(
            self.os.store.conn.execute("SELECT COUNT(*) FROM campaign_metric_snapshots WHERE campaign_id=?", (campaign["id"],)).fetchone()[0],
            1,
        )

    def test_import_tables_are_append_only(self) -> None:
        preview = self.os.onboarding.preview_csv_import(
            self.org.id, self.ws.id, self.owner.id, "campaigns",
            "name,objective,platform\nDemand,Leads,meta\n", "preview-append-1",
        )
        with self.assertRaisesRegex(Exception, "append-only"):
            self.os.store.conn.execute(
                "UPDATE onboarding_import_batches SET total_rows=99 WHERE id=?",
                (preview["batch"]["id"],),
            )


if __name__ == "__main__":
    unittest.main()
