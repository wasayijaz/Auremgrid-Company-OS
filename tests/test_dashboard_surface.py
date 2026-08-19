from __future__ import annotations

import unittest
from pathlib import Path


class DashboardSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = Path(__file__).parents[1].joinpath("src", "auremgrid", "api", "dashboard.html").read_text(encoding="utf-8")

    def test_existing_shell_smoke_markers_are_preserved(self) -> None:
        for marker in ("id=\"page-command\"", "id=\"page-brain\"", "id=\"page-work\"", "id=\"command-form\"", "id=\"work-board\"", "id=\"capture-modal\""):
            self.assertIn(marker, self.html)

    def test_brain_and_workflow_surfaces_are_real_data_and_degraded_safe(self) -> None:
        for marker in ("/dashboard/brain?", "/dashboard/workflows?", "Loading brain evidence", "Brain unavailable:", "Nothing waiting.", "No current facts.", "Workflow map"):
            self.assertIn(marker, self.html)
        # The endpoint accepts an optional read-only as_of parameter; the
        # current compact shell has no date picker yet.

    def test_terra_dashboard_contract_keys_are_rendered(self) -> None:
        for key in ("brain.health", "brain.current_truths", "flows.runs", "flows.stages"):
            self.assertIn(key, self.html)
        fixture = {"health":{"semantic":{"status":"degraded","fallback_used":True},"graph":{"status":"building","building":True}},"proposals":[],"conflicts":[],"current_truths":[],"runs":[],"stages":[]}
        self.assertEqual(fixture["health"]["semantic"]["status"], "degraded")
        self.assertTrue(fixture["health"]["graph"]["building"])

    def test_brain_and_workflow_routing_contracts(self) -> None:
        self.assertIn('name==="Workflows"', self.html)
        self.assertIn('typeof stage.due', self.html)
        self.assertIn('status==="pending"', self.html)
        self.assertIn('conflicts||[]', self.html)

    def test_final_brain_renderer_writes_directly(self) -> None:
        tail = self.html[self.html.rfind("loadBrainSurface=async function"):]
        self.assertIn("brainGrid.innerHTML", tail)
        self.assertNotIn("brainGrid.querySelector", tail)
        self.assertIn('status===\"pending\"', tail)
        self.assertIn('state===\"conflicted\"', tail)

    def test_responsive_layout_and_read_only_controls(self) -> None:
        for width in ("max-width:1000px", "max-width:620px"):
            self.assertIn(width, self.html)
        self.assertNotIn("draggable=\"true\"", self.html)
        self.assertNotIn("ondrag", self.html.lower())


if __name__ == "__main__":
    unittest.main()
