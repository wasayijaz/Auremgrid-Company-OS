from __future__ import annotations

import re
import unittest

try:
    import pytest
except ImportError:  # Keep `python -m unittest discover` usable without extras.
    class _Mark:
        def __getattr__(self, _name):
            return lambda function: function

        def parametrize(self, *_args, **_kwargs):
            return lambda function: function

    class _PytestStub:
        mark = _Mark()

    pytest = _PytestStub()  # type: ignore[assignment]

try:
    from playwright.sync_api import Page, expect
    _PLAYWRIGHT_IMPORTABLE = True
except ImportError:  # pragma: no cover - only used by dependency-free unittest discovery.
    _PLAYWRIGHT_IMPORTABLE = False
    Page = object  # type: ignore[assignment,misc]

    def expect(*_args, **_kwargs):
        raise RuntimeError("Playwright is not installed; install with `pip install -e '.[browser]'`.")

from .conftest import DashboardFixture, assert_no_browser_errors, open_dashboard


pytestmark = pytest.mark.browser


if not hasattr(pytest, "fixture") or not _PLAYWRIGHT_IMPORTABLE:
    class BrowserDependencyNotice(unittest.TestCase):
        @unittest.skip("Browser tests require Playwright and Chromium; run tools/run-dashboard-browser.ps1 after installing .[browser].")
        def test_playwright_dependency(self) -> None:
            pass


def wait_for_work(page: Page) -> None:
    page.locator(".nav button[data-name='Work']").click()
    page.locator("#work-summary").wait_for(state="visible", timeout=10_000)
    page.wait_for_function("!document.querySelector('#work-summary')?.textContent?.includes('Loading')", timeout=10_000)


def wait_for_command_data(page: Page) -> None:
    page.locator("#metrics .metric").first.wait_for(state="visible", timeout=10_000)
    page.locator("#space-list .space-button").first.wait_for(state="attached", timeout=10_000)


def test_unauthenticated_startup_requires_in_page_token_and_then_boots(browser, dashboard_app: DashboardFixture) -> None:
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    try:
        page.goto(f"{dashboard_app.base_url}/dashboard", wait_until="domcontentloaded")
        dialog = page.locator("#access-token-dialog")
        dialog.wait_for(state="visible", timeout=10_000)
        expect(dialog.locator("input[name=token]")).to_have_attribute("type", "password")
        dialog.locator("input[name=token]").fill(dashboard_app.owner_token)
        dialog.get_by_role("button", name="Connect").click()
        page.locator("#scope-organization").wait_for(state="visible", timeout=10_000)
        expect(page.locator("#scope-organization")).to_contain_text("Auremgrid")
        expect(page.locator("#ledger-status")).to_contain_text(re.compile("Ledger (healthy|degraded)"))
        assert_no_browser_errors(page)
    finally:
        context.close()


