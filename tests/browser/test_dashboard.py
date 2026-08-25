from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, sync_playwright

from arxiv_digest.web.api import BinaryPayload
from arxiv_digest.web.server import CSP, LoopbackServer, StaticAsset


STATIC_ROOT = Path(__file__).parents[2] / "src/arxiv_digest/web/static"

HARNESS_HTML = b"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Dashboard fixture</title>
    <link rel="stylesheet" href="/styles.css">
    <link rel="stylesheet" href="/vendor/katex/katex.min.css">
  </head>
  <body>
    <header class="site-header"><p class="brand">arXiv Digest fixture</p></header>
    <main id="content">
      <section aria-labelledby="math-heading">
        <h1 id="math-heading">Mathematical abstracts</h1>
        <p id="mixed"></p>
        <p id="malicious"></p>
        <p id="unknown"></p>
        <p id="expansion"></p>
      </section>
      <section id="library"></section>
      <section id="interests"></section>
      <section id="settings"></section>
      <section id="restore"></section>
      <button id="inspect-fixture" type="button">Inspect fixture backup</button>
      <div id="fixture-status" role="status" aria-live="polite"></div>
    </main>
    <script src="/vendor/katex/katex.min.js"></script>
    <script type="module" src="/dashboard-harness.js"></script>
  </body>
</html>
"""

HARNESS_JS = r"""
import { ApiClient } from "/api.mjs";
import { InterestsDraft, renderInterestsView } from "/interests_view.mjs";
import { renderLibraryView } from "/library_view.mjs";
import { renderMathText } from "/math_view.mjs";
import {
  SettingsController,
  renderRestoreError,
  renderRestoreInspection,
  renderSettingsView,
} from "/settings_view.mjs";

const parameters = new URLSearchParams(location.hash.slice(1));
const token = parameters.get("token");
history.replaceState({}, "", location.pathname);
const api = new ApiClient(location.origin, token);
const controller = new SettingsController(api);
const status = document.querySelector("#fixture-status");

renderMathText(
  document.querySelector("#mixed"),
  "Energy is $E=mc^2$ in this model.",
  globalThis.katex,
);
renderMathText(
  document.querySelector("#malicious"),
  "Literal <script>alert(1)</script> <img src=x> $\\href{javascript:alert(1)}{click}$",
  globalThis.katex,
);
renderMathText(
  document.querySelector("#unknown"),
  "Unknown $\\definitelyUnknown{x}$ command.",
  globalThis.katex,
);
renderMathText(
  document.querySelector("#expansion"),
  "Expansion $\\def\\a{\\a}\\a$ is bounded.",
  globalThis.katex,
);

renderLibraryView(
  document,
  document.querySelector("#library"),
  {
    query: "",
    limit: 20,
    offset: 0,
    next_offset: null,
    entries: [{
      metadata: {
        arxiv_id: "2608.01234",
        title: "A <script>literal</script> library title",
        authors: ["Ada Example"],
      },
      saved_version: 1,
      latest_version: 2,
      paper_available: false,
      local_pdf_versions: [1],
      new_version_available: true,
    }],
  },
  {},
);

const interestsDraft = new InterestsDraft({
  revision: 3,
  categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
  keywords: ["derived geometry"],
  phrases: ["mirror symmetry"],
  authors: ["Ada Example"],
  seed_papers: ["2608.01234"],
});
renderInterestsView(
  document,
  document.querySelector("#interests"),
  {
    draft: interestsDraft,
    suggestions_generated_at: "2026-08-22T12:00:00Z",
    suggestions: {
      categories: [{ category: "math.NT", set_spec: "arXiv:math.NT" }],
      keywords: [{ suggestion_id: "keyword_123", value: "spectral sequence" }],
      phrases: [],
      authors: [],
      seed_papers: [],
    },
  },
  {},
);

