from __future__ import annotations
import unittest
from datetime import datetime,timezone
from auremgrid.services.brain import CompanyOS

class PromotionSyncTests(unittest.TestCase):
    def setUp(self):
        self.os=CompanyOS(":memory:");self.org=self.os.create_organization("Agency");self.ws=self.os.create_organization_workspace(self.org.id,"Prime","client");self.owner=self.os.create_person(self.org.id,"Owner",role="owner");self.os.add_person_to_workspace(self.org.id,self.ws.id,self.owner.id,"admin");self.actor=self.os.create_actor(self.ws.id,"Admin","admin")
    def tearDown(self):self.os.close()
    def test_fact_proposal_promotes_to_cited_canonical_fact(self):
        source=self.os.ingest_text(self.ws.id,self.actor.id,"meeting.md","Meeting evidence","memory://meeting").source
        proposal=self.os.brain_ops.create_proposal(self.org.id,self.ws.id,"agent","luna","fact","Price is 199",{"subject":"Consultation","predicate":"price","object":"199 USD"},"Client confirmed 199",.9,source.id)
        reviewed=self.os.brain_ops.review_proposal(self.org.id,self.owner.id,proposal["id"],"approve")
        self.assertEqual(reviewed["promoted_type"],"fact");result=self.os.search(self.ws.id,self.actor.id,"consultation price");self.assertFalse(result.unknown);self.assertEqual(next(i for i in result.items if i.kind=="fact").citation.source_id,source.id)
    def test_memory_proposal_promotes_to_canonical_knowledge(self):
        proposal=self.os.brain_ops.create_proposal(self.org.id,self.ws.id,"agent","luna","memory","Prefers concise reviews",{},"Review history",.8)
        result=self.os.brain_ops.review_proposal(self.org.id,self.owner.id,proposal["id"],"approve")
        self.assertIsNotNone(result["promoted_id"]);self.assertEqual(self.os.store.conn.execute("SELECT content FROM canonical_knowledge").fetchone()[0],"Prefers concise reviews")
    def test_message_reply_closes_unanswered_state(self):
        conversation=self.os.client_ops.create_conversation(self.org.id,self.ws.id,self.owner.id,"gmail","email")
        message=self.os.client_ops.add_message(self.org.id,self.ws.id,self.owner.id,conversation.id,"contact","c","Please reply",datetime.now(timezone.utc),True)
        self.assertEqual(len(self.os.client_ops.unanswered_messages(self.org.id,self.ws.id,self.owner.id)),1)
        self.os.client_ops.reply_to_message(self.org.id,self.ws.id,self.owner.id,message.id,"Done",datetime.now(timezone.utc));self.assertEqual(self.os.client_ops.unanswered_messages(self.org.id,self.ws.id,self.owner.id),[])
    def test_connector_failure_is_persisted_and_visible(self):
        integration=self.os.agent_ops.upsert_integration(self.org.id,self.owner.id,"slack",{self.ws.id:"C1"},["read"],"connected")
        run=self.os.agent_ops.start_sync(self.org.id,self.owner.id,integration["id"]);failed=self.os.agent_ops.complete_sync(self.org.id,self.owner.id,run["id"],0,error="rate limited")
        state=self.os.store.conn.execute("SELECT * FROM integrations WHERE id=?",(integration["id"],)).fetchone();self.assertEqual(failed["status"],"failed");self.assertEqual(state["health"],"error");self.assertEqual(state["last_error"],"rate limited")

if __name__=="__main__":unittest.main()
