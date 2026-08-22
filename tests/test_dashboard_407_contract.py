from __future__ import annotations

import unittest
from pathlib import Path

from tests.dashboard_bundle import read_dashboard_bundle


class Dashboard407ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle = read_dashboard_bundle(Path(__file__).parents[1])

    def test_top_ask_routes_to_structured_intelligence(self) -> None:
        self.assertIn("cosmoCommandForm", self.bundle)
        self.assertIn("loadIntelligence(query)", self.bundle)
        self.assertNotIn("cosmoCommandForm)if(cosmoCommandForm)cosmoCommandForm.onsubmit=event=>{event.preventDefault();const query=$(\'command\').value.trim();if(!query)return;$('ask-modal')", self.bundle)

    def test_navigation_and_overview_controls_are_present(self) -> None:
        for marker in ("NAV_GROUPS_PARITY407", "'Signals'", "'Performance'", "Current canonical snapshot", "overview-attention", "overview-export", "auremgrid-portfolio.csv", "Over scope", "Healthy"):
            self.assertIn(marker, self.bundle)
        self.assertNotIn("overview-period", self.bundle)
        self.assertIn("uniqueAttention407", self.bundle)
        self.assertIn("uniqueAttention(", self.bundle)
        self.assertIn("Build first snapshot", self.bundle)

    def test_zero_build_shadcn_density_and_scroll_area_contract(self) -> None:
        for marker in (
            "04-shadcn-system.css",
            "data-dashboard-module=\"shadcn-system\"",
            "--control-h:36px",
            "--scrollbar-size:5px",
            "scrollbar-color:transparent transparent",
            "::-webkit-scrollbar-thumb",
            "focus-visible",
            'font-family:"Gellix",sans-serif',
            "@media(prefers-reduced-motion:reduce)",
        ):
            self.assertIn(marker, self.bundle)

    def test_portfolio_and_brain_provenance_contracts_are_rendered(self) -> None:
        for marker in ("Owner", "Scope", "Revenue", "Evidence span", "Validity", "Observed", "Affected decisions", "Related entities", "History"):
            self.assertIn(marker, self.bundle)

    def test_review_and_people_metadata_use_canonical_detail_routes(self) -> None:
        for marker in ("Creator:", "Reviewer:", "revisions", "/people/detail", "Clients", "Open work", "Review"):
            self.assertIn(marker, self.bundle)

    def test_client_hq_and_work_inspector_surface_operating_context(self) -> None:
        for marker in ("'Insights'", "Definition of Done", "Dependencies", "Brain context", "Versions", "Reviews", "Reviewer:", "Client:"):
            self.assertIn(marker, self.bundle)

    def test_auth_onboarding_explains_and_validates_token(self) -> None:
        for marker in ("temporary access token", "bootstrap-auth", "Token rejected", "Sign out / forget token", "auremgrid_session"):
            self.assertIn(marker, self.bundle)

    def test_scenario_form_exposes_agency_capacity_inputs_without_execution(self) -> None:
        for marker in ("additional_clients", "hours_per_new_client", "leave_hours_delta", "hiring_hours_delta", "client_action", "client_revenue_delta", "client_cost_delta", "client_hours_delta", "no changes made"):
            self.assertIn(marker, self.bundle)
        self.assertIn("what_if_${key}", self.bundle)

    def test_second_parity_controls_are_backend_wired(self) -> None:
        for marker in ("/work/comments", "/work/items/update", "/work/items/transition", "data-work-comment", "data-work-update", "data-work-transition", "Only server-granted transitions", "expected_version", "idempotency_key", "/reports/generate", "data-report-generate", "reportType", "/automations/activate", "/automations/trigger", "data-automation-activate", "data-automation-trigger", "training_state", "/integrations/verify", "/integrations/sync", "Not connected — add and verify credentials", "/dashboard/intelligence/attention", "/dashboard/intelligence/refresh", "Persisted attention", "worker is processing", "Agency performance KPIs", "Attributed revenue", "ROAS", "CTR"):
            self.assertIn(marker, self.bundle)

    def test_new_work_uses_canonical_work_items_route(self) -> None:
        self.assertIn("captureForm", self.bundle)
        self.assertIn("api('/work/items'", self.bundle)
        self.assertIn("organization_id:org", self.bundle)
        self.assertIn("person_id:person", self.bundle)
        self.assertIn("deadline:fields.get('needed_by')||null", self.bundle)
        final_capture = self.bundle[self.bundle.rfind("const captureForm="):self.bundle.rfind("const agencyBootstrap=")]
        self.assertNotIn("/work/capture", final_capture)


if __name__ == "__main__":
    unittest.main()
