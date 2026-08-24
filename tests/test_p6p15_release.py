from __future__ import annotations

import unittest
from pathlib import Path

from tests.dashboard_bundle import read_dashboard_bundle


ROOT = Path(__file__).parents[1]


class P6P15ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = ROOT.joinpath("docs", "release-verification.md").read_text(encoding="utf-8")
        self.http = ROOT.joinpath("src", "auremgrid", "api", "http.py").read_text(encoding="utf-8")
        self.dashboard = read_dashboard_bundle(ROOT)
        self.readme = ROOT.joinpath("README.md").read_text(encoding="utf-8")
        self.preview = ROOT.joinpath("docs", "assets", "dashboard-showcase.svg").read_text(encoding="utf-8")

    def test_release_matrix_contains_rows_6_through_15(self) -> None:
        for requirement in range(6, 16):
            self.assertIn(f"| {requirement}.", self.doc)

    def test_p6_p15_release_rows_have_matching_http_evidence(self) -> None:
        routes_by_row = {
            6: ("/work/items", "/work/items/update", "/work/dependencies", "/work/time"),
            7: ("/reviews/comment", "/approvals/decide", "/dashboard/review-center"),
            8: ("/meetings/responsibilities", "/signals"),
            9: ("/decisions", "/memory-proposals/review"),
            10: ("/dashboard/client",),
            11: ("/risks", "/opportunities", "/signals"),
            12: ("/finance",),
            13: ("/finance",),
            14: ("/campaigns", "/creative", "/content"),
            15: ("/agents", "/agents/runs", "/agents/tasks"),
        }
        for row, routes in routes_by_row.items():
            self.assertIn(f"| {row}.", self.doc)
            for route in routes:
                self.assertIn(route, self.http)

    def test_p10_p15_document_the_completed_operational_contracts(self) -> None:
        doc = self.doc.lower()
        for phrase in (
            "pure `explain_health` read model",
            "create/resolve/reopen risk",
            "explicit no-data/recorded/over-scope states",
            "client contribution/margin calculation",
            "reviewer-gated approval/revision flow",
            "workspace-fenced tool calls",
        ):
            self.assertIn(phrase, doc)

    def test_release_doc_does_not_claim_unsupported_full_surfaces(self) -> None:
        unsupported_claims = (
            "Client health is complete",
            "Risks and opportunities are complete",
            "Finance is complete",
            "Campaigns and creatives are complete",
            "Agents are fully autonomous",
        )
        for phrase in unsupported_claims:
            self.assertNotIn(phrase, self.doc)

    def test_release_schema_documentation_matches_current_migration(self) -> None:
        upgrade = ROOT.joinpath("docs", "upgrade-guide.md").read_text(encoding="utf-8")
        checklist = ROOT.joinpath("docs", "production-checklist.md").read_text(encoding="utf-8")
        migrations = ROOT.joinpath("src", "auremgrid", "storage", "migrations.py").read_text(encoding="utf-8")
        for document in (self.doc, upgrade, checklist):
            self.assertIn("56", document)
            self.assertNotIn("schema 54", document)
        self.assertIn('"durable_automation_actions_and_delegation_depth"', migrations)

    def test_feedback_performance_forecast_retention_batch_is_separate_from_p6_p15_matrix(self) -> None:
        for route in (
            "/feedback/record",
            "/feedback/patterns",
            "/feedback/patterns/promote",
            "/feedback/patterns/decide",
            "/insights/performance",
            "/insights/performance/generate",
            "/insights/performance/decide",
            "/forecasts",
            "/forecasts/generate",
            "/retention/policies",
            "/retention/execute",
        ):
            self.assertIn(route, self.http)
        for service_marker in ("feedback_patterns", "performance_insights", "forecasts", "retention_policies"):
            self.assertIn(service_marker, self.doc + self.http)

    def test_dashboard_release_blockers_are_guarded(self) -> None:
        for fake_marker in ("Auremgrid Demo", "Demo Owner", "Local ledger online"):
            self.assertEqual(0, self.dashboard.count(fake_marker), fake_marker)
        self.assertRegex(self.dashboard, r"/health(?:/detailed)?", "dashboard must fetch backend health")
        self.assertGreater(self.dashboard.count("/auth/me"), 0, "/auth/me")
        self.assertGreater(self.dashboard.count("/dashboard/settings"), 0, "/dashboard/settings")
        self.assertEqual(0, self.dashboard.count('name==="Settings"){target.innerHTML=['), "static Settings branch")

    def test_github_dashboard_showcase_is_sample_labeled_and_reproducible(self) -> None:
        self.assertIn("docs/assets/dashboard-showcase.svg", self.readme)
        self.assertIn("python scripts/auremgrid.py serve --host 127.0.0.1 --port 8791", self.readme)
        self.assertIn("SAMPLE DATA", self.readme)
        self.assertIn("SAMPLE DATA", self.preview)
        self.assertIn("sample.invalid", self.preview)
        for retired_marker in ("org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin", "Auremgrid Demo", "Demo Owner"):
            self.assertNotIn(retired_marker, self.preview)
        self.assertIn("python scripts/dashboard_showcase_svg.py", self.doc)


if __name__ == "__main__":
    unittest.main()
