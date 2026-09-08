from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from auremgrid.storage.migrations import migrate
from auremgrid.storage.sqlite import SCHEMA
from scripts.pilot_report import build_report


ROOT = Path(__file__).resolve().parents[1]


def insert_fixture(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO organizations(id, name, created_at) VALUES ('org_1', 'Agency', '2026-09-01T00:00:00')"
    )
    conn.executemany(
        "INSERT INTO workspaces(id, name, created_at) VALUES (?, ?, ?)",
        [
            ("ws_1", "Prime", "2026-09-01T00:00:00"),
            ("ws_2", "Second", "2026-09-01T00:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO workspace_organization(workspace_id, organization_id, kind) VALUES (?, ?, ?)",
        [("ws_1", "org_1", "client"), ("ws_2", "org_1", "client")],
    )
    conn.execute(
        """
        INSERT INTO people(
            id, organization_id, name, email, title, department, manager_id, status,
            created_at, updated_at
        ) VALUES (
            'person_1','org_1','Owner','owner@example.com','Owner','Ops',NULL,'active',
            '2026-09-01T00:00:00','2026-09-01T00:00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO projects(
            id, organization_id, workspace_id, name, description, owner_person_id,
            status, priority, start_date, due_date, budget, tags, health, progress,
            created_at, updated_at
        ) VALUES (
            'project_1','org_1','ws_1','Pilot','Pilot project','person_1',
            'active','normal','2026-09-01','2026-09-30',NULL,'[]','green',0.2,
            '2026-09-01T00:00:00','2026-09-01T00:00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO deliverables(
            id, organization_id, workspace_id, project_id, work_item_id, title, type,
            owner_person_id, current_version, approval_status, preview_url, final_url,
            reviewer_person_id, client_approver_contact_id, revision_count, created_at,
            shipped_at
        ) VALUES (
            'deliverable_1','org_1','ws_1','project_1',NULL,'Pilot deck','deck',
            'person_1',1,'in_review',NULL,NULL,'person_1',NULL,0,
            '2026-09-01T00:00:00',NULL
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO proactive_intelligence_snapshots(
            id, organization_id, workspace_id, person_id, snapshot_type, version,
            status, degraded_reason, projection_fingerprint, generated_at,
            payload_json, evidence_refs_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                "snap_1",
                "org_1",
                "ws_1",
                "person_1",
                "workspace",
                1,
                "ready",
                None,
                "projection_1",
                "2026-09-01T08:00:00",
                "{}",
                "[]",
                "2026-09-01T08:00:00",
            ),
            (
                "snap_2",
                "org_1",
                "ws_2",
                "person_1",
                "workspace",
                1,
                "ready",
                None,
                "projection_2",
                "2026-09-01T08:00:00",
                "{}",
                "[]",
                "2026-09-01T08:00:00",
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO proactive_intelligence_attention_items(
            id, snapshot_id, organization_id, workspace_id, person_id, rank, title,
            narrative, status, evidence_refs_json, generated_at, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                "item_1",
                "snap_1",
                "org_1",
                "ws_1",
                "person_1",
                1,
                "Watch spend",
                "Spend is moving",
                "open",
                "[]",
                "2026-09-01T08:00:00",
                "2026-09-01T08:00:00",
            ),
            (
                "item_2",
                "snap_1",
                "org_1",
                "ws_1",
                "person_1",
                2,
                "Watch reviews",
                "Reviews are waiting",
                "open",
                "[]",
                "2026-09-01T08:00:00",
                "2026-09-01T08:00:00",
            ),
            (
                "item_3",
                "snap_2",
                "org_1",
                "ws_2",
                "person_1",
                1,
                "Other workspace",
                "Other workspace item",
                "open",
                "[]",
                "2026-09-01T08:00:00",
                "2026-09-01T08:00:00",
            ),
        ],
    )
    rows = [
        (
            "att_1",
            "org_1",
            "ws_1",
            "person_1",
            "fp_1",
            "snap_1",
            "item_1",
            "acknowledged",
            "trace_1",
            "rec_1",
            "{}",
            None,
            "needs attention",
            "2026-09-01T08:00:00",
            "2026-09-01T10:00:00",
        ),
        (
            "att_2",
            "org_1",
            "ws_1",
            "person_1",
            "fp_2",
            "snap_1",
            "item_2",
            "dismissed",
            "trace_2",
            None,
            "{}",
            None,
            "not relevant",
            "2026-09-01T09:00:00",
            "2026-09-01T15:00:00",
        ),
        (
            "att_3",
            "org_1",
            "ws_2",
            "person_1",
            "fp_3",
            "snap_2",
            "item_3",
            "resolved",
            "trace_3",
            None,
            "{}",
            None,
            "other workspace",
            "2026-09-01T08:00:00",
            "2026-09-01T09:00:00",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO proactive_intelligence_attention_lifecycle(
            id, organization_id, workspace_id, person_id, fingerprint, snapshot_id,
            attention_item_id, status, trace_id, recommendation_id, action_descriptor_json,
            approval_request_id, reason, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.execute(
        """
        INSERT INTO intelligence_recommendations(
            id, organization_id, workspace_id, summary, runbook_id, runbook_version,
            profile_contributors_json, confidence, options_json, recommended_option_id,
            evidence_refs_json, generated_by_type, generated_by_id, recorded_by_person_id,
            evaluation_window_start, evaluation_window_end, created_at
        ) VALUES (
            'rec_1','org_1','ws_1','Raise budget','runbook_1',1,'[]',0.8,'[]','option_1',
            '[]','system','sys','person_1','2026-09-01','2026-09-07','2026-09-01T08:00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO intelligence_recommendation_lifecycle(
            id, organization_id, workspace_id, recommendation_id, event_type, accepted,
            rejected, chosen_option_id, evaluation_window_start, evaluation_window_end,
            measured_outcomes_json, score, lessons, evidence_refs_json, recorded_by_person_id,
            created_at
        ) VALUES (
            'life_1','org_1','ws_1','rec_1','accepted',1,0,'option_1','2026-09-01',
            '2026-09-07','{}',0.7,'worked','[]','person_1','2026-09-02T08:00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO intelligence_recommendation_handoffs(
            id, organization_id, workspace_id, recommendation_id, trace_id,
            reviewed_by_person_id, review_status, decision_id, approval_request_id,
            work_item_id, action_descriptor_json, outcome_refs_json, notes, created_at
        ) VALUES (
            'handoff_1','org_1','ws_1','rec_1','trace_1','person_1','accepted',
            'decision_1',NULL,NULL,'{}','[]','accepted','2026-09-02T09:00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO outcome_attribution_plans(
            id, organization_id, workspace_id, subject_kind, subject_id, metric_names_json,
            evaluation_window_days, evaluation_window_start, evaluation_window_end, status,
            baseline_summary_json, outcome_summary_json, created_by_person_id,
            evaluated_by_person_id, created_at, evaluated_at
        ) VALUES (
            'attr_1','org_1','ws_1','decision','decision_1','["revenue"]',7,
            '2026-09-01','2026-09-07','evaluated','{}','{}','person_1','person_1',
            '2026-09-01T08:00:00','2026-09-08T08:00:00'
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO outcome_attribution_snapshots(
            id, organization_id, workspace_id, attribution_id, snapshot_kind, captured_at,
            window_start, window_end, values_json, deltas_json, created_by_person_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                "snap_attr_1",
                "org_1",
                "ws_1",
                "attr_1",
                "baseline",
                "2026-09-01T08:00:00",
                "2026-08-25",
                "2026-08-31",
                "{}",
                None,
                "person_1",
            ),
            (
                "snap_attr_2",
                "org_1",
                "ws_1",
                "attr_1",
                "outcome",
                "2026-09-08T08:00:00",
                "2026-09-01",
                "2026-09-07",
                "{}",
                "{}",
                "person_1",
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO provider_import_cursors(
            id, organization_id, workspace_id, provider, account_id, resource,
            cursor_value, status, last_error, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                "cursor_1",
                "org_1",
                "ws_1",
                "meta_ads",
                "acct_1",
                "campaigns",
                "cur",
                "configured",
                None,
                "2026-09-01T08:00:00",
            ),
            (
                "cursor_2",
                "org_1",
                "ws_2",
                "google_ads",
                "acct_2",
                "campaigns",
                "cur",
                "degraded",
                "bad token",
                "2026-09-01T08:00:00",
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO provider_import_records(
            id, organization_id, workspace_id, provider, object_type, external_id,
            account_id, occurred_at, amount, currency, payload_hash, source, imported_at
        ) VALUES (
            'record_1','org_1','ws_1','meta_ads','campaign','ext_1','acct_1',
            '2026-09-01T08:00:00',NULL,NULL,'hash_1','import','2026-09-01T08:01:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO provider_import_quarantines(
            id, organization_id, provider, object_type, external_id, reason,
            evidence_digest, created_at, quarantine_details
        ) VALUES (
            'quarantine_1','org_1','meta_ads','campaign','bad_1','invalid',
            'digest','2026-09-01T08:00:00','{}'
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO provider_sync_tasks(
            id, workspace_id, connector, account_key, stream_key, generation_id,
            task_type, external_id, route_key, page_token, payload, status, lease_owner,
            lease_token, lease_expires_at, created_at, updated_at, completed_at,
            operation_key
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                "sync_1",
                "ws_1",
                "meta_ads",
                "acct_1",
                "campaigns",
                None,
                "reconcile",
                "ext_1",
                None,
                None,
                "{}",
                "completed",
                None,
                None,
                None,
                "2026-09-01T08:00:00",
                "2026-09-01T08:10:00",
                "2026-09-01T08:10:00",
                "op_1",
            ),
            (
                "sync_2",
                "ws_2",
                "google_ads",
                "acct_2",
                "campaigns",
                None,
                "reconcile",
                "ext_2",
                None,
                None,
                "{}",
                "pending",
                None,
                None,
                None,
                "2026-09-01T08:00:00",
                "2026-09-01T08:10:00",
                None,
                "op_2",
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO reviews(
            id, organization_id, workspace_id, deliverable_id, version, kind, status,
            reviewer_person_id, opened_at, closed_at, decision
        ) VALUES (
            'review_1','org_1','ws_1','deliverable_1',1,'internal','closed','person_1',
            '2026-09-01T08:00:00','2026-09-01T20:00:00','approve'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO memory_proposals(
            id, organization_id, workspace_id, kind, proposed_by_type, proposed_by_id,
            content, structured_payload, source_id, evidence, confidence, status,
            reviewed_by_person_id, reviewed_at, promoted_type, promoted_id, created_at
        ) VALUES (
            'proposal_1','org_1','ws_1','fact','person','person_1','content','{}',
            NULL,'evidence',0.9,'approved','person_1','2026-09-02T08:00:00',
            'canonical','entity_1','2026-09-01T08:00:00'
        )
        """
    )
    conn.commit()


class PilotReportTests(unittest.TestCase):
    def make_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        migrate(conn)
        insert_fixture(conn)
        return conn

    def test_valid_summary_on_small_fixture(self) -> None:
        conn = self.make_conn()
        report = build_report(conn)

        attention = report["metrics"]["attention"]
        self.assertEqual(attention["items_created"], 3)
        self.assertEqual(attention["acknowledged"], 1)
        self.assertEqual(attention["closed"], 2)
        self.assertEqual(attention["dismissed"], 1)
        self.assertEqual(attention["median_time_to_action_hours"], 2.0)

        recommendations = report["metrics"]["recommendations"]
        self.assertEqual(recommendations["recommendations_created"], 1)
        self.assertEqual(recommendations["accepted"], 1)
        self.assertEqual(recommendations["outcome_attribution_plans"]["evaluated"], 1)
        self.assertEqual(recommendations["outcome_attribution_snapshots"]["baseline"], 1)
        self.assertEqual(recommendations["outcome_attribution_snapshots"]["outcome"], 1)

        connectors = report["metrics"]["connectors"]["providers"]["meta_ads"]
        self.assertEqual(connectors["synced_cursors"], 1)
        self.assertEqual(connectors["imported_records"], 1)
        self.assertEqual(connectors["quarantines"], 1)
        self.assertEqual(connectors["reconcile_tasks"], 1)

        self.assertEqual(report["metrics"]["reviews"]["median_turnaround_hours"], 12.0)
        self.assertEqual(report["metrics"]["evidence"]["total_proposals"], 1)

    def test_unknown_metric_reporting(self) -> None:
        conn = self.make_conn()
        report = build_report(conn)

        attention = report["metrics"]["attention"]
        self.assertEqual(attention["false_alert_rate"]["status"], "unknown")
        self.assertIn("false-positive marker", attention["false_alert_rate"]["reason"])
        self.assertEqual(attention["attention_usefulness"]["status"], "unknown")

        evidence = report["metrics"]["evidence"]
        self.assertEqual(evidence["evidence_correctness"]["status"], "unknown")
        self.assertIn("correctness verdict", evidence["evidence_correctness"]["reason"])
        self.assertEqual(report["metrics"]["operator_verdicts"]["status"], "none_recorded")
        self.assertIn("No operator verdicts", report["metrics"]["operator_verdicts"]["message"])

    def test_captured_verdicts_replace_unknown_report_fields(self) -> None:
        conn = self.make_conn()
        conn.executescript(
            """
            INSERT INTO pilot_operator_verdicts(
                id,organization_id,workspace_id,scenario_id,verdict,notes,recorded_by_person_id,created_at
            ) VALUES
                ('verdict_1','org_1','ws_1','evidence.correctness','positive','citations checked','person_1','2026-09-03T08:00:00'),
                ('verdict_2','org_1','ws_1','attention.false_alert_rate','negative','too noisy','person_1','2026-09-03T09:00:00'),
                ('verdict_3','org_1','ws_2','evidence.correctness','mixed','other workspace','person_1','2026-09-03T10:00:00');
            """
        )
        report = build_report(conn, "ws_1")

        verdicts = report["metrics"]["operator_verdicts"]
        self.assertEqual(verdicts["total"], 2)
        self.assertEqual(verdicts["verdict_counts"]["positive"], 1)
        self.assertEqual(report["metrics"]["evidence"]["evidence_correctness"]["verdict"], "positive")
        self.assertEqual(report["metrics"]["attention"]["false_alert_rate"]["notes"], "too noisy")

    def test_workspace_scoping(self) -> None:
        conn = self.make_conn()
        report = build_report(conn, "ws_1")

        self.assertEqual(report["scope"]["workspace_id"], "ws_1")
        self.assertEqual(report["metrics"]["attention"]["items_created"], 2)
        self.assertEqual(report["metrics"]["connectors"]["provider_count"], 1)
        self.assertEqual(
            report["metrics"]["connectors"]["providers"]["meta_ads"]["imported_records"],
            1,
        )
        self.assertNotIn("google_ads", report["metrics"]["connectors"]["providers"])
        self.assertEqual(report["metrics"]["reviews"]["opened"], 1)

    def test_cli_outputs_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "pilot.sqlite"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(SCHEMA)
                migrate(conn)
                insert_fixture(conn)
            finally:
                conn.close()

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "pilot_report.py"),
                    "--db",
                    str(db_path),
                    "--workspace",
                    "ws_1",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["scope"]["workspace_id"], "ws_1")
            self.assertEqual(payload["metrics"]["attention"]["items_created"], 2)


if __name__ == "__main__":
    unittest.main()
