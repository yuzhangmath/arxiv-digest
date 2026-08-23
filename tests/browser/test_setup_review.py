from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Page, sync_playwright

from arxiv_digest.web.server import LoopbackServer, StaticAsset


STATIC_ROOT = Path(__file__).parents[2] / "src/arxiv_digest/web/static"


def _static_assets() -> dict[str, StaticAsset]:
    content_types = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".ttf": "font/ttf",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }
    return {
        f"/{path.relative_to(STATIC_ROOT).as_posix()}": StaticAsset(
            content_types[path.suffix], path.read_bytes()
        )
        for path in STATIC_ROOT.rglob("*")
        if path.is_file() and path.suffix in content_types
    }


class FixtureApplication:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.revision = 0
        self.step = "categories"
        self.submissions: list[dict[str, object]] = []
        self.completions: list[dict[str, object]] = []
        self.launcher_calls = 0
        self.saved_anchor: int | None = None
        self.dashboard_calls: list[tuple[str, dict[str, object]]] = []
        self.candidate_job: dict[str, object] = {
            "status": "completed",
            "complete": True,
            "failed": False,
            "corpus_complete": True,
            "minimum_met": True,
            "setup_ready": True,
            "can_resume": False,
            "corpus_hash": "a" * 64,
            "reduced_breadth": False,
            "message": "Corpus ready",
        }

    def record_dashboard(
        self, operation: str, payload: dict[str, object], result: object
    ) -> object:
        with self.lock:
            self.dashboard_calls.append((operation, dict(payload)))
        return result

    def draft(self, _payload: dict[str, object]) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": 1,
            "revision": self.revision,
            "current_step": self.step,
            "categories": [],
            "coverage_start": None,
            "coverage_warning": None,
            "corpus_complete": False,
            "corpus_hash": None,
            "corpus_reduced_breadth": False,
            "recommended_coverage_start": "2026-07-23",
        }
        if self.step == "review":
            value.update(
                {
                    "profile_summary": {
                        "categories": ["math.AG"],
                        "seed_papers": ["2608.01234", "2608.09999"],
                        "keywords": ["derived geometry", "custom keyword"],
                        "phrases": ["mirror symmetry", "custom phrase"],
                        "authors": ["Ada Example", "Custom Author"],
                        "pdf_destination_kind": "documents",
                    },
                    "profile_summary_sha256": "b" * 64,
                }
            )
        return value

    def update_draft(self, payload: dict[str, object]) -> dict[str, object]:
        transitions = {
            "categories": "initial_coverage",
            "coverage": "candidate_corpus",
            "seed_papers": "keywords_and_phrases",
            "terms": "authors",
            "authors": "pdf_destination",
            "pdf_destination": "review",
            "review": "desktop_launcher",
        }
        with self.lock:
            self.submissions.append(dict(payload))
            self.revision += 1
            self.step = transitions[str(payload["step"])]
        return self.draft({})

    def accept_corpus(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.submissions.append(dict(payload))
            self.revision += 1
            self.step = "seed_papers"
        return self.draft({})

    def complete(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.completions.append(dict(payload))
            if payload["launcher_choice"] == "create":
                self.launcher_calls += 1
        return {"profile_revision": 1}

    @staticmethod
    def review_summary(_payload: dict[str, object]) -> dict[str, object]:
        # A real summary query can finish after the selected-date request. The
        # shell must not start both when Calendar chooses a date.
        time.sleep(0.15)
        return {
            "unreviewed_dates": 10,
            "unreviewed_papers": 200,
            "newly_discovered": 1,
            "oldest_unreviewed_date": "2026-07-31",
        }

    def review_date(self, payload: dict[str, object]) -> dict[str, object]:
        day = str(payload["date"])
        requested_anchor = payload.get("anchor_event_id")
        anchor = int(requested_anchor or self.saved_anchor or 1)
        page_number = min(10, max(1, (anchor - 1) // 20 + 1))
        start = (page_number - 1) * 20 + 1
        cards = []
        for event_id in range(start, start + 20):
            tier = "top" if event_id == start else "possible" if event_id == start + 1 else "other"
            cards.append(
                {
                    "event_id": event_id,
                    "arxiv_id": f"2608.{event_id:05d}",
                    "announced_version": 2,
                    "title": f"Paper {event_id} <script>not markup</script>",
                    "authors": ["Ada Example"],
                    "abstract": "An accessible collapsed abstract.",
                    "primary_category": "math.AG",
                    "categories": ["math.AG", "math.CO"],
                    "category_observations": ["math.AG", "math.CO"],
                    "effective_date": day,
                    "date_label": "arXiv mailing date",
                    "confidence": "current",
                    "confidence_label": "Current announcement",
                    "newly_discovered": event_id == start,
                    "reviewed": False,
                    "tier": tier,
                    "score": 4.0,
                    "reasons": [
                        {
                            "kind": "keyword",
                            "label": "Matched selected keyword",
                            "location": "title",
                        }
                    ],
                    "evidence": [],
                }
            )
        return {
            "day": day,
            "cards": cards,
            "snapshot_revision": 77,
            "anchor_event_id": start,
            "previous_anchor_event_id": None if page_number == 1 else start - 20,
            "next_anchor_event_id": None if page_number == 10 else start + 20,
            "previous_date": "2026-06-30",
            "next_date": "2026-08-31",
            "next_unreviewed_date": "2026-09-01",
            "page_number": page_number,
            "page_count": 10,
            "total_cards": 200,
        }

    def record_position(self, payload: dict[str, object]) -> dict[str, object]:
        self.saved_anchor = int(payload["anchor_event_id"])
        return dict(payload)

    def handlers(self) -> dict[str, object]:
        empty = lambda _payload: {}
        return {
            "status": lambda _payload: {"state": "ready"},
            "categories": lambda _payload: [
                {
                    "category": "math.AG",
                    "set_spec": "arXiv:math.AG",
                    "label": "Algebraic Geometry",
                },
                {
                    "category": "math.CO",
                    "set_spec": "arXiv:math.CO",
                    "label": "Combinatorics",
                },
            ],
            "setup_draft_get": self.draft,
            "setup_draft_put": self.update_draft,
            "setup_corpus": lambda _payload: {"job_id": "corpus_job_1234"},
            "setup_job": lambda _payload: dict(self.candidate_job),
            "setup_corpus_accept": self.accept_corpus,
            "setup_candidate_papers": lambda _payload: {
                "items": [
                    {
                        "suggestion_id": "paper_suggestion_1",
                        "title": "Selected geometry",
                    }
                ]
            },
            "setup_candidate_terms": lambda _payload: {
                "keywords": [
                    {
                        "suggestion_id": "keyword_suggestion_1",
                        "label": "derived geometry",
                    }
                ],
                "phrases": [
                    {
                        "suggestion_id": "phrase_suggestion_1",
                        "label": "mirror symmetry",
                    }
                ],
            },
            "setup_candidate_authors": lambda _payload: {
                "items": [
                    {
                        "suggestion_id": "author_suggestion_1",
                        "label": "Ada Example",
                    }
                ]
            },
            "setup_folder_test": lambda _payload: {
                "tested_destination_token": "destination_12345678"
            },
            "setup_folder_pick": lambda _payload: {"cancelled": True},
            "setup_complete": self.complete,
            "sync_start": lambda payload: self.record_dashboard(
                "sync_start", payload, {"job_id": "sync_initial_1234"}
            ),
            "tabs_connect": empty,
            "tabs_heartbeat": empty,
            "tabs_disconnect": empty,
            "review_summary": self.review_summary,
            "review_calendar": lambda _payload: [
                {
                    "date": "2026-08-03",
                    "count": 4,
                    "total_papers": 4,
                    "unreviewed_papers": 2,
                    "status": "partial",
                }
            ],
            "review_date": self.review_date,
            "review_position": self.record_position,
            "review_finish": empty,
            "library_save": empty,
            "library": lambda payload: self.record_dashboard(
                "library",
                payload,
                {
                    "query": payload.get("q", ""),
                    "limit": 20,
                    "offset": int(payload.get("offset", 0)),
                    "previous_offset": None,
                    "next_offset": None,
                    "entries": [
                        {
                            "metadata": {
                                "arxiv_id": "2608.01234",
                                "title": "A dashboard library paper",
                                "authors": ["Ada Example"],
                            },
                            "saved_version": 1,
                            "latest_version": 2,
                            "paper_available": True,
                            "local_pdf_versions": [],
                            "new_version_available": True,
                        }
                    ],
                },
            ),
            "library_remove": lambda payload: self.record_dashboard(
                "library_remove", payload, {"saved": False}
            ),
            "library_pdf": lambda payload: self.record_dashboard(
                "library_pdf", payload, {"job_id": "download_1234"}
            ),
            "download_status": lambda payload: self.record_dashboard(
                "download_status", payload, {"status": "completed", "complete": True}
            ),
            "interests_get": lambda payload: self.record_dashboard(
                "interests_get",
                payload,
                {
                    "revision": 3,
                    "categories": [
                        {"category": "math.AG", "set_spec": "arXiv:math.AG"}
                    ],
                    "keywords": ["derived geometry"],
                    "phrases": ["mirror symmetry"],
                    "authors": ["Ada Example"],
                    "seed_papers": ["2608.01234"],
                    "suggestions_generated_at": "2026-08-22T12:00:00Z",
                    "suggestions": {
                        "categories": [
                            {"category": "math.NT", "set_spec": "arXiv:math.NT"}
                        ],
                        "keywords": [{"value": "spectral sequence"}],
                        "phrases": [],
                        "authors": [],
                        "seed_papers": [],
                    },
                },
            ),
            "interests_put": lambda payload: self.record_dashboard(
                "interests_put", payload, {"revision": 4}
            ),
            "settings_get": lambda payload: self.record_dashboard(
                "settings_get",
                payload,
                {
                    "revision": 3,
                    "online": False,
                    "pdf_destination": {"kind": "downloads"},
                    "categories": [
                        {
                            "category": "math.AG",
                            "metadata_synchronized_through": "2026-08-21",
                            "historical_backfill": {"status": "interrupted"},
                            "exact_enrichment": {"holes": ["2026-08-12"]},
                            "current_sync": {"status": "failed", "error_code": "offline"},
                        }
                    ],
                },
            ),
            "settings_doctor": lambda payload: self.record_dashboard(
                "settings_doctor",
                payload,
                {
                    "application_version": "0.1.0",
                    "database_status": "ok",
                    "category_count": 1,
                    "saved_paper_count": 1,
                    "destination_kind": "downloads",
                },
            ),
            "settings_launcher": lambda payload: self.record_dashboard(
                "settings_launcher", payload, {"installed": False, "operation": "none"}
            ),
            "settings_folder_test": lambda payload: self.record_dashboard(
                "settings_folder_test",
                payload,
                {"tested_destination_token": "destination_12345678"},
            ),
            "settings_folder": lambda payload: self.record_dashboard(
                "settings_folder", payload, {"revision": 4}
            ),
            "settings_folder_open": lambda payload: self.record_dashboard(
                "settings_folder_open", payload, {"status": "opened"}
            ),
            "settings_folder_pick": lambda payload: self.record_dashboard(
                "settings_folder_pick", payload, {"cancelled": True}
            ),
            "settings_cache_clear": lambda payload: self.record_dashboard(
                "settings_cache_clear", payload, {"cleared": True}
            ),
            "settings_coverage": lambda payload: self.record_dashboard(
                "settings_coverage", payload, {"category": payload["category"]}
            ),
            "settings_launcher_create": empty,
            "settings_launcher_not_now": empty,
            "settings_launcher_remove": empty,
            "application_quit": empty,
        }


@contextmanager
def running_fixture() -> Iterator[tuple[LoopbackServer, FixtureApplication]]:
    application = FixtureApplication()
    server = LoopbackServer(
        handlers=application.handlers(),
        known_paper=lambda _arxiv_id: True,
        static_assets=_static_assets(),
    )
    server.start()
    try:
        yield server, application
    finally:
        server.stop()


@contextmanager
def browser_page(engine: str) -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize(
    ("view", "heading"),
    [("library", "Library"), ("settings", "Settings"), ("interests", "Interests")],
)
def test_fragment_bootstrap_and_same_tab_reload(
    engine: str, view: str, heading: str
) -> None:
    with running_fixture() as (server, _application), browser_page(engine) as page:
        urls: list[str] = []
        page.on("request", lambda request: urls.append(request.url))
        page.goto(server.launch_url(view))
        page.get_by_role("heading", name=heading, exact=True).wait_for()

        assert "token=" not in page.url
        assert page.evaluate("sessionStorage.getItem('arxiv-digest.session-token')") == server.token
        page.reload()
        page.get_by_role("heading", name=heading, exact=True).wait_for()
        assert all("token=" not in url for url in urls if "/api/" in url)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_backup_export_401_clears_the_tab_session(engine: str) -> None:
    with running_fixture() as (server, _application), browser_page(engine) as page:
        page.goto(server.launch_url("settings"))
        page.get_by_role("heading", name="Settings", exact=True).wait_for()
        server.token = "B" * 43

        page.get_by_role("button", name="Export backup").click()
        page.get_by_text(
            "Your local session expired. Reopen arXiv Digest to continue.",
            exact=True,
        ).wait_for()

        assert page.evaluate(
            "sessionStorage.getItem('arxiv-digest.session-token')"
        ) is None


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_complete_setup_records_only_explicit_selections_and_not_now(
    engine: str,
) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        page.goto(server.launch_url("setup"))
        page.get_by_text("Algebraic Geometry").wait_for()
        assert page.get_by_text("Algebraic Geometry").count() == 1, page.locator(
            "body"
        ).inner_text()
        category_search = page.get_by_label("Search categories")
        category_search.fill("geometry")
        page.get_by_role("button", name="Search").click()
        page.get_by_text("Algebraic Geometry").click()
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Use recommended 30 days").click()
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Generate corpus").click()
        page.get_by_text("Corpus ready").wait_for()
        page.get_by_role("button", name="Continue").click()

        page.get_by_text("Selected geometry").click()
        page.get_by_role("button", name="Add custom paper id").click()
        page.locator(".custom-entries input").fill("2608.09999")
        page.get_by_role("button", name="Continue").click()

        page.get_by_text("derived geometry").click()
        page.get_by_text("mirror symmetry").click()
        page.get_by_role("button", name="Add custom keyword").click()
        page.locator(".custom-entries").nth(0).locator("input").fill("custom keyword")
        page.get_by_role("button", name="Add custom phrase").click()
        page.locator(".custom-entries").nth(1).locator("input").fill("custom phrase")
        page.get_by_role("button", name="Continue").click()

        page.get_by_text("Ada Example").click()
        page.get_by_role("button", name="Add custom author").click()
        page.locator(".custom-entries input").fill("Custom Author")
        page.get_by_role("button", name="Continue").click()

        downloads = page.locator('input[value="downloads"]')
        assert downloads.is_checked()
        page.get_by_role("button", name="Use Documents").click()
        assert page.locator('input[value="documents"]').is_checked()
        assert page.locator('input[type="text"]').count() == 0
        page.get_by_role("button", name="Test selected destination").click()
        page.get_by_text("destination is writable").wait_for()
        page.get_by_role("button", name="Continue").click()

        page.get_by_text("Categories", exact=True).wait_for()
        page.get_by_role("button", name="Continue").click()
        page.get_by_text("launcher", exact=True).wait_for()
        launcher_continue = page.get_by_role("button", name="Continue")
        assert launcher_continue.is_disabled()
        page.get_by_role("button", name="Not now").click()
        assert launcher_continue.is_enabled()
        launcher_continue.click()
        page.get_by_role("button", name="Start review").wait_for()

        assert application.launcher_calls == 0
        assert application.completions[-1]["launcher_choice"] == "not_now"
        assert ("sync_start", {}) in application.dashboard_calls
        category = next(item for item in application.submissions if item.get("step") == "categories")
        assert category["selections"] == [
            {"category": "math.AG", "set_spec": "arXiv:math.AG"}
        ]
        seeds = next(item for item in application.submissions if item.get("step") == "seed_papers")
        assert seeds["accepted_suggestion_ids"] == ["paper_suggestion_1"]
        assert seeds["custom_arxiv_ids"] == ["2608.09999"]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_review_home_page_navigation_safe_metadata_and_resume(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review").click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()

        assert page.locator("article").count() == 20
        for tier in ("Top", "Possible", "Other"):
            page.get_by_role("heading", name=tier).wait_for()
        assert page.locator("script", has_text="not markup").count() == 0
        assert page.get_by_text("<script>not markup</script>", exact=False).count() >= 1
        first = page.locator("article").first
        first.get_by_text("Why this ranking").wait_for()
        first.get_by_text("Current announcement").wait_for()
        first.get_by_text("Newly discovered").wait_for()
        first.locator("summary").click()
        assert first.locator("details").get_attribute("open") is not None

        page.get_by_role("button", name="Next page").click()
        page.get_by_text("Page 2 of 10").wait_for()
        assert application.saved_anchor == 21
        page.reload()
        page.get_by_role("button", name="Start review").click()
        page.get_by_text("Page 2 of 10").wait_for()

        page.get_by_role("button", name="Previous date").click()
        page.get_by_role("heading", name="Review 2026-06-30").wait_for()
        page.get_by_role("button", name="Next date").click()
        page.get_by_role("heading", name="Review 2026-08-31").wait_for()
        page.get_by_role("button", name="Next unreviewed").click()
        page.get_by_role("heading", name="Review 2026-09-01").wait_for()

        page.get_by_role("button", name="Finish date").click()
        assert page.get_by_role("button", name="Confirm finish").is_visible()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_calendar_navigation_opens_and_keeps_the_selected_date(engine: str) -> None:
    with running_fixture() as (server, _application), browser_page(engine) as page:
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Calendar").click()
        page.get_by_role("heading", name="Calendar", exact=True).wait_for()
        assert "view=calendar" in page.url
        page.reload()
        page.get_by_role("heading", name="Calendar", exact=True).wait_for()
        assert page.evaluate(
            "sessionStorage.getItem('arxiv-digest.session-token')"
        ) == server.token
        page.get_by_role(
            "listitem",
            name="2026-08-03: 4 papers, partial",
        ).click()
        page.get_by_role("heading", name="Review 2026-08-03").wait_for()
        page.wait_for_timeout(250)

        assert page.get_by_role(
            "heading", name="Review 2026-08-03"
        ).is_visible()
        assert "view=review" in page.url


def test_cancelled_folder_picker_keeps_downloads_and_sends_no_path() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 7
        application.step = "pdf_destination"
        seen_bodies: list[str] = []
        page.on(
            "request",
            lambda request: seen_bodies.append(request.post_data or "")
            if "/setup/folder" in request.url
            else None,
        )
        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Choose another folder").click()
        page.get_by_text("selection was cancelled").wait_for()
        assert page.locator('input[value="downloads"]').is_checked()
        assert all("/Users/" not in body and "C:\\" not in body for body in seen_bodies)


def test_capped_candidate_job_requires_resume_or_retry_before_continuing() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.candidate_job = {
            "status": "completed",
            "complete": True,
            "failed": False,
            "corpus_complete": False,
            "minimum_met": False,
            "setup_ready": False,
            "can_resume": True,
            "corpus_hash": "a" * 64,
            "message": "Invocation budget reached",
        }

        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Generate corpus").click()
        page.get_by_role("button", name="Resume", exact=True).wait_for()

        assert page.get_by_role("button", name="Retry", exact=True).is_visible()
        assert page.get_by_role("button", name="Continue", exact=True).is_disabled()
        page.get_by_text("below the required minimum", exact=False).wait_for()

        application.candidate_job.update(
            minimum_met=True,
            corpus_hash="b" * 64,
            message="Minimum candidate breadth reached",
        )
        page.get_by_role("button", name="Resume", exact=True).click()
        accept = page.get_by_role(
            "button", name="Accept reduced breadth and continue", exact=True
        )
        accept.wait_for()
        assert accept.is_enabled()
        page.get_by_text("explicitly accept this reduced breadth", exact=False).wait_for()


def test_create_launcher_is_explicit_and_dispatched_once() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 8
        application.step = "desktop_launcher"
        page.goto(server.launch_url("setup"))
        continue_button = page.get_by_role("button", name="Continue")
        assert continue_button.is_disabled()
        page.get_by_role("button", name="Create desktop launcher").click()
        assert continue_button.is_enabled()
        continue_button.click()
        page.get_by_role("button", name="Start review").wait_for()
        assert application.launcher_calls == 1
        assert application.completions == [
            {"draft_revision": 8, "launcher_choice": "create"}
        ]


def test_shared_application_wires_library_interests_and_settings_actions() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        page.goto(server.launch_url("library"))
        page.get_by_text("A dashboard library paper", exact=True).wait_for()
        page.get_by_role("button", name="Download v2 PDF", exact=True).click()
        page.get_by_text("PDF download complete").wait_for()

        page.get_by_role("button", name="Interests").click()
        page.get_by_text("spectral sequence", exact=True).wait_for()
        page.get_by_text("spectral sequence", exact=True).click()
        page.get_by_label("Add custom keywords").fill("custom topology")
        page.get_by_role("button", name="Add custom keyword").click()
        page.get_by_role("button", name="Get fresh suggestions").click()
        page.get_by_text("Suggestions refreshed").wait_for()
        page.get_by_role("button", name="Save interests").click()
        page.get_by_text("Interests saved").wait_for()

        page.get_by_role("button", name="Settings").click()
        page.get_by_text("Synchronization offline").wait_for()
        page.locator('input[value="documents"]').check()
        page.get_by_role("button", name="Test download").click()
        page.get_by_text("PDF destination saved").wait_for()
        page.get_by_role("button", name="Open folder").click()
        page.get_by_text("PDF folder opened").wait_for()

        operations = [operation for operation, _payload in application.dashboard_calls]
        assert "library_pdf" in operations
        assert "download_status" in operations
        assert "interests_put" in operations
        assert "settings_folder_test" in operations
        assert "settings_folder" in operations
        interests = next(
            payload
            for operation, payload in application.dashboard_calls
            if operation == "interests_put"
        )
        assert interests["keywords"] == [
            "derived geometry",
            "spectral sequence",
            "custom topology",
        ]
