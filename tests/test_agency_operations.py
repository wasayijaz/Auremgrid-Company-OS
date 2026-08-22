from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS


class AgencyOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os=CompanyOS(":memory:"); self.org=self.os.create_organization("Agency")
        self.ws=self.os.create_organization_workspace(self.org.id,"Prime","client")
        self.owner=self.os.create_person(self.org.id,"Owner",role="owner")
        self.member=self.os.create_person(self.org.id,"Member")
        self.os.add_person_to_workspace(self.org.id,self.ws.id,self.owner.id,"admin")
        self.os.add_person_to_workspace(self.org.id,self.ws.id,self.member.id,"operator")

    def tearDown(self) -> None: self.os.close()

    def test_campaign_metrics_are_derived_only_from_supplied_data(self) -> None:
        campaign=self.os.agency_ops.create_campaign(self.org.id,self.ws.id,self.owner.id,"Lead Gen","Booked calls","meta",budget=1000)
        empty=self.os.agency_ops.campaign_performance(self.org.id,self.ws.id,self.owner.id,campaign["id"])
        self.assertEqual(empty["metrics"]["status"],"not_connected")
        metric=self.os.agency_ops.record_campaign_metrics(self.org.id,self.ws.id,self.owner.id,campaign["id"],"manual",spend=100,revenue=400,leads=10,impressions=10000,clicks=200)
        self.assertEqual(metric["roas"],4)
        self.assertEqual(metric["ctr"],2)
        self.assertIsNone(metric["cac"])

    def test_metric_sources_are_required_and_normalized(self) -> None:
        campaign=self.os.agency_ops.create_campaign(self.org.id,self.ws.id,self.owner.id,"Lead Gen","Booked calls","meta")
        creative=self.os.agency_ops.create_creative(self.org.id,self.ws.id,self.owner.id,"Lead ad","image",campaign_id=campaign["id"])
        for source in (None,"","   "):
            with self.subTest(kind="campaign",source=source), self.assertRaisesRegex(ValidationError,"campaign metric source is required"):
                self.os.agency_ops.record_campaign_metrics(
                    self.org.id,self.ws.id,self.owner.id,campaign["id"],source,impressions=100,
                )
        for source in (None,"","\t "):
            with self.subTest(kind="creative",source=source), self.assertRaisesRegex(ValidationError,"creative performance source is required"):
                self.os.agency_ops.record_creative_performance(
                    self.org.id,self.ws.id,self.owner.id,creative["id"],source,impressions=100,
                )
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM campaign_metric_snapshots").fetchone()[0],0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM creative_performance").fetchone()[0],0)

        campaign_metric=self.os.agency_ops.record_campaign_metrics(
            self.org.id,self.ws.id,self.owner.id,campaign["id"],"  manual import  ",impressions=100,
        )
        creative_metric=self.os.agency_ops.record_creative_performance(
            self.org.id,self.ws.id,self.owner.id,creative["id"],"  platform sync  ",impressions=100,
        )
        self.assertEqual(campaign_metric["source"],"manual import")
        self.assertEqual(creative_metric["source"],"platform sync")

    def test_creative_library_is_workspace_scoped_and_searchable(self) -> None:
        self.os.agency_ops.create_creative(self.org.id,self.ws.id,self.owner.id,"Consultation room ad","image",style_tags=["consultation-room","clinical"])
        hits=self.os.agency_ops.search_creative(self.org.id,self.ws.id,self.member.id,"consultation")
        self.assertEqual(len(hits),1)
        outsider=self.os.create_person(self.org.id,"Outsider")
        with self.assertRaises(AuthorizationError):
            self.os.agency_ops.search_creative(self.org.id,self.ws.id,outsider.id,"consultation")

    def test_content_pipeline_cannot_skip_stages(self) -> None:
        item=self.os.agency_ops.create_content(self.org.id,self.ws.id,self.owner.id,"Founder post","Awareness","Founders")
        with self.assertRaises(ValidationError):
            self.os.agency_ops.advance_content(self.org.id,self.ws.id,self.owner.id,item["id"],"published")
        updated=self.os.agency_ops.advance_content(self.org.id,self.ws.id,self.owner.id,item["id"],"research")
        self.assertEqual(updated["stage"],"research")

    def test_finance_returns_not_connected_without_fabricated_numbers(self) -> None:
        status=self.os.agency_ops.finance_status(self.org.id,self.owner.id,self.ws.id)
        self.assertEqual(status,{"status":"not_connected","mrr":None,"outstanding_revenue":None,"client_margin":None})

    def test_capacity_reports_overload(self) -> None:
        self.os.agency_ops.set_availability(self.org.id,self.member.id,"2026-08-17",40)
        capacity=self.os.agency_ops.calculate_capacity(self.org.id,self.owner.id,self.member.id,"2026-08-17",48,44)
        self.assertTrue(capacity["overloaded"])
        self.assertEqual(capacity["remaining_hours"],-8)

    def test_approval_is_enforced_and_auditable(self) -> None:
        request=self.os.agency_ops.request_approval(self.org.id,"person",self.member.id,"email send","email.send",
            {"to":"client@example.test"},"External communication",approver_person_id=self.owner.id)
        self.assertEqual(request["status"],"pending")
        with self.assertRaises(AuthorizationError):
            self.os.agency_ops.decide_approval(self.org.id,self.member.id,request["id"],True)
        decided=self.os.agency_ops.decide_approval(self.org.id,self.owner.id,request["id"],True,"Approved")
        self.assertEqual(decided["status"],"approved")

    def test_attention_returns_only_top_three_by_priority(self) -> None:
        for index in range(5):
            self.os.agency_ops.create_notification(self.org.id,self.owner.id,f"N{index}","risk",severity=index/4,urgency=index/4)
        items=self.os.agency_ops.attention(self.org.id,self.owner.id)
        self.assertEqual(len(items),3)
        self.assertGreaterEqual(items[0]["priority"],items[1]["priority"])


if __name__=="__main__": unittest.main()
