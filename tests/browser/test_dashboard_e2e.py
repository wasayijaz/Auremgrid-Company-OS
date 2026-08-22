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


def test_disconnected_finance_and_integrations(owner_page: Page, dashboard_app: DashboardFixture) -> None:
    page = owner_page
    open_dashboard(page, dashboard_app)
    wait_for_command_data(page)
    page.locator(".nav button[data-name='Finance']").click()
    expect(page.locator("#finance-body")).to_contain_text(re.compile("Not connected|No financial source is connected", re.I))
    page.locator(".nav button[data-name='Integrations']").click()
    expect(page.locator("#system-modules")).to_contain_text(re.compile("not connected|credentials", re.I))


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
