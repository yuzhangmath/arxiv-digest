"""Browser update notices consume real, closed discovery/checker payloads."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Page

from arxiv_digest.update_check import UpdateChecker
from arxiv_digest.update_contract import DISCOVERY_DEADLINE_SECONDS, REPOSITORY
from arxiv_digest.update_discovery import discover_updates
from arxiv_digest.web.server import LoopbackServer
from tests.browser.test_setup_review import (
    FixtureApplication,
    FixtureLifecycle,
    _static_assets,
    browser_page,
    running_fixture,
)
from tests.unit.test_update_discovery import _eligible_bundle, _release_server


def discovery_payload(tmp_path: Path, status: str) -> dict[str, bool | str]:
    fixture_root = tmp_path / status
    fixture_root.mkdir()
    installed, target, installation = _eligible_bundle(fixture_root)
    releases = (installed,) if status == "current" else (installed, target)
    open_url = _release_server(releases, [])
    if status == "manual_fallback":
        def open_url(*_args, **_kwargs):
            raise OSError("synthetic unavailable release service")
    result = discover_updates(
        current_version=installed.version,
        installation=installation if status == "available_automatic" else None,
        open_url=open_url,
    )
    assert result.public_snapshot["status"] == status
    return dict(result.public_snapshot)


@contextmanager
def running_notice(
    provider: Callable[[], dict[str, bool | str]],
) -> Iterator[tuple[LoopbackServer, FixtureApplication]]:
    class NoticeApplication(FixtureApplication):
        def release_update(self, _payload):
            with self.lock:
                self.update_requests += 1
            return provider()

    application = NoticeApplication()
    server = LoopbackServer(
        handlers=application.handlers(),
        known_paper=lambda _arxiv_id: True,
        static_assets=_static_assets(),
        lifecycle=FixtureLifecycle(application),
    )
    server.start()
    try:
        yield server, application
    finally:
        server.stop()


def instrument_updates(page: Page, *, hold_first: bool = False) -> None:
    page.add_init_script(
        """
        (() => {
          const originalFetch = globalThis.fetch;
          window.__updatePayloads = [];
          window.__updateReturned = 0;
          window.__holdNextUpdate = HOLD_FIRST;
          globalThis.fetch = async function(url, options) {
            const response = await Reflect.apply(originalFetch, globalThis, [url, options]);
            if (new URL(String(url), location.href).pathname === "/api/v1/update") {
              const payload = await response.clone().json();
              window.__updatePayloads.push(payload.data);
              if (window.__holdNextUpdate) {
                window.__holdNextUpdate = false;
                await new Promise(resolve => { window.__releaseUpdateResponse = resolve; });
              }
              window.__updateReturned += 1;
            }
            return response;
          };
        })();
        """.replace("HOLD_FIRST", "true" if hold_first else "false")
    )


def freeze_clock(page: Page) -> None:
    page.clock.install(time="2026-09-01T12:00:00Z")
    page.clock.pause_at("2026-09-01T12:00:01Z")


def wait_for_updates(page: Page, count: int) -> None:
    page.wait_for_function(
        "count => window.__updateReturned >= count", arg=count,
        polling=10, timeout=2000,
    )
    # Let the real fetch body's JSON read and the polling continuation settle.
    page.wait_for_timeout(20)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("status", ["available_manual", "available_automatic"])
def test_real_discovery_payload_selects_automatic_action_or_manual_guidance(
    tmp_path: Path, engine: str, status: str,
) -> None:
    payload = discovery_payload(tmp_path, status)
    with running_fixture() as (server, application), browser_page(engine) as page:
        instrument_updates(page)
        freeze_clock(page)
        application.update_status = payload
        page.goto(server.launch_url("setup"))
        label = "View release notes" if status == "available_automatic" else "View update instructions"
        link = page.get_by_role("link", name=label)
        link.wait_for(timeout=2000)
        assert link.get_attribute("href") == payload["release_notes_url"]
        assert "arXiv Digest 0.3.2 is available (installed: 0.3.0)." in page.locator("#update-notice").inner_text()
        assert page.locator("#update-notice button").count() == (1 if status == "available_automatic" else 0)
        assert link.get_attribute("target") == "_blank"
        assert link.get_attribute("rel") == "noopener noreferrer"
        assert link.get_attribute("aria-label") == (
            f"{label} (opens in a new tab)"
        )
        assert link.evaluate("element => element.tabIndex") == 0
        link.evaluate(
            """element => element.addEventListener("click", event => {
              event.preventDefault();
              window.__updateLinkActivated = true;
            })"""
        )
        link.focus()
        page.keyboard.press("Enter")
        assert link.evaluate("element => document.activeElement === element")
        assert page.evaluate("window.__updateLinkActivated") is True
        page.clock.fast_forward(70_000)
        page.wait_for_timeout(30)
        assert application.update_requests == 1


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("status", ["current", "manual_fallback"])
def test_real_discovery_terminal_states_stop_polling(
    tmp_path: Path, engine: str, status: str,
) -> None:
    payload = discovery_payload(tmp_path, status)
    with running_fixture() as (server, application), browser_page(engine) as page:
        instrument_updates(page)
        freeze_clock(page)
        application.update_status = payload
        page.goto(server.launch_url("setup"))
        wait_for_updates(page, 1)
        notice = page.locator("#update-notice")
        assert notice.get_attribute("aria-live") == "polite"
        if status == "current":
            assert notice.inner_text() == ""
        else:
            link = page.get_by_role("link", name="View update instructions")
            assert link.get_attribute("href") == f"{REPOSITORY}/releases"
            assert "Could not check for updates." in notice.inner_text()
        page.clock.fast_forward(70_000)
        page.wait_for_timeout(30)
        assert application.update_requests == 1


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_checking_stays_quiet_and_observes_discovery_after_ten_seconds(
    tmp_path: Path, engine: str,
) -> None:
    payload = discovery_payload(tmp_path, "available_manual")
    gate = threading.Event()
    entered = threading.Event()

    def discover(**arguments):
        entered.set()
        assert gate.wait(timeout=20)
        return discover_updates(**arguments)

    installed, target, _installation = _eligible_bundle(tmp_path)
    checker = UpdateChecker(
        current_version=installed.version, discover=discover,
        open_url=_release_server((installed, target), []),
    )
    checker.start()
    assert entered.wait(timeout=2)
    try:
        with running_notice(checker.snapshot) as (server, _application), browser_page(engine) as page:
            instrument_updates(page)
            freeze_clock(page)
            page.goto(server.launch_url("setup"))
            wait_for_updates(page, 1)
            notice = page.locator("#update-notice")
            assert notice.inner_text() == ""
            assert page.evaluate("window.__updatePayloads[0]") == checker.snapshot()
            page.evaluate(
                """() => {
                  window.__noticeMutations = 0;
                  new MutationObserver(records => {
                    window.__noticeMutations += records.length;
                  }).observe(document.querySelector("#update-notice"), {
                    childList: true, characterData: true, subtree: true,
                  });
                }"""
            )
            # Exercise every interval so the old 40-attempt cutoff cannot pass.
            for count in range(2, 46):
                page.clock.run_for(250)
                wait_for_updates(page, count)
            assert page.evaluate("window.__noticeMutations") == 0
            gate.set()
            deadline = time.monotonic() + 2
            while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
                time.sleep(0.005)
            assert checker.snapshot() == payload
            page.clock.run_for(250)
            link = page.get_by_role("link", name="View update instructions")
            link.wait_for(timeout=2000)
            assert link.get_attribute("href") == payload["release_notes_url"]
    finally:
        gate.set()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("outcome", ["manual_fallback", "available_manual"])
def test_final_observation_renders_the_backend_deadline_result(
    tmp_path: Path, engine: str, outcome: str,
) -> None:
    now = [0.0]
    entered = threading.Event()
    gate = threading.Event()

    def discover(**arguments):
        entered.set()
        assert gate.wait(timeout=20)
        return discover_updates(**arguments)

    installed, target, _installation = _eligible_bundle(tmp_path)
    checker = UpdateChecker(
        current_version=installed.version, discover=discover,
        open_url=_release_server((installed, target), []),
        monotonic=lambda: now[0],
    )
    checker.start()
    assert entered.wait(timeout=2)
    try:
        with running_notice(checker.snapshot) as (server, application), browser_page(engine) as page:
            instrument_updates(page)
            freeze_clock(page)
            page.goto(server.launch_url("setup"))
            wait_for_updates(page, 1)
            page.clock.fast_forward(int(DISCOVERY_DEADLINE_SECONDS * 1000))
            wait_for_updates(page, 2)
            assert page.locator("#update-notice").inner_text() == ""
            if outcome == "manual_fallback":
                now[0] = DISCOVERY_DEADLINE_SECONDS
                # Trigger the actual deadline callback without a minute's sleep.
                checker._expire()
            else:
                # Backend success just before its deadline becomes observable
                # after the browser's last ordinary poll saw "checking".
                now[0] = DISCOVERY_DEADLINE_SECONDS - 0.001
                gate.set()
                deadline = time.monotonic() + 2
                while checker.snapshot()["status"] == "checking" and time.monotonic() < deadline:
                    time.sleep(0.005)
            assert checker.snapshot()["status"] == outcome
            page.clock.run_for(1000)
            link = page.get_by_role("link", name="View update instructions")
            link.wait_for(timeout=2000)
            assert link.get_attribute("href") == (
                f"{REPOSITORY}/releases" if outcome == "manual_fallback"
                else target.release_notes_url
            )
            requests = application.update_requests
            page.clock.fast_forward(70_000)
            page.wait_for_timeout(30)
            assert application.update_requests == requests
    finally:
        gate.set()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_suspend_resume_ignores_a_stale_notice_response(
    tmp_path: Path, engine: str,
) -> None:
    available = discovery_payload(tmp_path, "available_manual")
    current = discovery_payload(tmp_path, "current")
    with running_fixture() as (server, application), browser_page(engine) as page:
        instrument_updates(page, hold_first=True)
        freeze_clock(page)
        application.update_status = available
        page.goto(server.launch_url("setup"))
        page.wait_for_function("() => window.__releaseUpdateResponse !== undefined", polling=10)
        page.evaluate("dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }))")
        page.clock.run_for(5000)
        assert application.update_requests == 1
        application.update_status = current
        page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))")
        wait_for_updates(page, 1)
        assert application.update_requests == 2
        page.evaluate("window.__releaseUpdateResponse()")
        wait_for_updates(page, 2)
        assert page.locator("#update-notice").inner_text() == ""
        page.clock.fast_forward(70_000)
        page.wait_for_timeout(30)
        assert application.update_requests == 2


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_quit_cancels_a_pending_notice_and_cannot_resume_it(
    tmp_path: Path, engine: str,
) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        instrument_updates(page, hold_first=True)
        freeze_clock(page)
        application.update_status = discovery_payload(tmp_path, "available_manual")
        page.goto(server.launch_url("setup"))
        page.wait_for_function("() => window.__releaseUpdateResponse !== undefined", polling=10)
        page.get_by_role("button", name="Quit", exact=True).click()
        page.get_by_role("heading", name="arXiv Digest is closed", exact=True).wait_for()
        page.evaluate("window.__releaseUpdateResponse()")
        wait_for_updates(page, 1)
        page.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))")
        page.clock.fast_forward(70_000)
        page.wait_for_timeout(30)
        assert page.locator("#update-notice").inner_text() == ""
        assert application.update_requests == 1


@contextmanager
def running_update_flow():
    from arxiv_digest.maintenance import MaintenanceBarrier
    from arxiv_digest.web.api import JsonPayload

    class UpdateApplication(FixtureApplication):
        def __init__(self):
            super().__init__()
            self.update_status = {
                "status": "available_automatic", "automatic_update": True,
                "installed_version": "0.3.0", "available_version": "0.3.1",
                "release_notes_url": f"{REPOSITORY}/releases/tag/v0.3.1",
            }
            self.job_id = "a" * 64
            self.phase = "downloading"
            self.state = "running"
            self.error_code = None
            self.starts = []
            self.commits = 0
            self.acks = 0
            self.job_reads = 0
            self.receipt = None
            self.receipt_acks = []
            self.receipt_ack_failures = 0
            self.receipt_ack_gate = None

        def start_update(self, payload):
            self.starts.append(payload)
            self.phase = "downloading"
            self.state = "running"
            self.error_code = None
            return JsonPayload(202, {"job_id": self.job_id, "state": "running", "phase": "downloading"})

        def update_job(self, payload):
            assert payload == {"job_id": self.job_id}
            self.job_reads += 1
            value = {"job_id": self.job_id, "state": self.state, "phase": self.phase,
                     "complete": self.state in {"failed", "canceled"}}
            if value["complete"]:
                value.update(error_code=self.error_code, message="Synthetic fixed update failure.")
            return value

        def commit_update(self, payload):
            assert payload == {"job_id": self.job_id}
            self.commits += 1
            return JsonPayload(202, {
                "job_id": self.job_id, "state": "restarting", "phase": "restarting",
                "message": "Updating arXiv Digest. A new dashboard will open automatically. You can close this tab.",
            })

        def ack_update(self, payload):
            assert payload == {"job_id": self.job_id}
            self.acks += 1
            return {"job_id": self.job_id, "state": "restarting", "phase": "restarting"}

        def ack_receipt(self, payload):
            if self.receipt_ack_gate is not None:
                assert self.receipt_ack_gate.wait(5), "receipt test did not release acknowledgement"
            if self.receipt_ack_failures:
                self.receipt_ack_failures -= 1
                raise RuntimeError("synthetic lost receipt acknowledgement")
            self.receipt_acks.append(payload)
            self.receipt = None
            return {"acknowledged": True}

        def handlers(self):
            return {**super().handlers(),
                "update_start": self.start_update, "update_job": self.update_job,
                "update_commit": self.commit_update, "update_handoff_ack": self.ack_update,
                "update_receipt": lambda payload: {"receipt": self.receipt},
                "update_receipt_ack": self.ack_receipt,
            }

    application = UpdateApplication()
    server = LoopbackServer(
        handlers=application.handlers(), known_paper=lambda _: True,
        static_assets=_static_assets(), lifecycle=FixtureLifecycle(application),
        maintenance=MaintenanceBarrier(),
    )
    server.start()
    try:
        yield server, application
    finally:
        server.stop()


def instrument_handoff(page):
    page.add_init_script("""
    (() => {
      const original = globalThis.fetch;
      window.__handoffViews = [];
      window.__receiptViews = [];
      window.__allRequests = [];
      globalThis.fetch = function(url, options) {
        const path = new URL(String(url), location.href).pathname;
        window.__allRequests.push(path);
        if (path.endsWith('/handoff-ack')) {
          let marker = null;
          try { marker = JSON.parse(localStorage.getItem('arxiv-digest.update-transition.v1')); } catch {}
          window.__handoffViews.push({text: document.querySelector('#update-notice').textContent,
            inert: document.querySelector('#content').inert, marker});
        }
        if (path.includes('/update/receipt/') && path.endsWith('/ack')) {
          window.__receiptViews.push(document.querySelector('#update-notice').textContent);
        }
        return Reflect.apply(original, globalThis, [url, options]);
      };
    })();
    """)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_one_click_renders_handoff_before_ack_and_stops_old_server_polling(engine):
    with running_update_flow() as (server, application), browser_page(engine) as page:
        instrument_handoff(page)
        freeze_clock(page)
        page.emulate_media(reduced_motion="reduce")
        page.goto(server.launch_url("setup"))
        button = page.get_by_role("button", name="Update and restart", exact=True)
        button.wait_for()
        button.focus()
        page.keyboard.press("Enter")
        page.get_by_role("button", name="Preparing update…", exact=True).wait_for()
        assert not page.locator("#content").evaluate("element => element.inert")
        assert application.starts == [{"target_version": "0.3.1"}]
        application.phase = "snapshotting_environment"
        page.clock.run_for(500)
        page.get_by_text("Creating a recovery snapshot…", exact=True).wait_for()
        with server.maintenance.update_latch():
            application.phase = "stopping_work"
            page.clock.run_for(500)
            page.get_by_text("Update preparation is finishing; actions are temporarily unavailable.", exact=True).wait_for()
            assert page.locator("#content").evaluate("element => element.inert")
            assert page.locator("#quit").is_disabled()
            assert page.evaluate("localStorage.getItem('arxiv-digest.update-transition.v1')") is None
            application.phase = application.state = "ready_to_restart"
            page.clock.run_for(500)
            page.wait_for_function("() => window.__handoffViews.length === 1")
            page.wait_for_timeout(30)
            view = page.evaluate("window.__handoffViews[0]")
            assert "Once it appears, this tab is safe to close." in view["text"]
            assert view["inert"] is True
            assert view["marker"]["job_id"] == application.job_id
            assert view["marker"]["startup_nonce"] == server.startup_nonce
            assert application.commits == application.acks == 1
            requests = page.evaluate("window.__allRequests.length")
            page.clock.fast_forward(180_000)
            page.wait_for_timeout(30)
            assert page.evaluate("window.__allRequests.length") == requests
            assert "Updated successfully" not in page.locator("#update-notice").inner_text()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_other_tab_and_bfcache_resume_use_exact_handoff_marker(engine):
    with running_update_flow() as (server, application), browser_page(engine) as page:
        freeze_clock(page)
        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Update and restart", exact=True).wait_for()
        second = page.context.new_page()
        freeze_clock(second)
        second.goto(server.launch_url("setup"))
        second.get_by_role("button", name="Update and restart", exact=True).wait_for()
        second.evaluate("dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
        page.get_by_role("button", name="Update and restart", exact=True).click()
        page.get_by_role("button", name="Preparing update…", exact=True).wait_for()
        with server.maintenance.update_latch():
            application.phase = application.state = "ready_to_restart"
            page.clock.run_for(500)
            page.get_by_text("Updating and restarting…", exact=False).wait_for()
            second.evaluate("dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
            second.get_by_text("Updating and restarting…", exact=False).wait_for()
            assert second.locator("#content").evaluate("element => element.inert")
            assert second.locator("#quit").is_disabled()
        second.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("code,guarded", [("helper_commit_aborted", False), ("helper_commit_failed", True)])
def test_handoff_reconciliation_resumes_only_proven_cleanup(engine, code, guarded):
    with running_update_flow() as (server, application), browser_page(engine) as page:
        freeze_clock(page)
        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Update and restart", exact=True).click()
        page.get_by_role("button", name="Preparing update…", exact=True).wait_for()
        application.phase = application.state = "ready_to_restart"
        page.clock.run_for(500)
        page.get_by_text("Updating and restarting…", exact=False).wait_for()
        application.state = "failed"
        application.phase = "restarting"
        application.error_code = code
        page.evaluate("dispatchEvent(new Event('focus'))")
        if guarded:
            page.get_by_text("The update handoff could not be resolved safely.", exact=False).wait_for()
            assert page.locator("#content").evaluate("element => element.inert")
            assert page.locator("#quit").is_enabled()
            assert page.evaluate("localStorage.getItem('arxiv-digest.update-transition.v1')") is not None
        else:
            page.get_by_role("button", name="Update and restart", exact=True).wait_for()
            assert not page.locator("#content").evaluate("element => element.inert")
            assert page.evaluate("localStorage.getItem('arxiv-digest.update-transition.v1')") is None


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_receipt_is_visible_before_its_durable_acknowledgement(engine):
    with running_update_flow() as (server, application), browser_page(engine) as page:
        instrument_handoff(page)
        application.receipt = {
            "receipt_id": "c" * 64, "outcome": "updated", "installed_version": "0.3.1",
            "attempted_version": "0.3.1", "message_code": "update_succeeded",
        }
        application.receipt_ack_gate = threading.Event()
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and response.url.endswith(f"/update/receipt/{'c' * 64}/ack")) as acknowledgement:
            try:
                page.goto(server.launch_url("setup"))
                page.wait_for_function("() => window.__receiptViews.length === 1")
                assert "Updated successfully to 0.3.1." in page.evaluate("window.__receiptViews[0]")
                assert application.receipt_acks == []
                assert application.receipt is not None
            finally:
                application.receipt_ack_gate.set()
        response = acknowledgement.value
        response.finished()
        assert response.status == 200
        assert response.json()["data"] == {"acknowledged": True}
        assert application.receipt_acks == [{"receipt_id": "c" * 64}]
        assert application.receipt is None


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_receipt_ack_failure_blocks_next_update_until_focus_retry(engine):
    with running_update_flow() as (server, application), browser_page(engine) as page:
        instrument_handoff(page)
        application.receipt_ack_failures = 1
        application.receipt = {
            "receipt_id": "c" * 64, "outcome": "updated", "installed_version": "0.3.1",
            "attempted_version": "0.3.1", "message_code": "update_succeeded",
        }
        with page.expect_response(lambda response: response.request.method == "POST"
                                  and response.url.endswith(f"/update/receipt/{'c' * 64}/ack")) as acknowledgement:
            page.goto(server.launch_url("setup"))
        response = acknowledgement.value
        response.finished()
        assert response.status == 500
        page.wait_for_function("() => window.__receiptViews.length === 1")
        assert page.get_by_role("button", name="Update and restart", exact=True).count() == 0
        assert application.receipt is not None
        page.evaluate("dispatchEvent(new Event('focus'))")
        page.get_by_role("button", name="Update and restart", exact=True).wait_for()
        assert application.receipt is None
        assert application.receipt_acks == [{"receipt_id": "c" * 64}]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_storage_and_broadcast_denial_do_not_block_initiating_handoff(engine):
    with running_update_flow() as (server, application), browser_page(engine) as page:
        instrument_handoff(page)
        page.add_init_script("""
        Object.defineProperty(window, 'localStorage', {get() {throw new Error('unavailable');}});
        Object.defineProperty(window, 'BroadcastChannel', {value: function() {throw new Error('unavailable');}});
        """)
        freeze_clock(page)
        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Update and restart", exact=True).click()
        page.get_by_role("button", name="Preparing update…", exact=True).wait_for()
        application.phase = application.state = "ready_to_restart"
        page.clock.run_for(500)
        page.wait_for_function("() => window.__handoffViews.length === 1")
        assert "Once it appears, this tab is safe to close." in page.evaluate("window.__handoffViews[0].text")
        assert page.evaluate("window.__handoffViews[0].marker") is None
        page.wait_for_timeout(30)
        assert application.acks == 1
