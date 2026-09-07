"""Tests for LeadsReadService."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.leads_read import LeadsReadService


class LeadsReadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org_a = self.os.create_organization("Prime Agency", "org_prime")
        self.org_b = self.os.create_organization("Rival Agency", "org_rival")

        self.owner_a = self.os.create_person(self.org_a.id, "Alice Owner", role="owner", person_id="person_owner_a")
        self.lead_a = self.os.create_person(self.org_a.id, "Bob Lead", role="member", person_id="person_lead_a")
        self.owner_b = self.os.create_person(self.org_b.id, "Mallory Owner", role="owner", person_id="person_owner_b")

        self.client_1 = self.os.create_organization_workspace(self.org_a.id, "Lumina Client", "client", "ws_lumina")
        self.client_2 = self.os.create_organization_workspace(self.org_a.id, "Apex Client", "client", "ws_apex")
        self.client_b = self.os.create_organization_workspace(self.org_b.id, "Rival Client", "client", "ws_rival")

        for p in (self.owner_a, self.lead_a):
            self.os.add_person_to_workspace(self.org_a.id, self.client_1.id, p.id, "admin")
            self.os.add_person_to_workspace(self.org_a.id, self.client_2.id, p.id, "admin")
        self.os.add_person_to_workspace(self.org_b.id, self.client_b.id, self.owner_b.id, "admin")

        self.service = LeadsReadService(self.os)

    def tearDown(self) -> None:
        self.os.close()

    def test_ads_lead_view_with_campaigns_and_snapshots(self) -> None:
        # Create campaigns in Org A
        now_iso = datetime.now(timezone.utc).isoformat()
        self.os.store.conn.execute(
            """
            INSERT INTO campaigns (
                id, organization_id, workspace_id, name, objective, platform,
                budget, currency, start_date, status, owner_person_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("camp_1", self.org_a.id, self.client_1.id, "Meta Prospecting Q3", "conversions", "meta",
             5000.0, "USD", "2026-08-01", "active", self.owner_a.id, now_iso, now_iso),
        )
        self.os.store.conn.execute(
            """
            INSERT INTO campaigns (
                id, organization_id, workspace_id, name, objective, platform,
                budget, currency, start_date, status, owner_person_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("camp_2", self.org_a.id, self.client_1.id, "Google Search Brand", "traffic", "google",
             2000.0, "USD", "2026-08-01", "active", self.owner_a.id, now_iso, now_iso),
        )

        # Attach snapshots
        self.os.store.conn.execute(
            """
            INSERT INTO campaign_metric_snapshots (
                id, organization_id, workspace_id, campaign_id, captured_at,
                spend, revenue, leads, impressions, clicks, ctr, cvr, roas, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("snap_1", self.org_a.id, self.client_1.id, "camp_1", now_iso,
             3000.0, 9000.0, 120, 100000, 2500, 2.5, 4.8, 3.0, "meta_api"),
        )
        self.os.store.conn.execute(
            """
            INSERT INTO campaign_metric_snapshots (
                id, organization_id, workspace_id, campaign_id, captured_at,
                spend, revenue, leads, impressions, clicks, ctr, cvr, roas, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("snap_2", self.org_a.id, self.client_1.id, "camp_2", now_iso,
             1000.0, 4000.0, 50, 20000, 1000, 5.0, 5.0, 4.0, "google_api"),
        )
        self.os.store.conn.commit()

        scope = {"organization_id": self.org_a.id, "person_id": self.owner_a.id}
        view = self.service.get_ads_lead_view(scope)

        self.assertEqual(view["role"], "ads_lead")
        self.assertEqual(view["organization_id"], self.org_a.id)

        summary = view["summary"]
        self.assertEqual(summary["total_campaigns"], 2)
        self.assertEqual(summary["active_campaigns"], 2)
        self.assertEqual(summary["total_budget"], 7000.0)
        self.assertEqual(summary["total_spend"], 4000.0)
        self.assertEqual(summary["total_revenue"], 13000.0)
        self.assertEqual(summary["blended_roas"], 3.25)
        self.assertEqual(summary["total_leads"], 170)

        # Platform metrics
        platforms = {p["platform"]: p for p in view["platforms"]}
        self.assertIn("meta", platforms)
        self.assertIn("google", platforms)
        self.assertEqual(platforms["meta"]["spend"], 3000.0)
        self.assertEqual(platforms["meta"]["roas"], 3.0)

    def test_design_lead_view_pipeline_states_and_bottlenecks(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        # Seed 4 creative assets in various states
        assets_data = [
            ("asset_1", "Summer Lifestyle Video", "meta", "video", "draft", 1),
            ("asset_2", "Carousel Variant A", "meta", "carousel", "in_review", 3),
            ("asset_3", "Search Responsive Banner", "google", "banner", "in_review", 4),
            ("asset_4", "Product Reel Cutdown", "tiktok", "video", "approved", 2),
        ]
        for aid, title, plat, fmt, st, rev in assets_data:
            self.os.store.conn.execute(
                """
                INSERT INTO creative_assets (
                    id, organization_id, workspace_id, title, platform, format,
                    approval_state, revision_count, style_tags, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (aid, self.org_a.id, self.client_1.id, title, plat, fmt, st, rev, "[]", now_iso),
            )
        self.os.store.conn.commit()

        scope = {"organization_id": self.org_a.id, "person_id": self.owner_a.id}
        view = self.service.get_design_lead_view(scope)

        self.assertEqual(view["role"], "design_lead")
        pipeline = view["pipeline_state"]
        self.assertEqual(pipeline["total_creative_assets"], 4)
        self.assertEqual(pipeline["review_queue_count"], 2)
        self.assertEqual(pipeline["high_revision_count"], 2)
        self.assertEqual(pipeline["approval_states"]["in_review"], 2)
        self.assertEqual(pipeline["approval_states"]["draft"], 1)
        self.assertEqual(pipeline["approval_states"]["approved"], 1)
        self.assertEqual(pipeline["bottleneck_status"], "moderate")

        # Review queue contains in_review items
        queue_ids = {a["id"] for a in view["review_queue"]}
        self.assertEqual(queue_ids, {"asset_2", "asset_3"})

    def test_marketing_lead_view_channels_and_content(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        # 1. Campaigns on 2 platforms
        self.os.store.conn.execute(
            """
            INSERT INTO campaigns (
                id, organization_id, workspace_id, name, objective, platform,
                budget, currency, status, owner_person_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("c_m1", self.org_a.id, self.client_1.id, "Omnichannel Meta", "reach", "meta",
             4000.0, "USD", "active", self.owner_a.id, now_iso, now_iso),
        )
        self.os.store.conn.execute(
            """
            INSERT INTO campaigns (
                id, organization_id, workspace_id, name, objective, platform,
                budget, currency, status, owner_person_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("c_m2", self.org_a.id, self.client_1.id, "TikTok Spark Ads", "views", "tiktok",
             2500.0, "USD", "active", self.owner_a.id, now_iso, now_iso),
        )

        # 2. Deliverables
        project = self.os.create_project(self.org_a.id, self.client_1.id, self.owner_a.id, "Brand Q3", "Desc")
        self.os.create_deliverable(self.org_a.id, self.client_1.id, self.owner_a.id, project.id, "Ad Copy Set", "copy")
        self.os.create_deliverable(self.org_a.id, self.client_1.id, self.owner_a.id, project.id, "Hero Video", "video")

        scope = {"organization_id": self.org_a.id, "person_id": self.owner_a.id}
        view = self.service.get_marketing_lead_view(scope)

        self.assertEqual(view["role"], "marketing_lead")
        overview = view["channels_overview"]
        self.assertEqual(overview["active_channels_count"], 2)

        content = view["content_pipeline"]
        self.assertEqual(content["total_deliverables"], 2)
        self.assertEqual(content["by_type"]["copy"], 1)
        self.assertEqual(content["by_type"]["video"], 1)

    def test_executive_rollup_cross_discipline_posture(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        # Seed an underperforming campaign (low ROAS with spend)
        self.os.store.conn.execute(
            """
            INSERT INTO campaigns (
                id, organization_id, workspace_id, name, objective, platform,
                budget, currency, status, owner_person_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("c_bad", self.org_a.id, self.client_1.id, "Bleeding Campaign", "sales", "meta",
             1000.0, "USD", "active", self.owner_a.id, now_iso, now_iso),
        )
        self.os.store.conn.execute(
            """
            INSERT INTO campaign_metric_snapshots (
                id, organization_id, workspace_id, campaign_id, captured_at,
                spend, revenue, leads, roas, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("snap_bad", self.org_a.id, self.client_1.id, "c_bad", now_iso,
             800.0, 400.0, 2, 0.5, "meta_api"),
        )
        self.os.store.conn.commit()

        scope = {"organization_id": self.org_a.id, "person_id": self.owner_a.id}
        rollup = self.service.get_executive_rollup(scope)

        self.assertEqual(rollup["role"], "executive_rollup")
        self.assertEqual(rollup["discipline_postures"]["ads"], "critical")
        self.assertEqual(rollup["overall_posture"], "critical")
        self.assertGreater(len(rollup["alerts"]), 0)

        # Unified dispatch test
        dispatched_exec = self.service.get_lead_view(scope, role="executive")
        self.assertEqual(dispatched_exec["role"], "executive_rollup")
        dispatched_ads = self.service.get_lead_view(scope, role="ads")
        self.assertEqual(dispatched_ads["role"], "ads_lead")

    def test_org_fencing_and_unauthorized_access(self) -> None:
        scope_rival = {
            "organization_id": self.org_b.id,
            "workspace_id": self.client_b.id,
            "person_id": self.owner_b.id,
        }
        with self.assertRaises(AuthorizationError):
            self.service.get_ads_lead_view(scope_rival, workspace_id=self.client_1.id)

        scope_mismatch = {
            "organization_id": self.org_a.id,
            "workspace_id": self.client_2.id,
            "person_id": self.owner_a.id,
        }
        with self.assertRaises(AuthorizationError):
            self.service.get_design_lead_view(scope_mismatch, workspace_id=self.client_1.id)

        with self.assertRaises(ValidationError):
            self.service.get_lead_view({}, role="ads")

        with self.assertRaises(ValidationError):
            self.service.get_lead_view({"organization_id": self.org_a.id}, role="invalid_role")

    def test_empty_projections_for_empty_org(self) -> None:
        scope = {"organization_id": self.org_a.id, "person_id": self.owner_a.id}
        ads = self.service.get_ads_lead_view(scope)
        self.assertEqual(ads["summary"]["total_campaigns"], 0)
        self.assertEqual(ads["platforms"], [])

        design = self.service.get_design_lead_view(scope)
        self.assertEqual(design["pipeline_state"]["total_creative_assets"], 0)
        self.assertEqual(design["pipeline_state"]["bottleneck_status"], "clear")

        marketing = self.service.get_marketing_lead_view(scope)
        self.assertEqual(marketing["channels_overview"]["active_channels_count"], 0)

        exec_rollup = self.service.get_executive_rollup(scope)
        self.assertEqual(exec_rollup["overall_posture"], "healthy")


if __name__ == "__main__":
    unittest.main()
