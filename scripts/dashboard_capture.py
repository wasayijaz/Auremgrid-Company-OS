from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from auremgrid.api.http import serve
from auremgrid.demo_agency import ORG_ID, seed_realistic_agency_demo
from auremgrid.services.brain import CompanyOS
from tests.auth_support import issue_identity

try:
    from playwright.sync_api import Page, sync_playwright
except ImportError as exc:  # pragma: no cover - script dependency guard.
    raise SystemExit("Playwright is not installed; install browser dependencies before capture.") from exc


OUTPUT_DIR = ROOT / "ui-review"
WORKSPACE_ID = "ws_prime_clinics"
OWNER_PERSON_ID = "person_realistic_owner"
ACTOR_ID = "act_ws_prime_clinics"


def _wait_for_boot(page: Page, owner_token: str) -> None:
    page.goto("/dashboard", wait_until="domcontentloaded")
    dialog = page.locator("#access-token-dialog")
    dialog.wait_for(state="visible", timeout=10_000)
    dialog.locator("input[name=token]").fill(owner_token)
    dialog.get_by_role("button", name="Connect").click()
    page.locator("#scope-organization").wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        "document.querySelector('#scope-organization')?.textContent?.trim() !== 'Loading organization'",
        timeout=10_000,
    )


def _save_capture(page: Page, name: str, captures: dict[str, Path]) -> None:
    path = OUTPUT_DIR / name
    page.screenshot(path=str(path), full_page=True)
    captures[name] = path
    print(f"saved {path}")


def _try_step(description: str, action: Callable[[], None]) -> bool:
    try:
        action()
        return True
    except Exception as exc:  # Keep later captures running for UI review.
        print(f"warning: {description} failed: {exc}", file=sys.stderr)
        return False


def _click_nav(page: Page, name: str, timeout: int = 3_000) -> None:
    button = page.locator(f".nav button[data-name={json.dumps(name)}]")
    button.wait_for(state="visible", timeout=timeout)
    button.click()


def _capture_command(page: Page, captures: dict[str, Path]) -> None:
    page.locator("#metrics .metric, #command-kpis .metric").first.wait_for(state="visible", timeout=10_000)
    _save_capture(page, "command.png", captures)


def _capture_work(page: Page, captures: dict[str, Path]) -> None:
    _click_nav(page, "Work", timeout=10_000)
    page.locator("#work-summary").wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        "!document.querySelector('#work-summary')?.textContent?.includes('Loading')",
        timeout=10_000,
    )
    _save_capture(page, "work.png", captures)


def _capture_onboarding(page: Page, captures: dict[str, Path]) -> None:
    navigated = (
        _try_step("open Systems navigation", lambda: _click_nav(page, "Systems"))
        or _try_step("open Integrations navigation", lambda: _click_nav(page, "Integrations", timeout=10_000))
        or _try_step("open Onboarding navigation", lambda: _click_nav(page, "Onboarding", timeout=10_000))
    )
    if navigated:
        _try_step(
            "open Onboarding module",
            lambda: page.get_by_role("button", name="Onboarding").click(timeout=3_000),
        )
        _try_step(
            "wait for system modules",
            lambda: page.wait_for_function(
                """
                () => {
                  const target = document.querySelector('#system-modules');
                  return target && target.textContent && !target.textContent.includes('Loading');
                }
                """,
                timeout=10_000,
            ),
        )
    _try_step("return to Command onboarding imports", lambda: _click_nav(page, "Command", timeout=10_000))
    _try_step("open Command Delivery tab", lambda: page.locator(".command-tab[data-command-tab='delivery']").click(timeout=10_000))
    page.wait_for_function(
        """
        () => Boolean(
          document.querySelector('[data-onboarding-imports]') ||
          document.querySelector('#dashboard-completion-overview') ||
          [...document.querySelectorAll('.empty')].some(node => /CSV import|onboarding import/i.test(node.textContent || ''))
        )
        """,
        timeout=10_000,
    )
    _save_capture(page, "onboarding.png", captures)


def _capture_client_portal(page: Page, captures: dict[str, Path]) -> None:
    _click_nav(page, "Client Portal", timeout=10_000)
    page.locator("#system-modules").wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        """
        () => {
          const target = document.querySelector('#system-modules');
          return target && target.textContent && !target.textContent.includes('Loading');
        }
        """,
        timeout=10_000,
    )
    _save_capture(page, "client-portal.png", captures)


def main() -> int:
    OUTPUT_DIR.mkdir(exist_ok=True)
    company_os = CompanyOS(":memory:")
    server = None
    thread: threading.Thread | None = None
    captures: dict[str, Path] = {}

    try:
        seed_realistic_agency_demo(company_os, ORG_ID, OWNER_PERSON_ID)
        owner_token, _ = issue_identity(company_os, ORG_ID, OWNER_PERSON_ID, WORKSPACE_ID, ACTOR_ID)

        server = serve(company_os, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                base_url=base_url,
                viewport={"width": 1600, "height": 1000},
                device_scale_factor=1,
            )
            page = context.new_page()
            try:
                _wait_for_boot(page, owner_token)
                _try_step("capture command view", lambda: _capture_command(page, captures))
                _try_step("capture work view", lambda: _capture_work(page, captures))
                _try_step("capture onboarding view", lambda: _capture_onboarding(page, captures))
                _try_step("capture client portal view", lambda: _capture_client_portal(page, captures))
            finally:
                context.close()
                browser.close()
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        company_os.close()

    print("saved paths:")
    for path in captures.values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
