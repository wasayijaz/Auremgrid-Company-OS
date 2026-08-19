from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from auremgrid.adapters.hybrid import hashed_embedding
from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class BrainMaturityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os=CompanyOS(":memory:"); self.org=self.os.create_organization("Agency")
        self.ws=self.os.create_organization_workspace(self.org.id,"Prime","client")
        self.owner=self.os.create_person(self.org.id,"Owner",role="owner")
        self.os.add_person_to_workspace(self.org.id,self.ws.id,self.owner.id,"admin")
        _, self.identity = issue_identity(self.os,self.org.id,self.owner.id,self.ws.id)

    def tearDown(self) -> None: self.os.close()

    def test_low_confidence_alias_is_not_silently_resolved(self) -> None:
        entity=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Northstar Labs","client")
        alias=self.os.brain_ops.propose_alias(self.org.id,self.ws.id,self.owner.id,entity["id"],"Prime Canada",0.7)
        self.assertEqual(alias["status"],"proposed")
        self.assertEqual(self.os.brain_ops.resolve_entity(self.org.id,self.ws.id,self.owner.id,"Prime Canada")["status"],"unknown")
        self.assertEqual(self.os.brain_ops.resolve_entity(self.org.id,self.ws.id,self.owner.id,"Northstar Labs")["status"],"resolved")

    def test_low_confidence_entities_cannot_merge(self) -> None:
        first=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Prime","client")
        second=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Northstar Labs","client")
        with self.assertRaises(ValidationError):
            self.os.brain_ops.merge_entities(self.org.id,self.ws.id,self.owner.id,first["id"],second["id"],0.6,"Name similarity")
        proposal=self.os.brain_ops.brain_propose(self.org.id,self.ws.id,self.identity,"merge",[first["id"],second["id"]],0.98,"Confirmed same legal entity","review evidence",target_id=second["id"])
        merge=self.os.brain_ops.brain_promote(self.org.id,self.ws.id,self.identity,proposal["id"],"approve")
        self.assertEqual(merge["status"],"approved")

    def test_agent_decision_proposal_requires_human_promotion(self) -> None:
        proposal=self.os.brain_ops.create_proposal(self.org.id,self.ws.id,"agent",self.identity,"decision","Use room imagery",
            {"statement":"Use room imagery","rationale":"Approved in review"},"Meeting transcript line 20",0.8)
        self.assertEqual(self.os.company.list_decisions(self.org.id,self.ws.id),[])
        with self.assertRaises(ValidationError):
            self.os.brain_ops.review_proposal(self.org.id,self.owner.id,proposal["id"],"approve")

    def test_knowledge_health_finds_unsourced_decision(self) -> None:
        self.os.create_decision(self.org.id,self.owner.id,"Use new hook","Client asked for it",workspace_id=self.ws.id)
        health=self.os.brain_ops.knowledge_health(self.org.id,self.ws.id,self.owner.id)
        self.assertIn("unsourced_decision",{issue["type"] for issue in health["issues"]})
        self.assertEqual(health["projections"][0]["status"],"healthy")

    def test_offline_fallback_embedding_is_deterministic(self) -> None:
        self.assertEqual(hashed_embedding("Prime consultation"),hashed_embedding("Prime consultation"))


class ProjectionRestartTests(unittest.TestCase):
    def test_projections_rebuild_from_canonical_data_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"brain.sqlite"; first=CompanyOS(path)
            workspace=first.create_workspace("Client","ws_client"); admin=first.create_actor(workspace.id,"Admin","admin","act_admin")
            first.ingest_text(workspace.id,admin.id,"offer.md","FACT: Consultation | price | 199 USD","memory://offer")
            first.close(); second=CompanyOS(path)
            result=second.search(workspace.id,admin.id,"consultation price")
            status=second.store.conn.execute("SELECT * FROM projection_state WHERE workspace_id=?",(workspace.id,)).fetchone()
            second.close()
            self.assertFalse(result.unknown)
            self.assertEqual(status["document_count"],1)
            self.assertEqual(status["fact_count"],1)


if __name__=="__main__": unittest.main()
