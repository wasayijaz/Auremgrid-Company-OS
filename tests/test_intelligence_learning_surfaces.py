from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter, _mcp_capability
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class IntelligenceLearningSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.create_organization("Auremgrid", "org_learning_surface")
        self.os.create_organization_workspace("org_learning_surface", "Allowed", "client", "ws_learning_allowed")
        self.os.create_organization_workspace("org_learning_surface", "Denied", "client", "ws_learning_denied")
        self.os.create_person(
            "org_learning_surface", "Owner", "owner@learning.test", role="owner", person_id="person_learning_owner"
        )
        self.os.add_person_to_workspace("org_learning_surface", "ws_learning_allowed", "person_learning_owner", "admin")
        self.os.create_actor("ws_learning_allowed", "Bound actor", "admin", "actor_learning_allowed")
        self.token, self.identity = issue_identity(
            self.os, "org_learning_surface", "person_learning_owner", "ws_learning_allowed", "actor_learning_allowed"
        )
        ingested = self.os.ingest_text(
            "ws_learning_allowed",
            "actor_learning_allowed",
            "learning-source",
            "FACT: Learning Client | status | under review",
            "memory://learning-source",
        )
        self.source_id = ingested.source.id
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
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

    def scoped_path(self, route: str, **extra: object) -> str:
        params = {
            "organization_id": "org_learning_surface",
            "workspace_id": "ws_learning_allowed",
            "person_id": "person_learning_owner",
            **extra,
        }
        return route + "?" + "&".join(f"{key}={value}" for key, value in params.items())

    def recommendation_payload(self) -> dict:
        return {
            "organization_id": "org_learning_surface",
            "workspace_id": "ws_learning_allowed",
            "person_id": "person_learning_owner",
            "summary": "Review the visible learning source before changing the plan.",
            "runbook_id": "client_health_drop",
            "runbook_version": 1,
            "profile_contributors": [{"profile_id": "account_strategist", "version": 1, "role": "lead"}],
            "confidence": 0.74,
            "options": [{"id": "accept", "label": "Accept"}, {"id": "reject", "label": "Reject"}],
            "recommended_option_id": "accept",
            "evidence_refs": [{"type": "source", "id": self.source_id}],
            "evaluation_window_start": self.now.isoformat(),
            "evaluation_window_end": (self.now + timedelta(days=7)).isoformat(),
            "generated_by": {"type": "runbook", "id": "client_health_drop"},
        }

    def test_rest_learning_lifecycle_and_evaluation_safety_are_scoped(self) -> None:
        status, hypothesis = self.request(
            "POST",
            "/dashboard/intelligence/hypotheses",
            {
                "organization_id": "org_learning_surface",
                "workspace_id": "ws_learning_allowed",
                "person_id": "person_learning_owner",
                "text": "Hypothesis: the client status may indicate delivery ambiguity.",
                "evidence_for_refs": [{"type": "source", "id": self.source_id}],
                "confidence": 0.62,
                "assumptions": ["Only visible evidence was considered."],
            "generated_by": {"type": "expert_profile", "id": "delivery_analyst"},
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(hypothesis["hypothesis"]["status"], "proposed")

        status, recommendation = self.request(
            "POST", "/dashboard/intelligence/recommendations", self.recommendation_payload()
        )
        self.assertEqual(status, 201)
        recommendation_id = recommendation["recommendation"]["id"]

        status, lifecycle = self.request(
            "POST",
            "/dashboard/intelligence/recommendations/lifecycle",
            {
                "organization_id": "org_learning_surface",
                "workspace_id": "ws_learning_allowed",
                "person_id": "person_learning_owner",
                "recommendation_id": recommendation_id,
                "event_type": "accepted",
                "lessons": "Approved for observation only.",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(lifecycle["event"]["event_type"], "accepted")

        work = self.os.work_ops.create(
            "org_learning_surface",
            "ws_learning_allowed",
            "person_learning_owner",
            "Evaluate recommendation outcome",
            "Outcome evidence for learning surface.",
            "person_learning_owner",
        )
        status, evaluated = self.request(
            "POST",
            "/dashboard/intelligence/recommendations/lifecycle",
            {
                "organization_id": "org_learning_surface",
                "workspace_id": "ws_learning_allowed",
                "person_id": "person_learning_owner",
                "recommendation_id": recommendation_id,
                "event_type": "evaluated",
                "measured_outcomes": [
                    {
                        "type": "work_item",
                        "id": work.id,
                        "occurred_at": (self.now + timedelta(days=1)).isoformat(),
                        "metric": "completed_review",
                        "value": 1,
                    }
                ],
                "evidence_refs": [{"type": "work_item", "id": work.id}],
                "score": 0.8,
                "lessons": "The visible outcome matched the chosen option.",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(evaluated["event"]["score"], 0.8)

        status, learning = self.request("GET", self.scoped_path("/dashboard/intelligence/learning"))
        self.assertEqual(status, 200)
        self.assertEqual(len(learning["hypotheses"]), 1)
        self.assertEqual(len(learning["recommendations"]), 1)
        self.assertEqual({event["event_type"] for event in learning["recommendation_lifecycle"]}, {"accepted", "evaluated"})

        status, quality = self.request("GET", self.scoped_path("/dashboard/intelligence/recommendation-quality"))
        self.assertEqual(status, 200)
        self.assertEqual(quality["status"], "ready")
        self.assertEqual(quality["denominator"], 1)
        self.assertEqual(quality["correctness_rate"], 1.0)
        self.assertEqual(quality["pending_count"], 0)

        status, evaluation = self.request(
            "POST",
            "/dashboard/intelligence/evaluation/start",
            {
                "organization_id": "org_learning_surface",
                "workspace_id": "ws_learning_allowed",
                "person_id": "person_learning_owner",
                "task_class": "reasoning",
                "provider": "shadow",
                "model": "deterministic",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(evaluation["evaluation"]["shadow_only"], 1)
        evaluation_id = evaluation["evaluation"]["id"]

        status, completed = self.request(
            "POST",
            "/dashboard/intelligence/evaluation/complete",
            {
                "organization_id": "org_learning_surface",
                "workspace_id": "ws_learning_allowed",
                "person_id": "person_learning_owner",
                "evaluation_id": evaluation_id,
                "input_tokens": 10,
                "output_tokens": 5,
                "evaluator_score": 0.7,
                "human_acceptance": True,
                "metadata": {"note": "shadow only"},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["evaluation"]["status"], "completed")

        status, safety = self.request(
            "GET", self.scoped_path("/dashboard/intelligence/evaluation-safety", task_class="reasoning")
        )
        self.assertEqual(status, 200)
        self.assertTrue(safety["circuit"]["shadow_only"])
        self.assertEqual(safety["evaluations"][0]["id"], evaluation_id)

    def test_cross_workspace_and_approval_boundary_are_enforced(self) -> None:
        status, denied = self.request(
            "GET",
            "/dashboard/intelligence/learning?organization_id=org_learning_surface&workspace_id=ws_learning_denied&person_id=person_learning_owner",
        )
        self.assertEqual(status, 403)
        self.assertNotIn("Hypothesis", json.dumps(denied))

        viewer = self.os.create_person(
            "org_learning_surface", "Viewer", "viewer@learning.test", role="member", person_id="person_learning_viewer"
        )
        self.os.add_person_to_workspace("org_learning_surface", "ws_learning_allowed", viewer.id, "viewer")
        viewer_token, _ = issue_identity(self.os, "org_learning_surface", viewer.id, "ws_learning_allowed")
        status, denied_write = self.request(
            "POST",
            "/dashboard/intelligence/hypotheses",
            {
                "organization_id": "org_learning_surface",
                "workspace_id": "ws_learning_allowed",
                "person_id": viewer.id,
                "text": "Viewer cannot propose.",
            },
            token=viewer_token,
        )
        self.assertEqual(status, 403)

        propose_token = self.os.auth.create_api_token(self.identity.principal_id, "propose only", ["brain_read", "brain_propose"])
        status, recommendation = self.request(
            "POST", "/dashboard/intelligence/recommendations", self.recommendation_payload(), token=propose_token["token"]
        )
        self.assertEqual(status, 201)
        status, denied_lifecycle = self.request(
            "POST",
            "/dashboard/intelligence/recommendations/lifecycle",
            {
                "organization_id": "org_learning_surface",
                "workspace_id": "ws_learning_allowed",
                "person_id": "person_learning_owner",
                "recommendation_id": recommendation["recommendation"]["id"],
                "event_type": "accepted",
            },
            token=propose_token["token"],
        )
        self.assertEqual(status, 403)

    def test_mcp_learning_and_safety_tools_match_capability_boundary(self) -> None:
        self.assertEqual(_mcp_capability("intelligence.learning.get"), "brain_read")
        self.assertEqual(_mcp_capability("intelligence.recommendations.quality"), "brain_read")
        self.assertEqual(_mcp_capability("intelligence.hypotheses.record"), "brain_propose")
        self.assertEqual(_mcp_capability("intelligence.recommendations.record"), "brain_propose")
        self.assertEqual(_mcp_capability("intelligence.recommendations.lifecycle"), "brain_promote")
        self.assertEqual(_mcp_capability("intelligence.recommendations.handoff"), "brain_propose")
        self.assertEqual(_mcp_capability("intelligence.evaluation_safety.get"), "brain_read")
        self.assertEqual(_mcp_capability("intelligence.evaluation.start"), "brain_propose")
        self.assertEqual(_mcp_capability("intelligence.evaluation.complete"), "brain_promote")

        router = McpToolRouter(self.os, self.identity)
        tool_names = {tool["name"] for tool in router.list_tools()}
        self.assertIn("intelligence.learning.get", tool_names)
        self.assertIn("intelligence.recommendations.handoff", tool_names)
        self.assertIn("intelligence.recommendations.quality", tool_names)
        hypothesis = router.call(
            "intelligence.hypotheses.record",
            {
                "workspace_id": "ws_learning_allowed",
                "text": "Hypothesis: visible source needs interpretation.",
                "evidence_for_refs": [{"type": "source", "id": self.source_id}],
            },
        )
        self.assertNotIn("error", hypothesis)
        recommendation = router.call("intelligence.recommendations.record", self.recommendation_payload())
        recommendation_id = recommendation["recommendation"]["id"]
        event = router.call(
            "intelligence.recommendations.lifecycle",
            {"workspace_id": "ws_learning_allowed", "recommendation_id": recommendation_id, "event_type": "rejected"},
        )
        self.assertEqual(event["event"]["event_type"], "rejected")
        evaluation = router.call(
            "intelligence.evaluation.start",
            {"workspace_id": "ws_learning_allowed", "task_class": "mcp_shadow"},
        )
        completed = router.call(
            "intelligence.evaluation.complete",
            {"workspace_id": "ws_learning_allowed", "evaluation_id": evaluation["evaluation"]["id"], "evaluator_score": 0.4},
        )
        self.assertEqual(completed["evaluation"]["status"], "completed")
        learning = router.call("intelligence.learning.get", {"workspace_id": "ws_learning_allowed"})
        self.assertEqual(len(learning["hypotheses"]), 1)
        quality = router.call("intelligence.recommendations.quality", {"workspace_id": "ws_learning_allowed"})
        self.assertEqual(quality["status"], "pending")
        self.assertEqual(quality["denominator"], 0)
        safety = router.call(
            "intelligence.evaluation_safety.get", {"workspace_id": "ws_learning_allowed", "task_class": "mcp_shadow"}
        )
        self.assertTrue(safety["circuit"]["shadow_only"])

        forged = router.call("intelligence.learning.get", {"workspace_id": "ws_learning_denied"})
        self.assertEqual(forged["error"], "AuthorizationError")
        propose_identity = self.os.auth.authenticate_api_token(
            self.os.auth.create_api_token(self.identity.principal_id, "mcp propose", ["brain_read", "brain_propose"])["token"],
            "org_learning_surface",
            "ws_learning_allowed",
        )
        denied = McpToolRouter(self.os, propose_identity).call(
            "intelligence.recommendations.lifecycle",
            {"workspace_id": "ws_learning_allowed", "recommendation_id": recommendation_id, "event_type": "accepted"},
        )
        self.assertEqual(denied["error"], "AuthorizationError")


if __name__ == "__main__":
    unittest.main()
