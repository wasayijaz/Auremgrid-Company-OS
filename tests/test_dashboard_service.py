from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone

from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


def workflow_template(key: str = "dashboard_flow") -> dict:
    return {
        "key": key,
        "name": "Dashboard flow",
        "version": "1",
        "stages": [
            {
                "key": "brief", "name": "Brief", "sequence": 1,
                "assignee": {"wing": "strategy", "role": "lead"},
                "required_evidence": ["brief"],
                "handoff_to": {"wing": "delivery", "role": "operator"},
                "handoff_contract": "approved brief",
            },
            {
                "key": "deliver", "name": "Deliver", "sequence": 2,
                "assignee": {"wing": "delivery", "role": "operator"},
                "depends_on": ["brief"], "required_evidence": ["deliverable"],
                "requires_approval": True,
            },
        ],
    }


class DashboardServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Visible", "client")
        self.other_ws = self.os.create_organization_workspace(self.org.id, "Hidden", "client")
        self.owner = self.os.create_person(self.org.id, "Owner", role="owner")
        self.viewer = self.os.create_person(self.org.id, "Viewer", role="member")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.viewer.id, "viewer")
        self.os.add_person_to_workspace(self.org.id, self.other_ws.id, self.owner.id, "admin")
        self.visible_actor = self.os.create_actor(self.ws.id, "Visible reader", "agent", "actor-visible")
        self.viewer_actor = self.os.create_actor(self.ws.id, "Viewer reader", "agent", "actor-viewer")
        self.hidden_actor = self.os.create_actor(self.ws.id, "Other reader", "agent", "actor-hidden")
        self.ingest_actor = self.os.create_actor(self.ws.id, "Ingest", "admin", "actor-ingest")
        _, self.owner_identity = issue_identity(
            self.os, self.org.id, self.owner.id, self.ws.id, self.visible_actor.id
        )
        _, self.viewer_identity = issue_identity(self.os, self.org.id, self.viewer.id, self.ws.id)
        self.os.store.conn.execute(
            "INSERT INTO principal_actor_bindings(principal_id,workspace_id,actor_id,created_at) VALUES (?,?,?,?)",
            (self.viewer_identity.principal_id,self.ws.id,self.viewer_actor.id,datetime.now(timezone.utc).isoformat()),
        )
        self.os.store.conn.commit()
        self.dashboard = self.os.dashboard

    def tearDown(self) -> None:
        self.os.close()

    def test_brain_aggregate_applies_source_acl_and_sanitizes_health(self) -> None:
        self.os.ingest_text(
            self.ws.id, self.ingest_actor.id, "visible.md", "FACT: Plan | price | 100",
            "memory://visible", allowed_actor_ids=[self.visible_actor.id,self.viewer_actor.id],
        )
        self.os.ingest_text(
            self.ws.id, self.ingest_actor.id, "hidden.md", "FACT: Secret | price | 999",
            "memory://hidden", allowed_actor_ids=[self.hidden_actor.id],
        )
        self.os.embedding_health = {
            "status": "degraded", "provider": "safe-provider", "model": "safe-model",
            "version": "1", "detail": "Bearer should-never-leak",
        }
        result = self.dashboard.brain(
            self.owner_identity, self.org.id, self.ws.id, self.owner.id
        )
        self.assertEqual(result["summary"]["sources"], 1)
        self.assertEqual({item["subject"] for item in result["current_truths"]}, {"Plan"})
        self.assertNotIn("detail", result["health"]["semantic"])
        self.assertNotIn("Secret", repr(result))

    def test_brain_hides_entities_and_aliases_supported_only_by_restricted_sources(self) -> None:
        hidden=self.os.ingest_text(
            self.ws.id,self.ingest_actor.id,"hidden-entity.md","private evidence","memory://hidden-entity",
            allowed_actor_ids=[self.hidden_actor.id],
        )
        now=datetime.now(timezone.utc).isoformat(); entity_id="entity-hidden-only"
        self.os.store.conn.execute(
            "INSERT INTO entities(id,organization_id,workspace_id,canonical_name,type,created_at,status,merged_into,updated_at) "
            "VALUES (?,?,?,?,?,?,'active',NULL,?)",
            (entity_id,self.org.id,self.ws.id,"Restricted Entity","client",now,now),
        )
        self.os.brain_ops._add_alias(entity_id,"Restricted Alias",1.0,"approved",hidden.source.id)
        result=self.dashboard.brain(self.owner_identity,self.org.id,self.ws.id,self.owner.id)
        self.assertEqual(result["summary"]["sources"],0)
        self.assertEqual(result["summary"]["entities"],0)
        self.assertEqual(result["entities"],[])
        self.assertNotIn("Restricted",repr(result))

    def test_brain_as_of_and_workspace_scope_do_not_leak_future_or_other_workspace(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=2)
        future = datetime.now(timezone.utc) + timedelta(days=2)
        self.os.ingest_text(
            self.ws.id, self.ingest_actor.id, "old.md", "FACT: Plan | phase | old",
            "memory://old", allowed_actor_ids=[self.visible_actor.id], observed_at=old,
        )
        self.os.ingest_text(
            self.ws.id, self.ingest_actor.id, "future.md", "FACT: Future Plan | phase | future",
            "memory://future", allowed_actor_ids=[self.visible_actor.id], observed_at=future,
        )
        historical = self.dashboard.brain(
            self.owner_identity, self.org.id, self.ws.id, self.owner.id,
            as_of=datetime.now(timezone.utc),
        )
        self.assertEqual({item["object"] for item in historical["current_truths"]}, {"old"})
        with self.assertRaises(AuthorizationError):
            self.dashboard.brain(
                self.owner_identity, self.org.id, self.other_ws.id, self.owner.id
            )

    def test_brain_conflicts_include_states_citations_and_historical_alternatives(self) -> None:
        ingested = self.os.ingest_text(
            self.ws.id, self.ingest_actor.id, "conflict.md", "evidence", "memory://conflict",
            allowed_actor_ids=[self.visible_actor.id],
        )
        proposals = [self.os.brain_ops.create_proposal(
            self.org.id,self.ws.id,"agent",self.owner_identity,"fact",f"price {value}",
            {"subject":"Plan","predicate":"price","object":value},"evidence",.9,ingested.source.id,
        ) for value in ("100","200")]
        for proposal in proposals:
            self.os.brain_ops.brain_promote_fact(self.owner_identity,proposal["id"],"approve")
        facts=self.os.store.conn.execute(
            "SELECT id,conflict_group FROM facts WHERE workspace_id=? AND subject='Plan' ORDER BY id",(self.ws.id,)
        ).fetchall()
        before=datetime.now(timezone.utc); time.sleep(.01)
        self.os.brain_ops.resolve_fact_conflict(self.owner_identity,facts[0]["conflict_group"],facts[0]["id"])
        historical=self.dashboard.brain(self.owner_identity,self.org.id,self.ws.id,self.owner.id,as_of=before)
        current=self.dashboard.brain(self.owner_identity,self.org.id,self.ws.id,self.owner.id)
        self.assertEqual(historical["conflicts"][0]["state"],"conflicted")
        self.assertEqual({item["state"] for item in historical["conflicts"][0]["alternatives"]},{"conflicted"})
        self.assertTrue(all(item["citation"]["source_id"]==ingested.source.id for item in historical["conflicts"][0]["alternatives"]))
        self.assertEqual(current["conflicts"][0]["state"],"resolved")
        self.assertEqual({item["id"] for item in current["current_truths"]},{facts[0]["id"]})

    def test_workflow_board_derives_readiness_and_capability_actions(self) -> None:
        run = self.os.workflow_ops.create_run(
            self.org.id, self.ws.id, self.owner.id, workflow_template()
        )
        owner_board = self.dashboard.workflow_board(
            self.owner_identity, self.org.id, self.ws.id, self.owner.id
        )
        stages = {stage["stage_key"]: stage for stage in owner_board["stages"]}
        self.assertTrue(stages["brief"]["readiness"]["ready"])
        self.assertIn("start_stage", stages["brief"]["allowed_actions"])
        self.assertFalse(stages["deliver"]["readiness"]["ready"])
        self.assertEqual(stages["deliver"]["dependencies"][0]["status"], "pending")

        self.os.workflow_ops.start_stage(self.org.id,self.ws.id,self.owner.id,run["id"],"brief")
        self.os.workflow_ops.submit_evidence(
            self.org.id,self.ws.id,self.owner.id,run["id"],"brief","brief",text="approved brief"
        )
        self.os.workflow_ops.complete_stage(self.org.id,self.ws.id,self.owner.id,run["id"],"brief")
        waiting_handoff=self.dashboard.workflow_board(
            self.owner_identity,self.org.id,self.ws.id,self.owner.id
        )
        waiting={stage["stage_key"]:stage for stage in waiting_handoff["stages"]}
        self.assertEqual(waiting["brief"]["evidence"]["by_kind"],{"brief":1})
        self.assertFalse(waiting["deliver"]["readiness"]["handoffs_clear"])
        self.os.workflow_ops.acknowledge_handoff(
            self.org.id,self.ws.id,self.owner.id,run["id"],"brief","deliver","approved brief"
        )
        after_ack=self.dashboard.workflow_board(self.owner_identity,self.org.id,self.ws.id,self.owner.id)
        self.assertTrue(next(stage for stage in after_ack["stages"] if stage["stage_key"]=="deliver")["readiness"]["ready"])

        viewer_board = self.dashboard.workflow_board(
            self.viewer_identity, self.org.id, self.ws.id, self.viewer.id
        )
        self.assertTrue(all(not stage["allowed_actions"] for stage in viewer_board["stages"]))
        self.assertEqual([item["id"] for item in viewer_board["runs"]], [run["id"]])

    def test_workflow_board_as_of_reconstructs_status_and_disables_actions(self) -> None:
        run = self.os.workflow_ops.create_run(
            self.org.id, self.ws.id, self.owner.id, workflow_template("temporal_board")
        )
        cutoff = datetime.now(timezone.utc)
        time.sleep(1.05)
        self.os.workflow_ops.start_stage(
            self.org.id, self.ws.id, self.owner.id, run["id"], "brief"
        )
        historical = self.dashboard.workflow_board(
            self.owner_identity, self.org.id, self.ws.id, self.owner.id, as_of=cutoff
        )
        current = self.dashboard.workflow_board(
            self.owner_identity, self.org.id, self.ws.id, self.owner.id
        )
        old_brief = next(stage for stage in historical["stages"] if stage["stage_key"] == "brief")
        current_brief = next(stage for stage in current["stages"] if stage["stage_key"] == "brief")
        self.assertEqual(old_brief["status"], "pending")
        self.assertEqual(current_brief["status"], "in_progress")
        self.assertEqual(old_brief["allowed_actions"], [])
        self.assertEqual(historical["runs"][0]["status"], "pending")
        self.assertEqual(current["runs"][0]["status"], "in_progress")

    def test_workflow_board_rejects_identity_person_or_workspace_mismatch(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.dashboard.workflow_board(
                self.owner_identity, self.org.id, self.ws.id, self.viewer.id
            )
        with self.assertRaises(AuthorizationError):
            self.dashboard.workflow_board(
                self.owner_identity, self.org.id, self.other_ws.id, self.owner.id
            )


if __name__ == "__main__":
    unittest.main()
