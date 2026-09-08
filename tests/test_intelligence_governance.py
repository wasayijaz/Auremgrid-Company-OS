from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.domain.errors import AuthorizationError, ValidationError
from auremgrid.services.brain import CompanyOS
from auremgrid.services.intelligence_governance import IntelligenceGovernanceService
from tests.auth_support import issue_identity


class IntelligenceGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.create_organization("Auremgrid", "org_governance")
        self.os.create_organization_workspace("org_governance", "Allowed", "client", "ws_governance_allowed")
        self.os.create_organization_workspace("org_governance", "Denied", "client", "ws_governance_denied")
        self.os.create_person(
            "org_governance", "Owner", "owner@governance.test", role="owner", person_id="person_governance_owner"
        )
        self.evaluator = self.os.create_person(
            "org_governance", "Evaluator", "evaluator@governance.test", role="owner", person_id="person_governance_evaluator"
        )
        self.member = self.os.create_person(
            "org_governance", "Member", "member@governance.test", role="member", person_id="person_governance_member"
        )
        self.os.add_person_to_workspace("org_governance", "ws_governance_allowed", "person_governance_owner", "admin")
        self.os.add_person_to_workspace("org_governance", "ws_governance_allowed", self.evaluator.id, "admin")
        self.os.add_person_to_workspace("org_governance", "ws_governance_allowed", self.member.id, "operator")
        self.os.create_actor("ws_governance_allowed", "Bound actor", "admin", "actor_governance_allowed")
        self.token, self.identity = issue_identity(
            self.os, "org_governance", "person_governance_owner", "ws_governance_allowed", "actor_governance_allowed"
        )
        self.evaluator_token, _ = issue_identity(
            self.os, "org_governance", self.evaluator.id, "ws_governance_allowed"
        )
        self.member_token, _ = issue_identity(
            self.os, "org_governance", self.member.id, "ws_governance_allowed"
        )
        ingested = self.os.ingest_text(
            "ws_governance_allowed",
            "actor_governance_allowed",
            "governance-source",
            "FACT: Governance Client | status | under review",
            "memory://governance-source",
        )
        self.source_id = ingested.source.id
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.governance = IntelligenceGovernanceService(self.os, self.os.jobs.new_id)
        self.server = serve(self.os, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.os.close()

    def request(
        self, method: str, path: str, payload: dict | None = None, token: str | None = None
    ) -> tuple[int, dict]:
        connection = HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Authorization": f"Bearer {token or self.token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def governance_path(self, route: str) -> str:
        return route + "?" + "&".join(
            f"{key}={value}"
            for key, value in {
                "organization_id": "org_governance",
                "workspace_id": "ws_governance_allowed",
                "person_id": "person_governance_owner",
            }.items()
        )

    def create_evaluated_recommendation(self, lessons: str) -> str:
        recommendation = self.os.intelligence_learning.record_recommendation(
            "org_governance", "ws_governance_allowed", "person_governance_owner",
            "Review the visible governance source before changing the plan.",
            runbook_id="client_health_drop", runbook_version=1,
            profile_contributors=[{"profile_id": "account_strategist", "version": 1, "role": "lead"}],
            confidence=0.74,
            options=[{"id": "accept", "label": "Accept"}, {"id": "reject", "label": "Reject"}],
            recommended_option_id="accept",
            evidence_refs=[{"type": "source", "id": self.source_id}],
            evaluation_window_start=self.now.isoformat(),
            evaluation_window_end=(self.now + timedelta(days=7)).isoformat(),
        )
        self.os.intelligence_learning.append_recommendation_event(
            "org_governance", "ws_governance_allowed", "person_governance_owner",
            recommendation["id"], "accepted",
        )
        work = self.os.work_ops.create(
            "org_governance", "ws_governance_allowed", "person_governance_owner",
            "Evaluate governance recommendation outcome",
            "Outcome evidence for governance lesson gate.",
            "person_governance_owner",
        )
        self.os.intelligence_learning.append_recommendation_event(
            "org_governance", "ws_governance_allowed", self.evaluator.id,
            recommendation["id"], "evaluated",
            measured_outcomes=[{
                "type": "work_item",
                "id": work.id,
                "occurred_at": work.created_at.isoformat(),
                "metric": "completed_review",
                "value": 1,
            }],
            evidence_refs=[{"type": "work_item", "id": work.id}],
            score=0.8,
            lessons=lessons,
        )
        return recommendation["id"]

    # ------------------------------------------------------------------ lessons

    def test_evaluated_lesson_lands_proposed_and_flows_after_owner_approval(self) -> None:
        self.create_evaluated_recommendation("The visible outcome matched the chosen option.")

        status, pending = self.request("GET", self.governance_path("/dashboard/intelligence/governance/lessons"))
        self.assertEqual(status, 200)
        self.assertEqual(len(pending["lessons"]), 1)
        lesson = pending["lessons"][0]
        self.assertEqual(lesson["outcome"]["origin"], "evaluated_outcome_lesson")
        self.assertEqual(lesson["outcome"]["score"], 0.8)
        self.assertEqual(lesson["outcome"]["runbook_id"], "client_health_drop")

        # Evaluated events without lessons create no lesson row.
        self.create_evaluated_recommendation("")
        status, pending = self.request("GET", self.governance_path("/dashboard/intelligence/governance/lessons"))
        self.assertEqual(status, 200)
        self.assertEqual(len(pending["lessons"]), 1)

        status, decision = self.request(
            "POST",
            "/dashboard/intelligence/governance/lessons/decide",
            {
                "workspace_id": "ws_governance_allowed",
                "lesson_id": lesson["id"],
                "decision": "approve",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(decision["decision"]["hypothesis"]["status"], "supported")
        self.assertEqual(decision["decision"]["hypothesis"]["supersedes_hypothesis_id"], lesson["id"])

        status, pending = self.request("GET", self.governance_path("/dashboard/intelligence/governance/lessons"))
        self.assertEqual(status, 200)
        self.assertEqual(pending["lessons"], [])

        status, error = self.request(
            "POST",
            "/dashboard/intelligence/governance/lessons/decide",
            {
                "workspace_id": "ws_governance_allowed",
                "lesson_id": lesson["id"],
                "decision": "reject",
            },
        )
        self.assertEqual(status, 400)

        learning = self.os.intelligence_learning.workspace_learning(
            "org_governance", "ws_governance_allowed", "person_governance_owner"
        )
        supported = [row for row in learning["hypotheses"] if row["status"] == "supported"]
        self.assertEqual(len(supported), 1)

    def test_rejected_lesson_is_refuted_and_stays_append_only(self) -> None:
        self.create_evaluated_recommendation("The delivery fix did not hold.")
        pending = self.governance.proposed_lessons(
            "org_governance", "ws_governance_allowed", "person_governance_owner"
        )
        lesson_id = pending["lessons"][0]["id"]
        decision = self.governance.decide_lesson(
            "org_governance", "ws_governance_allowed", "person_governance_owner", lesson_id, "reject"
        )
        self.assertEqual(decision["hypothesis"]["status"], "refuted")
        self.assertEqual(
            self.governance.proposed_lessons(
                "org_governance", "ws_governance_allowed", "person_governance_owner"
            )["lessons"],
            [],
        )
        with self.assertRaises(ValidationError):
            self.governance.decide_lesson(
                "org_governance", "ws_governance_allowed", "person_governance_owner", lesson_id, "approve"
            )

    def test_lesson_review_is_owner_only(self) -> None:
        self.create_evaluated_recommendation("Only owners decide lessons.")
        status, _ = self.request(
            "GET",
            self.governance_path("/dashboard/intelligence/governance/lessons"),
            token=self.member_token,
        )
        self.assertEqual(status, 403)
        pending = self.governance.proposed_lessons(
            "org_governance", "ws_governance_allowed", "person_governance_owner"
        )
        lesson_id = pending["lessons"][0]["id"]
        status, _ = self.request(
            "POST",
            "/dashboard/intelligence/governance/lessons/decide",
            {
                "workspace_id": "ws_governance_allowed",
                "lesson_id": lesson_id,
                "decision": "approve",
            },
            token=self.member_token,
        )
        self.assertEqual(status, 403)

    # ----------------------------------------------------------------- runbooks

    def test_runbook_listing_includes_role_runbooks_and_draft_states(self) -> None:
        status, listing = self.request("GET", self.governance_path("/dashboard/intelligence/governance/runbooks"))
        self.assertEqual(status, 200)
        runbooks = listing["runbooks"]
        self.assertEqual(len(runbooks), 19)
        ids = {runbook["id"] for runbook in runbooks}
        for role_id in (
            "client_success_lead_review", "ads_lead_review", "design_lead_review",
            "marketing_lead_review", "executive_review", "cadence_owner_review", "meeting_owner_review",
        ):
            self.assertIn(role_id, ids)
        for runbook in runbooks:
            self.assertGreaterEqual(len(runbook["steps"]), 8)
            self.assertEqual(runbook["approval_state"]["status"], "draft")

    def test_runbook_approve_retire_and_execution_gate(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.governance.require_runbook_approved(
                "org_governance", "ws_governance_allowed", "client_health_drop", 1
            )

        status, approval = self.request(
            "POST",
            "/dashboard/intelligence/governance/runbooks/approve",
            {
                "workspace_id": "ws_governance_allowed",
                "runbook_id": "client_health_drop",
                "runbook_version": 1,
                "action": "approve",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(approval["approval"]["status"], "approved")

        self.governance.require_runbook_approved(
            "org_governance", "ws_governance_allowed", "client_health_drop", 1
        )
        visible = self.os.intelligence_contracts.list_runbooks(
            "org_governance", "ws_governance_allowed", "person_governance_owner",
            execution_approved=True,
        )
        self.assertEqual([runbook["id"] for runbook in visible], ["client_health_drop"])

        status, _ = self.request(
            "POST",
            "/dashboard/intelligence/governance/runbooks/approve",
            {
                "workspace_id": "ws_governance_allowed",
                "runbook_id": "client_health_drop",
                "runbook_version": 1,
                "action": "approve",
            },
        )
        self.assertEqual(status, 400)

        status, approval = self.request(
            "POST",
            "/dashboard/intelligence/governance/runbooks/approve",
            {
                "workspace_id": "ws_governance_allowed",
                "runbook_id": "client_health_drop",
                "runbook_version": 1,
                "action": "retire",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(approval["approval"]["status"], "retired")
        visible = self.os.intelligence_contracts.list_runbooks(
            "org_governance", "ws_governance_allowed", "person_governance_owner",
            execution_approved=True,
        )
        self.assertEqual(visible, ())

        status, approval = self.request(
            "POST",
            "/dashboard/intelligence/governance/runbooks/approve",
            {
                "workspace_id": "ws_governance_allowed",
                "runbook_id": "client_health_drop",
                "runbook_version": 1,
                "action": "approve",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(approval["approval"]["status"], "approved")

    def test_runbook_customization_creates_draft_version_and_keeps_base_state(self) -> None:
        self.governance.decide_runbook(
            "org_governance", "ws_governance_allowed", "person_governance_owner", "client_health_drop", 1, "approve"
        )
        status, custom = self.request(
            "POST",
            "/dashboard/intelligence/governance/runbooks/customize",
            {
                "workspace_id": "ws_governance_allowed",
                "runbook_id": "client_health_drop",
                "patch": {
                    "name": "Client Health Drop (Tailored)",
                    "domains": ["client_success", "retention"],
                    "profile_ids": ["relationship_analyst", "account_strategist", "reality_checker"],
                },
            },
        )
        self.assertEqual(status, 201)
        runbook = custom["runbook"]
        self.assertEqual(runbook["id"], "client_health_drop")
        self.assertEqual(runbook["version"], 2)
        self.assertEqual(runbook["name"], "Client Health Drop (Tailored)")
        self.assertEqual(runbook["approval_state"]["status"], "draft")
        self.assertNotEqual(runbook["content_hash"], "")

        base_state = self.governance.runbook_approval_state(
            "org_governance", "ws_governance_allowed", "client_health_drop", 1
        )
        self.assertEqual(base_state["status"], "approved")

        status, _ = self.request(
            "POST",
            "/dashboard/intelligence/governance/runbooks/customize",
            {
                "workspace_id": "ws_governance_allowed",
                "runbook_id": "client_health_drop",
                "patch": {"profile_ids": ["nonexistent_profile"]},
            },
        )
        self.assertEqual(status, 400)

        status, _ = self.request(
            "POST",
            "/dashboard/intelligence/governance/runbooks/customize",
            {
                "workspace_id": "ws_governance_allowed",
                "runbook_id": "client_health_drop",
                "patch": {"steps": []},
            },
        )
        self.assertEqual(status, 400)

    def test_runbook_governance_is_owner_only_and_workspace_scoped(self) -> None:
        status, _ = self.request(
            "POST",
            "/dashboard/intelligence/governance/runbooks/approve",
            {
                "workspace_id": "ws_governance_allowed",
                "runbook_id": "client_health_drop",
                "runbook_version": 1,
                "action": "approve",
            },
            token=self.member_token,
        )
        self.assertEqual(status, 403)

        status, _ = self.request(
            "GET",
            self.governance_path("/dashboard/intelligence/governance/runbooks").replace(
                "ws_governance_allowed", "ws_governance_denied"
            ),
        )
        self.assertEqual(status, 403)
