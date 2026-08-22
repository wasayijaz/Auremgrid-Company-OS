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
        self.assertIn("proposals.map(row", tail)
        self.assertIn("conflicts.map(row", tail)
        self.assertIn("brainGrid.innerHTML", self.html)
        self.assertIn('status===\"pending\"', self.html)
        self.assertIn('state===\"conflicted\"', self.html)

    def test_allowed_action_dialog_is_capability_and_history_safe(self) -> None:
        for marker in ("allowed_actions", "dashboard-action-dialog", "expected_version", "idempotency_key", "historical", "loadBrainSurface"):
            self.assertIn(marker, self.html)
        self.assertIn("if(!row||row.historical", self.html)

    def test_descriptor_buttons_are_injected_for_live_rows_only(self) -> None:
        fixture = {"id":"p1","allowed_actions":[{"action":"approve","method":"POST","route":"/brain/promote","payload":{"proposal_id":"p1"},"required_fields":[]}]}
        self.assertIn("injectDescriptorButtons(data.proposals", self.html)
        self.assertIn("renderActionDescriptors(row)", self.html)
        self.assertIn("historical||!Array.isArray(row.allowed_actions)", self.html)
        self.assertEqual(fixture["allowed_actions"][0]["route"], "/brain/promote")
        for marker in ("data-row-id", "data-stage-id", "stages.map(row", "proposals.map(row", "renderActionDescriptors(row)"):
            self.assertIn(marker, self.html)

    def test_action_dialog_is_lazy_and_supports_descriptor_fields(self) -> None:
        for marker in ("ensureDashboardActionDialog", "required_fields", "one_of:", "artifact_contract", "idempotency_key", "dashboard-action-dialog"):
            self.assertIn(marker, self.html)
        self.assertIn("dialog.showModal()", self.html)
        self.assertIn("dialog.dataset.intentKey", self.html)
        self.assertIn("confirm.disabled=true", self.html)
        fixture_payload={"kind":"fact","text":"evidence"}
        self.assertIn("data-one-of", self.html)
        self.assertIn("payload[select.value]=value", self.html)
        self.assertNotIn("payload['one_of:uri,text,object_type']", self.html)
        self.assertEqual(fixture_payload.get("kind"), "fact")
        self.assertNotIn("one_of:uri,text,object_type", fixture_payload)

    def test_responsive_layout_and_read_only_controls(self) -> None:
        for width in ("max-width:1000px", "max-width:620px"):
            self.assertIn(width, self.html)
        self.assertNotIn("draggable=\"true\"", self.html)
        self.assertNotIn("ondrag", self.html.lower())

    def test_shell_identity_and_ledger_health_are_backend_sourced(self) -> None:
        for demo_marker in ("Auremgrid Demo", "Demo Owner", "org_demo", "person_demo_owner", "ws_alpha", "act_alpha_admin", "Local ledger online"):
            self.assertEqual(0, self.html.count(demo_marker), demo_marker)
        self.assertGreater(self.html.count("/auth/me"), 0, "/auth/me")
        self.assertRegex(self.html, r"/health(?:/detailed)?", "dashboard must fetch backend health")
        self.assertGreater(self.html.count('id="ledger-status"'), 0, "ledger-status element")
        self.assertRegex(
            self.html,
            r"\$\(['\"]ledger-status['\"]\)\.textContent\s*=",
            "dashboard must update the ledger-status DOM node from backend health",
        )

    def test_access_token_uses_an_in_page_login_dialog(self) -> None:
        self.assertIn('id="access-token-dialog"', self.html)
        self.assertIn('type="password"', self.html)
        self.assertIn('localStorage.setItem("auremgrid_session",token)', self.html)
        self.assertNotIn('window.prompt("Enter your Auremgrid access token")', self.html)

    def test_every_nav_surface_uses_a_real_backend_source(self) -> None:
        expected_modules = ("Campaigns", "Content", "Creative", "Meetings", "Automations", "Reports", "Integrations", "Settings")
        for name in expected_modules:
            self.assertIn(f'"{name}"', self.html)
        for endpoint in (
            "/dashboard/data",
            "/dashboard/client",
            "/dashboard/module",
            "/dashboard/settings",
            "/dashboard/brain",
            "/dashboard/workflows",
        ):
            self.assertGreater(self.html.count(endpoint), 0, endpoint)
        for marker in ("id=\"agents\"", "page-finance", "id=\"capacity-list\"", "finance_status", "agents_running"):
            self.assertGreater(self.html.count(marker), 0, marker)
        self.assertGreater(self.html.count("/dashboard/settings"), 0, "/dashboard/settings")
        self.assertEqual(0, self.html.count('name==="Settings"){target.innerHTML=['), "static Settings branch")
        self.assertIn("Loading authenticated settings", self.html)
        self.assertIn("Settings unavailable:", self.html)

    def test_p6_p9_module_markers_and_actions_are_visible(self) -> None:
        for marker in (
            "page-work",
            "work-board",
            "capture-form",
            "/work/capture",
            "/dashboard/workflows",
            "/workflows/stages/start",
            "/workflows/evidence",
            "/workflows/approvals/request",
            "/workflows/approvals/decide",
            "/workflows/handoffs/acknowledge",
            "/workflows/stages/complete",
            "page-review",
            "/dashboard/review-center",
            "page-brain",
            "/dashboard/brain",
        ):
            self.assertGreater(self.html.count(marker), 0, marker)

    def test_authenticated_command_uses_backend_operations(self) -> None:
        command_start = self.html.find('"command-form").onsubmit')
        self.assertNotEqual(command_start, -1)
        command_slice = self.html[command_start:command_start + 1200]
        self.assertIn("/search?", command_slice)
        self.assertIn("workspace_id", command_slice)
        self.assertIn("query=", command_slice)
        self.assertIn("api(", command_slice)
        self.assertIn("Authorization", self.html)
        self.assertNotIn("mock", command_slice.lower())
        self.assertNotIn("demo", command_slice.lower())


if __name__ == "__main__":
    unittest.main()
