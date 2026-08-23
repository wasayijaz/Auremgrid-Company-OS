from __future__ import annotations

import unittest
from pathlib import Path

from auremgrid.services.brain import CompanyOS


class OnboardAndEngineTests(unittest.TestCase):
    def test_demo_seed_is_repeatable_without_duplicate_work_or_touchpoints(self) -> None:
        os = CompanyOS(":memory:")
        fixtures = Path(__file__).resolve().parents[1] / "fixtures"
        os.seed_demo(fixtures)
        counts_before = (
            os.store.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
            os.store.conn.execute("SELECT COUNT(*) FROM touchpoints").fetchone()[0],
        )
        os.seed_demo(fixtures)
        counts_after = (
            os.store.conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
            os.store.conn.execute("SELECT COUNT(*) FROM touchpoints").fetchone()[0],
        )
        os.close()
        self.assertEqual(counts_before, counts_after)

    def test_any_agency_can_onboard_without_demo_names(self) -> None:
        os = CompanyOS(":memory:")
        result = os.onboard_agency(
            "Northwind Studio",
            "ws_northwind",
            "Northwind Admin",
        )
        self.assertEqual(result["workspace"]["id"], "ws_northwind")
        self.assertEqual(result["ingested_sources"], 0)
        self.assertIn("client_workspaces", result["import_templates"]["templates"])
        self.assertGreaterEqual(len(result["engines"]), 8)
        names = {engine for engine in result["engines"]}
        self.assertTrue({
            "local_graphiti_style_projection", "local_cognee_style_projection", "local_mem0_style_projection",
            "local_onyx_style_projection", "local_ragflow_style_projection", "local_lightrag_style_projection",
            "local_graphrag_style_projection", "local_letta_style_projection",
        } <= names)
        brief = os.account_brief("ws_northwind", result["operator"]["id"], query="charcoal")
        self.assertTrue(brief.evidence["unknown"])
        os.close()

    def test_engines_stay_workspace_scoped(self) -> None:
        os = CompanyOS(":memory:")
        os.onboard_agency("Studio A", "ws_a", "Admin A")
        os.onboard_agency("Studio B", "ws_b", "Admin B")
        os.ingest_text("ws_a", "act_ws_a_admin", "a.md", "FACT: Studio A | offer | intro week", "memory://a")
        leaked = os.search("ws_b", "act_ws_b_admin", "intro week")
        self.assertTrue(leaked.unknown)
        status = os.engine_status("ws_b", "act_ws_b_admin", "intro week")
        graphiti_hits = next(item for item in status["engines"] if item["name"] == "local_graphiti_style_projection")["hits"]
        self.assertEqual(graphiti_hits, [])
        os.close()


if __name__ == "__main__":
    unittest.main()
