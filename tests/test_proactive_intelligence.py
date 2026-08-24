from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

from auremgrid.api.http import serve
from auremgrid.domain.errors import ValidationError
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
        detectors = {item["type"]: item for item in snapshot["payload"]["proactive_detectors"]}
        self.assertTrue(all(item["status"] == "insufficient_evidence" for item in detectors.values()))
        self.assertEqual(snapshot["attention"], [])

    def test_executive_snapshot_includes_read_only_8am_detectors(self) -> None:
        as_of = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
        org = self.os.create_organization("Detector org", "org_detector_intel")
        ws = self.os.create_organization_workspace(org.id, "Detector client", "client", "ws_detector_intel")
        person = self.os.create_person(org.id, "Owner", "detector@intel.test", role="owner", person_id="person_detector_intel")
        self.os.add_person_to_workspace(org.id, ws.id, person.id, "admin")
        project = self.os.create_project(org.id, ws.id, person.id, "Detector project")

        self.os.store.conn.execute(
            """INSERT INTO client_health_snapshots(
                id, organization_id, workspace_id, overall, relationship, delivery,
                performance, finance, communication, scope, sentiment, contributing_signals,
                explanation, previous_score, trend, calculated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "health_detector", org.id, ws.id, 62, 65, 58, 70, 50, 72, 55, -0.2,
                json.dumps(["late delivery", "scope concern"]), "Delivery and scope have both deteriorated.",
                78, "down", (as_of - timedelta(hours=2)).isoformat(),
            ),
        )
        self.os.work_ops.create(
            org.id, ws.id, person.id, "Overdue detector commitment",
            "Ship the overdue commitment", "Owner", project_id=project.id,
            deadline="2026-08-20",
        )
        contract = self.os.client_ops.create_contract(
            org.id, ws.id, person.id, "retainer", "monthly", "2026-01-01",
            10000, renewal_date="2026-09-15",
        )
        allowance = self.os.client_ops.add_scope_allowance(
            org.id, ws.id, person.id, contract["id"], "creative", "monthly",
            included_quantity=10,
        )
        self.os.client_ops.record_scope_usage(
            org.id, ws.id, person.id, contract["id"], allowance["id"],
            "2026-08-01", delivered=12,
        )
        self.os.store.conn.execute(
            """INSERT INTO client_economics(
                id, organization_id, workspace_id, period_start, revenue, labor_cost,
                software_cost, ai_cost, other_cost, gross_contribution, margin, calculated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "economics_detector", org.id, ws.id, "2026-08-01",
                10000, 7800, 500, 200, 500, 1000, 0.1, as_of.isoformat(),
            ),
        )
        deliverable = self.os.create_deliverable(org.id, ws.id, person.id, project.id, "Detector brief", "report")
        review = self.os.open_review(org.id, ws.id, person.id, deliverable.id, reviewer_person_id=person.id)
        self.os.store.conn.execute(
            "UPDATE reviews SET opened_at=? WHERE id=?",
            ((as_of - timedelta(days=3)).isoformat(), review.id),
        )
        self.os.agency_ops.calculate_capacity(org.id, person.id, person.id, "2026-08-24", 8, 12)
        campaign = self.os.agency_ops.create_campaign(org.id, ws.id, person.id, "Detector campaign", "Lead gen", "Meta")
        self.os.store.conn.execute(
            "INSERT INTO campaign_anomalies VALUES (?,?,?,?,?,?,?,?)",
            (
                "anomaly_detector", campaign["id"], "cpl", "high",
                "CPL spiked above the recorded control.", "campaign_metric_snapshots:latest",
                as_of.isoformat(), "open",
            ),
        )
        self.os.store.conn.execute(
            """INSERT INTO feedback_patterns(
                id, organization_id, workspace_id, category, pattern_key, occurrence_count,
                first_seen_at, last_seen_at, sample_evidence, proposed_preference_id,
                preference_status, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "feedback_detector", org.id, ws.id, "process", "wants earlier creative review",
                3, "2026-08-01T00:00:00+00:00", as_of.isoformat(),
                json.dumps(["Asked for review earlier", "Repeated in kickoff"]), None,
                "observing", as_of.isoformat(), as_of.isoformat(),
            ),
        )
        self.os.store.conn.commit()

        snapshot = self.os.proactive_intelligence.refresh_snapshot(org.id, person.id, as_of=as_of)
        detectors = {item["type"]: item for item in snapshot["payload"]["proactive_detectors"]}
        expected = {
            "health", "overdue_commitment", "scope", "margin", "stalled_review",
            "capacity", "campaign_anomaly", "feedback", "renewal", "expansion",
        }
        self.assertEqual(set(detectors), expected)
        self.assertTrue(all(detectors[item]["status"] == "open" for item in expected))
        self.assertTrue(all(detectors[item]["evidence"] for item in expected))
        self.assertLessEqual(len(snapshot["attention"]), 3)
        self.assertTrue({item["title"] for item in snapshot["attention"]}.issubset({item["title"] for item in detectors.values()}))
        self.assertTrue(all(item["action_descriptor"] is None for item in snapshot["attention"]))
        detector_text = json.dumps(snapshot["payload"]["proactive_detectors"], sort_keys=True)
        self.assertNotIn("action_descriptor", detector_text)
        self.assertNotIn("action_descriptors", detector_text)

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

    def test_refresh_status_distinguishes_worker_lifecycle_states(self) -> None:
        empty = self.os.proactive_intelligence.refresh_status(self.identity, "executive")
        self.assertEqual(empty["status"], "no_snapshot")
        self.assertTrue(empty["worker_required"])
        self.assertIn("worker-once", empty["worker_command"])
        self.assertIsNone(empty["latest_job"])
        self.assertIsNone(empty["latest_snapshot"])

        job = self.os.proactive_intelligence.enqueue_refresh(self.identity, "executive")
        queued = self.os.proactive_intelligence.refresh_status(self.identity, "executive")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["latest_job"]["id"], job["id"])
        self.assertTrue(queued["worker_required"])

        result = run_one_job(self.os, "org_demo", None, "worker-status")
        self.assertEqual(result["status"], "succeeded")
        ready = self.os.proactive_intelligence.refresh_status(self.identity, "executive")
        self.assertEqual(ready["status"], "ready")
        self.assertFalse(ready["worker_required"])
        self.assertEqual(ready["latest_job"]["status"], "succeeded")
        self.assertGreaterEqual(ready["latest_snapshot"]["version"], 1)

    def test_workspace_refresh_status_is_scoped_and_validated(self) -> None:
        with self.assertRaises(ValidationError):
            self.os.proactive_intelligence.refresh_status(self.identity, "workspace")
        status = self.os.proactive_intelligence.refresh_status(
            self.identity, "workspace", "ws_alpha"
        )
        self.assertEqual(status["workspace_id"], "ws_alpha")
        self.assertIn("--workspace ws_alpha", status["worker_command"])

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
