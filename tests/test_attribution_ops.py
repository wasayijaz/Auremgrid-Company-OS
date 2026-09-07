from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import tempfile
import unittest
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, NotFoundError
from auremgrid.services.brain import CompanyOS


class AttributionOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client")
        self.other_ws = self.os.create_organization_workspace(self.org.id, "Other", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", "owner@attrib.test", role="owner")
        self.viewer = self.os.create_person(self.org.id, "Viewer", "viewer@attrib.test", role="member")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.viewer.id, "viewer")
        self.os.add_person_to_workspace(self.org.id, self.other_ws.id, self.owner.id, "admin")

    def tearDown(self) -> None:
        self.os.close()

    def _campaign(self) -> dict:
        return self.os.agency_ops.create_campaign(
            self.org.id,
            self.ws.id,
            self.owner.id,
            "Search Launch",
            "Lead generation",
            "google",
        )

    def _insert_metric(
        self,
        campaign_id: str,
        captured_at: str,
        *,
        spend: float | None = None,
        revenue: float | None = None,
        leads: float | None = None,
        source: str = "manual import",
    ) -> str:
        metric_id = f"metric_{len(captured_at)}_{self.os.store.conn.execute('SELECT COUNT(*) FROM campaign_metric_snapshots').fetchone()[0]}"
        self.os.store.conn.execute(
            """INSERT INTO campaign_metric_snapshots(
                id,organization_id,workspace_id,campaign_id,captured_at,spend,revenue,leads,
                impressions,clicks,cpl,cac,ctr,cvr,roas,source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                metric_id,
                self.org.id,
                self.ws.id,
                campaign_id,
                captured_at,
                spend,
                revenue,
                leads,
                None,
                None,
                round(spend / leads, 4) if spend is not None and leads else None,
                None,
                None,
                None,
                round(revenue / spend, 4) if revenue is not None and spend else None,
                source,
            ),
        )
        self.os.store.conn.commit()
        return metric_id

    def test_plan_creation_stores_sourced_baseline_with_unknown_tolerance(self) -> None:
        campaign = self._campaign()
        self._insert_metric(
            campaign["id"],
            (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat(),
            spend=100,
            revenue=None,
            leads=5,
            source="ads export",
        )
        plan = self.os.attribution.create_attribution_plan(
            self.org.id,
            self.ws.id,
            self.owner.id,
            "campaign",
            campaign["id"],
            ["spend", "revenue", "leads"],
            7,
        )

        self.assertEqual(plan["baseline_summary"], {"known_count": 2, "unknown_count": 1, "metrics": 3})
        baseline = plan["snapshots"][0]
        self.assertEqual(baseline["snapshot_kind"], "baseline")
        values = {item["metric_name"]: item for item in baseline["values"]}
        self.assertEqual(values["spend"]["value"], 100)
        self.assertEqual(values["spend"]["source"], "ads export")
        self.assertEqual(values["revenue"]["status"], "unknown")
        self.assertIsNone(values["revenue"]["value"])

    def test_evaluate_computes_deltas_and_writes_learning_record(self) -> None:
        campaign = self._campaign()
        self._insert_metric(
            campaign["id"],
            (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat(),
            spend=100,
            revenue=300,
            leads=5,
        )
        plan = self.os.attribution.create_attribution_plan(
            self.org.id,
            self.ws.id,
            self.owner.id,
            "campaign",
            campaign["id"],
            ["spend", "revenue", "leads"],
            7,
        )
        self._insert_metric(
            campaign["id"],
            (_parse(plan["evaluation_window_start"]) + timedelta(days=1)).isoformat(),
            spend=120,
            revenue=480,
            leads=8,
            source="ads outcome export",
        )

        evaluated = self.os.attribution.evaluate(self.org.id, self.ws.id, self.owner.id, plan["id"])
        self.assertEqual(evaluated["status"], "evaluated")
        outcome = [snapshot for snapshot in evaluated["snapshots"] if snapshot["snapshot_kind"] == "outcome"][0]
        deltas = {item["metric_name"]: item for item in outcome["deltas"]}
        self.assertEqual(deltas["spend"]["delta"], 20)
        self.assertEqual(deltas["revenue"]["delta"], 180)
        self.assertEqual(deltas["leads"]["delta"], 3)
        learning = evaluated["learning_record"]
        self.assertEqual(learning["subject"], f"campaign:{campaign['id']}")
        self.assertEqual(learning["status"], "resolved")
        self.assertEqual(learning["outcome"]["attribution_id"], plan["id"])
        self.assertEqual(
            self.os.store.conn.execute(
                "SELECT COUNT(*) FROM intelligence_hypotheses WHERE subject=?",
                (f"campaign:{campaign['id']}",),
            ).fetchone()[0],
            1,
        )

    def test_restart_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attribution.sqlite"
            first = CompanyOS(path)
            try:
                org = first.create_organization("Agency")
                ws = first.create_organization_workspace(org.id, "Client", "client")
                owner = first.create_person(org.id, "Owner", role="owner")
                first.add_person_to_workspace(org.id, ws.id, owner.id, "admin")
                campaign = first.agency_ops.create_campaign(org.id, ws.id, owner.id, "Launch", "Leads", "meta")
                first.agency_ops.record_campaign_metrics(org.id, ws.id, owner.id, campaign["id"], "manual", spend=10, revenue=20)
                plan = first.attribution.create_attribution_plan(
                    org.id, ws.id, owner.id, "campaign", campaign["id"], ["spend", "revenue"], 3
                )
            finally:
                first.close()

            second = CompanyOS(path)
            try:
                loaded = second.attribution.get_attribution(org.id, ws.id, owner.id, plan["id"])
                self.assertEqual(loaded["id"], plan["id"])
                self.assertEqual(loaded["metric_names"], ["spend", "revenue"])
                self.assertEqual(len(loaded["snapshots"]), 1)
            finally:
                second.close()

    def test_org_and_workspace_isolation(self) -> None:
        campaign = self._campaign()
        plan = self.os.attribution.create_attribution_plan(
            self.org.id, self.ws.id, self.owner.id, "campaign", campaign["id"], ["spend"], 7
        )
        with self.assertRaises(AuthorizationError):
            self.os.attribution.create_attribution_plan(
                self.org.id, self.ws.id, self.viewer.id, "campaign", campaign["id"], ["spend"], 7
            )
        with self.assertRaises(NotFoundError):
            self.os.attribution.get_attribution(self.org.id, self.other_ws.id, self.owner.id, plan["id"])
        other_campaign = self.os.agency_ops.create_campaign(
            self.org.id,
            self.other_ws.id,
            self.owner.id,
            "Other Launch",
            "Leads",
            "meta",
        )
        with self.assertRaises(NotFoundError):
            self.os.attribution.create_attribution_plan(
                self.org.id, self.ws.id, self.owner.id, "campaign", other_campaign["id"], ["spend"], 7
            )

    def test_no_fabrication_when_data_missing(self) -> None:
        campaign = self._campaign()
        plan = self.os.attribution.create_attribution_plan(
            self.org.id,
            self.ws.id,
            self.owner.id,
            "campaign",
            campaign["id"],
            ["spend", "roas"],
            7,
        )
        evaluated = self.os.attribution.evaluate(self.org.id, self.ws.id, self.owner.id, plan["id"])
        baseline = [snapshot for snapshot in evaluated["snapshots"] if snapshot["snapshot_kind"] == "baseline"][0]
        outcome = [snapshot for snapshot in evaluated["snapshots"] if snapshot["snapshot_kind"] == "outcome"][0]
        self.assertTrue(all(item["status"] == "unknown" for item in baseline["values"]))
        self.assertTrue(all(item["status"] == "unknown" for item in outcome["values"]))
        self.assertTrue(all(item["status"] == "unknown" for item in outcome["deltas"]))
        stored = self.os.store.conn.execute(
            "SELECT outcome_json FROM intelligence_hypotheses WHERE subject=?",
            (f"campaign:{campaign['id']}",),
        ).fetchone()
        outcome_json = json.loads(stored["outcome_json"])
        self.assertEqual(outcome_json["measured_outcomes"], [])
        self.assertTrue(all(item["status"] == "unknown" for item in outcome_json["deltas"]))


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


if __name__ == "__main__":
    unittest.main()
