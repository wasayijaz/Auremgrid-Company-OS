from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

from tests.dashboard_bundle import read_dashboard_bundle
from urllib.parse import urlencode

from auremgrid.api.http import serve
from auremgrid.api.mcp import McpToolRouter
from auremgrid.domain.errors import ValidationError
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity


class BrainTruthSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.org = self.os.create_organization("Agency")
        self.ws = self.os.create_organization_workspace(self.org.id, "Client", "client")
        self.person = self.os.create_person(self.org.id, "Owner", role="owner")
        self.os.add_person_to_workspace(self.org.id, self.ws.id, self.person.id, "admin")
        self.actor = self.os.create_actor(self.ws.id, "Brain reader", "admin", "brain-reader")
        self.token, self.identity = issue_identity(
            self.os, self.org.id, self.person.id, self.ws.id, self.actor.id
        )
        self.os.ingest_text(
            self.ws.id, self.actor.id, "plan.md", "FACT: Plan | price | 100",
            "memory://plan", allowed_actor_ids=[self.actor.id],
        )

    def tearDown(self) -> None:
        self.os.close()

    def test_fact_reads_include_effective_state_and_temporal_watermark(self) -> None:
        search = self.os.search(self.ws.id, self.actor.id, "Plan price").to_dict()
        fact = next(item["payload"] for item in search["items"] if item["kind"] == "fact")
        self.assertEqual(fact["effective_state"], "inferred")

        entity = self.os.entity(self.ws.id, self.actor.id, "Plan")
        history = self.os.history(self.ws.id, self.actor.id, "Plan", "price")
        neighbors = self.os.neighbors(self.ws.id, self.actor.id, "Plan")
        self.assertEqual(entity["facts"][0]["effective_state"], "inferred")
        self.assertEqual(history["facts"][0]["effective_state"], "inferred")
        self.assertIn("as_of", history)
        self.assertIn("as_of", neighbors)

        transition = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.os.brain_ops.record_knowledge_state(
            self.org.id, self.ws.id, "fact", fact["id"], "verified",
            "reviewed", self.identity, effective_from=transition,
        )
        before = self.os.entity(
            self.ws.id, self.actor.id, "Plan", as_of=transition - timedelta(milliseconds=1)
        )
        after = self.os.history(
            self.ws.id, self.actor.id, "Plan", "price", as_of=transition + timedelta(seconds=1)
        )
        self.assertEqual(before["facts"][0]["effective_state"], "inferred")
        self.assertEqual(after["facts"][0]["effective_state"], "verified")

    def test_dashboard_and_mcp_report_fallback_and_degraded_graph_truthfully(self) -> None:
        watermark = self.os.store.graph_snapshot_watermark(self.ws.id)
        self.os.store.start_graph_generation(self.ws.id, "verified-generation", watermark)
        self.os.store.activate_graph_generation(self.ws.id, "verified-generation")
        active = self.os.store.graph_generation_state(self.ws.id)["active_generation"]
        self.assertIsNotNone(active)
        self.os.graph_health = {"status": "degraded", "generation": None, "detail": "secret"}
        view = self.os.dashboard.brain(
            self.identity, self.org.id, self.ws.id, self.person.id
        )
        self.assertTrue(view["health"]["semantic"]["fallback_used"])
        self.assertEqual(view["health"]["semantic"]["mode"], "deterministic_fallback")
        self.assertEqual(view["health"]["graph"]["status"], "degraded")
        self.assertTrue(view["health"]["graph"]["serving_stale_generation"])
        self.assertNotIn("detail", view["health"]["graph"])

        router = McpToolRouter(self.os, self.identity)
        names = {tool["name"] for tool in router.list_tools()}
        self.assertTrue({"brain.read", "brain.health"} <= names)
        health = router.call("brain.health", {"workspace_id": self.ws.id})
        read = router.call("brain.read", {"workspace_id": self.ws.id})
        self.assertEqual(health["health"], read["health"])
        self.assertEqual(read["current_truths"][0]["state"], "inferred")

    def test_exceptional_extraction_confidence_is_explicit_without_human_verification(self) -> None:
        result = self.os.ingest_text(
            self.ws.id, self.actor.id, "strong.md",
            "META: confidence=0.95\nFACT: Strong Plan | status | confirmed",
            "memory://strong", allowed_actor_ids=[self.actor.id],
        )
        fact_id = result.fact_ids[0]
        state = self.os.brain_ops.knowledge_state(
            self.org.id, self.ws.id, self.person.id, "fact", fact_id
        )
        self.assertEqual(state["state"], "high_confidence")
        self.assertNotEqual(state["state"], "verified")

    def test_rest_knowledge_health_is_read_only_and_uses_scoped_truth_view(self) -> None:
        before = self.os.store.conn.execute(
            "SELECT COUNT(*) FROM knowledge_health_issues"
        ).fetchone()[0]
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            query = urlencode({
                "organization_id": self.org.id,
                "workspace_id": self.ws.id,
                "person_id": self.person.id,
                "as_of": datetime.now(timezone.utc).isoformat(),
            })
            connection = HTTPConnection(*server.server_address, timeout=5)
            connection.request(
                "GET", f"/knowledge-health?{query}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["summary"]["current_truths"], 1)
            self.assertTrue(payload["health"]["semantic"]["fallback_used"])
        finally:
            server.shutdown()
            server.server_close()
        after = self.os.store.conn.execute(
            "SELECT COUNT(*) FROM knowledge_health_issues"
        ).fetchone()[0]
        self.assertEqual(after, before)

    def test_retroactive_evidence_does_not_leak_before_recording_time(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        self.os.ingest_text(
            self.ws.id, self.actor.id, "retro.md",
            "META: valid_from=2020-01-01T00:00:00+00:00\nFACT: Retro Plan | price | 999",
            "memory://retro", allowed_actor_ids=[self.actor.id],
            observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        self.assertFalse(self.os.search(
            self.ws.id, self.actor.id, "Retro price", as_of=cutoff,
        ).items)
        self.assertEqual(self.os.entity(
            self.ws.id, self.actor.id, "Retro Plan", as_of=cutoff,
        )["facts"], [])
        self.assertEqual(self.os.history(
            self.ws.id, self.actor.id, "Retro Plan", as_of=cutoff,
        )["facts"], [])
        historical = self.os.dashboard.brain(
            self.identity, self.org.id, self.ws.id, self.person.id, cutoff,
        )
        self.assertEqual(historical["summary"]["current_truths"], 0)
        self.assertNotIn("Retro Plan", str(historical))

    def test_brain_temporal_reads_reject_timezone_naive_as_of(self) -> None:
        naive = datetime(2026, 8, 20, 12)
        calls = (
            lambda: self.os.search(self.ws.id, self.actor.id, "Plan", as_of=naive),
            lambda: self.os.entity(self.ws.id, self.actor.id, "Plan", as_of=naive),
            lambda: self.os.history(self.ws.id, self.actor.id, "Plan", as_of=naive),
            lambda: self.os.neighbors(self.ws.id, self.actor.id, "Plan", as_of=naive),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(ValidationError):
                    call()

    def test_rest_brain_reads_reject_timezone_naive_as_of_as_validation_error(self) -> None:
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            query = urlencode({
                "workspace_id": self.ws.id,
                "as_of": "2026-08-20T12:00:00",
            })
            for path in ("/knowledge-health", "/dashboard/brain"):
                with self.subTest(path=path):
                    connection = HTTPConnection(*server.server_address, timeout=5)
                    connection.request(
                        "GET", f"{path}?{query}",
                        headers={"Authorization": f"Bearer {self.token}"},
                    )
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    connection.close()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(payload["error"], "validation_error")
                    self.assertEqual(payload["message"], "as_of must include a timezone")
        finally:
            server.shutdown()
            server.server_close()

    def test_final_brain_renderer_shows_fact_state_and_provider_health(self) -> None:
        html = read_dashboard_bundle(Path(__file__).parents[1])
        tail = html[html.rfind("loadBrainSurface=async function"):]
        for marker in (
            "data-brain-health", "semantic.fallback_used", "semantic.provider",
            "graph.serving_stale_generation", "row.state||'inferred'",
        ):
            self.assertIn(marker, tail)


if __name__ == "__main__":
    unittest.main()