function showSettings(pickerChoice = null, pickerDisplayName = null) {
  renderSettingsView(
    document,
    document.querySelector("#settings"),
    {
      revision: 9,
      online: false,
      pdf_destination: { kind: "custom", writable: true },
      picker_choice: pickerChoice,
      picker_display_name: pickerDisplayName,
      coverage_min: "2026-07-01",
      coverage_max: "2026-08-24",
      metadata_sync: {
        checkpoint_count: 1,
        categories: [{
          category: "math.AG",
          synchronized_through: "2026-08-21",
          error_codes: ["offline"],
        }],
      },
      daily_list_coverage: {
        target: 32,
        checked: 18,
        with_papers: 10,
        empty: 8,
        failed: 2,
        pending: 12,
        unavailable: 1,
        categories: [{
          category: "math.AG",
          coverage_start: "2026-07-01",
          target: 32,
          checked: 18,
          with_papers: 10,
          empty: 8,
          failed: 2,
          pending: 12,
          unavailable: 1,
          error_codes: ["catchup_fetch_failed"],
          retryable_failed_dates: ["2026-08-11", "2026-08-12"],
        }],
      },
      version_resolution: {
        canonical_event_count: 20,
        atom_confirmed: 17,
        chronology_matched: 2,
        unconfirmed: 1,
      },
      candidate_cache: { status: "ready", file_count: 1 },
      library: { saved_paper_count: 1 },
      pdf_presence: { downloaded_pdf_count: 1 },
      doctor: {
        application_version: "0.2.0",
        database_status: "ok",
        category_count: 1,
        saved_paper_count: 1,
        destination_kind: "custom",
      },
      launcher: { installed: false, operation: "create_failed" },
    },
    {
      openFolder: async () => {
        await controller.openFolder();
        status.textContent = "Active destination opened";
      },
      pickFolder: async () => {
        const result = await controller.pickFolder();
        const choice = result.destination_choice ?? result.picker_result_id;
        showSettings(choice, result.display_name ?? null);
        status.textContent = "Folder selected";
      },
      testFolder: async (choice) => {
        await controller.testFolder(choice);
        status.textContent = "Destination tested";
      },
      exportBackup: () => controller.downloadBackup(),
      inspectBackup: () => {},
      clearCache: async () => {
        const result = await controller.clearCache(true);
        status.textContent = result.durable_state_retained
          ? "Cache deleted; durable state retained"
          : "Unexpected cache response";
      },
      retryLauncher: async () => {
        await controller.createLauncher();
        status.textContent = "Launcher retry complete";
      },
      notNowLauncher: async () => {
        await controller.notNowLauncher();
        status.textContent = "Launcher deferred";
      },
      quit: () => { status.textContent = "Quit requested"; },
    },
  );
}
showSettings();

let restoreOptions = null;
function showInspection(inspection) {
  renderRestoreInspection(
    document,
    document.querySelector("#restore"),
    inspection,
    {
      confirmDestination: (pendingId, choice) =>
        controller.reconfirmRestoreDestination(pendingId, choice),
      pickFolder: async () => {
        const result = await controller.pickFolder();
        const choice = result.destination_choice ?? result.picker_result_id;
        showInspection({
          ...inspection,
          picker_choice: choice,
          picker_display_name: result.display_name ?? null,
        });
      },
      restore: async (pendingId, options) => {
        restoreOptions = options;
        try {
          const result = await controller.restoreBackup(pendingId, options);
          status.textContent = result.pre_restore_backup_created
            ? "Restore complete after pre-restore backup"
            : "Unexpected restore response";
        } catch (error) {
          renderRestoreError(
            document,
            document.querySelector("#restore"),
            "Restore failed safely; this session remains usable.",
            async () => {
              const result = await controller.restoreBackup(pendingId, restoreOptions);
              status.textContent = result.pre_restore_backup_created
                ? "Restore retry complete after pre-restore backup"
                : "Unexpected restore response";
            },
          );
        }
      },
    },
  );
}

