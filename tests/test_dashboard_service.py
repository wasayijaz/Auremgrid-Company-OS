from __future__ import annotations

import time
import unittest
import json
import threading
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


def workflow_template(key: str = "dashboard_flow", assignee_person_id: str | None = None) -> dict:
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
                "expected_duration_hours": 4,
                **({"assignee_person_id": assignee_person_id} if assignee_person_id else {}),
            },
            {
                "key": "deliver", "name": "Deliver", "sequence": 2,
                "assignee": {"wing": "delivery", "role": "operator"},
                "depends_on": ["brief"], "required_evidence": ["deliverable"],
                "requires_approval": True,
                "expected_duration_hours": 6,
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
        self.operator = self.os.create_person(self.org.id, "Operator", role="member")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.owner.id, "admin")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.viewer.id, "viewer")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.operator.id, "operator")
        self.os.add_person_to_workspace(self.org.id, self.other_ws.id, self.owner.id, "admin")
        self.os.client_ops.create_client_roster(
            self.org.id,
            self.ws.id,
            self.owner.id,
            [
                {"role_key": "client_success_dri", "person_id": self.owner.id},
                {"role_key": "client_success_backup", "person_id": self.operator.id},
                {"role_key": "wing_lead", "wing": "strategy", "person_id": self.owner.id},
                {"role_key": "wing_executive", "wing": "delivery", "person_id": self.operator.id},
            ],
        )
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

    def test_brain_exposes_explicit_collections_and_honest_empty_states(self) -> None:
        result = self.dashboard.brain(self.owner_identity, self.org.id, self.ws.id, self.owner.id)
        self.assertEqual(
            set(result["collections"]),
            {"current_truth", "decisions", "preferences", "entities", "conflicts", "proposed", "sources", "history"},
        )
        self.assertEqual(result["collections"]["decisions"], [])
        self.assertEqual(result["collections"]["preferences"], [])
        self.assertEqual(result["collections"]["history"], [])

    def test_brain_collections_keep_hidden_sources_and_history_out_of_acl_scope(self) -> None:
        self.os.ingest_text(
            self.ws.id, self.ingest_actor.id, "visible-history.md", "FACT: Public | status | current",
            "memory://visible-history", allowed_actor_ids=[self.visible_actor.id],
        )
        self.os.ingest_text(
            self.ws.id, self.ingest_actor.id, "hidden-history.md", "FACT: Secret | status | hidden",
            "memory://hidden-history", allowed_actor_ids=[self.hidden_actor.id],
        )
        result = self.dashboard.brain(self.owner_identity, self.org.id, self.ws.id, self.owner.id)
        collections = result["collections"]
        self.assertEqual({row["source_key"] for row in collections["sources"]}, {"visible-history.md"})
        self.assertEqual({row["subject"] for row in collections["history"]}, {"Public"})
        self.assertNotIn("Secret", repr(collections))

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

    def test_brain_normalizes_proposal_families_and_only_exposes_executable_actions(self) -> None:
        source=self.os.ingest_text(
            self.ws.id,self.ingest_actor.id,"proposal-source.md","evidence","memory://proposal-source",
            allowed_actor_ids=[self.visible_actor.id],
        ).source
        knowledge=[]
        for kind in ("memory","fact","decision"):
            knowledge.append(self.os.brain_ops.create_proposal(
                self.org.id,self.ws.id,"agent",self.owner_identity,kind,f"{kind} proposal",
                {"subject":"Plan","predicate":"price","object":"100"} if kind=="fact" else {},
                "evidence",.9,source.id if kind=="fact" else None,
            ))
        first=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"First Entity","client")
        second=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Second Entity","client")
        alias=self.os.brain_ops.brain_propose(
            self.org.id,self.ws.id,self.owner_identity,"alias",[first["id"]],.9,"alias","evidence",alias="First Co"
        )
        merge=self.os.brain_ops.brain_propose(
            self.org.id,self.ws.id,self.owner_identity,"merge",[first["id"],second["id"]],.9,"merge","evidence",target_id=second["id"]
        )
        current=self.dashboard.brain(self.owner_identity,self.org.id,self.ws.id,self.owner.id)
        rows={item["id"]:item for item in current["proposals"]}
        self.assertEqual({rows[item["id"]]["kind"] for item in knowledge},{"memory","fact","decision"})
        self.assertEqual({rows[alias["id"]]["family"],rows[merge["id"]]["family"]},{"entity_resolution"})
        self.assertEqual(rows[knowledge[0]["id"]]["allowed_actions"],[])
        self.assertEqual(rows[knowledge[2]["id"]]["allowed_actions"],[])
        self.assertEqual({action["action"] for action in rows[knowledge[1]["id"]]["allowed_actions"]},{"approve","reject"})
        self.assertEqual({action["route"] for action in rows[alias["id"]]["allowed_actions"]},{"/brain/promote"})
        before=datetime.now(timezone.utc)
        self.os.brain_ops.brain_promote(self.org.id,self.ws.id,self.owner_identity,alias["id"],"reject")
        decided={item["id"]:item for item in self.dashboard.brain(self.owner_identity,self.org.id,self.ws.id,self.owner.id)["proposals"]}
        historical={item["id"]:item for item in self.dashboard.brain(self.owner_identity,self.org.id,self.ws.id,self.owner.id,as_of=before)["proposals"]}
        self.assertEqual(decided[alias["id"]]["status"],"rejected"); self.assertEqual(decided[alias["id"]]["allowed_actions"],[])
        self.assertEqual(historical[alias["id"]]["status"],"pending"); self.assertEqual(historical[alias["id"]]["allowed_actions"],[])

    def test_conflict_actions_require_full_acl_and_resolution_is_idempotent(self) -> None:
        source=self.os.ingest_text(
            self.ws.id,self.ingest_actor.id,"full-conflict.md","evidence","memory://full-conflict",
            allowed_actor_ids=[self.visible_actor.id],
        ).source
        proposals=[self.os.brain_ops.create_proposal(
            self.org.id,self.ws.id,"agent",self.owner_identity,"fact",value,
            {"subject":"Plan","predicate":"price","object":value},"evidence",.9,source.id,
        ) for value in ("100","200")]
        for proposal in proposals: self.os.brain_ops.brain_promote_fact(self.owner_identity,proposal["id"],"approve")
        facts=self.os.store.conn.execute("SELECT id,conflict_group FROM facts WHERE source_id=? ORDER BY id",(source.id,)).fetchall()
        group=facts[0]["conflict_group"]
        card=next(item for item in self.dashboard.brain(self.owner_identity,self.org.id,self.ws.id,self.owner.id)["conflicts"] if item["id"]==group)
        self.assertEqual({action["payload"]["winner_fact_id"] for action in card["allowed_actions"]},{row["id"] for row in facts})
        before_count=self.os.store.conn.execute("SELECT COUNT(*) FROM knowledge_state_events").fetchone()[0]
        first=self.os.brain_ops.resolve_fact_conflict(self.owner_identity,group,facts[0]["id"])
        after_count=self.os.store.conn.execute("SELECT COUNT(*) FROM knowledge_state_events").fetchone()[0]
        replay=self.os.brain_ops.resolve_fact_conflict(self.owner_identity,group,facts[0]["id"])
        self.assertTrue(first["changed"]); self.assertFalse(replay["changed"])
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM knowledge_state_events").fetchone()[0],after_count)
        self.assertEqual(after_count-before_count,len(facts))
        with self.assertRaises(ValidationError):
            self.os.brain_ops.resolve_fact_conflict(self.owner_identity,group,facts[1]["id"])

        visible=self.os.ingest_text(
            self.ws.id,self.ingest_actor.id,"partial-visible.md","evidence","memory://partial-visible",
            allowed_actor_ids=[self.visible_actor.id],
        ).source
        hidden=self.os.ingest_text(
            self.ws.id,self.ingest_actor.id,"partial-hidden.md","evidence","memory://partial-hidden",
            allowed_actor_ids=[self.hidden_actor.id],
        ).source
        partial=[]
        for value,evidence_source in (("one",visible),("two",hidden)):
            proposal=self.os.brain_ops.create_proposal(
                self.org.id,self.ws.id,"agent",self.owner_identity,"fact",value,
                {"subject":"Partial","predicate":"price","object":value},"evidence",.9,evidence_source.id,
            )
            self.os.brain_ops.brain_promote_fact(self.owner_identity,proposal["id"],"approve")
        partial_facts=self.os.store.conn.execute("SELECT id,conflict_group FROM facts WHERE subject='Partial' ORDER BY id").fetchall()
        partial_group=partial_facts[0]["conflict_group"]
        self.assertNotIn(partial_group,{item["id"] for item in self.dashboard.brain(self.owner_identity,self.org.id,self.ws.id,self.owner.id)["conflicts"]})
        with self.assertRaises(NotFoundError):
            self.os.brain_ops.resolve_fact_conflict(self.owner_identity,partial_group,partial_facts[0]["id"])
        with self.assertRaises(NotFoundError):
            self.os.brain_ops.resolve_fact_conflict(self.owner_identity,"forged",partial_facts[0]["id"])

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
            self.org.id, self.ws.id, self.owner.id, workflow_template(assignee_person_id=self.owner.id)
        )
        owner_board = self.dashboard.workflow_board(
            self.owner_identity, self.org.id, self.ws.id, self.owner.id
        )
        stages = {stage["stage_key"]: stage for stage in owner_board["stages"]}
        self.assertTrue(stages["brief"]["readiness"]["ready"])
        self.assertEqual(stages["brief"]["owner"]["role"], "lead")
        self.assertEqual(stages["brief"]["owner"]["person_id"], self.owner.id)
        self.assertEqual(stages["brief"]["owner"]["person"]["name"], "Owner")
        self.assertEqual(stages["brief"]["expected_duration"]["hours"], 4.0)
        self.assertEqual(owner_board["runs"][0]["rollups"]["expected_duration_hours"], 10.0)
        self.assertEqual(owner_board["runs"][0]["rollups"]["active_expected_duration_hours"], 10.0)
        self.assertEqual(owner_board["runs"][0]["rollups"]["owner_roles"], ["lead", "operator"])
        self.assertIn("start_stage", {action["action"] for action in stages["brief"]["allowed_actions"]})
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
        self.assertEqual(after_ack["runs"][0]["rollups"]["active_expected_duration_hours"], 6.0)

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

    def test_workflow_actions_carry_versions_gate_context_and_handoff_contract(self) -> None:
        run=self.os.workflow_ops.create_run(self.org.id,self.ws.id,self.owner.id,workflow_template("action_contract"))
        board=self.dashboard.workflow_board(self.owner_identity,self.org.id,self.ws.id,self.owner.id)
        brief=next(stage for stage in board["stages"] if stage["stage_key"]=="brief")
        start=next(action for action in brief["allowed_actions"] if action["action"]=="start_stage")
        self.assertEqual(start["route"],"/workflows/stages/start")
        self.assertEqual(start["payload"],{
            "workspace_id":self.ws.id,"run_id":run["id"],"stage_id":"brief","expected_version":1,
        })
        cancel=board["runs"][0]["allowed_actions"][0]
        self.assertEqual(cancel["payload"]["expected_version"],run["version"])

        self.os.workflow_ops.start_stage(self.org.id,self.ws.id,self.owner.id,run["id"],"brief")
        self.os.workflow_ops.submit_evidence(self.org.id,self.ws.id,self.owner.id,run["id"],"brief","brief",text="brief")
        self.os.workflow_ops.complete_stage(self.org.id,self.ws.id,self.owner.id,run["id"],"brief")
        board=self.dashboard.workflow_board(self.owner_identity,self.org.id,self.ws.id,self.owner.id)
        deliver=next(stage for stage in board["stages"] if stage["stage_key"]=="deliver")
        handoff=next(action for action in deliver["allowed_actions"] if action["action"]=="acknowledge_handoff")
        self.assertEqual(handoff["payload"]["artifact_contract"],"approved brief")
        self.assertEqual(handoff["payload"]["from_stage_id"],"brief")
        self.assertEqual(handoff["payload"]["to_stage_id"],"deliver")
        self.os.workflow_ops.acknowledge_handoff(
            self.org.id,self.ws.id,self.owner.id,run["id"],"brief","deliver",handoff["payload"]["artifact_contract"]
        )
        self.os.workflow_ops.start_stage(self.org.id,self.ws.id,self.owner.id,run["id"],"deliver")
        self.os.workflow_ops.submit_evidence(self.org.id,self.ws.id,self.owner.id,run["id"],"deliver","deliverable",text="done")
        approval=self.os.agency_ops.request_approval(
            self.org.id,"person",self.owner.id,f"workflow:{run['id']}:deliver","workflow_stage_approval",
            {"run_id":run["id"],"stage_key":"deliver"},"workflow gate","human",self.ws.id,self.owner.id,
        )
        board=self.dashboard.workflow_board(self.owner_identity,self.org.id,self.ws.id,self.owner.id)
        deliver=next(stage for stage in board["stages"] if stage["stage_key"]=="deliver")
        request=next(action for action in deliver["allowed_actions"] if action["action"]=="request_approval")
        self.assertEqual(request["payload"]["approval_request_id"],approval["id"])
        self.assertEqual(request["payload"]["expected_version"],deliver["version"])
        self.os.workflow_ops.request_approval(
            self.org.id,self.ws.id,self.owner.id,run["id"],"deliver","ready",approval["id"]
        )
        self.os.agency_ops.decide_approval(self.org.id,self.owner.id,approval["id"],True,"approved")
        board=self.dashboard.workflow_board(self.owner_identity,self.org.id,self.ws.id,self.owner.id)
        deliver=next(stage for stage in board["stages"] if stage["stage_key"]=="deliver")
        decide=next(action for action in deliver["allowed_actions"] if action["action"]=="decide_approval")
        self.assertEqual(decide["payload"]["decision"],"approve")
        self.assertEqual(decide["payload"]["approval_request_id"],approval["id"])

    def test_workflow_board_rejects_identity_person_or_workspace_mismatch(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.dashboard.workflow_board(
                self.owner_identity, self.org.id, self.ws.id, self.viewer.id
            )
        with self.assertRaises(AuthorizationError):
            self.dashboard.workflow_board(
                self.owner_identity, self.org.id, self.other_ws.id, self.owner.id
            )

    def test_command_dashboard_is_canonical_not_demo_seeded(self) -> None:
        result = self.dashboard.command(self.org.id, self.owner.id)
        self.assertEqual(result["workspaces"][0]["id"], self.ws.id)
        self.assertEqual(result["workspaces"][0]["name"], "Visible")
        self.assertEqual(result["metrics"]["finance_status"], "not_connected")
        client = next(item for item in result["clients"] if item["id"] == self.ws.id)
        self.assertEqual(client["attention"], "low")
        self.assertEqual(client["scope"]["status"], "unknown")
        self.assertEqual(client["finance"]["status"], "not_connected")
        self.assertIsNone(client["finance"]["recognized_revenue"])
        self.assertIn("owner", client)
        self.assertTrue(any(item["kind"] == "client" and item["id"] == self.ws.id for item in result["agency_map"]))
        self.assertEqual(set(result["trends"]), {"work_created", "reviews_opened", "campaign_metrics"})
        self.assertNotIn("Auremgrid Demo", repr(result))
        self.assertNotIn("Demo Owner", repr(result))

    def test_cosmo_queue_is_derived_from_canonical_attention_and_routes_to_real_surfaces(self) -> None:
        work = self.os.work_ops.create(
            self.org.id, self.ws.id, self.owner.id,
            "Recover overdue launch", "Replan the overdue launch work", "Owner",
            deadline="2020-01-01",
        )

        result = self.dashboard.command(self.org.id, self.owner.id)
        self.assertEqual(result["cosmo"]["name"], "Cosmo")
        self.assertEqual(result["cosmo"]["mode"], "evidence_grounded")
        self.assertTrue(result["cosmo"]["writes_require_canonical_routes"])
        queued = next(item for item in result["cosmo"]["queue"] if item["source_id"] == work.id)
        self.assertEqual(queued["source_type"], "work_item")
        self.assertEqual(queued["surface"], "Work")
        self.assertEqual(queued["action_kind"], "open_surface")

    def test_settings_read_model_is_authenticated_and_backend_sourced(self) -> None:
        settings = self.dashboard.settings(self.owner_identity, self.org.id, self.ws.id)
        self.assertEqual(settings["identity"]["organization"]["id"], self.org.id)
        self.assertEqual(settings["identity"]["person"]["id"], self.owner.id)
        self.assertEqual(settings["workspace"]["id"], self.ws.id)
        self.assertIn("workspace_read", settings["permissions"]["capabilities"])
        self.assertIn("pending_count", settings["approvals"])
        self.assertIn("integrations", settings)
        self.assertIn("health", settings)

    def test_dashboard_settings_endpoint_requires_auth_and_returns_renderable_health(self) -> None:
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address

            def request(path: str, token: str | None = None) -> tuple[int, dict]:
                connection = HTTPConnection(host, port, timeout=5)
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                return response.status, payload

            path = f"/dashboard/settings?organization_id={self.org.id}&workspace_id={self.ws.id}"
            status, body = request(path)
            self.assertEqual(status, 401)
            self.assertEqual(body["error"], "authentication_error")

            token, _ = issue_identity(self.os, self.org.id, self.owner.id, self.ws.id, self.visible_actor.id)
            status, settings = request(path, token)
            self.assertEqual(status, 200)
            self.assertEqual(settings["workspace"]["id"], self.ws.id)
            self.assertIsInstance(settings["health"]["schema_version"], int)
            self.assertIn(settings["health"]["status"], {"healthy", "degraded"})

            status, health = request("/health/detailed")
            self.assertEqual(status, 200)
            self.assertIsInstance(health["schema_version"], int)
            self.assertIn("warnings", health)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_empty_dashboard_payload_is_renderable_and_not_sampled(self) -> None:
        empty_os = CompanyOS(":memory:")
        try:
            org = empty_os.create_organization("Quiet Agency", "org_quiet")
            person = empty_os.create_person(org.id, "Quiet Owner", "quiet.owner@example.invalid", role="owner", person_id="person_quiet")
            result = empty_os.dashboard.command(org.id, person.id)
            self.assertEqual(result["metrics"]["active_clients"], 0)
            self.assertEqual(result["workspaces"], [])
            self.assertEqual(result["attention"], [])
            self.assertEqual(result["clients"], [])
            self.assertNotIn("SAMPLE DATA", repr(result))
            self.assertNotIn("Client Alpha", repr(result))
        finally:
            empty_os.close()

    def test_release_route_surface_for_p6_p15_rows(self) -> None:
        http = Path(__file__).parents[1].joinpath("src", "auremgrid", "api", "http.py").read_text(encoding="utf-8")
        service_names = ("workflow_ops", "client_ops", "agency_ops", "agent_ops")
        for name in service_names:
            self.assertTrue(hasattr(self.os, name), name)
        for route in (
            "/work/items",
            "/reviews/comment",
            "/dashboard/review-center",
            "/meetings/responsibilities",
            "/decisions",
            "/dashboard/workflows",
            "/workflows/runs",
            "/workflows/stages/start",
            "/workflows/evidence",
            "/workflows/approvals/request",
            "/workflows/approvals/decide",
            "/workflows/handoffs/acknowledge",
            "/signals",
            "/risks",
            "/opportunities",
            "/finance",
            "/campaigns",
            "/creative",
            "/agents",
        ):
            self.assertIn(route, http)

    def test_newer_feedback_performance_forecast_retention_batch_is_separately_guarded(self) -> None:
        http = Path(__file__).parents[1].joinpath("src", "auremgrid", "api", "http.py").read_text(encoding="utf-8")
        for name in ("feedback", "performance", "forecasts", "retention"):
            self.assertTrue(hasattr(self.os, name), name)
        for route in (
            "/feedback/record",
            "/feedback/patterns",
            "/feedback/patterns/promote",
            "/feedback/patterns/decide",
            "/insights/performance",
            "/insights/performance/generate",
            "/insights/performance/decide",
            "/forecasts",
            "/forecasts/generate",
            "/retention/policies",
            "/retention/execute",
        ):
            self.assertIn(route, http)


if __name__ == "__main__":
    unittest.main()
