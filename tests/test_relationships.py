from __future__ import annotations
import unittest
from datetime import datetime,timezone
from auremgrid.domain.errors import NotFoundError
from auremgrid.services.brain import CompanyOS

class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.os=CompanyOS(":memory:");self.org=self.os.create_organization("Agency");self.ws=self.os.create_organization_workspace(self.org.id,"Prime","client");self.other=self.os.create_organization_workspace(self.org.id,"BASE","client");self.owner=self.os.create_person(self.org.id,"Owner",role="owner");self.os.add_person_to_workspace(self.org.id,self.ws.id,self.owner.id,"admin");self.os.add_person_to_workspace(self.org.id,self.other.id,self.owner.id,"admin")
    def tearDown(self):self.os.close()
    def test_relationship_graph_answers_approver_and_declining_sentiment(self):
        approver=self.os.client_ops.create_contact(self.org.id,self.ws.id,self.owner.id,"Alex","Prime","CMO","high","final")
        operator=self.os.client_ops.create_contact(self.org.id,self.ws.id,self.owner.id,"Sam","Prime","Marketing Manager")
        self.os.client_ops.link_contacts(self.org.id,self.ws.id,self.owner.id,operator["id"],approver["id"],"reports_to",0.9,"Org chart")
        self.os.client_ops.record_sentiment(self.org.id,self.ws.id,self.owner.id,0.8,"positive","Call 1",approver["id"])
        self.os.client_ops.record_sentiment(self.org.id,self.ws.id,self.owner.id,0.2,"neutral","Call 2",approver["id"])
        graph=self.os.client_ops.relationship_graph(self.org.id,self.ws.id,self.owner.id)
        self.assertEqual(graph["approvers"][0]["name"],"Alex");self.assertEqual(graph["declining"][0]["name"],"Alex")
    def test_cross_workspace_relationship_is_rejected(self):
        first=self.os.client_ops.create_contact(self.org.id,self.ws.id,self.owner.id,"A","Prime","Lead")
        second=self.os.client_ops.create_contact(self.org.id,self.other.id,self.owner.id,"B","BASE","Lead")
        with self.assertRaises(NotFoundError):self.os.client_ops.link_contacts(self.org.id,self.ws.id,self.owner.id,first["id"],second["id"],"approves",1,"No")
    def test_meeting_outputs_remain_proposed_and_create_signals(self):
        meeting=self.os.client_ops.create_meeting(self.org.id,self.ws.id,self.owner.id,"Call",datetime.now(timezone.utc))
        output=self.os.client_ops.add_meeting_output(self.org.id,self.ws.id,self.owner.id,meeting.id,"decision","Use room imagery",0.8)
        self.assertEqual(output["status"],"proposed");self.assertEqual(self.os.client_ops.list_signals(self.org.id,self.ws.id,self.owner.id)[0]["type"],"decision")

if __name__=="__main__":unittest.main()
