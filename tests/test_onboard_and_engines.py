from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from auremgrid.services.brain import CompanyOS


class OnboardAndEngineTests(unittest.TestCase):
    def test_any_agency_can_onboard_without_demo_names(self) -> None:
        os = CompanyOS(":memory:")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "brand.md"
            source.write_text(
                "META: valid_from=2026-01-01T00:00:00+00:00\nFACT: Northwind Studio | visual_rule | charcoal only\n",
                encoding="utf-8",
            )
            result = os.onboard_agency(
                "Northwind Studio",
                "ws_northwind",
                "Northwind Admin",
                source_dir=tmp,
            )
        self.assertEqual(result["workspace"]["id"], "ws_northwind")
        self.assertEqual(result["ingested_sources"], 1)
        self.assertGreaterEqual(len(result["engines"]), 8)
        names = {engine for engine in result["engines"]}
        self.assertTrue({"graphiti", "cognee", "mem0", "onyx", "ragflow", "lightrag", "graphrag", "letta"} <= names)
        brief = os.account_brief("ws_northwind", result["operator"]["id"], query="charcoal")
        self.assertFalse(brief.evidence["unknown"])
        os.close()

    def test_engines_stay_workspace_scoped(self) -> None:
        os = CompanyOS(":memory:")
        os.onboard_agency("Studio A", "ws_a", "Admin A")
        os.onboard_agency("Studio B", "ws_b", "Admin B")
        os.ingest_text("ws_a", "act_ws_a_admin", "a.md", "FACT: Studio A | offer | intro week", "memory://a")
        leaked = os.search("ws_b", "act_ws_b_admin", "intro week")
        self.assertTrue(leaked.unknown)
        status = os.engine_status("ws_b", "act_ws_b_admin", "intro week")
        graphiti_hits = next(item for item in status["engines"] if item["name"] == "graphiti")["hits"]
        self.assertEqual(graphiti_hits, [])
        os.close()


if __name__ == "__main__":
    unittest.main()
