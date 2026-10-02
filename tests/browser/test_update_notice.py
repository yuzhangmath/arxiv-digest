"""The optional release notice never blocks ordinary dashboard use."""

from __future__ import annotations

import pytest

from tests.browser.test_setup_review import browser_page, running_fixture


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("status", ["current", "manual_fallback", "checking"])
def test_release_notice_settles_and_stops_polling(engine: str, status: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.update_status = {"status": status, "installed_version": "0.3.1"}
        page.clock.install(time="2026-09-01T12:00:00Z")
        page.clock.pause_at("2026-09-01T12:00:01Z")
        with page.expect_response("**/api/v1/update"):
            page.goto(server.launch_url("library"))
        page.get_by_role("heading", name="Library", exact=True).wait_for()
        notice = page.locator("#update-notice")
        assert notice.get_attribute("aria-live") == "polite"
        assert notice.locator("button").count() == 0

        if status == "checking":
            assert notice.inner_text() == ""
            page.clock.fast_forward(66_000)
        if status != "current":
            link = page.get_by_role("link", name="View update instructions")
            link.wait_for()
            assert link.get_attribute("href") == (
                "https://github.com/yuzhangmath/arxiv-digest/releases"
            )
        else:
            assert notice.inner_text() == ""
        requests = application.update_requests
        page.clock.fast_forward(70_000)
        # An unrelated API response also lets any stray polling request settle.
        with page.expect_response("**/api/v1/library*"):
            page.get_by_role("button", name="Library", exact=True).click()
        assert application.update_requests == requests


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_restored_tab_ignores_a_notice_from_its_previous_request(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.update_status = {
            "status": "available_manual",
            "installed_version": "0.3.0",
            "available_version": "0.3.1",
        }
        page.add_init_script(
            """(() => {
              const originalFetch = globalThis.fetch;
              let holdNotice = true;
              globalThis.fetch = async function(url, options) {
                const response = await originalFetch.call(globalThis, url, options);
                if (holdNotice && new URL(url).pathname === "/api/v1/update") {
                  holdNotice = false;
                  await new Promise(resolve => { window.releaseNotice = resolve; });
                }
                return response;
              };
            })();"""
        )
        page.goto(server.launch_url("library"))
        page.get_by_role("heading", name="Library", exact=True).wait_for()
        page.wait_for_function("() => typeof window.releaseNotice === 'function'")
        page.evaluate("dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
        application.update_status = {"status": "current", "installed_version": "0.3.1"}
        with page.expect_response("**/api/v1/update"):
            page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        page.evaluate("window.releaseNotice()")
        with page.expect_response("**/api/v1/library*"):
            page.get_by_role("button", name="Library", exact=True).click()
        assert page.locator("#update-notice").inner_text() == ""
        assert application.update_requests == 2
