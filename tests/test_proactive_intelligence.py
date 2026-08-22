from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.services.brain import CompanyOS
from auremgrid.services.worker import run_one_job
from tests.auth_support import issue_identity


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class CountingReasoningProvider:
    name = "counting"
    model = "counting-model"
    version = "v1"

    def __init__(self) -> None:
        self.calls = 0

    def deliberate(self, context):
        self.calls += 1
        return {
            "hypotheses": [{"text": "Provider hypothesis", "confidence": 0.6}],
            "options": [{"title": "Provider option", "summary": "Provider summary", "tradeoffs": []}],
            "scenarios": [{"name": "provider", "assumptions": [], "mitigations": []}],
            "recommendation": {"summary": "Provider recommendation", "rationale": "Provider rationale"},
            "confidence": 0.6,
            "dissent": [],
        }


class ProactiveIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.os = CompanyOS(":memory:")
        self.os.seed_demo(FIXTURES)
        self.token, self.identity = issue_identity(
            self.os, "org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin"
        )

    def tearDown(self) -> None:
        self.os.close()

    def test_refresh_persists_append_only_executive_snapshot_and_attention(self) -> None:
        first = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner")
        second = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner")
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["unchanged"])
        self.assertIn(first["status"], {"ready", "degraded", "insufficient_evidence"})
        self.assertIn("projection_fingerprint", first)
        self.assertIn("generated_at", first)
        self.assertIn("top_three", first["payload"]["sections"])
        self.assertLessEqual(len(first["attention"]), 3)
        self.assertTrue(all("evidence_refs" in item for item in first["attention"]))
        latest = self.os.proactive_intelligence.require_latest_snapshot("org_demo", "person_demo_owner")
        self.assertEqual(latest["id"], first["id"])
        queue = self.os.proactive_intelligence.attention_queue("org_demo", "person_demo_owner")
        self.assertEqual([item["snapshot_id"] for item in queue], [first["id"]] * len(queue))
        count = self.os.store.conn.execute("SELECT COUNT(*) FROM proactive_intelligence_snapshots").fetchone()[0]
        self.assertEqual(count, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.os.store.conn.execute(
                "UPDATE proactive_intelligence_snapshots SET status='degraded' WHERE id=?",
                (first["id"],),
            )

    def test_workspace_snapshot_preserves_insufficient_evidence_state(self) -> None:
        org = self.os.create_organization("Empty org", "org_empty_intel")
        ws = self.os.create_organization_workspace(org.id, "Empty client", "client", "ws_empty_intel")
        person = self.os.create_person(org.id, "Owner", "empty@intel.test", role="owner", person_id="person_empty_intel")
        self.os.add_person_to_workspace(org.id, ws.id, person.id, "admin")
        snapshot = self.os.proactive_intelligence.refresh_snapshot(org.id, person.id, "workspace", ws.id)
        self.assertEqual(snapshot["status"], "insufficient_evidence")
        self.assertEqual(snapshot["payload"]["degraded_reason"], "no_visible_evidence")
        self.assertEqual(snapshot["attention"], [])

    def test_zero_delta_refresh_skips_append_and_changed_projection_versions(self) -> None:
        org = self.os.create_organization("Delta org", "org_delta_intel")
        ws = self.os.create_organization_workspace(org.id, "Delta client", "client", "ws_delta_intel")
        person = self.os.create_person(org.id, "Owner", "delta@intel.test", role="owner", person_id="person_delta_intel")
        self.os.add_person_to_workspace(org.id, ws.id, person.id, "admin")
        first = self.os.proactive_intelligence.refresh_snapshot(org.id, person.id, "workspace", ws.id)
        same = self.os.proactive_intelligence.refresh_snapshot(org.id, person.id, "workspace", ws.id)
        self.assertEqual(same["id"], first["id"])
        self.assertTrue(same["unchanged"])
        self.os.client_ops.create_risk(
            org.id, ws.id, person.id, "delivery", "critical", 0.9,
            "New blocker", "new blocker evidence", "Assign an owner",
        )
        changed = self.os.proactive_intelligence.refresh_snapshot(org.id, person.id, "workspace", ws.id)
        self.assertNotEqual(changed["id"], first["id"])
        self.assertFalse(changed["unchanged"])
        self.assertEqual(changed["version"], 2)
        self.assertNotEqual(changed["projection_fingerprint"], first["projection_fingerprint"])

    def test_workspace_work_item_delta_appends_new_reader_payload_version(self) -> None:
        first = self.os.proactive_intelligence.refresh_snapshot(
            "org_demo", "person_demo_owner", "workspace", "ws_alpha", "act_alpha_admin"
        )
        open_work_before = first["payload"]["context"]["open_work_count"]
        self.os.work_ops.create(
            "org_demo", "ws_alpha", "person_demo_owner",
            "Fingerprint workspace work", "Track a workspace payload delta", "Owner",
        )
        changed = self.os.proactive_intelligence.refresh_snapshot(
            "org_demo", "person_demo_owner", "workspace", "ws_alpha", "act_alpha_admin"
        )
        self.assertNotEqual(changed["id"], first["id"])
        self.assertEqual(changed["version"], first["version"] + 1)
        self.assertEqual(changed["payload"]["context"]["open_work_count"], open_work_before + 1)

    def test_executive_work_item_delta_appends_new_reader_payload_version(self) -> None:
        first = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner")
        open_work_before = first["payload"]["portfolio"]["open_work"]
        self.os.work_ops.create(
            "org_demo", "ws_alpha", "person_demo_owner",
            "Fingerprint executive work", "Track an executive payload delta", "Owner",
        )
        changed = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner")
        self.assertNotEqual(changed["id"], first["id"])
        self.assertEqual(changed["version"], first["version"] + 1)
        self.assertEqual(changed["payload"]["portfolio"]["open_work"], open_work_before + 1)

    def test_refresh_enqueue_default_is_unique_and_explicit_key_dedupes(self) -> None:
        first = self.os.proactive_intelligence.enqueue_refresh(self.identity, "executive")
        second = self.os.proactive_intelligence.enqueue_refresh(self.identity, "executive")
        explicit = self.os.proactive_intelligence.enqueue_refresh(self.identity, "executive", idempotency_key="manual-key")
        explicit_again = self.os.proactive_intelligence.enqueue_refresh(self.identity, "executive", idempotency_key="manual-key")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(explicit["id"], explicit_again["id"])
        self.assertEqual(first["type"], "proactive_intelligence.refresh")
        self.assertIsNone(first["workspace_id"])

    def test_completed_manual_refresh_job_does_not_block_changed_state_refresh(self) -> None:
        first_job = self.os.proactive_intelligence.enqueue_refresh(self.identity, "workspace", "ws_alpha")
        first_result = run_one_job(self.os, "org_demo", "ws_alpha", "worker-manual-1")
        self.assertEqual(first_result["id"], first_job["id"])
        self.os.work_ops.create(
            "org_demo", "ws_alpha", "person_demo_owner",
            "Manual refresh work", "Ensure completed job does not stale future refresh", "Owner",
        )
        second_job = self.os.proactive_intelligence.enqueue_refresh(self.identity, "workspace", "ws_alpha")
        self.assertNotEqual(first_job["id"], second_job["id"])
        second_result = run_one_job(self.os, "org_demo", "ws_alpha", "worker-manual-2")
        self.assertEqual(second_result["id"], second_job["id"])
        self.assertFalse(second_result["result"]["unchanged"])
        self.assertEqual(second_result["result"]["version"], first_result["result"]["version"] + 1)

    def test_persisted_refresh_does_not_call_reasoning_provider(self) -> None:
        provider = CountingReasoningProvider()
        os = CompanyOS(":memory:", strategic_reasoning_provider=provider)
        os.seed_demo(FIXTURES)
        try:
            live = os.intelligence.workspace("org_demo", "ws_alpha", "person_demo_owner", "act_alpha_admin")
            self.assertEqual(provider.calls, 1)
            self.assertEqual(live["deliberation"]["provider_metadata"]["status"], "used")
            snapshot = os.proactive_intelligence.refresh_snapshot(
                "org_demo", "person_demo_owner", "workspace", "ws_alpha", "act_alpha_admin"
            )
            self.assertEqual(provider.calls, 1)
            self.assertEqual(snapshot["payload"]["deliberation"]["provider_metadata"]["status"], "disabled")
        finally:
            os.close()

    def test_worker_refreshes_snapshot_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.sqlite"
            os = CompanyOS(path)
            os.seed_demo(FIXTURES)
            _token, identity = issue_identity(os, "org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin")
            job = os.proactive_intelligence.enqueue_refresh(identity, "workspace", "ws_alpha")
            os.close()

            worker_os = CompanyOS(path)
            result = run_one_job(worker_os, "org_demo", "ws_alpha", "worker-1")
            self.assertEqual(result["id"], job["id"])
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["result"]["snapshot_type"], "workspace")
            latest = worker_os.proactive_intelligence.require_latest_snapshot(
                "org_demo", "person_demo_owner", "workspace", "ws_alpha"
            )
            self.assertEqual(latest["id"], result["result"]["snapshot_id"])
            worker_os.close()

    def test_http_refresh_and_read_are_authenticated_and_scoped(self) -> None:
        server = serve(self.os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        def request(method: str, path: str, token: str | None = self.token, payload: dict | None = None) -> tuple[int, dict]:
            connection = HTTPConnection(host, port, timeout=5)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            body = None
            if payload is not None:
                body = json.dumps(payload)
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            connection.close()
            return response.status, data

        try:
            status, body = request("GET", "/dashboard/intelligence/snapshots?organization_id=org_demo&person_id=person_demo_owner", None)
            self.assertEqual(status, 401)
            status, body = request("POST", "/dashboard/intelligence/refresh", payload={"snapshot_type": "executive"})
            self.assertEqual(status, 202)
            status, same = request("POST", "/dashboard/intelligence/refresh", payload={"snapshot_type": "executive"})
            self.assertNotEqual(body["job"]["id"], same["job"]["id"])
            status, keyed = request("POST", "/dashboard/intelligence/refresh", payload={"snapshot_type": "executive", "idempotency_key": "http-refresh-key"})
            self.assertEqual(status, 202)
            status, keyed_again = request("POST", "/dashboard/intelligence/refresh", payload={"snapshot_type": "executive", "idempotency_key": "http-refresh-key"})
            self.assertEqual(keyed["job"]["id"], keyed_again["job"]["id"])
            run_one_job(self.os, "org_demo", None, "worker-http")
            status, snapshot = request("GET", "/dashboard/intelligence/snapshots?organization_id=org_demo&person_id=person_demo_owner")
            self.assertEqual(status, 200)
            self.assertEqual(snapshot["snapshot"]["snapshot_type"], "executive")
            self.assertIn("generated_at", snapshot["snapshot"])
            status, attention = request("GET", "/dashboard/intelligence/attention?organization_id=org_demo&person_id=person_demo_owner")
            self.assertEqual(status, 200)
            self.assertLessEqual(len(attention["attention"]), 3)
            status, _ = request("GET", "/dashboard/intelligence/snapshots?organization_id=org_demo&person_id=someone_else")
            self.assertEqual(status, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_person_scoped_executive_snapshot_does_not_leak_unpermitted_workspace(self) -> None:
        visible = self.os.create_organization_workspace("org_demo", "Visible proactive", "client", "ws_proactive_visible")
        self.os.add_person_to_workspace("org_demo", visible.id, "person_demo_owner", "admin")
        hidden = self.os.create_organization_workspace("org_demo", "Hidden proactive", "client", "ws_proactive_hidden")
        self.os.store.conn.execute(
            """INSERT INTO risks(
                id, organization_id, workspace_id, project_id, type, severity, probability,
                impact, owner_person_id, detected_at, status, evidence, recommended_action,
                resolved_at, resolution
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "risk_hidden_proactive", "org_demo", hidden.id, None, "delivery", "critical", 0.95,
                "Hidden launch blocker", "person_demo_owner", "2026-08-20T00:00:00+00:00",
                "open", "hidden workspace evidence", "Do not leak", None, None,
            ),
        )
        self.os.store.conn.commit()
        snapshot = self.os.proactive_intelligence.refresh_snapshot("org_demo", "person_demo_owner")
        payload_text = json.dumps(snapshot["payload"], sort_keys=True)
        self.assertIn(visible.id, payload_text)
        self.assertNotIn(hidden.id, payload_text)
        self.assertNotIn("hidden workspace evidence", payload_text)


if __name__ == "__main__":
    unittest.main()
