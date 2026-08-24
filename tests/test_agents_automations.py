from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.reversible_actions import validate_reversible_action_descriptor
from auremgrid.services.worker import run_one_job


class AgentAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os=CompanyOS(":memory:"); self.org=self.os.create_organization("Agency")
        self.ws=self.os.create_organization_workspace(self.org.id,"Prime","client")
        self.owner=self.os.create_person(self.org.id,"Owner",role="owner")
        self.member=self.os.create_person(self.org.id,"Member")
        self.os.add_person_to_workspace(self.org.id,self.ws.id,self.owner.id,"admin")
        self.agents=self.os.agent_ops.seed_primary_agents(self.org.id,self.owner.id)
        self.os.auth.create_principal(self.org.id,self.owner.id,"owner@automation.test")

    def tearDown(self) -> None: self.os.close()

    def test_agent_permissions_queue_and_run_trace_are_durable(self) -> None:
        luna=next(a for a in self.agents if a["name"]=="Luna")
        self.os.agent_ops.configure_agent(self.org.id,self.owner.id,luna["id"],"local",["work.list"],[self.ws.id],["domain.write"])
        task=self.os.agent_ops.enqueue_task(self.org.id,self.owner.id,luna["id"],"Inspect work","List open work",self.ws.id)
        run=self.os.agent_ops.start_run(self.org.id,self.owner.id,luna["id"],task["id"])
        self.os.agent_ops.record_tool_call(self.org.id,luna["id"],run["id"],"work.list",{},"0 items")
        completed=self.os.agent_ops.complete_run(self.org.id,luna["id"],run["id"],"No open work",10,5,0.01,["work_items"])
        self.assertEqual(completed["status"],"completed")
        center=self.os.agent_ops.command_center(self.org.id,self.owner.id)
        self.assertEqual(center["recent_runs"][0]["output_tokens"],5)
        self.assertEqual(center["token_cost"],0.01)

    def test_agent_cannot_queue_work_in_unapproved_workspace(self) -> None:
        luna=next(a for a in self.agents if a["name"]=="Luna")
        with self.assertRaises(AuthorizationError):
            self.os.agent_ops.enqueue_task(self.org.id,self.owner.id,luna["id"],"Leak","Read Prime",self.ws.id)

    def test_new_automation_runs_in_training_mode_with_human_checkpoint(self) -> None:
        automation=self.os.agent_ops.create_automation(self.org.id,self.owner.id,"Silence risk","client_silence",
            [{"field":"days","operator":"gt","value":5}],[{"type":"risk.create","config":{"type":"relationship"}}],"auto")
        self.assertEqual(automation["status"],"training")
        ignored=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":3})
        self.assertEqual(ignored,[])
        runs=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":6})
        self.assertEqual(runs[0]["status"],"waiting_approval")
        self.assertIsNotNone(runs[0]["approval_request_id"])

    def test_approved_training_run_executes_then_allows_activation(self) -> None:
        automation=self.os.agent_ops.create_automation(self.org.id,self.owner.id,"Silence risk","client_silence",
            [{"field":"days","operator":"gt","value":5}],[{"type":"risk.create","config":{"workspace_id":self.ws.id,"type":"relationship","severity":"high"}}],"auto")
        run=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":6,"workspace_id":self.ws.id})[0]
        self.os.agency_ops.decide_approval(self.org.id,self.owner.id,run["approval_request_id"],True)
        queued=self.os.agent_ops.execute_approved_automation_run(self.org.id,self.owner.id,run["run_id"])
        self.assertEqual(queued["status"],"queued")
        self.assertEqual(len(self.os.client_ops.list_risks(self.org.id,self.ws.id,self.owner.id)),0)
        completed=run_one_job(self.os,self.org.id,self.ws.id,"automation-worker")
        self.assertEqual(completed["status"],"succeeded");self.assertEqual(len(self.os.client_ops.list_risks(self.org.id,self.ws.id,self.owner.id)),1)
        active=self.os.agent_ops.activate_automation(self.org.id,self.owner.id,automation["id"]);self.assertEqual(active["status"],"active")

    def test_active_automation_dedupes_unchanged_trigger_and_uses_worker(self) -> None:
        automation=self.os.agent_ops.create_automation(self.org.id,self.owner.id,"Silence risk","client_silence",
            [{"field":"days","operator":"gt","value":5}],[{"type":"risk.create","config":{"workspace_id":self.ws.id,"type":"relationship","severity":"high"}}],"auto")
        training=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":6,"workspace_id":self.ws.id})[0]
        self.os.agency_ops.decide_approval(self.org.id,self.owner.id,training["approval_request_id"],True)
        self.os.agent_ops.execute_approved_automation_run(self.org.id,self.owner.id,training["run_id"])
        run_one_job(self.os,self.org.id,self.ws.id,"automation-training-worker")
        self.os.agent_ops.activate_automation(self.org.id,self.owner.id,automation["id"])

        first=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":9,"workspace_id":self.ws.id})[0]
        second=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":9,"workspace_id":self.ws.id})[0]
        self.assertEqual(first["run_id"],second["run_id"])
        self.assertTrue(second["deduped"])
        self.assertEqual(first["status"],"queued")
        self.assertIsNotNone(first["job_id"])
        approval=self.os.store.conn.execute("SELECT status,policy FROM approval_requests WHERE id=?",(first["approval_request_id"],)).fetchone()
        self.assertEqual(dict(approval),{"status":"approved","policy":"auto"})
        self.assertEqual(len(self.os.client_ops.list_risks(self.org.id,self.ws.id,self.owner.id)),1)
        run_one_job(self.os,self.org.id,self.ws.id,"automation-active-worker")
        self.assertEqual(len(self.os.client_ops.list_risks(self.org.id,self.ws.id,self.owner.id)),2)
        third=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":9,"workspace_id":self.ws.id})[0]
        self.assertEqual(third["run_id"],first["run_id"])
        self.assertEqual(len(self.os.client_ops.list_risks(self.org.id,self.ws.id,self.owner.id)),2)

    def test_active_auto_unsupported_action_still_requires_human_and_never_queues_job(self) -> None:
        automation=self.os.agent_ops.create_automation(self.org.id,self.owner.id,"External send","client_silence",
            [{"field":"days","operator":"gt","value":5}],[{"type":"email.send","config":{"workspace_id":self.ws.id},"one_way":True}],"auto")
        training=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":6,"workspace_id":self.ws.id})[0]
        self.os.agency_ops.decide_approval(self.org.id,self.owner.id,training["approval_request_id"],True)
        with self.assertRaises(ValidationError):
            self.os.agent_ops.execute_approved_automation_run(self.org.id,self.owner.id,training["run_id"])
        self.os.store.conn.execute("UPDATE automations SET status='active' WHERE id=?",(automation["id"],))
        self.os.store.conn.commit()

        run=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":9,"workspace_id":self.ws.id})[0]

        self.assertEqual(run["status"],"waiting_approval")
        self.assertIsNone(run["job_id"])
        approval=self.os.store.conn.execute("SELECT status,policy FROM approval_requests WHERE id=?",(run["approval_request_id"],)).fetchone()
        self.assertEqual(dict(approval),{"status":"pending","policy":"human"})
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM jobs WHERE type='automation.execute'").fetchone()[0],0)

    def _activate_after_training(self, actions: list[dict[str, object]]) -> dict[str, object]:
        automation=self.os.agent_ops.create_automation(self.org.id,self.owner.id,"Allowlisted action","manual_trigger",
            [{"field":"ready","operator":"eq","value":True}],actions,"auto")
        training=self.os.agent_ops.trigger_automations(self.org.id,"manual_trigger",{"ready":True,"workspace_id":self.ws.id})[0]
        self.os.agency_ops.decide_approval(self.org.id,self.owner.id,training["approval_request_id"],True)
        self.os.agent_ops.execute_approved_automation_run(self.org.id,self.owner.id,training["run_id"])
        run_one_job(self.os,self.org.id,self.ws.id,"automation-training-worker")
        return self.os.agent_ops.activate_automation(self.org.id,self.owner.id,automation["id"])

    def _trigger_active_auto(self, payload: dict[str, object]) -> dict[str, object]:
        run=self.os.agent_ops.trigger_automations(self.org.id,"manual_trigger",{"ready":True,"workspace_id":self.ws.id,**payload})[0]
        self.assertEqual(run["status"],"queued")
        self.assertIsNotNone(run["job_id"])
        approval=self.os.store.conn.execute("SELECT status,policy FROM approval_requests WHERE id=?",(run["approval_request_id"],)).fetchone()
        self.assertEqual(dict(approval),{"status":"approved","policy":"auto"})
        completed=run_one_job(self.os,self.org.id,self.ws.id,"automation-active-worker")
        self.assertEqual(completed["status"],"succeeded")
        return run

    def test_active_auto_acknowledge_attention_runs_unattended(self) -> None:
        now=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        snapshot_id=self.os.jobs.new_id("intelsnap")
        attention_id=self.os.jobs.new_id("intelattn")
        fingerprint="attention-auto-fingerprint"
        self.os.store.conn.execute(
            "INSERT INTO proactive_intelligence_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id,self.org.id,self.ws.id,self.owner.id,"workspace",1,"ready",None,"attention-auto-projection",now,"{}", "[]", now),
        )
        self.os.store.conn.execute(
            "INSERT INTO proactive_intelligence_attention_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (attention_id,snapshot_id,self.org.id,self.ws.id,self.owner.id,1,"Follow up","Client response is quiet","open","[]",now,now),
        )
        self.os.store.conn.execute(
            "INSERT INTO proactive_intelligence_attention_lifecycle VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.os.jobs.new_id("intelcycle"),self.org.id,self.ws.id,self.owner.id,fingerprint,snapshot_id,attention_id,"new",None,None,"{}",None,"seeded for automation test",now,now),
        )
        self.os.store.conn.commit()
        self._activate_after_training([{"type":"proactive_attention.acknowledge","config":{"workspace_id":self.ws.id,"fingerprint":fingerprint}}])

        self._trigger_active_auto({"fingerprint":fingerprint})

        latest=self.os.store.conn.execute("SELECT status FROM proactive_intelligence_attention_lifecycle WHERE fingerprint=?",(fingerprint,)).fetchone()
        self.assertEqual(latest["status"],"acknowledged")

    def test_active_auto_add_work_comment_runs_unattended(self) -> None:
        work=self.os.work_ops.create(self.org.id,self.ws.id,self.owner.id,"Follow up","Check account","Owner")
        self._activate_after_training([{"type":"work.comment.create","config":{"workspace_id":self.ws.id,"work_item_id":work.id,"body":"Automation comment"}}])

        self._trigger_active_auto({"work_item_id":work.id})

        comment=self.os.store.conn.execute("SELECT body FROM work_comments WHERE work_item_id=? ORDER BY created_at DESC LIMIT 1",(work.id,)).fetchone()
        self.assertEqual(comment["body"],"Automation comment")

    def test_active_auto_create_proposal_runs_unattended(self) -> None:
        self._activate_after_training([{"type":"brain.proposal.create","config":{
            "workspace_id":self.ws.id,"proposer_type":"automation","kind":"memory",
            "content":"Client prefers concise updates.","payload":{"preference":"concise updates"},
            "evidence":"Automation observed repeated concise-update signal","confidence":0.8,
        }}])

        self._trigger_active_auto({"run_marker":"active"})

        proposal=self.os.store.conn.execute("SELECT kind,status,content FROM memory_proposals WHERE organization_id=? ORDER BY created_at DESC LIMIT 1",(self.org.id,)).fetchone()
        self.assertEqual(dict(proposal),{"kind":"memory","status":"pending","content":"Client prefers concise updates."})

    def test_concurrent_duplicate_trigger_creates_one_run_and_one_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"automation-race.sqlite"
            seeded=CompanyOS(path)
            org=seeded.create_organization("Race Agency")
            ws=seeded.create_organization_workspace(org.id,"Prime","client")
            owner=seeded.create_person(org.id,"Owner",role="owner")
            seeded.add_person_to_workspace(org.id,ws.id,owner.id,"admin")
            seeded.auth.create_principal(org.id,owner.id,"owner@race.test")
            automation=seeded.agent_ops.create_automation(org.id,owner.id,"Silence risk","client_silence",
                [{"field":"days","operator":"gt","value":5}],[{"type":"risk.create","config":{"workspace_id":ws.id,"type":"relationship","severity":"high"}}],"auto")
            seeded.close()

            barrier=threading.Barrier(2)
            results: list[dict[str, object]]=[]
            errors: list[BaseException]=[]

            def trigger() -> None:
                os=CompanyOS(path)
                try:
                    barrier.wait()
                    results.append(os.agent_ops.trigger_automations(org.id,"client_silence",{"days":9,"workspace_id":ws.id})[0])
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    os.close()

            threads=[threading.Thread(target=trigger),threading.Thread(target=trigger)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()

            self.assertEqual(errors,[])
            self.assertEqual(len(results),2)
            self.assertEqual({result["run_id"] for result in results},{results[0]["run_id"]})
            checked=CompanyOS(path)
            try:
                self.assertEqual(checked.store.conn.execute("SELECT COUNT(*) FROM automation_runs WHERE automation_id=?",(automation["id"],)).fetchone()[0],1)
                self.assertEqual(checked.store.conn.execute("SELECT COUNT(*) FROM approval_requests WHERE requested_by_id=?",(automation["id"],)).fetchone()[0],1)
            finally:
                checked.close()

    def test_reclaimed_stale_running_automation_action_is_fenced_without_duplicate_local_write(self) -> None:
        automation=self.os.agent_ops.create_automation(self.org.id,self.owner.id,"Silence risk","client_silence",
            [{"field":"days","operator":"gt","value":5}],[{"type":"risk.create","config":{"workspace_id":self.ws.id,"type":"relationship","severity":"high"}}],"auto")
        run=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":9,"workspace_id":self.ws.id})[0]
        self.os.agency_ops.decide_approval(self.org.id,self.owner.id,run["approval_request_id"],True)
        queued=self.os.agent_ops.execute_approved_automation_run(self.org.id,self.owner.id,run["run_id"])
        claimed=self.os.jobs.claim_job(self.org.id,self.ws.id,"automation-worker-a",lease_seconds=60)
        self.assertEqual(claimed["id"],queued["job_id"])
        run_row=self.os.store.conn.execute(
            """SELECT ar.*,a.created_by_person_id,a.organization_id
               FROM automation_runs ar JOIN automations a ON a.id=ar.automation_id
               WHERE ar.id=?""",
            (run["run_id"],),
        ).fetchone()
        descriptor=json.loads(run_row["action_descriptor_json"])[0]
        validated=validate_reversible_action_descriptor(
            self.os.store.conn,self.org.id,self.ws.id,self.owner.id,descriptor
        )
        self.os.agent_ops._begin_automation_action_execution(
            run_row,validated,f"automation:{run['run_id']}:{validated['payload_hash']}"
        )
        self.os.store.conn.execute("UPDATE automation_runs SET status='running' WHERE id=?",(run["run_id"],))
        self.os.store.conn.execute(
            "UPDATE jobs SET lease_expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc).replace(microsecond=0)-timedelta(minutes=1)).isoformat(),queued["job_id"]),
        )
        self.os.store.conn.commit()

        failed=run_one_job(self.os,self.org.id,self.ws.id,"automation-worker-b")

        self.assertEqual(failed["status"],"failed")
        self.assertEqual(len(self.os.client_ops.list_risks(self.org.id,self.ws.id,self.owner.id)),0)
        latest_run=self.os.store.conn.execute("SELECT status,output FROM automation_runs WHERE id=?",(run["run_id"],)).fetchone()
        self.assertEqual(latest_run["status"],"failed")
        self.assertEqual(json.loads(latest_run["output"])["error"]["type"],"ValidationError")
        execution=self.os.store.conn.execute("SELECT status,error_json FROM automation_action_executions WHERE run_id=?",(run["run_id"],)).fetchone()
        self.assertEqual(execution["status"],"failed")
        self.assertEqual(json.loads(execution["error_json"])["type"],"LeaseRecovered")

    def test_failed_automation_action_blocks_same_fingerprint_replay(self) -> None:
        automation=self.os.agent_ops.create_automation(self.org.id,self.owner.id,"Bad risk","client_silence",
            [{"field":"days","operator":"gt","value":5}],[{"type":"risk.create","config":{"workspace_id":self.ws.id,"type":"unsupported_local_type","severity":"high"}}],"auto")
        run=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":7,"workspace_id":self.ws.id})[0]
        self.os.agency_ops.decide_approval(self.org.id,self.owner.id,run["approval_request_id"],True)
        self.os.agent_ops.execute_approved_automation_run(self.org.id,self.owner.id,run["run_id"])
        failed=run_one_job(self.os,self.org.id,self.ws.id,"automation-fail-worker")
        self.assertEqual(failed["status"],"failed")
        replay=self.os.agent_ops.trigger_automations(self.org.id,"client_silence",{"days":7,"workspace_id":self.ws.id})[0]
        self.assertEqual(replay["run_id"],run["run_id"])
        self.assertEqual(self.os.store.conn.execute("SELECT COUNT(*) FROM automation_action_executions").fetchone()[0],1)

    def test_delegation_depth_is_persisted_and_bounded(self) -> None:
        luna=next(a for a in self.agents if a["name"]=="Luna")
        self.os.agent_ops.configure_agent(self.org.id,self.owner.id,luna["id"],"local",["work.list"],[self.ws.id],["domain.write"])
        root=self.os.agent_ops.enqueue_task(self.org.id,self.owner.id,luna["id"],"Root","Start",self.ws.id)
        child=self.os.agent_ops.enqueue_task(self.org.id,self.owner.id,luna["id"],"Child","Continue",self.ws.id,parent_task_id=root["id"])
        grandchild=self.os.agent_ops.enqueue_task(self.org.id,self.owner.id,luna["id"],"Grandchild","Continue",self.ws.id,parent_task_id=child["id"])
        last=self.os.agent_ops.enqueue_task(self.org.id,self.owner.id,luna["id"],"Last","Continue",self.ws.id,parent_task_id=grandchild["id"])
        self.assertEqual(child["delegation_depth"],1)
        run=self.os.agent_ops.start_run(self.org.id,self.owner.id,luna["id"],child["id"])
        self.assertEqual(run["delegation_depth"],1)
        with self.assertRaises(ValidationError):
            self.os.agent_ops.enqueue_task(self.org.id,self.owner.id,luna["id"],"Too deep","Stop",self.ws.id,parent_task_id=last["id"])

    def test_integration_requires_admin_and_tracks_not_connected(self) -> None:
        member_principal=self.os.auth.create_principal(self.org.id,self.member.id,"member@integration.test")
        member_identity=self.os.auth.identity_for_principal(member_principal["id"])
        with self.assertRaises(AuthorizationError):
            self.os.integrations.configure(member_identity,"slack","T1",{"C123":self.ws.id},["channels:history"])
        owner_principal=self.os.auth.create_principal(self.org.id,self.owner.id,"owner@integration.test")
        owner_identity=self.os.auth.identity_for_principal(owner_principal["id"])
        integration=self.os.integrations.configure(owner_identity,"slack","T1",{"C123":self.ws.id},["channels:history"])
        self.assertEqual(integration["status"],"not_connected")
        self.assertEqual(integration["object_count"],0)

    def test_reports_cite_canonical_records(self) -> None:
        self.os.agency_ops.set_availability(self.org.id,self.owner.id,"2026-08-17",40)
        report=self.os.agent_ops.generate_report(self.org.id,self.owner.id,"capacity_report")
        tables = {citation["table"] for citation in report["citations"]}
        self.assertEqual(tables,{
            "availability","leave_records","work_items","work_versions","time_entries",
            "workflow_runs","workflow_stage_runs","workflow_transition_history",
            "client_account_rosters","client_account_roster_roles",
        })
        self.assertNotIn("capacity_snapshots",tables)


if __name__=="__main__": unittest.main()
