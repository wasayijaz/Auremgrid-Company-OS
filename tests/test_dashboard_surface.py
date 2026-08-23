from __future__ import annotations

import unittest
from pathlib import Path


class DashboardSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parents[1].joinpath("src", "auremgrid", "api")
        dashboard = self.root.joinpath("dashboard")
        assets = [dashboard.joinpath("index.html"), *sorted(dashboard.joinpath("css").glob("*.css")),
                  *sorted(dashboard.joinpath("js").glob("*.js")), dashboard.joinpath("dashboard.css"), dashboard.joinpath("dashboard.js")]
        self.html = "\n".join(path.read_text(encoding="utf-8") for path in assets)

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
        self.assertIn("No backend route is available", self.html)
        self.assertIn("!descriptor.route", self.html)
        for marker in ("data-row-id", "data-stage-id", "stages.map(row", "proposals.map(row", "renderActionDescriptors(row)"):
            self.assertIn(marker, self.html)

    def test_scenario_descriptor_buttons_require_backend_routes(self) -> None:
        self.assertIn("actions=s.action_descriptors||s.allowed_actions||[]", self.html)
        self.assertIn("a&&a.route", self.html)
        self.assertIn("data-scenario-action", self.html)
        self.assertIn("No backend route is available", self.html)

    def test_brain_surface_names_all_canonical_collections_and_empty_states(self) -> None:
        for marker in (
            "current_truth", "Decisions", "Preferences", "Entities", "Conflicts",
            "Proposed", "Sources", "History", "data.collections", "emptyLabel.toLowerCase()",
        ):
            self.assertIn(marker, self.html)
        self.assertIn('"collections"', Path(__file__).parents[1].joinpath("src", "auremgrid", "services", "dashboard.py").read_text(encoding="utf-8"))

    def test_cosmo_relationship_label_is_visible_without_renaming_operator(self) -> None:
        self.assertIn("Auremgrid is the product/OS. Cosmo Intelligence is its named operating assistant.", self.html)
        self.assertIn('aria-label="Cosmo Intelligence"', self.html)
        self.assertIn("Ask Cosmo", self.html)
        self.assertIn('result["cosmo"]["name"]', Path(__file__).parents[1].joinpath("tests", "test_dashboard_service.py").read_text(encoding="utf-8"))

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
            "/work/items",
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

    def test_cosmo_queue_is_backend_sourced_and_navigates_to_canonical_surfaces(self) -> None:
        for marker in (
            "Company command", "Intelligence queue", "state.cosmo", "data-cosmo-surface",
            "Ask Cosmo", "writes_require_canonical_routes",
        ):
            self.assertIn(marker, self.html if marker != "writes_require_canonical_routes" else Path(__file__).parents[1].joinpath("src", "auremgrid", "services", "dashboard.py").read_text(encoding="utf-8"))
        self.assertIn("button.onclick=()=>show(button.dataset.cosmoSurface)", self.html)

    def test_every_dashboard_backend_reference_has_a_real_http_handler(self) -> None:
        http = Path(__file__).parents[1].joinpath("src", "auremgrid", "api", "http.py").read_text(encoding="utf-8")
        paths = (
            "/auth/me", "/health/detailed", "/dashboard/data", "/dashboard/client",
            "/dashboard/module", "/dashboard/settings", "/dashboard/review-center",
            "/dashboard/brain", "/dashboard/workflows", "/dashboard/intelligence",
            "/dashboard/intelligence/executive", "/people", "/work/detail",
            "/work/items", "/work/items/transition", "/search", "/entity/candidates", "/tools/call", "/finance",
            "/campaigns", "/content", "/creative",
            "/brain/promote", "/brain/conflicts/resolve", "/workflows/stages/start",
            "/workflows/evidence", "/workflows/approvals/request",
            "/workflows/approvals/decide", "/workflows/handoffs/acknowledge",
            "/workflows/stages/complete", "/insights/performance/generate",
            "/forecasts/generate",
        )
        for path in paths:
            self.assertIn(path, self.html, f"dashboard does not reference {path}")
            self.assertIn(path, http, f"backend does not implement {path}")

    def test_finance_page_is_backend_rendered_and_honest_when_disconnected(self) -> None:
        for marker in ("id=\"finance-body\"", "renderFinanceSurface", "/finance?", "recognized_revenue", "outstanding_revenue"):
            self.assertIn(marker, self.html)
        self.assertNotIn('<div class="card"><span class="state off">Not connected</span>', self.html)
        for marker in ("data-finance-connect", "FINANCE_ACTIONS407", "Record revenue", "Record invoice", "Record cost", "Set budget", "Record software cost", "Record AI usage", "Calculate client economics", "/finance/connect", "/finance/revenue", "/finance/invoices", "/finance/costs", "/finance/budgets", "/finance/software-costs", "/finance/ai-usage-costs", "/finance/economics/calculate"):
            self.assertIn(marker, self.html)

    def test_scope_surface_exposes_contract_allowance_usage_and_history_actions(self) -> None:
        for marker in ("data-scope-contract", "data-scope-allowance", "data-scope-usage", "/contracts", "/scope/allowances", "/scope/usage", "period_history", "generated"):
            self.assertIn(marker, self.html)
        http = self.root.joinpath("http.py").read_text(encoding="utf-8")
        for path in ("/contracts", "/scope/allowances", "/scope/usage", "/finance/connect", "/finance/revenue", "/finance/invoices"):
            self.assertIn(path, http)

    def test_dashboard_mutations_guard_double_submit_and_recover_on_failure(self) -> None:
        for marker in (
            "scopePost=async(button,path,payload)",
            "catch(error){button.disabled=false;toast(`Scope unavailable",
            "if(submit.disabled)return;submit.disabled=true",
            "catch(error){submit.disabled=false;toast(`Project unavailable",
            "if(add.disabled)return;add.disabled=true",
            "catch(error){add.disabled=false;toast(`Deliverable unavailable",
            "catch(error){submit.disabled=false;toast(`Finance unavailable",
            "catch(error){button.disabled=false;toast(`Finance unavailable",
            "if(confirm.disabled)return;confirm.disabled=true",
            "catch(error){confirm.disabled=false;toast(error.message)",
        ):
            self.assertIn(marker, self.html)

    def test_marketing_surfaces_have_permission_aware_backend_create_flows(self) -> None:
        for marker in ("marketingFlows", "canOperateWorkspace", "renderMarketingModule", "openMarketingCreate"):
            self.assertIn(marker, self.html)
        for route in ("route:'/campaigns'", "route:'/content'", "route:'/creative'"):
            self.assertIn(route, self.html)
        self.assertIn("['admin','operator']", self.html)

    def test_intelligence_is_a_permanent_contextual_backend_surface(self) -> None:
        for marker in (
            'class="intelligence-rail"', 'id="intelligence-findings"',
            "loadIntelligence", "renderIntelligence", "/dashboard/intelligence?",
            "intelligenceConfidence", "intelligenceEvidence", "data-intelligence-surface",
        ):
            self.assertIn(marker, self.html)
        self.assertIn("grid-template-columns:248px minmax(0,1fr) 372px", self.html)
        self.assertIn("There is not enough permitted evidence", self.html)

    def test_every_visible_static_dashboard_control_is_wired(self) -> None:
        handlers = {
            "command-form": '$("command-form").onsubmit',
            "capture": '$("capture").onclick',
            "cancel-capture": '$("cancel-capture").onclick',
            "capture-form": '$("capture-form").onsubmit',
            "board-view": '$("board-view").onclick',
            "list-view": '$("list-view").onclick',
            "clear-work-filters": '$("clear-work-filters").onclick',
            "close-work-detail": '$("close-work-detail").onclick',
            "intelligence-toggle": "$('intelligence-toggle').onclick",
            "intelligence-close": "$('intelligence-close').onclick",
            "intelligence-form": "$('intelligence-form').onsubmit",
        }
        for element_id, handler in handlers.items():
            self.assertIn(f'id="{element_id}"', self.html)
            self.assertIn(handler, self.html, f"visible control {element_id} has no handler")

    def test_work_board_list_toggle_preserves_filters_and_backend_detail_contract(self) -> None:
        for marker in (
            "function setWorkView(view)",
            '$("board-view").onclick=()=>setWorkView("board")',
            '$("list-view").onclick=()=>setWorkView("list")',
            "filteredWork()",
            'data-work-list-table',
            'work-list-table',
            'data-work-row="list"',
            'data-work-state',
            'Fetching /dashboard/client for this workspace.',
            'Fetching /work/detail for the selected backend work item.',
            'data-backend-route="/work/detail"',
            'const data=await api(`/work/detail?',
            "if(!item)throw new Error('The backend response did not include work_item.')",
            "allowed_transitions",
            "data-work-transition",
            "/work/items/transition",
            "expected_version",
            "idempotency_key",
            "await loadWorkBoard()",
            "Only server-granted transitions returned by /work/detail are shown",
            "deadline:fields.get('needed_by')||null",
        ):
            self.assertIn(marker, self.html)
        self.assertNotIn("localWorkDetail", self.html)
        self.assertNotIn("canonical board record remains usable", self.html)

    def test_407_shell_completion_surface_markers_are_present(self) -> None:
        for marker in (
            "dashboard-407-surface",
            "nav-section",
            "nav-toggle",
            "data-overview-action-cards",
            "overview-action",
            "client-portfolio-table",
            "people-capacity-table",
            "agents-ops-grid",
            "agent-worker-card",
            "campaign-evidence",
            "side-inspector",
            "work-side-inspector",
            "ensureWorkSideInspector",
            "executive-brief",
            "loadExecutiveBrief407",
            "intelligenceDetail407",
        ):
            self.assertIn(marker, self.html)

    def test_407_honest_states_and_backend_sources_are_explicit(self) -> None:
        for marker in (
            "honestState",
            "No connected or permitted finance source",
            "Unknown values stay unknown",
            "No revenue or margin numbers are shown",
            "Fetching /dashboard/client",
            "Operational worker from /dashboard/data",
            "Records above are loaded through /dashboard/module",
            "Sourced capacity",
            "permission",
            "disconnected",
            "degraded",
        ):
            self.assertIn(marker, self.html)

    def test_407_rich_surfaces_remain_backend_wired(self) -> None:
        for marker in (
            "renderOverview407",
            "renderClientTab407",
            "renderPeople407",
            "renderAgents407",
            "renderMarketingModule=async function",
            "renderIntelligence=function",
            "data-intelligence-object",
            "data.uncertainty?[data.uncertainty]",
            "auremgrid:auth-required",
            "localStorage.removeItem(\"auremgrid_session\")",
            "data-project-id",
            "data-campaign-id",
            "data-person-id",
            "/people?organization_id=",
            "people.capacity",
            "/dashboard/client?organization_id=",
            "/dashboard/module?organization_id=",
            "/dashboard/intelligence/executive?organization_id=",
            "data-overview-surface",
            "data-cosmo-surface",
        ):
            self.assertIn(marker, self.html)

    def test_dashboard_live_shell_is_modular_static_assets(self) -> None:
        shell = self.root.joinpath("dashboard", "index.html").read_text(encoding="utf-8")
        self.assertIn('/dashboard-assets/css/03-dashboard-v2.css', shell)
        self.assertIn('/dashboard-assets/js/04-agency-dashboard-contract.js', shell)
        self.assertEqual(0, shell.count("<style"))
        self.assertEqual(0, shell.count("<script>"))
        css = self.root.joinpath("dashboard", "css", "03-dashboard-v2.css").read_text(encoding="utf-8")
        js = self.root.joinpath("dashboard", "js", "04-agency-dashboard-contract.js").read_text(encoding="utf-8")
        for marker in ("agency-map", "trend-strip"):
            self.assertIn(marker, css)
        for marker in (
            "renderAgencyMap407",
            "renderTrendStrip407",
            "scopeLabel407",
            "revenueLabel407",
            "Sol / Terra / Luna deliberation",
            "Decision → workflow → outcome → learning",
        ):
            self.assertIn(marker, js)

    def test_work_view_visibility_helpers_cannot_be_overridden(self) -> None:
        css = self.root.joinpath("dashboard", "css", "04-shadcn-system.css").read_text(encoding="utf-8")
        self.assertIn("[hidden]{display:none!important}", css)
        self.assertIn(".sr-only{position:absolute!important", css)

    def test_completion_style_is_inserted_beside_its_actual_parent(self) -> None:
        self.assertIn("systemStyles.parentNode.insertBefore(css,systemStyles)", self.html)
        self.assertNotIn("document.head.insertBefore(css,systemStyles)", self.html)


if __name__ == "__main__":
    unittest.main()
