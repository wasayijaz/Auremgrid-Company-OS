from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Iterator

try:
    import pytest
except ImportError:  # unittest discovery should remain dependency-free.
    class _PytestStub:
        def fixture(self, *args, **kwargs):
            return lambda function: function

        def skip(self, message: str) -> None:
            raise RuntimeError(message)

    pytest = _PytestStub()  # type: ignore[assignment]

from auremgrid.api.http import serve
from auremgrid.demo_agency import ORG_ID, seed_realistic_agency_demo
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity

try:  # Keep ordinary unit/unittest runs dependency-free.
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
except ImportError:  # pragma: no cover - exercised only on minimal installations.
    Browser = BrowserContext = Page = Playwright = Any  # type: ignore[misc,assignment]
    sync_playwright = None  # type: ignore[assignment]


@dataclass(frozen=True)
class DashboardFixture:
    base_url: str
    organization_id: str
    owner_person_id: str
    owner_token: str
    viewer_person_id: str
    viewer_token: str
    workspaces: tuple[str, ...]


@pytest.fixture(scope="session")
def dashboard_app() -> Iterator[DashboardFixture]:
    os = CompanyOS(":memory:")
    seed_realistic_agency_demo(os, ORG_ID, "person_realistic_owner")
    workspaces = ("ws_prime_clinics", "ws_base_ryder", "ws_evolve")

    owner_token, owner_identity = issue_identity(
        os, ORG_ID, "person_realistic_owner", workspaces[0], "act_ws_prime_clinics"
    )
    # Workspace switching is a first-class UI flow; bind the same principal to
    # each deterministic fixture actor so intelligence reads remain scoped.
    for workspace_id in workspaces[1:]:
        actor_id = f"act_{workspace_id}"
        os.auth.bind_actor(owner_identity, workspace_id, actor_id)

    viewer_id = "person_browser_viewer"
    if os.company.get_person(ORG_ID, viewer_id) is None:
        os.create_person(ORG_ID, "Browser Viewer", "browser-viewer@demo.invalid", role="member", person_id=viewer_id)
        os.add_person_to_workspace(ORG_ID, workspaces[0], viewer_id, "viewer")
    # Viewers intentionally have no auth_manage capability, so do not attempt
    # an actor binding; all browser GETs derive access from the membership.
    viewer_token, _ = issue_identity(os, ORG_ID, viewer_id, workspaces[0])

    server = serve(os, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield DashboardFixture(
            f"http://{host}:{port}", ORG_ID, "person_realistic_owner", owner_token,
            viewer_id, viewer_token, workspaces,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        os.close()


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Playwright]:
    if sync_playwright is None:
        pytest.skip("Playwright is not installed; install with `pip install -e '.[browser]'`.")
    with sync_playwright() as instance:
        yield instance


@pytest.fixture
def browser(playwright_instance: Playwright) -> Iterator[Browser]:
    try:
        instance = playwright_instance.chromium.launch(headless=True)
    except Exception as exc:  # Missing browser binary or OS dependency.
        pytest.skip(f"Playwright Chromium is unavailable; run `python -m playwright install chromium` ({exc}).")
    try:
        yield instance
    finally:
        instance.close()


def _install_session(context: BrowserContext, token: str) -> None:
    # The token is injected into browser storage only. It never appears in a URL,
    # test output, trace, screenshot, or request assertion.
    context.add_init_script(f"localStorage.setItem('auremgrid_session', {json.dumps(token)});")


@pytest.fixture
def owner_page(browser: Browser, dashboard_app: DashboardFixture) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    _install_session(context, dashboard_app.owner_token)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("requestfailed", lambda request: errors.append(f"request failed: {request.url.split('?')[0]}"))
    page._dashboard_browser_errors = errors  # type: ignore[attr-defined]
    page._dashboard_fixture = dashboard_app  # type: ignore[attr-defined]
    try:
        yield page
    finally:
        context.close()


@pytest.fixture
def viewer_page(browser: Browser, dashboard_app: DashboardFixture) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    _install_session(context, dashboard_app.viewer_token)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("requestfailed", lambda request: errors.append(f"request failed: {request.url.split('?')[0]}"))
    page._dashboard_browser_errors = errors  # type: ignore[attr-defined]
    page._dashboard_fixture = dashboard_app  # type: ignore[attr-defined]
    try:
        yield page
    finally:
        context.close()


def open_dashboard(page: Page, fixture: DashboardFixture) -> None:
    page.goto(f"{fixture.base_url}/dashboard", wait_until="domcontentloaded")
    page.locator("#page-command").wait_for(state="visible", timeout=10_000)
    page.wait_for_function("document.querySelector('#scope-organization')?.textContent?.trim() !== 'Loading organization'", timeout=10_000)


def assert_no_browser_errors(page: Page) -> None:
    errors = getattr(page, "_dashboard_browser_errors", [])
    assert not errors, "browser errors: " + "; ".join(errors[:5])
