from __future__ import annotations
import unittest
from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS

class TeamFinanceTests(unittest.TestCase):
    def setUp(self):
        self.os=CompanyOS(":memory:");self.org=self.os.create_organization("Agency");self.ws=self.os.create_organization_workspace(self.org.id,"Prime","client");self.owner=self.os.create_person(self.org.id,"Owner",role="owner");self.member=self.os.create_person(self.org.id,"Designer",department="Creative");self.os.add_person_to_workspace(self.org.id,self.ws.id,self.owner.id,"admin");self.os.add_person_to_workspace(self.org.id,self.ws.id,self.member.id,"operator")
    def tearDown(self):self.os.close()
    def test_people_directory_combines_skills_clients_and_workload(self):
        skill=self.os.agency_ops.create_skill(self.org.id,self.owner.id,"Motion","Creative");self.os.agency_ops.assign_skill(self.org.id,self.owner.id,self.member.id,skill["id"],4)
        self.os.work_ops.create(self.org.id,self.ws.id,self.member.id,"Animation","Animate","Client")
        directory=self.os.agency_ops.people_directory(self.org.id,self.owner.id);designer=next(p for p in directory if p["id"]==self.member.id)
        self.assertEqual(designer["skills"][0]["level"],4);self.assertEqual(designer["active_clients"][0]["name"],"Prime")
    def test_finance_rejects_values_before_connection_then_uses_real_records(self):
        with self.assertRaises(ValidationError):self.os.agency_ops.record_revenue(self.org.id,self.ws.id,self.owner.id,5000,"2026-08-01","manual")
        self.os.agency_ops.connect_finance(self.org.id,self.owner.id,"accounting-test")
        self.os.agency_ops.record_revenue(self.org.id,self.ws.id,self.owner.id,5000,"2026-08-01","accounting-test")
        self.os.agency_ops.record_invoice(self.org.id,self.ws.id,self.owner.id,1200,"2026-08-01","2026-08-15","accounting-test")
        status=self.os.agency_ops.finance_status(self.org.id,self.owner.id,self.ws.id)
        self.assertEqual(status["recognized_revenue"],5000);self.assertEqual(status["outstanding_revenue"],1200)
    def test_review_supports_timestamped_comments(self):
        project=self.os.create_project(self.org.id,self.ws.id,self.owner.id,"Video");deliverable=self.os.create_deliverable(self.org.id,self.ws.id,self.owner.id,project.id,"Cut","video");review=self.os.open_review(self.org.id,self.ws.id,self.owner.id,deliverable.id)
        comment=self.os.add_review_comment(self.org.id,self.ws.id,self.member.id,review.id,"Trim this section",12.5)
        self.assertEqual(comment.timestamp_seconds,12.5)

if __name__=="__main__":unittest.main()
