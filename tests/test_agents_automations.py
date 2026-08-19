from __future__ import annotations

import unittest

from auremgrid.domain.errors import AuthorizationError
from auremgrid.services.brain import CompanyOS


class AgentAutomationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os=CompanyOS(":memory:"); self.org=self.os.create_organization("Agency")
        self.ws=self.os.create_organization_workspace(self.org.id,"Prime","client")
        self.owner=self.os.create_person(self.org.id,"Owner",role="owner")
        self.member=self.os.create_person(self.org.id,"Member")
        self.os.add_person_to_workspace(self.org.id,self.ws.id,self.owner.id,"admin")
        self.agents=self.os.agent_ops.seed_primary_agents(self.org.id,self.owner.id)

    def tearDown(self) -> None: self.os.close()

    def test_agent_permissions_queue_and_run_trace_are_durable(self) -> None:
        luna=next(a for a in self.agents if a["name"]=="Luna")
        self.os.agent_ops.configure_agent(self.org.id,self.owner.id,luna["id"],"local",["work.list"],[self.ws.id],["domain.write"])
        task=self.os.agent_ops.enqueue_task(self.org.id,self.owner.id,luna["id"],"Inspect work","List open work",self.ws.id)
        run=self.os.agent_ops.start_run(self.org.id,luna["id"],task["id"])
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
        completed=self.os.agent_ops.execute_approved_automation_run(self.org.id,self.owner.id,run["run_id"])
        self.assertEqual(completed["status"],"completed");self.assertEqual(len(self.os.client_ops.list_risks(self.org.id,self.ws.id,self.owner.id)),1)
        active=self.os.agent_ops.activate_automation(self.org.id,self.owner.id,automation["id"]);self.assertEqual(active["status"],"active")

    def test_integration_requires_admin_and_tracks_not_connected(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.os.agent_ops.upsert_integration(self.org.id,self.member.id,"slack",{},[])
        integration=self.os.agent_ops.upsert_integration(self.org.id,self.owner.id,"slack",{self.ws.id:"C123"},["channels:read"])
        self.assertEqual(integration["status"],"not_connected")
        self.assertEqual(integration["object_count"],0)

    def test_reports_cite_canonical_records(self) -> None:
        self.os.agency_ops.set_availability(self.org.id,self.owner.id,"2026-08-17",40)
        capacity=self.os.agency_ops.calculate_capacity(self.org.id,self.owner.id,self.owner.id,"2026-08-17",20,20)
        report=self.os.agent_ops.generate_report(self.org.id,self.owner.id,"capacity_report")
        self.assertEqual(report["citations"][0],{"table":"capacity_snapshots","id":capacity["id"]})


if __name__=="__main__": unittest.main()
