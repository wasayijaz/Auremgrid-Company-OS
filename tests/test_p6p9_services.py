from __future__ import annotations

import sqlite3
import unittest
import uuid

from auremgrid.domain.errors import ValidationError
from auremgrid.services.feedback_ops import FeedbackOperations
from auremgrid.services.performance_ops import PerformanceOperations
from auremgrid.services.forecast_ops import ForecastOperations
from auremgrid.services.retention_ops import RetentionOperations


SCHEMA = """
CREATE TABLE feedback_patterns (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, category TEXT, pattern_key TEXT, occurrence_count INTEGER, first_seen_at TEXT, last_seen_at TEXT, sample_evidence TEXT, proposed_preference_id TEXT, preference_status TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE feedback_events (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, pattern_id TEXT, category TEXT, raw_feedback TEXT, source_type TEXT, source_id TEXT, recorded_by_person_id TEXT, created_at TEXT);
CREATE TABLE performance_insights (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, insight_type TEXT, subject_type TEXT, subject_id TEXT, comparison_subject_id TEXT, metric_name TEXT, metric_value_a REAL, metric_value_b REAL, delta REAL, direction TEXT, confidence REAL, evidence_summary TEXT, source_snapshot_ids TEXT, status TEXT, approved_by_person_id TEXT, approved_at TEXT, created_at TEXT);
CREATE TABLE forecasts (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, forecast_type TEXT, subject_id TEXT, period_start TEXT, period_end TEXT, predicted_value REAL, confidence REAL, basis TEXT, data_points INTEGER, status TEXT, created_at TEXT);
CREATE TABLE retention_policies (id TEXT PRIMARY KEY, organization_id TEXT, scope TEXT, scope_id TEXT, data_category TEXT, max_age_days INTEGER, action TEXT, created_by_person_id TEXT, created_at TEXT);
CREATE TABLE deletion_audit (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, table_name TEXT, record_id TEXT, reason TEXT, initiated_by TEXT, retention_policy_id TEXT, snapshot_json TEXT, deleted_at TEXT);
CREATE TABLE campaigns (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, name TEXT, objective TEXT, platform TEXT, budget REAL, currency TEXT, start_date TEXT, end_date TEXT, status TEXT, owner_person_id TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE creative_assets (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, project_id TEXT, campaign_id TEXT, title TEXT, platform TEXT, format TEXT, dimensions TEXT, creator_person_id TEXT, reviewer_person_id TEXT, approval_state TEXT, source_url TEXT, final_url TEXT, thumbnail_url TEXT, revision_count INTEGER, style_tags TEXT, created_at TEXT);
CREATE TABLE creative_performance (id TEXT PRIMARY KEY, asset_id TEXT, campaign_id TEXT, captured_at TEXT, impressions REAL, clicks REAL, conversions REAL, spend REAL, revenue REAL, ctr REAL, cvr REAL, roas REAL, source TEXT);
CREATE TABLE campaign_metric_snapshots (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, campaign_id TEXT, captured_at TEXT, metric_name TEXT, metric_value REAL, spend REAL, revenue REAL, leads REAL, impressions REAL, clicks REAL, cpl REAL, cac REAL, ctr REAL, cvr REAL, roas REAL, source TEXT);
CREATE TABLE revenues (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, project_id TEXT, amount REAL, currency TEXT, kind TEXT, recognized_at TEXT, source TEXT);
CREATE TABLE contracts (id TEXT PRIMARY KEY, organization_id TEXT, client_id TEXT, end_date TEXT, status TEXT);
CREATE TABLE capacity_snapshots (id TEXT PRIMARY KEY, organization_id TEXT, person_id TEXT, week_start TEXT, available_hours REAL, estimated_assigned_hours REAL, booked_hours REAL, remaining_hours REAL, overloaded INTEGER, calculated_at TEXT, captured_at TEXT, utilized_hours REAL, utilization_pct REAL);
CREATE TABLE projects (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, name TEXT);
CREATE TABLE deliverables (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, title TEXT);
CREATE TABLE work_items (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT, title TEXT);
CREATE TABLE content_performance (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT);
CREATE TABLE meetings (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT);
CREATE TABLE documents (id TEXT PRIMARY KEY, organization_id TEXT, workspace_id TEXT);
"""


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.n = 0
        def new_id(prefix):
            self.n += 1
            return f"{prefix}_{self.n}_{uuid.uuid4().hex[:6]}"
        self.new_id = new_id
        self.feedback = FeedbackOperations(self.conn, new_id, lambda *a, **k: None)
        self.performance = PerformanceOperations(self.conn, new_id, lambda *a, **k: None)
        self.forecast = ForecastOperations(self.conn, new_id, lambda *a, **k: None)
        self.retention = RetentionOperations(self.conn, new_id, lambda *a, **k: None)
        self.org, self.ws, self.person = "org1", "ws1", "p1"

    def test_record_feedback_creates_pattern_and_event(self):
        out = self.feedback.record_feedback(self.org, self.ws, self.person, "design", "Use blue", "review")
        self.assertEqual(out["preference_status"], "observing")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback_patterns").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 1)

    def test_record_feedback_accumulates(self):
        for _ in range(3): out = self.feedback.record_feedback(self.org, self.ws, self.person, "copy", "Shorter", "review")
        self.assertEqual(out["occurrence_count"], 3); self.assertEqual(out["preference_status"], "proposed")

    def test_semantically_equivalent_design_feedback_converges(self):
        samples = (
            "too polished",
            "this looks AI generated",
            "do not smooth the skin this much",
        )
        results = [self.feedback.record_feedback(self.org, self.ws, self.person, "design", text, "review") for text in samples]
        self.assertEqual({item["pattern_id"] for item in results}, {results[0]["pattern_id"]})
        self.assertEqual(results[-1]["pattern_key"], "semantic:natural-human-texture")
        self.assertEqual(results[-1]["occurrence_count"], 3)
        self.assertEqual(results[-1]["preference_status"], "proposed")

    def test_embedding_provider_clusters_non_literal_paraphrases(self):
        class Provider:
            def embed(self, texts):
                return [[1.0, 0.0] if "headline" in text or "title" in text else [0.0, 1.0] for text in texts]

        feedback = FeedbackOperations(self.conn, self.new_id, lambda *a, **k: None, Provider())
        first = feedback.record_feedback(self.org, self.ws, self.person, "copy", "Make the headline punchier", "review")
        second = feedback.record_feedback(self.org, self.ws, self.person, "copy", "Give the title more energy", "review")
        self.assertEqual(first["pattern_id"], second["pattern_id"])
        self.assertEqual(second["occurrence_count"], 2)

    def test_list_patterns_filters_by_category(self):
        self.feedback.record_feedback(self.org, self.ws, self.person, "design", "A", "x")
        self.feedback.record_feedback(self.org, self.ws, self.person, "copy", "B", "x")
        rows = self.feedback.list_patterns(self.org, self.ws, self.person, category="copy")
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]["category"], "copy")

    def test_promote_and_decide_pattern(self):
        p = self.feedback.record_feedback(self.org, self.ws, self.person, "other", "A", "x")["pattern_id"]
        self.assertEqual(self.feedback.promote_pattern(self.org, self.ws, self.person, p)["preference_status"], "proposed")
        self.assertEqual(self.feedback.decide_pattern(self.org, self.ws, self.person, p, "approved")["preference_status"], "approved")

    def test_anomaly_detection(self):
        for i, value in enumerate((10, 11, 100)):
            self.conn.execute("INSERT INTO campaign_metric_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"s{i}", self.org, self.ws, "c1", f"2026-08-0{i+1}", "roas", value, None, None, None, None, None, None, None, None, None, None, "x"))
        rows = self.performance.generate_insights(self.org, self.ws, self.person, "anomaly")
        self.assertTrue(rows); self.assertEqual(rows[0]["insight_type"], "anomaly")

    def test_list_insights_and_decide(self):
        self.conn.execute("INSERT INTO campaign_metric_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("s1", self.org, self.ws, "c1", "2026-08-01", "roas", 10, None, None, None, None, None, None, None, None, None, None, "x"))
        for i in (2, 3): self.conn.execute("INSERT INTO campaign_metric_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"s{i}", self.org, self.ws, "c1", f"2026-08-0{i}", "roas", 1, None, None, None, None, None, None, None, None, None, None, "x"))
        iid = self.performance.generate_insights(self.org, self.ws, self.person, "anomaly")[0]["id"]
        self.assertEqual(len(self.performance.list_insights(self.org, self.ws, self.person)), 1)
        self.assertEqual(self.performance.decide_insight(self.org, self.ws, self.person, iid, "approved")["status"], "approved")

    def _revenues(self):
        for i, amount in enumerate((100, 120, 150)):
            self.conn.execute("INSERT INTO revenues VALUES (?,?,?,?,?,?,?,?,?)", (f"r{i}", self.org, self.ws, None, amount, "USD", "subscription", f"2026-0{i+5}-01", "x"))

    def test_revenue_forecast(self):
        self._revenues(); rows = self.forecast.generate_forecasts(self.org, self.person, "revenue")
        self.assertEqual(len(rows), 3); self.assertEqual(rows[0]["forecast_type"], "revenue")

    def test_list_forecasts(self):
        self._revenues(); self.forecast.generate_forecasts(self.org, self.person, "revenue")
        self.assertEqual(len(self.forecast.list_forecasts(self.org, self.person, "revenue")), 3)

    def test_create_and_list_policy(self):
        self.retention.create_policy(self.org, self.person, "workspace", "feedback", 30, "delete", self.ws)
        self.assertEqual(len(self.retention.list_policies(self.org, self.person, "workspace")), 1)

    def test_execute_deletion_scoped(self):
        self.conn.execute("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("c1", self.org, self.ws, "N", "O", "x", 1, "USD", None, None, "active", None, "t", "t"))
        out = self.retention.execute_deletion(self.org, self.person, "campaigns", ["c1"], "expired")
        self.assertEqual(out["count"], 1); self.assertIsNone(self.conn.execute("SELECT * FROM campaigns WHERE id='c1'").fetchone()); self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deletion_audit").fetchone()[0], 1)

    def test_execute_deletion_rejects_invalid_table(self):
        with self.assertRaises(ValidationError): self.retention.execute_deletion(self.org, self.person, "organizations", ["x"], "x")

    def test_export_workspace(self):
        self.conn.execute("INSERT INTO projects VALUES (?,?,?,?)", ("p", self.org, self.ws, "Project")); self.conn.commit()
        data = self.retention.export_workspace(self.org, self.ws, self.person)
        self.assertIn("projects", data); self.assertEqual(data["_meta"]["workspace_id"], self.ws)


if __name__ == "__main__": unittest.main()
