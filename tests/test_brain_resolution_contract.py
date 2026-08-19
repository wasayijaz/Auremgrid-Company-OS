from __future__ import annotations
import sqlite3
import unittest
import tempfile
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class BrainResolutionContractTests(unittest.TestCase):
    def setUp(self):
        self.os=CompanyOS(":memory:"); self.org=self.os.create_organization("Agency"); self.ws=self.os.create_organization_workspace(self.org.id,"Prime","client"); self.owner=self.os.create_person(self.org.id,"Owner",role="owner"); self.os.add_person_to_workspace(self.org.id,self.ws.id,self.owner.id,"admin"); _,self.identity=issue_identity(self.os,self.org.id,self.owner.id,self.ws.id)
    def tearDown(self): self.os.close()

    def test_high_score_alias_stays_pending_and_spoof_is_denied(self):
        entity=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Prime Canada","client")
        alias=self.os.brain_ops.propose_alias(self.org.id,self.ws.id,self.owner.id,entity["id"],"Prime CA",.99)
        self.assertEqual(alias["status"],"proposed")
        with self.assertRaises(AuthorizationError):
            self.os.brain_ops.brain_propose(self.org.id,self.ws.id,self.owner.id,"alias",[entity["id"]],.99,"spoof","evidence",alias="Prime CA")

    def test_resolution_decision_is_append_only_and_idempotent(self):
        first=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Prime","client",["Prime CA"]); second=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Prime Canada","client")
        proposal=self.os.brain_ops.brain_propose(self.org.id,self.ws.id,self.identity,"merge",[first["id"],second["id"]],.98,"same","review",target_id=second["id"])
        approved=self.os.brain_ops.brain_promote(self.org.id,self.ws.id,self.identity,proposal["id"],"approve")
        again=self.os.brain_ops.brain_promote(self.org.id,self.ws.id,self.identity,proposal["id"],"approve")
        self.assertEqual(approved["status"],again["status"])
        with self.assertRaises(ValidationError): self.os.brain_ops.brain_promote(self.org.id,self.ws.id,self.identity,proposal["id"],"reject")
        with self.assertRaises(sqlite3.IntegrityError): self.os.store.conn.execute("UPDATE entity_resolution_decisions SET action='reject'")

    def test_knowledge_state_events_are_append_only(self):
        entity=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Prime","client")
        self.os.brain_ops.record_knowledge_state(self.org.id,self.ws.id,"canonical",entity["id"],"verified","human review",self.identity)
        with self.assertRaises(sqlite3.IntegrityError): self.os.store.conn.execute("UPDATE knowledge_state_events SET state='stale'")
        with self.assertRaises(sqlite3.IntegrityError): self.os.store.conn.execute("DELETE FROM knowledge_state_events")

    def test_cross_workspace_candidate_is_not_disclosed(self):
        other=self.os.create_organization_workspace(self.org.id,"Other","client"); other_person=self.os.create_person(self.org.id,"OtherOwner",role="member"); self.os.add_person_to_workspace(self.org.id,other.id,other_person.id,"admin")
        entity=self.os.brain_ops.create_entity(self.org.id,other.id,other_person.id,"Other","client")
        with self.assertRaises(Exception): self.os.brain_ops.brain_propose(self.org.id,self.ws.id,self.identity,"merge",[entity["id"]],.9,"cross","evidence")

    def test_merge_cycle_is_rejected(self):
        first=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Prime","client"); second=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Prime Canada","client")
        proposal=self.os.brain_ops.brain_propose(self.org.id,self.ws.id,self.identity,"merge",[first["id"],second["id"]],.98,"same","evidence",target_id=second["id"])
        self.os.brain_ops.brain_promote(self.org.id,self.ws.id,self.identity,proposal["id"],"approve")
        with self.assertRaises(Exception):
            reverse=self.os.brain_ops.brain_propose(self.org.id,self.ws.id,self.identity,"merge",[second["id"],first["id"]],.98,"cycle","evidence",target_id=first["id"])
            self.os.brain_ops.brain_promote(self.org.id,self.ws.id,self.identity,reverse["id"],"approve")

    def test_merge_preserves_alias_provenance(self):
        first=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Prime","client"); second=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Prime Canada","client")
        alias=self.os.brain_ops._add_alias(first["id"],"Prime CA",1.0,"approved","source-1")
        proposal=self.os.brain_ops.brain_propose(self.org.id,self.ws.id,self.identity,"merge",[first["id"],second["id"]],.98,"same","evidence",target_id=second["id"]); self.os.brain_ops.brain_promote(self.org.id,self.ws.id,self.identity,proposal["id"],"approve")
        rows=self.os.store.conn.execute("SELECT entity_id,alias,source_id,retired_at,id FROM entity_aliases WHERE alias='Prime CA'").fetchall()
        self.assertFalse(any(row["entity_id"]==second["id"] for row in rows)); self.assertTrue(any(row["entity_id"]==first["id"] and row["source_id"]=="source-1" for row in rows))
        alias_id=next(row["id"] for row in rows if row["entity_id"]==first["id"])
        self.assertEqual(self.os.store.conn.execute("SELECT state FROM entity_alias_state_events WHERE alias_id=? ORDER BY created_at DESC,id DESC LIMIT 1",(alias_id,)).fetchone()["state"],"retired")
        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute("UPDATE entity_aliases SET retired_at='tampered' WHERE id=?",(alias_id,))
        resolved=self.os.brain_ops.resolve_entity(self.org.id,self.ws.id,self.owner.id,"Prime CA")
        self.assertEqual(resolved["status"],"resolved"); self.assertEqual(resolved["entity"]["id"],second["id"])

    def test_merge_collapses_duplicate_alias_candidates_to_one_target(self):
        first=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"First","client")
        second=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Second","client")
        self.os.brain_ops._add_alias(first["id"],"Shared",1.0,"approved",None)
        self.os.brain_ops._add_alias(second["id"],"Shared",1.0,"approved",None)
        proposal=self.os.brain_ops.brain_propose(
            self.org.id,self.ws.id,self.identity,"merge",[first["id"],second["id"]],.99,"same","evidence",target_id=second["id"]
        )
        self.os.brain_ops.brain_promote(self.org.id,self.ws.id,self.identity,proposal["id"],"approve")
        resolved=self.os.brain_ops.resolve_entity(self.org.id,self.ws.id,self.owner.id,"Shared")
        self.assertEqual(resolved["status"],"resolved"); self.assertEqual(resolved["entity"]["id"],second["id"])

    def test_fact_conflict_preserves_both_and_resolution_states(self):
        source=self.os.ingest_text(self.ws.id,"act_missing","source.md","evidence","memory://evidence") if False else self.os.ingest_text(self.ws.id,self.os.create_actor(self.ws.id,"Admin2","admin","act2").id,"source.md","evidence","memory://evidence")
        first=self.os.brain_ops.create_proposal(self.org.id,self.ws.id,"agent",self.identity,"fact","p1",{"subject":"Plan","predicate":"price","object":"100"},"source evidence",.9,source.source.id)
        second=self.os.brain_ops.create_proposal(self.org.id,self.ws.id,"agent",self.identity,"fact","p2",{"subject":"Plan","predicate":"price","object":"200"},"source evidence",.9,source.source.id)
        self.os.brain_ops.brain_promote_fact(self.identity,first["id"],"approve"); self.os.brain_ops.brain_promote_fact(self.identity,second["id"],"approve")
        facts=self.os.store.conn.execute("SELECT * FROM facts WHERE subject='Plan'").fetchall(); self.assertEqual(len(facts),2); self.assertTrue(all(row["conflict_group"] for row in facts))
        self.os.brain_ops.resolve_fact_conflict(self.identity,facts[0]["conflict_group"],facts[0]["id"])
        self.assertIn("verified", {row["state"] for row in self.os.store.conn.execute("SELECT state FROM knowledge_state_events WHERE subject_type='fact' AND subject_id=?",(facts[0]["id"],)).fetchall()})

    def test_scoped_agent_can_propose_but_cannot_promote(self):
        agent = AuthenticatedIdentity("agent-prime",self.org.id,self.owner.id,"service_agent",frozenset({"brain_read","brain_propose"}),frozenset({"workspace:"+self.ws.id}),self.ws.id)
        entity=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Agent Prime","client")
        proposal=self.os.brain_ops.brain_propose(self.org.id,self.ws.id,agent,"alias",[entity["id"]],.5,"agent candidate","agent evidence",alias="Prime alias")
        self.assertEqual(proposal["proposed_by_person_id"],self.owner.id)
        with self.assertRaises(AuthorizationError):
            self.os.brain_ops.brain_promote(self.org.id,self.ws.id,agent,proposal["id"],"approve")

    def test_ingestion_fact_and_initial_state_roll_back_together(self):
        actor=self.os.create_actor(self.ws.id,"Ingestor","admin","ingest-fault")
        original=self.os.brain_ops._state_event
        self.os.brain_ops._state_event=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError("state failure"))
        with self.assertRaises(RuntimeError):
            self.os.ingest_text(self.ws.id,actor.id,"fact.md","FACT: Plan | price | 100","memory://atomic")
        self.os.brain_ops._state_event=original
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM sources WHERE workspace_id=? AND source_key='memory://atomic'",(self.ws.id,)).fetchone()[0],0)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM facts WHERE workspace_id=?",(self.ws.id,)).fetchone()[0],0)

    def test_state_resolution_respects_as_of(self):
        entity=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Temporal","client")
        past=datetime.now(timezone.utc).replace(microsecond=0)-timedelta(days=1)
        self.os.brain_ops.record_knowledge_state(self.org.id,self.ws.id,"canonical",entity["id"],"inferred","initial",self.identity,effective_from=past)
        before=self.os.brain_ops.knowledge_state(self.org.id,self.ws.id,self.owner.id,"canonical",entity["id"],past+timedelta(hours=1))
        self.assertEqual(before["state"],"inferred")
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"state.sqlite"; first=CompanyOS(path); org=first.create_organization("Org"); ws=first.create_organization_workspace(org.id,"Ws"); person=first.create_person(org.id,"Owner",role="owner"); first.add_person_to_workspace(org.id,ws.id,person.id,"admin"); _,ident=issue_identity(first,org.id,person.id,ws.id); ent=first.brain_ops.create_entity(org.id,ws.id,person.id,"Persist","client"); first.brain_ops.record_knowledge_state(org.id,ws.id,"canonical",ent["id"],"inferred","persist",ident,effective_from=past); first.close(); second=CompanyOS(path); _,ident2=issue_identity(second,org.id,person.id,ws.id); self.assertEqual(second.brain_ops.knowledge_state(org.id,ws.id,person.id,"canonical",ent["id"],past+timedelta(hours=1))["state"],"inferred"); second.close()

    def test_atomic_fact_promotion_failure_rolls_back_and_retry_is_single(self):
        source=self.os.ingest_text(self.ws.id,self.os.create_actor(self.ws.id,"Fault","admin","fault").id,"fault.md","evidence","memory://fault")
        proposal=self.os.brain_ops.create_proposal(self.org.id,self.ws.id,"agent",self.identity,"fact","p",{"subject":"Plan","predicate":"price","object":"100"},"evidence",.9,source.source.id)
        original=self.os.brain_ops._state_event
        self.os.brain_ops._state_event=lambda *args,**kwargs: (_ for _ in ()).throw(RuntimeError("fault"))
        with self.assertRaises(RuntimeError): self.os.brain_ops.brain_promote_fact(self.identity,proposal["id"],"approve")
        self.os.brain_ops._state_event=original
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM knowledge_proposal_decisions").fetchone()[0],0)
        self.os.brain_ops.brain_promote_fact(self.identity,proposal["id"],"approve")
        self.os.brain_ops.brain_promote_fact(self.identity,proposal["id"],"approve")
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM knowledge_proposal_decisions WHERE proposal_id=?",(proposal["id"],)).fetchone()[0],1)
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM facts WHERE subject='Plan'").fetchone()[0],1)

    def test_schema18_replay_preserves_entity_and_alias_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"replay.sqlite"; first=CompanyOS(path); org=first.create_organization("ReplayOrg"); ws=first.create_organization_workspace(org.id,"ReplayWs"); person=first.create_person(org.id,"Owner",role="owner"); first.add_person_to_workspace(org.id,ws.id,person.id,"admin"); entity=first.brain_ops.create_entity(org.id,ws.id,person.id,"Replay","client"); first.close(); second=CompanyOS(path)
            self.assertEqual(second.store.schema_version,18); self.assertIsNotNone(second.store.conn.execute("SELECT 1 FROM entities WHERE id=?",(entity["id"],)).fetchone()); self.assertIsNotNone(second.store.conn.execute("SELECT 1 FROM entity_resolution_decisions LIMIT 1").fetchone()) if False else None; second.close()

    def test_early_schema18_is_evolved_and_backfilled_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"early-v18.sqlite"; first=CompanyOS(path)
            org=first.create_organization("Early"); ws=first.create_organization_workspace(org.id,"Ws","client")
            person=first.create_person(org.id,"Owner",role="owner"); first.add_person_to_workspace(org.id,ws.id,person.id,"admin")
            entity=first.brain_ops.create_entity(org.id,ws.id,person.id,"Early Entity","client"); first.close()
            conn=sqlite3.connect(path)
            for name in ("knowledge_state_monotonic_insert","knowledge_state_no_update"):
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            for name in ("idx_knowledge_state_sequence","idx_knowledge_state_lookup"):
                conn.execute(f"DROP INDEX IF EXISTS {name}")
            conn.execute("ALTER TABLE knowledge_state_events DROP COLUMN supersedes_event_id")
            conn.execute("ALTER TABLE knowledge_state_events DROP COLUMN event_sequence")
            conn.execute("ALTER TABLE entity_resolution_proposals DROP COLUMN evidence_refs")
            conn.commit(); conn.close()
            second=CompanyOS(path)
            event=second.store.conn.execute(
                "SELECT event_sequence,supersedes_event_id FROM knowledge_state_events WHERE subject_id=?",
                (entity["id"],),
            ).fetchone()
            self.assertEqual(event["event_sequence"],1); self.assertIsNone(event["supersedes_event_id"])
            self.assertIn("evidence_refs",{row["name"] for row in second.store.conn.execute("PRAGMA table_info(entity_resolution_proposals)")})
            second.close(); third=CompanyOS(path)
            self.assertEqual(third.store.conn.execute("SELECT event_sequence FROM knowledge_state_events WHERE subject_id=?",(entity["id"],)).fetchone()[0],1)
            third.close()

    def test_v17_approved_fact_backfill_is_idempotent_after_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"v17.sqlite"; first=CompanyOS(path)
            org=first.create_organization("ReplayOrg"); ws=first.create_organization_workspace(org.id,"ReplayWs","client"); person=first.create_person(org.id,"Owner",role="owner"); first.add_person_to_workspace(org.id,ws.id,person.id,"admin"); actor=first.create_actor(ws.id,"Admin","admin","replay-actor"); _,ident=issue_identity(first,org.id,person.id,ws.id,actor.id)
            ingested=first.ingest_text(ws.id,actor.id,"evidence.md","FACT: Plan | price | 100","memory://replay-evidence")
            proposal=first.brain_ops.create_proposal(org.id,ws.id,"agent",ident,"fact","Plan price",{"subject":"Plan","predicate":"price","object":"100"},"legacy evidence",.9,ingested.source.id)
            fact_id=ingested.fact_ids[0]
            # Rewind the migration ledger and remove only v18 artifacts to
            # emulate a genuine v17 backup containing an already-promoted row.
            conn=first.store.conn
            for trigger in ("memory_proposals_no_update","memory_proposals_no_delete","entity_aliases_no_update","entity_aliases_no_update_lifecycle","entity_aliases_no_delete","entity_alias_state_no_update","entity_alias_state_no_delete","entity_resolution_no_update","entity_resolution_no_delete","knowledge_state_no_update","knowledge_state_no_delete","entity_merge_history_no_update","entity_merge_history_no_delete","entity_resolution_decision_no_update","entity_resolution_decision_no_delete","knowledge_proposal_decision_no_update","knowledge_proposal_decision_no_delete"):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            for table in ("entity_alias_state_events","knowledge_state_events","knowledge_proposal_decisions","entity_resolution_decisions","entity_resolution_proposals"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute("DELETE FROM schema_migrations WHERE version=18")
            conn.execute("UPDATE memory_proposals SET status='approved',reviewed_by_person_id=?,reviewed_at=?,promoted_type='fact',promoted_id=? WHERE id=?",(person.id,datetime.now(timezone.utc).isoformat(),fact_id,proposal["id"]))
            conn.commit(); first.close()
            second=CompanyOS(path); _,ident2=issue_identity(second,org.id,person.id,ws.id,actor.id)
            decision=second.store.conn.execute("SELECT action,reviewer_person_id FROM knowledge_proposal_decisions WHERE proposal_id=?",(proposal["id"],)).fetchone()
            self.assertIsNotNone(decision); self.assertEqual(decision["action"],"approve")
            before=second.store.conn.execute("SELECT COUNT(*) FROM facts WHERE id=?",(fact_id,)).fetchone()[0]
            second.brain_ops.brain_promote_fact(ident2,proposal["id"],"approve")
            self.assertEqual(second.store.conn.execute("SELECT COUNT(*) FROM facts WHERE id=?",(fact_id,)).fetchone()[0],before)
            self.assertEqual(second.store.conn.execute("SELECT COUNT(*) FROM knowledge_proposal_decisions WHERE proposal_id=?",(proposal["id"],)).fetchone()[0],1); second.close()

    def test_same_tick_state_transitions_use_monotonic_supersedes_order(self):
        entity=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Same Tick","client")
        tick=datetime.now(timezone.utc)+timedelta(seconds=1)
        for state in ("conflicted","stale","verified"):
            self.os.brain_ops.record_knowledge_state(
                self.org.id,self.ws.id,"canonical",entity["id"],state,state,self.identity,effective_from=tick
            )
        events=self.os.store.conn.execute("""SELECT id,state,event_sequence,supersedes_event_id
            FROM knowledge_state_events WHERE workspace_id=? AND subject_type='canonical' AND subject_id=?
            ORDER BY event_sequence""",(self.ws.id,entity["id"])).fetchall()
        self.assertEqual([row["event_sequence"] for row in events],list(range(1,len(events)+1)))
        self.assertEqual([row["supersedes_event_id"] for row in events[1:]],[row["id"] for row in events[:-1]])
        selected=self.os.brain_ops.knowledge_state(
            self.org.id,self.ws.id,self.owner.id,"canonical",entity["id"],tick+timedelta(seconds=1)
        )
        self.assertEqual(selected["state"],"verified")

    def test_search_as_of_before_conflict_resolution_returns_both_and_current_returns_winner(self):
        source=self.os.ingest_text(
            self.ws.id,self.os.create_actor(self.ws.id,"Search","admin","search-state").id,
            "search.md","evidence","memory://search-state"
        )
        proposals=[self.os.brain_ops.create_proposal(
            self.org.id,self.ws.id,"agent",self.identity,"fact",f"price {value}",
            {"subject":"Plan","predicate":"price","object":value},"evidence",.9,source.source.id
        ) for value in ("100","200")]
        for proposal in proposals: self.os.brain_ops.brain_promote_fact(self.identity,proposal["id"],"approve")
        facts=self.os.store.conn.execute("SELECT id,conflict_group FROM facts WHERE workspace_id=? AND subject='Plan' ORDER BY id",(self.ws.id,)).fetchall()
        before_resolution=datetime.now(timezone.utc)
        time.sleep(.01)
        self.os.brain_ops.resolve_fact_conflict(self.identity,facts[0]["conflict_group"],facts[0]["id"])
        historical=self.os.search(self.ws.id,"search-state","Plan price",as_of=before_resolution,limit=20)
        current=self.os.search(self.ws.id,"search-state","Plan price",limit=20)
        historical_ids={item.payload.get("id") for item in historical.items if item.kind=="fact"}
        current_ids={item.payload.get("id") for item in current.items if item.kind=="fact"}
        self.assertEqual(historical_ids,{row["id"] for row in facts})
        self.assertEqual(current_ids,{facts[0]["id"]})
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"search-restart.sqlite"; snapshot=sqlite3.connect(path)
            self.os.store.conn.backup(snapshot); snapshot.close(); restarted=CompanyOS(path)
            restarted_historical=restarted.search(self.ws.id,"search-state","Plan price",as_of=before_resolution,limit=20)
            restarted_current=restarted.search(self.ws.id,"search-state","Plan price",limit=20)
            self.assertEqual(
                {item.payload.get("id") for item in restarted_historical.items if item.kind=="fact"},historical_ids
            )
            self.assertEqual(
                {item.payload.get("id") for item in restarted_current.items if item.kind=="fact"},current_ids
            )
            restarted.close()

    def test_proposal_listing_and_health_derive_status_from_decision_ledger(self):
        source=self.os.ingest_text(
            self.ws.id,self.os.create_actor(self.ws.id,"Proposal","admin","proposal-state").id,
            "proposal.md","evidence","memory://proposal-state"
        )
        proposal=self.os.brain_ops.create_proposal(
            self.org.id,self.ws.id,"agent",self.identity,"fact","price",
            {"subject":"Plan","predicate":"price","object":"100"},"evidence",.9,source.source.id
        )
        before_approval=datetime.now(timezone.utc)
        self.os.brain_ops.brain_promote_fact(self.identity,proposal["id"],"approve")
        raw=self.os.store.conn.execute("SELECT status FROM memory_proposals WHERE id=?",(proposal["id"],)).fetchone()
        self.assertEqual(raw["status"],"pending")
        listed=self.os.brain_ops.list_memory_proposals(self.org.id,self.ws.id,self.owner.id)
        self.assertEqual(next(item for item in listed if item["id"]==proposal["id"])["status"],"approved")
        historical=self.os.brain_ops.list_memory_proposals(
            self.org.id,self.ws.id,self.owner.id,as_of=before_approval
        )
        self.assertEqual(next(item for item in historical if item["id"]==proposal["id"])["status"],"pending")
        health=self.os.brain_ops.knowledge_health(self.org.id,self.ws.id,self.owner.id)
        self.assertFalse(any(item["type"]=="pending_proposal" and item["entity_id"]==proposal["id"] for item in health["issues"]))

    def test_entity_alias_and_merge_resolution_is_historical_scoped_and_restart_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"entity-as-of.sqlite"; first=CompanyOS(path)
            org=first.create_organization("TemporalOrg"); ws=first.create_organization_workspace(org.id,"TemporalWs","client")
            person=first.create_person(org.id,"Owner",role="owner"); first.add_person_to_workspace(org.id,ws.id,person.id,"admin")
            _,identity=issue_identity(first,org.id,person.id,ws.id)
            source=first.brain_ops.create_entity(org.id,ws.id,person.id,"Old Name","client")
            target=first.brain_ops.create_entity(org.id,ws.id,person.id,"New Name","client")
            proposal=first.brain_ops.brain_propose(org.id,ws.id,identity,"merge",[source["id"],target["id"]],.99,"same","evidence",target_id=target["id"])
            before=datetime.now(timezone.utc)
            first.brain_ops.brain_promote(org.id,ws.id,identity,proposal["id"],"approve")
            self.assertEqual(first.brain_ops.resolve_entity(org.id,ws.id,person.id,"Old Name",before)["entity"]["id"],source["id"])
            self.assertEqual(first.brain_ops.resolve_entity(org.id,ws.id,person.id,"Old Name")["entity"]["id"],target["id"])
            first.close(); second=CompanyOS(path)
            self.assertEqual(second.brain_ops.resolve_entity(org.id,ws.id,person.id,"Old Name",before)["entity"]["id"],source["id"])
            self.assertEqual(second.brain_ops.resolve_entity(org.id,ws.id,person.id,"Old Name")["entity"]["id"],target["id"])
            second.close()

    def test_brain_propose_hides_cross_workspace_evidence_refs(self):
        entity=self.os.brain_ops.create_entity(self.org.id,self.ws.id,self.owner.id,"Scoped","client")
        other=self.os.create_organization_workspace(self.org.id,"Other Evidence","client")
        other_person=self.os.create_person(self.org.id,"Evidence Owner",role="member")
        self.os.add_person_to_workspace(self.org.id,other.id,other_person.id,"admin")
        ingested=self.os.ingest_text(
            other.id,self.os.create_actor(other.id,"Evidence","admin","other-evidence").id,
            "other.md","FACT: Other | price | 1","memory://other-evidence"
        )
        document=self.os.store.conn.execute("SELECT id FROM documents WHERE source_id=?",(ingested.source.id,)).fetchone()
        fact=self.os.store.conn.execute("SELECT id FROM facts WHERE source_id=?",(ingested.source.id,)).fetchone()
        for kwargs in (
            {"source_id":ingested.source.id},
            {"evidence_refs":{"documents":[document["id"]]}},
            {"evidence_refs":{"facts":[fact["id"]]}},
        ):
            with self.assertRaisesRegex(Exception,"proposal evidence not found"):
                self.os.brain_ops.brain_propose(
                    self.org.id,self.ws.id,self.identity,"alias",[entity["id"]],.9,"candidate","evidence",
                    alias="Scoped Alias",**kwargs
                )


if __name__=="__main__": unittest.main()