document.querySelector("#inspect-fixture").addEventListener("click", async () => {
  const inspection = await controller.inspectBackup(
    new Blob(["synthetic zip"], { type: "application/zip" }),
  );
  showInspection(inspection);
});

globalThis.dashboardHarnessReady = true;
status.textContent = "Dashboard fixture ready";
""".encode()


def _content_type(path: Path) -> str:
    return {
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".ttf": "font/ttf",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }.get(path.suffix, "application/octet-stream")


def _static_assets() -> dict[str, StaticAsset]:
    assets = {
        f"/{path.relative_to(STATIC_ROOT).as_posix()}": StaticAsset(
            _content_type(path), path.read_bytes()
        )
        for path in STATIC_ROOT.rglob("*")
        if path.is_file()
    }
    assets["/dashboard-test.html"] = StaticAsset(
        "text/html; charset=utf-8", HARNESS_HTML
    )
    assets["/dashboard-harness.js"] = StaticAsset(
        "text/javascript; charset=utf-8", HARNESS_JS
    )
    return assets


class DashboardFixture:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.restore_attempts = 0

    def record(self, operation: str, payload: dict[str, object]) -> None:
        with self.lock:
            self.calls.append((operation, dict(payload)))

    def restore(self, payload: dict[str, object]) -> dict[str, object]:
        self.record("backup_restore", payload)
        self.restore_attempts += 1
        if self.restore_attempts == 1:
            raise ValueError("fixture restore failure")
        return {"restored": True, "pre_restore_backup_created": True}

    def handlers(self) -> dict[str, object]:
        def recorded(operation: str, result: dict[str, object]):
            def handler(payload: dict[str, object]) -> dict[str, object]:
                self.record(operation, payload)
                return result

            return handler

        return {
            "settings_folder_open": recorded(
                "settings_folder_open", {"opened": True}
            ),
            "settings_folder_pick": recorded(
                "settings_folder_pick",
                {
                    "destination_choice": "picker_87654321",
                    "display_name": "Research PDFs",
                },
            ),
            "settings_folder_test": recorded(
                "settings_folder_test",
                {"tested_destination_token": "destination_12345678"},
            ),
            "settings_cache_clear": recorded(
                "settings_cache_clear", {"durable_state_retained": True}
            ),
            "settings_launcher_create": recorded(
                "settings_launcher_create", {"installed": True}
            ),
            "settings_launcher_not_now": recorded(
                "settings_launcher_not_now", {"operation": "none"}
            ),
            "backup_export": lambda payload: (
                self.record("backup_export", payload)
                or BinaryPayload(
                    b"portable fixture",
                    filename="arxiv-digest-browser-fixture.zip",
                )
            ),
            "backup_inspect": recorded(
                "backup_inspect",
                {
                    "pending_restore_id": "pending_restore_123",
                    "summary": {"categories": 1, "saved_papers": 1},
                },
            ),
            "backup_restore": self.restore,
        }


@contextmanager
def running_dashboard() -> Iterator[tuple[LoopbackServer, DashboardFixture]]:
    fixture = DashboardFixture()
    server = LoopbackServer(
        handlers=fixture.handlers(),
        static_assets=_static_assets(),
    )
    server.start()
    try:
        yield server, fixture
    finally:
        server.stop()


@contextmanager
def browser_page(engine: str, *, width: int = 1280) -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        context = browser.new_context(viewport={"width": width, "height": 900})
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


def _open(page: Page, server: LoopbackServer):
    response = page.goto(
        f"http://127.0.0.1:{server.port}/dashboard-test.html"
        f"#token={server.token}"
    )
    assert response is not None
    page.wait_for_function("globalThis.dashboardHarnessReady === true")
    return response


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_mixed_math_and_malicious_paper_text_under_final_csp(engine: str) -> None:
    with running_dashboard() as (server, _fixture), browser_page(engine) as page:
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        response = _open(page, server)

        assert response.header_value("content-security-policy") == CSP
        assert "script-src 'self'" in CSP
        assert "style-src-attr 'unsafe-inline'" in CSP
        assert "'unsafe-eval'" not in CSP
        assert page.locator("script:not([src])").count() == 0
        assert page.locator("#mixed > span .katex").count() == 1
        assert page.eval_on_selector_all(
            "#mixed > *",
            "nodes => nodes.map(node => node.tagName)",
        ) == ["SPAN"]
        assert page.eval_on_selector(
            "#mixed",
            "node => [node.childNodes[0].nodeType, node.childNodes[0].textContent, "
            "node.childNodes[node.childNodes.length - 1].nodeType, "
            "node.childNodes[node.childNodes.length - 1].textContent]",
        ) == [3, "Energy is ", 3, " in this model."]

        malicious = page.locator("#malicious")
        assert "<script>alert(1)</script>" in malicious.inner_text()
        assert "<img src=x>" in malicious.inner_text()
        assert malicious.locator("a, img, script, [href], [src]").count() == 0
        assert "\\definitelyUnknown{x}" in page.locator("#unknown").inner_text()
        assert "\\def\\a{\\a}\\a" in page.locator("#expansion").inner_text()
        assert page.locator("#mixed [style]").count() > 0
        assert all(
            urlsplit(url).hostname in {"127.0.0.1", None}
            for url in requests
        )


@pytest.mark.parametrize("width", [360, 1280])
def test_dashboard_views_are_accessible_and_responsive(width: int) -> None:
    with running_dashboard() as (server, _fixture), browser_page(
        "chromium", width=width
    ) as page:
        _open(page, server)
        page.emulate_media(reduced_motion="reduce")

        assert page.get_by_role("heading", name="Library", exact=True).is_visible()
        assert page.get_by_role("heading", name="Interests", exact=True).is_visible()
        assert page.get_by_role("heading", name="Settings", exact=True).is_visible()
        assert page.get_by_text(
            "A <script>literal</script> library title", exact=True
        ).is_visible()
        assert page.get_by_text(
            "Paper unavailable from arXiv", exact=True
        ).is_visible()
        assert page.get_by_text("Local PDF available: v1", exact=True).is_visible()
        assert page.locator("#library script").count() == 0
        assert page.get_by_text("Synchronization offline", exact=False).is_visible()
        assert page.get_by_text(
            "Cached Review and Library remain available", exact=False
        ).is_visible()
        assert page.get_by_text(
            "Target dates: 32 · 18 checked · 10 with papers · 8 empty · "
            "2 failed · 12 pending · 1 unavailable.",
            exact=True,
        ).is_visible()
        assert page.get_by_text(
            "Metadata synchronized through 2026-08-21.", exact=True
        ).is_visible()
        settings_text = page.locator("#settings").inner_text()
        for internal_label in (
            "Canonical-event version resolution",
            "Canonical events",
            "Atom-confirmed",
            "Chronology-matched",
            "Unconfirmed",
        ):
            assert internal_label not in settings_text
        assert "current daily feed" not in settings_text
        assert "inferred from version history" not in settings_text
        assert "Historical coverage backfill" not in settings_text
        assert page.locator('[role="status"][aria-live="polite"]').count() >= 2

        unlabeled = page.locator(
            "input:visible:not([aria-label])"
        ).evaluate_all(
            "nodes => nodes.filter(node => node.labels.length === 0).map(node => node.outerHTML)"
        )
        assert unlabeled == []
        overflow = page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        assert overflow is False
        hidden_controls = page.locator(
            "button:not([hidden]), input:not([hidden])"
        ).evaluate_all(
            "nodes => nodes.filter(node => getComputedStyle(node).display === 'none').length"
        )
        assert hidden_controls == 0
        open_folder = page.locator("#settings").get_by_role(
            "button", name="Open folder"
        )
        open_folder.focus()
        assert open_folder.evaluate(
            "node => getComputedStyle(node).outlineStyle !== 'none'"
        )
        assert page.locator("body").evaluate(
            "node => parseFloat(getComputedStyle(node).animationDuration || '0')"
        ) <= 0.01


def test_backup_folder_cache_and_failed_restore_keep_the_session_usable() -> None:
    with running_dashboard() as (server, fixture), browser_page(
        "chromium"
    ) as page:
        request_bodies: list[tuple[str, str | None]] = []
        page.on(
            "request",
            lambda request: request_bodies.append(
                (request.url, request.post_data)
            )
            if "/api/v1/" in request.url
            else None,
        )
        _open(page, server)

        assert page.locator(
            '#settings input[value="downloads"], #settings input[value="documents"]'
        ).count() == 0
        with page.expect_response(
            lambda response: response.url.endswith("/settings/folder/pick")
        ):
            page.locator("#settings").get_by_role(
                "button", name="Choose PDF folder"
            ).click()
        page.get_by_text("Selected folder: Research PDFs", exact=True).wait_for()
        with page.expect_response(
            lambda response: response.url.endswith("/settings/folder/test")
        ):
            page.get_by_role("button", name="Test and use folder").click()
        assert fixture.calls[-1] == (
            "settings_folder_test",
            {"destination_choice": "picker_87654321"},
        )

        page.locator("#settings").get_by_role(
            "button", name="Open folder"
        ).click()
        page.get_by_text("Active destination opened", exact=True).wait_for()
        open_requests = [
            body for url, body in request_bodies if url.endswith("/settings/folder/open")
        ]
        assert open_requests == [None]
        assert all(
            "/Users/" not in (body or "") and "C:\\" not in (body or "")
            for _url, body in request_bodies
        )

        with page.expect_download() as download_info:
            page.get_by_role("button", name="Export backup").click()
        assert download_info.value.suggested_filename == "arxiv-digest-backup.zip"
        export_requests = [
            (url, body)
            for url, body in request_bodies
            if url.endswith("/backup/export")
        ]
        assert export_requests == [
            (f"http://127.0.0.1:{server.port}/api/v1/backup/export", None)
        ]

        page.locator("#settings").get_by_role(
            "button", name="Delete suggestion cache"
        ).click()
        page.locator("#settings").get_by_role(
            "button", name="Confirm delete suggestion cache"
        ).click()
        page.get_by_text("Cache deleted; durable state retained", exact=True).wait_for()
        assert (
            "settings_cache_clear",
            {},
        ) in fixture.calls

        page.get_by_role("button", name="Inspect fixture backup").click()
        page.get_by_role("heading", name="Restore inspection").wait_for()
        assert page.get_by_text("Inspection made no changes", exact=False).is_visible()
        assert page.locator("#restore label").filter(
            has_text="I understand a pre-restore backup"
        ).is_visible()
        with page.expect_response(
            lambda response: response.url.endswith("/settings/folder/pick")
        ):
            page.locator("#restore").get_by_role(
                "button", name="Choose PDF folder"
            ).click()
        page.locator("#restore").get_by_text(
            "Selected folder: Research PDFs", exact=True
        ).wait_for()
        page.locator("#restore").get_by_role(
            "button", name="Use this folder"
        ).click()
        page.locator(
            '#restore input[name="confirm-pre-restore-backup"]'
        ).check()
        page.get_by_role("button", name="Restore backup").click()
        page.get_by_text("this session remains usable", exact=False).wait_for()
        assert page.locator("#settings").get_by_role(
            "button", name="Open folder"
        ).is_enabled()
        page.get_by_role("button", name="Retry restore").click()
        page.get_by_text(
            "Restore retry complete after pre-restore backup", exact=True
        ).wait_for()
        assert fixture.restore_attempts == 2
        assert all(
            payload["destination_choice"] == "picker_87654321"
            for operation, payload in fixture.calls
            if operation == "backup_restore"
        )
        assert sum(
            operation == "settings_folder_pick"
            for operation, _payload in fixture.calls
        ) == 2
