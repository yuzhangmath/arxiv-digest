"""Pending work must not redraw a view after the user navigates away."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page

from tests.browser.test_setup_review import browser_page, running_fixture


def hold_response(page: Page, path: str) -> None:
    page.add_init_script(
        """((path) => {
          const originalFetch = globalThis.fetch;
          globalThis.fetch = async function(url, options) {
            const response = await originalFetch.call(globalThis, url, options);
            if (new URL(url).pathname === path) {
              const payload = await response.json();
              response.json = async () => {
                await new Promise(resolve => { window.releaseResponse = resolve; });
                return payload;
              };
            }
            return response;
          };
        })(""" + json.dumps(path) + ");"
    )


def release_response(page: Page) -> None:
    page.evaluate(
        """async () => {
          window.releaseResponse();
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
        }"""
    )


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_pdf_completion_does_not_replace_the_current_view(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        hold_response(page, "/api/v1/downloads/download_1234")
        page.goto(server.launch_url("library"))
        page.get_by_role("button", name="Download v2 PDF", exact=True).click()
        page.wait_for_function("() => typeof window.releaseResponse === 'function'")
        page.get_by_role("button", name="Settings", exact=True).click()
        page.get_by_role("heading", name="Settings", exact=True).wait_for()
        release_response(page)
        assert page.get_by_role("heading", name="Settings", exact=True).count() == 1
        assert "PDF download complete" not in page.locator("#status").inner_text()
        assert [name for name, _payload in application.dashboard_calls].count("library_pdf") == 1

        # The server-side download still completes and is reflected on return.
        page.get_by_role("button", name="Library", exact=True).click()
        page.get_by_role("heading", name="Library", exact=True).wait_for()
        assert page.get_by_role("button", name="Download v2 PDF", exact=True).count() == 0


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_restored_tab_resumes_observing_its_pending_download(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        page.add_init_script(
            """(() => {
              const originalFetch = globalThis.fetch;
              window.downloadComplete = false;
              window.downloadChecks = 0;
              globalThis.fetch = async function(url, options) {
                const response = await originalFetch.call(globalThis, url, options);
                if (new URL(url).pathname.startsWith('/api/v1/downloads/')) {
                  const payload = await response.json();
                  payload.data = window.downloadComplete
                    ? { status: 'completed', complete: true, value: { version: 2 } }
                    : { status: 'running', complete: false };
                  window.downloadChecks++;
                  return new Response(JSON.stringify(payload), {
                    status: response.status,
                    headers: response.headers,
                  });
                }
                return response;
              };
            })();"""
        )
        page.goto(server.launch_url("library"))
        page.get_by_role("button", name="Download v2 PDF", exact=True).click()
        page.wait_for_function("() => window.downloadChecks > 0")
        page.evaluate(
            """() => {
              dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }));
              window.downloadComplete = true;
              window.checksBeforeRestore = window.downloadChecks;
              dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
            }"""
        )
        page.wait_for_function(
            "() => window.downloadChecks > window.checksBeforeRestore", timeout=5_000,
        )
        page.get_by_role("button", name="Download v2 PDF", exact=True).wait_for(
            state="hidden", timeout=5_000,
        )
        assert [name for name, _payload in application.dashboard_calls].count("library_pdf") == 1


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize(
    ("view", "path"),
    [
        ("library", "/api/v1/library"),
        ("interests", "/api/v1/interests"),
        ("calendar", "/api/v1/review/calendar"),
    ],
)
def test_late_view_load_cannot_replace_settings(engine: str, view: str, path: str) -> None:
    with running_fixture() as (server, _application), browser_page(engine) as page:
        hold_response(page, path)
        page.goto(server.launch_url(view))
        page.wait_for_function("() => typeof window.releaseResponse === 'function'")
        page.get_by_role("button", name="Settings", exact=True).click()
        page.get_by_role("heading", name="Settings", exact=True).wait_for()
        release_response(page)
        assert page.get_by_role("heading", name="Settings", exact=True).count() == 1
        assert page.locator("#status").inner_text() == ""