def test_authenticated_workspace_switching_scrollbars_and_command_tile_geometry(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    expect(page.locator("#access-token-dialog")).to_have_count(0)
    expect(page.locator(".space-button")).to_have_count(3)
    expect(page.locator("#metrics .metric")).to_have_count(8)
    gap = page.locator("#metrics").evaluate("el => parseFloat(getComputedStyle(el).gap)")
    assert gap >= 8
    boxes = page.locator("#metrics .metric").evaluate_all("els => els.map(e => { const r=e.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; })")
    for index, left in enumerate(boxes):
        for right in boxes[index + 1:]:
            x_overlap = min(left["x"] + left["w"], right["x"] + right["w"]) - max(left["x"], right["x"])
            y_overlap = min(left["y"] + left["h"], right["y"] + right["h"]) - max(left["y"], right["y"])
            assert x_overlap <= 0 or y_overlap <= 0

    for workspace_id in dashboard_app.workspaces:
        button = page.locator(f".space-button[data-space-id='{workspace_id}']")
        button.click()
        expect(button).to_have_class(re.compile("active"))
        page.locator("#work-summary").wait_for(state="visible", timeout=10_000)
        expect(page.locator("#work-summary")).to_contain_text("item")
        expect(page.locator("#intelligence-scope")).not_to_be_empty()

    wait_for_work(page)
    board = page.locator("#work-board")
    assert board.evaluate("el => el.scrollWidth > el.clientWidth")
    before = board.evaluate("el => el.scrollLeft")
    board.hover(position={"x": min(200, board.bounding_box()["width"] - 2), "y": 100})
    page.locator("#work-board [data-work-id]").first.focus()
    assert board.evaluate("el => el.matches(':focus-within')")
    board.evaluate("el => el.scrollLeft = Math.min(120, el.scrollWidth - el.clientWidth)")
    assert board.evaluate("el => el.scrollLeft") >= before
    scrollbar = board.evaluate("el => ({width:getComputedStyle(el).scrollbarWidth, color:getComputedStyle(el).scrollbarColor})")
    assert scrollbar["width"] in {"thin", "auto"}  # Chromium exposes thin; WebKit may expose auto.
    css = page.request.get(f"{dashboard_app.base_url}/dashboard-assets/css/04-shadcn-system.css").text()
    assert "scrollbar-color:var(--scrollbar-thumb)" in css.replace(" ", "")
    assert "focus-within" in css and "::-webkit-scrollbar-thumb" in css


def test_work_board_list_filters_detail_comment_and_status_lanes(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    wait_for_work(page)
    for lane in ("captured", "assigned", "in_progress", "review", "shipped"):
        expect(page.locator(f".work-lane[data-lane='{lane}']")).to_have_count(1)
    expect(page.locator(".work-lane[data-lane='captured'] [data-work-id]")).to_have_count(1)
    assert page.locator(".work-lane[data-lane='review'] [data-work-id]").count() >= 1
    expect(page.locator(".work-lane[data-lane='shipped'] [data-work-id]")).to_have_count(1)
    assert page.locator("#status-filter option").count() >= 4

    page.locator("#list-view").click()
    expect(page.locator("#work-table")).to_be_visible()
    expect(page.locator("[data-work-list-table]")).to_be_visible()
    initial_status = page.locator("#status-filter").input_value()
    page.locator("#status-filter").select_option("captured")
    expect(page.locator("[data-work-list-table] tbody tr")).to_have_count(1)
    page.locator("#work-search").fill("discovery")
    expect(page.locator("[data-work-list-table] tbody tr")).to_have_count(1)
    page.locator("#board-view").click()
    expect(page.locator("#status-filter")).to_have_value("captured")
    expect(page.locator("#work-search")).to_have_value("discovery")
    page.locator("#clear-work-filters").click()
    expect(page.locator("#status-filter")).to_have_value(initial_status)

    page.locator("[data-work-id]").first.click()
    inspector = page.locator("#work-side-inspector")
    expect(inspector).to_have_class(re.compile("open"))
    expect(page.locator("#inspector-title")).not_to_have_text("Work detail")
    expect(inspector.locator("#inspector-body")).to_contain_text("Status transitions")
    expect(inspector.locator("[data-work-transition='assigned']")).to_have_count(0)
    assignment = inspector.locator("[data-work-assign]")
    if assignment.count():
        assignee = inspector.locator("select[name=assignee_person_id]")
        assignee.select_option(index=1)
        with page.expect_response(
            lambda response: response.url.endswith("/work/items/assign")
            and response.request.method == "POST"
        ):
            assignment.click()
        expect(page.locator("#toast")).to_contain_text("Work assigned")
        expect(inspector).to_contain_text("Owner")
    transition = inspector.locator("[data-work-transition]").first
    expect(transition).to_be_visible()
    with page.expect_response(
        lambda response: response.url.endswith("/work/items/transition")
        and response.request.method == "POST"
    ):
        transition.click()
    expect(page.locator("#toast")).to_contain_text("Work moved to")
    expect(inspector.locator("#inspector-body")).to_contain_text("Version")
    comment = inspector.locator("textarea[name=comment]")
    expect(comment).to_be_visible()
    comment.fill("Browser verification comment")
    with page.expect_response(lambda response: response.url.endswith("/work/comments") and response.request.method == "POST"):
        inspector.get_by_role("button", name="Add comment").click()
    expect(page.locator("#toast")).to_contain_text("Comment added")
    expect(inspector.locator("#inspector-body")).to_contain_text("Browser verification comment")


def test_new_work_captures_delivery_context_and_assigns_a_person(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    wait_for_work(page)
    page.locator("#capture").click()
    modal = page.locator("#capture-modal")
    expect(modal).to_be_visible()
    expect(modal.locator("select[name=project_id] option")).not_to_have_count(1)
    expect(modal.locator("select[name=assignee_person_id] option")).not_to_have_count(1)
    modal.locator("input[name=title]").fill("Browser delivery work")
    modal.locator("textarea[name=request]").fill("Create a backend-connected delivery item")
    modal.locator("input[name=requested_by]").fill("Browser QA")
    modal.locator("select[name=project_id]").select_option(index=1)
    modal.locator("select[name=assignee_person_id]").select_option(index=1)
    modal.locator("select[name=priority]").select_option("high")
    modal.locator("input[name=estimate_hours]").fill("4.5")
    modal.locator("input[name=tags]").fill("qa, delivery")
    modal.locator("textarea[name=brief]").fill("Verify project, priority, tags, estimate, and owner.")
    with page.expect_response(lambda response: response.url.endswith("/work/items/assign") and response.request.method == "POST"):
        with page.expect_response(lambda response: response.url.endswith("/work/items") and response.request.method == "POST"):
            modal.get_by_role("button", name="Capture").click()
    expect(page.locator("#toast")).to_contain_text("captured and assigned")
    expect(page.locator("[data-work-id]", has_text="Browser delivery work")).to_be_visible()
    assert_no_browser_errors(page)


def test_intelligence_context_drawer_and_degraded_state(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    rail = page.locator("#intelligence-rail")
    expect(rail).to_be_visible()
    page.locator("#clients [data-client]").first.wait_for(state="visible", timeout=10_000)
    page.locator("#clients [data-client]").first.click()
    expect(page.locator("#intelligence-scope")).to_contain_text("client")
    wait_for_work(page)
    page.locator("[data-work-id]").first.click()
    expect(page.locator("#intelligence-scope")).to_contain_text("work")
    page.route("**/dashboard/intelligence*", lambda route: route.fulfill(status=503, content_type="application/json", body='{"message":"offline"}'))
    page.get_by_role("button", name="What changed", exact=True).click()
    expect(page.locator("#intelligence-findings")).to_contain_text("Intelligence unavailable")
    expect(page.locator("#intelligence-findings")).to_contain_text("Retry")

    page.set_viewport_size({"width": 1100, "height": 800})
    expect(page.locator("#intelligence-toggle")).to_be_visible()
    page.locator("#intelligence-toggle").click()
    expect(page.locator(".shell")).to_have_class(re.compile("intelligence-open"))
    page.locator("#intelligence-close").click()
    expect(page.locator(".shell")).not_to_have_class(re.compile("intelligence-open"))


def test_intelligence_surfaces_disagreement_learning_and_scenario_analysis(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    payload = """{
      "status":"ready",
      "confidence":0.64,
      "findings":[{
        "type":"Recommendation",
        "title":"Contested staffing choice",
        "summary":"The current plan has bounded disagreement.",
        "confidence":0.64,
        "action_descriptors":[{
          "action":"push_plan",
          "label":"Push plan",
          "route":"/work/items",
          "safe":false,
          "reason":"Needs human approval"
        }]
      }],
      "scenarios":[{
        "name":"approval_required_scenario",
        "summary":"A scenario action was returned, but it still needs approval.",
        "action_descriptors":[{
          "action":"apply_scenario",
          "label":"Apply scenario",
          "route":"/work/items",
          "safe":false,
          "reason":"Scenario requires approval"
        }]
      }],
      "disagreement":{"status":"contested","resolution":"human_review","positions":["Capacity risk","Revenue upside"]},
      "historical_learning":{"status":"available","lesson":"Similar launches needed staged capacity."},
      "scenario_analysis":{"status":"bounded","scenarios":[{"name":"baseline","constraint":"No added capacity"}]}
    }"""
    page.route(
        re.compile(r".*/dashboard/intelligence(\?.*)?$"),
        lambda route: route.fulfill(status=200, content_type="application/json", body=payload),
    )
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    page.locator(".nav button[data-name='Intelligence']").click()
    panel = page.locator("[data-executive-intelligence]")
    panel.wait_for(state="visible", timeout=10_000)
    expect(panel).to_contain_text("Disagreement")
    expect(panel).to_contain_text("contested")
    expect(panel).to_contain_text("Historical learning")
    expect(panel).to_contain_text("Similar launches needed staged capacity")
    expect(panel).to_contain_text("Scenario analysis")
    expect(panel).to_contain_text("baseline")
    expect(page.locator(".intelligence-actions button", has_text="Push plan unavailable")).to_be_disabled()
    expect(page.locator("[data-intelligence-action]")).to_have_count(0)
    page.get_by_role("button", name="Run scenario", exact=True).click()
    expect(page.locator("#intelligence-status")).to_contain_text("scenario modeled")
    expect(page.locator(".scenario-card", has_text="approval_required_scenario")).to_be_visible()
    expect(page.locator(".scenario-actions button", has_text="Apply scenario unavailable")).to_be_disabled()
    expect(page.locator("[data-scenario-action]")).to_have_count(0)
    assert_no_browser_errors(page)


def test_disconnected_finance_and_integrations(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    page.locator(".nav button[data-name='Finance']").click()
    expect(page.locator("#finance-body")).to_contain_text(re.compile("Not connected|No financial source is connected", re.I))
    page.once("dialog", lambda dialog: dialog.accept("browser-ledger"))
    with page.expect_response(lambda response: response.url.endswith("/finance/connect") and response.request.method == "POST"):
        page.get_by_role("button", name="Connect source").click()
    for label in ("Record revenue", "Record invoice", "Record cost", "Set budget", "Record software cost", "Record AI usage", "Calculate client economics"):
        expect(page.get_by_role("button", name=label, exact=True)).to_be_visible()
    page.get_by_role("button", name="Record revenue", exact=True).click()
    finance_dialog = page.locator("#finance-action-dialog")
    finance_dialog.locator("input[name=amount]").fill("42")
    finance_dialog.locator("input[name=recognized_at]").fill("2026-08-01")
    finance_dialog.locator("input[name=source]").fill("browser-failure-proof")
    finance_dialog.locator("input[name=kind]").fill("service")
    page.route("**/finance/revenue", lambda route: route.fulfill(status=503, content_type="application/json", body='{"message":"forced failure"}'))
    save = finance_dialog.get_by_role("button", name="Save finance record")
    with page.expect_response(lambda response: response.url.endswith("/finance/revenue") and response.request.method == "POST"):
        save.click()
    expect(page.locator("#toast")).to_contain_text("Finance unavailable")
    expect(save).to_be_enabled()
    finance_dialog.get_by_role("button", name="Cancel").click()
    page.locator(".nav button[data-name='Integrations']").click()
    expect(page.locator("#system-modules")).to_contain_text(re.compile("not connected|credentials", re.I))


def test_integration_onboarding_binds_only_environment_reference(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    page.locator(".nav button[data-name='Integrations']").click()
    page.get_by_role("button", name="Configure integration").click()
    dialog = page.locator("#integration-onboarding-dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text(re.compile("never paste an API key", re.I), use_inner_text=True)
    dialog.locator("select[name=source]").select_option("slack")
    dialog.locator("input[name=expected_account_id]").fill("T_BROWSER")
    dialog.locator("input[name=external_key]").fill("C_BROWSER")
    dialog.locator("input[name=reference]").fill("env:AUREMGRID_BROWSER_SLACK_TOKEN")
    with page.expect_response(lambda response: response.url.endswith("/integrations/credentials") and response.request.method == "POST"):
        dialog.get_by_role("button", name="Save configuration").click()
    expect(page.locator("#toast")).to_contain_text("configuration saved")
    card = page.locator("[data-integration-id]", has_text="slack")
    expect(card).to_contain_text("unverified")
    expect(page.locator("body")).not_to_contain_text("AUREMGRID_BROWSER_SLACK_TOKEN")
    response = page.request.get(
        f"{dashboard_app.base_url}/integrations?organization_id={dashboard_app.organization_id}",
        headers={"Authorization": f"Bearer {dashboard_app.owner_token}"},
    )
    assert response.ok
    serialized = response.text()
    assert "AUREMGRID_BROWSER_SLACK_TOKEN" not in serialized
    assert "reference" not in response.json()["integrations"][-1].get("credential", {})
    assert_no_browser_errors(page)


def test_client_portal_submits_intake_and_staff_accepts_into_work(
    browser, client_page: Page, dashboard_app: DashboardFixture,
) -> None:
    client = client_page
    owner_headers = {"Authorization": f"Bearer {dashboard_app.owner_token}"}
    projects = client.request.get(
        f"{dashboard_app.base_url}/projects?organization_id={dashboard_app.organization_id}"
        f"&workspace_id={dashboard_app.workspaces[0]}&person_id={dashboard_app.owner_person_id}",
        headers=owner_headers,
    ).json()["projects"]
    deliverable_response = client.request.post(
        f"{dashboard_app.base_url}/deliverables", headers=owner_headers,
        data={"organization_id": dashboard_app.organization_id, "workspace_id": dashboard_app.workspaces[0],
              "person_id": dashboard_app.owner_person_id, "project_id": projects[0]["id"],
              "title": "Browser client approval proof", "type": "report"},
    )
    assert deliverable_response.ok
    review_response = client.request.post(
        f"{dashboard_app.base_url}/reviews", headers=owner_headers,
        data={"organization_id": dashboard_app.organization_id, "workspace_id": dashboard_app.workspaces[0],
              "person_id": dashboard_app.owner_person_id, "deliverable_id": deliverable_response.json()["id"],
              "kind": "client", "reviewer_person_id": dashboard_app.client_person_id},
    )
    assert review_response.ok
    review_id = review_response.json()["id"]

    open_dashboard(client, dashboard_app)
    wait_for_command_data(client)
    expect(client.locator(".nav button[data-name='Client Portal']")).to_be_visible()
    client.locator(".nav button[data-name='Client Portal']").click()
    form = client.locator("[data-client-intake-form]")
    form.locator("input[name=title]").fill("Browser client launch request")
    form.locator("textarea[name=request]").fill("Please prepare the approved launch handoff package.")
    with client.expect_response(lambda response: response.url.endswith("/client-portal/intake") and response.request.method == "POST"):
        form.get_by_role("button", name="Submit request").click()
    expect(client.locator("#system-modules")).to_contain_text("Browser client launch request")
    review_card = client.locator(f"[data-client-review-id='{review_id}']")
    review_card.locator(f"[data-client-review-comment='{review_id}']").fill("Approved claims and layout reviewed by the client.")
    with client.expect_response(lambda response: response.url.endswith("/client-portal/reviews/comment") and response.request.method == "POST"):
        review_card.get_by_role("button", name="Add comment").click()
    expect(client.locator("#toast")).to_contain_text("comment added")
    review_card = client.locator(f"[data-client-review-id='{review_id}']")
    client.once("dialog", lambda dialog: dialog.accept())
    with client.expect_response(lambda response: response.url.endswith("/client-portal/reviews/decide") and response.request.method == "POST"):
        review_card.get_by_role("button", name="Approve").click()
    expect(client.locator("#toast")).to_contain_text("decision recorded")
    expect(client.locator(f"[data-client-review-id='{review_id}']")).to_contain_text("approved")
    assert_no_browser_errors(client)

    owner_context = browser.new_context(viewport={"width": 1440, "height": 1000})
    owner_context.add_init_script(
        f"localStorage.setItem('auremgrid_session', {dashboard_app.owner_token!r});"
    )
    staff = owner_context.new_page()
    try:
        open_dashboard(staff, dashboard_app)
        wait_for_command_data(staff)
        staff.locator(".nav button[data-name='Client Portal']").click()
        request_card = staff.locator("[data-intake-id]", has_text="Browser client launch request")
        expect(request_card).to_be_visible()
        with staff.expect_response(lambda response: response.url.endswith("/client-portal/intake/accept") and response.request.method == "POST"):
            request_card.get_by_role("button", name="Accept into Work").click()
        expect(staff.locator("#toast")).to_contain_text("accepted into Work")
        work = staff.request.get(
            f"{dashboard_app.base_url}/dashboard/client?organization_id={dashboard_app.organization_id}"
            f"&workspace_id={dashboard_app.workspaces[0]}&person_id={dashboard_app.owner_person_id}",
            headers={"Authorization": f"Bearer {dashboard_app.owner_token}"},
        )
        assert work.ok
        assert any(item["title"] == "Browser client launch request" for item in work.json()["work"])
    finally:
        owner_context.close()


def test_agent_operations_inspector_observes_canonical_runs(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    page.locator(".nav button[data-name='Agents']").click()
    cards = page.locator("[data-agent-id]")
    cards.first.wait_for(state="visible", timeout=10_000)
    cards.first.click()
    inspector = page.locator("#agent-side-inspector")
    expect(inspector).to_have_class(re.compile("open"))
    expect(inspector).to_contain_text("Operate this worker")
    expect(inspector.locator("[data-agent-action-descriptor]")).to_have_count(2)
    expect(inspector).to_contain_text("Queue")
    expect(inspector).to_contain_text("Recent runs")
    run = inspector.locator("[data-agent-run-id]").first
    run.wait_for(state="visible", timeout=10_000)
    with page.expect_response(lambda response: "/agents/runs/detail?" in response.url and response.request.method == "GET"):
        run.click()
    expect(inspector.locator("[data-agent-run-detail]")).to_contain_text(re.compile("Run|output|No output", re.I))
    assert_no_browser_errors(page)


def test_client_health_tab_renders_explainable_backend_contract(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    page.locator("#clients [data-client]").first.click()
    page.locator("#client-tabs [data-tab='Health']").click()
    body = page.locator("#client-body")
    expect(body).to_contain_text("Client health")
    expect(body.locator(".metric-value")).not_to_have_text("Unknown")
    expect(body.locator("article.card.module")).to_have_count(6)
    expect(body).to_contain_text("delivery", use_inner_text=True)
    assert_no_browser_errors(page)


def test_projects_has_backend_list_create_detail_and_deliverables(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    page.locator(".nav button[data-name='Projects']").click()
    expect(page.locator("#page-work .page-title")).to_have_text("Projects")
    expect(page.locator("#capture")).to_be_hidden()
    cards = page.locator("#work-board [data-project-id]")
    cards.first.wait_for(state="visible", timeout=10_000)
    initial_count = cards.count()

    page.locator("[data-create-project]").click()
    dialog = page.locator("#project-create-dialog")
    dialog.locator("input[name=name]").fill("Browser verification project")
    dialog.locator("textarea[name=description]").fill("Canonical project created from the dashboard")
    dialog.locator("select[name=priority]").select_option("high")
    with page.expect_response(lambda response: response.url.endswith("/projects") and response.request.method == "POST"):
        dialog.get_by_role("button", name="Create project").click()
    expect(page.locator("#toast")).to_contain_text("Project created")
    expect(page.locator("#work-board [data-project-id]")).to_have_count(initial_count + 1)

    page.locator("#work-board [data-project-id]").filter(has_text="Browser verification project").click()
    inspector = page.locator("#project-side-inspector")
    expect(inspector).to_have_class(re.compile("open"))
    expect(inspector).to_contain_text("Canonical project created from the dashboard")
    expect(inspector).to_contain_text("Deliverables")

    prompt_values = iter(("Browser verification deliverable", "campaign_output"))
    page.on("dialog", lambda prompt: prompt.accept(next(prompt_values)))
    with page.expect_response(lambda response: response.url.endswith("/deliverables") and response.request.method == "POST"):
        inspector.get_by_role("button", name="Add deliverable").click()
    expect(inspector).to_contain_text("Browser verification deliverable")
    assert_no_browser_errors(page)


def test_review_center_exposes_server_granted_decisions(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    page.locator(".nav button[data-name='Review']").click()
    action = page.locator("[data-review-decision='approved']").first
    action.wait_for(state="visible", timeout=10_000)
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.url.endswith("/reviews/decide") and response.request.method == "POST"):
        action.click()
    expect(page.locator("#toast")).to_contain_text("Review approved")
    assert_no_browser_errors(page)


def test_content_cards_open_and_advance_backend_lifecycle(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    page.locator(".nav button[data-name='Content']").click()
    page.get_by_role("button", name="Create content item").click()
    dialog = page.locator("#dashboard-action-dialog")
    dialog.locator("[data-marketing-field='title']").fill("Lifecycle browser proof")
    dialog.locator("[data-marketing-field='objective']").fill("Verify the canonical content lifecycle")
    dialog.locator("[data-marketing-field='audience']").fill("Agency operators")
    with page.expect_response(lambda response: response.url.endswith("/content") and response.request.method == "POST"):
        dialog.get_by_role("button", name=re.compile("create", re.I)).click()
    card = page.locator("[data-content-id]", has_text="Lifecycle browser proof")
    card.wait_for(state="visible", timeout=10_000)
    card.click()
    assert_no_browser_errors(page)
    inspector = page.locator("#content-side-inspector")
    expect(inspector).to_have_class(re.compile("open"))
    advance = inspector.locator("[data-content-advance]")
    expect(advance).to_be_visible()
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.url.endswith("/content/advance") and response.request.method == "POST"):
        advance.click()
    expect(page.locator("#toast")).to_contain_text("Content advanced to")
    assert_no_browser_errors(page)


def test_viewer_permission_denial_and_read_only_inspector(viewer_page: Page, dashboard_app: DashboardFixture) -> None:
    page = viewer_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    expect(page.locator(".space-button")).to_have_count(1)
    denied = page.request.get(
        f"{dashboard_app.base_url}/dashboard/client?organization_id={dashboard_app.organization_id}"
        f"&workspace_id={dashboard_app.workspaces[1]}&person_id={dashboard_app.viewer_person_id}",
        headers={"Authorization": f"Bearer {dashboard_app.viewer_token}"},
    )
    assert denied.status == 403
    wait_for_work(page)
    page.locator("[data-work-id]").first.click()
    expect(page.locator("#inspector-body")).to_contain_text(re.compile("Read-only workspace|updates and comments are unavailable", re.I))
    expect(page.locator("[data-work-controls]")).to_have_count(0)


@pytest.mark.parametrize("viewport", [(1440, 1000), (1100, 800), (768, 900), (390, 844)])
def test_responsive_layout_has_no_unintended_page_overflow(owner_page: Page, dashboard_app: DashboardFixture, viewport: tuple[int, int]) -> None:
    page = owner_page
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    expect(page.locator("#page-command")).to_be_visible()
    overflow = page.evaluate("({width:document.documentElement.scrollWidth, client:document.documentElement.clientWidth})")
    assert overflow["width"] <= overflow["client"] + 2
    expect(page.locator("#intelligence-rail")).to_be_visible()
    if viewport[0] <= 1380:
        expect(page.locator("#intelligence-toggle")).to_be_visible()
