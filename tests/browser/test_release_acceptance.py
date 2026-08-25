from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page

from arxiv_digest.web.server import LoopbackServer
from tests.browser.test_setup_review import (
    FixtureApplication,
    _static_assets,
    browser_page,
)


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "acceptance"


def _fixture() -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / "release.json").read_text(encoding="utf-8"))


class ReleaseFixtureApplication(FixtureApplication):
    def __init__(self) -> None:
        super().__init__()
        self.finishes: list[dict[str, object]] = []
        spec = _fixture()
        progress = spec["daily_list_progress"]
        confirmed = spec["confirmed_review"]
        raw = spec["candidate_corpus"]
        assert isinstance(progress, dict)
        assert isinstance(confirmed, dict)
        assert isinstance(raw, dict)
        raw_categories = raw["categories"]
        assert isinstance(raw_categories, list)
        categories = [str(item["category"]) for item in raw_categories]
        self.active_categories = categories
        self.review_support_categories = tuple(categories[:2])
        self.category_coverage_starts = {
            category: str(spec["synchronization"]["initial_coverage_start"])
            for category in categories
        }
        self.daily_list_target_dates = int(progress["target_dates"])
        self.daily_list_checked_dates = int(progress["checked_dates"])
        self.daily_list_dates_with_papers = int(progress["dates_with_papers"])
        self.daily_list_empty_dates = int(progress["empty_dates"])
        self.daily_list_failed_dates = int(progress["failed_dates"])
        self.daily_list_pending_dates = int(progress["pending_dates"])
        self.daily_list_unavailable_dates = int(progress["unavailable_dates"])
        self.review_profile_revision = int(confirmed["profile_revision"])
        self.review_projection_revision = int(confirmed["projection_revision"])
        self.review_unconfirmed_latest_version = int(
            confirmed["unconfirmed_latest_version"]
        )
        self.review_finish_next_later_unreviewed_date = str(
            confirmed["next_later_unreviewed_date"]
        )
        self.sync_running = True

    def finish(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.finishes.append(dict(payload))
        return {
            "reviewed_count": 20,
            "through_revision": 77,
            "next_later_unreviewed_date": (
                self.review_finish_next_later_unreviewed_date
            ),
        }

    def handlers(self) -> dict[str, object]:
        handlers = super().handlers()
        spec = _fixture()
        raw = spec["candidate_corpus"]
        assert isinstance(raw, dict)
        raw_categories = raw["categories"]
        assert isinstance(raw_categories, list)
        categories = [
            {
                "category": str(item["category"]),
                "set_spec": str(item["set_spec"]),
                "label": str(item["category"]).replace("synthetic.", "Synthetic ").title(),
            }
            for item in raw_categories
        ]
        papers = []
        for index in range(int(raw["visible_count"])):
            category = str(raw_categories[index % len(raw_categories)]["category"])
            display_category = category.replace("synthetic.", "Synthetic ").title()
            title = f"{display_category} candidate {index + 1:02d}"
            if index == 1:
                title = f"{display_category} $K$-theory candidate {index + 1:02d}"
            papers.append(
                {
                    "suggestion_id": f"paper_suggestion_{index + 1}",
                    "title": title,
                }
            )
        handlers.update(
            {
                "categories": lambda _payload: categories,
                "setup_candidate_papers": lambda _payload: {"items": papers},
                "setup_candidate_terms": lambda _payload: {
                    "keywords": [
                        {
                            "suggestion_id": "keyword_orchard",
                            "label": str(raw["recurring_keyword"]),
                        }
                    ],
                    "phrases": [
                        {
                            "suggestion_id": "phrase_spectral_garden",
                            "label": str(raw["recurring_phrase"]),
                        }
                    ],
                },
                "setup_candidate_authors": lambda _payload: {
                    "items": [
                        {
                            "suggestion_id": "author_crosscategory",
                            "label": str(raw["recurring_author"]),
                        }
                    ]
                },
                "review_finish": self.finish,
            }
        )
        return handlers


@contextmanager
def running_release_fixture() -> Iterator[
    tuple[LoopbackServer, ReleaseFixtureApplication]
]:
    application = ReleaseFixtureApplication()
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


def _continue(page: Page) -> None:
    page.get_by_role("button", name="Continue").click()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_release_setup_and_two_hundred_card_review_are_explicit_and_resumable(
    engine: str,
) -> None:
    spec = _fixture()
    raw = spec["candidate_corpus"]
    explicit = spec["explicit_profile"]
    assert isinstance(raw, dict)
    assert isinstance(explicit, dict)
    raw_categories = raw["categories"]
    assert isinstance(raw_categories, list)

    with running_release_fixture() as (server, application), browser_page(
        engine
    ) as page:
        requests: list[str] = []
        page.on("request", lambda request: requests.append(request.url))
        page.goto(server.launch_url("setup"))

        category_labels = [
            str(item["category"]).replace("synthetic.", "Synthetic ").title()
            for item in raw_categories
        ]
        category_setup_labels = [
            f"{label} · {item['category']}"
            for label, item in zip(category_labels, raw_categories)
        ]
        page.locator("details.category-group-more > summary").click()
        page.get_by_text(category_setup_labels[0], exact=True).wait_for()
        category_rows = page.locator(".suggestion-list label")
        first_category = category_rows.filter(
            has_text=category_setup_labels[0]
        ).locator('input[type="checkbox"]')
        first_category.focus()
        first_category.press("Space")
        assert first_category.is_checked()
        assert first_category.evaluate(
            "checkbox => document.activeElement === checkbox"
        )
        for label in category_setup_labels[1:]:
            page.get_by_text(label, exact=True).click()
        _continue(page)
        page.get_by_role("button", name="Use recommended 30 days").click()
        coverage_box = page.get_by_label("Coverage start").bounding_box()
        assert coverage_box is not None
        assert coverage_box["width"] <= 320, coverage_box
        _continue(page)
        page.get_by_role("button", name="Generate corpus").click()
        page.get_by_text("Corpus ready", exact=True).wait_for()
        page.get_by_role(
            "button", name="Use this corpus and continue", exact=True
        ).click()

        page.get_by_text(
            f"{category_labels[0]} candidate 01", exact=True
        ).wait_for()
        guidance_box = page.locator(".step-guidance").bounding_box()
        assert guidance_box is not None
        assert guidance_box["width"] <= 800, guidance_box
        search_box = page.get_by_label(
            "Search by title, author, or arXiv ID"
        ).bounding_box()
        search_button_box = page.get_by_role(
            "button", name="Search"
        ).bounding_box()
        assert search_box is not None
        assert search_button_box is not None
        assert (
            search_button_box["x"]
            >= search_box["x"] + search_box["width"] + 8
        ), (search_box, search_button_box)
        suggestion_rows = page.locator(".suggestion-list label")
        assert suggestion_rows.count() == int(raw["visible_count"]) == 30
        assert page.locator(".suggestion-list .katex").count() == 1
        row_boxes = suggestion_rows.evaluate_all(
            """rows => rows.map((row) => {
                const box = row.getBoundingClientRect();
                return {top: box.top, bottom: box.bottom};
            })"""
        )
        assert all(
            row_boxes[index]["top"] >= row_boxes[index - 1]["bottom"] + 4
            for index in range(1, len(row_boxes))
        ), row_boxes
        for label in category_labels:
            assert suggestion_rows.filter(has_text=label).count() == 10
        selected_row = suggestion_rows.nth(1)
        assert selected_row.locator(".katex").count() == 1
        selected_checkbox = selected_row.locator('input[type="checkbox"]')
        selected_checkbox.focus()
        selected_checkbox.press("Space")
        assert selected_checkbox.is_checked()
        assert selected_checkbox.evaluate(
            "checkbox => document.activeElement === checkbox"
        )
        add_custom_button = page.get_by_role(
            "button", name="Add custom paper id"
        )
        add_custom_box = add_custom_button.bounding_box()
        continue_box = page.get_by_role(
            "button", name="Continue with selected seed papers", exact=True
        ).bounding_box()
        assert add_custom_box is not None
        assert continue_box is not None
        assert (
            continue_box["y"]
            >= add_custom_box["y"] + add_custom_box["height"] + 8
        ), (add_custom_box, continue_box)
        add_custom_button.click()
        page.locator(".custom-entries input").fill("2608.09999")
        _continue(page)

        page.get_by_text(str(raw["recurring_keyword"]), exact=True).click()
        page.get_by_text(str(raw["recurring_phrase"]), exact=True).click()
        page.get_by_role("button", name="Add custom term").click()
        page.locator(".custom-entries input").nth(0).fill(
            str(explicit["custom_keyword"])
        )
        page.get_by_role("button", name="Add custom term").click()
        page.locator(".custom-entries input").nth(1).fill(
            str(explicit["custom_phrase"])
        )
        _continue(page)

        page.get_by_text(str(raw["recurring_author"]), exact=True).click()
        page.get_by_role("button", name="Add custom author").click()
        page.locator(".custom-entries input").fill(
            str(explicit["custom_author"])
        )
        _continue(page)

        page.get_by_role("button", name="Choose PDF folder").wait_for()
        assert page.get_by_role("button", name="Use Downloads").count() == 0
        assert page.get_by_role("button", name="Use Documents").count() == 0
        assert page.locator('input[type="text"]').count() == 0
        page.get_by_role("button", name="Choose PDF folder").click()
        page.get_by_text("Selected folder: Research PDFs", exact=True).wait_for()
        page.get_by_role("button", name="Test selected destination").click()
        page.get_by_text("selected folder is writable", exact=False).wait_for()
        _continue(page)
        page.get_by_text("Categories", exact=True).wait_for()
        assert page.locator(".setup-summary-destination-path").inner_text() == (
            "~/Documents/Research PDFs"
        )
        _continue(page)
        page.get_by_text("launcher", exact=True).wait_for()

        launcher_continue = page.get_by_role(
            "button", name="Finish setup", exact=True
        )
        assert launcher_continue.is_disabled()
        page.get_by_role("button", name="Not now").click()
        assert launcher_continue.is_enabled()
        stale_launcher_continue = launcher_continue.element_handle()
        assert stale_launcher_continue is not None
        launcher_continue.click()
        page.get_by_role("button", name="Start review").wait_for()
        stale_launcher_continue.evaluate("control => control.click()")
        page.wait_for_timeout(200)

        assert application.launcher_calls == 0
        assert application.completions == [
            {"draft_revision": 8, "launcher_choice": "not_now"}
        ]
        assert [
            payload
            for operation, payload in application.dashboard_calls
            if operation == "sync_start"
        ] == [{}]
        assert page.url.endswith("?view=review")
        assert page.evaluate("history.state") == {"view": "review"}
        assert page.get_by_role("button", name="Retry", exact=True).count() == 0
        categories_payload = next(
            payload
            for payload in application.submissions
            if payload.get("step") == "categories"
        )
        assert categories_payload["selections"] == raw_categories
        seed_payload = next(
            payload
            for payload in application.submissions
            if payload.get("step") == "seed_papers"
        )
        assert seed_payload == {
            "revision": 3,
            "step": "seed_papers",
            "accepted_suggestion_ids": ["paper_suggestion_2"],
            "custom_arxiv_ids": ["2608.09999"],
        }
        term_payload = next(
            payload
            for payload in application.submissions
            if payload.get("step") == "terms"
        )
        assert term_payload["accepted_keyword_suggestion_ids"] == [
            "keyword_orchard"
        ]
        assert term_payload["accepted_phrase_suggestion_ids"] == [
            "phrase_spectral_garden"
        ]
        assert term_payload["custom_keywords"] == [explicit["custom_keyword"]]
        assert term_payload["custom_phrases"] == [explicit["custom_phrase"]]
        author_payload = next(
            payload
            for payload in application.submissions
            if payload.get("step") == "authors"
        )
        assert author_payload["accepted_suggestion_ids"] == [
            "author_crosscategory"
        ]
        assert author_payload["custom_authors"] == [explicit["custom_author"]]

        progress_text = (
            "Checking historical daily lists: 18 of 32 dates checked · "
            "10 with papers · 8 empty · 2 failed · 12 remaining."
        )
        page.get_by_text(progress_text, exact=True).wait_for()
        progress = page.get_by_role("progressbar", name=progress_text)
        assert progress.get_attribute("value") == "18"
        assert progress.get_attribute("max") == "32"
        page.get_by_text(
            "200 unreviewed paper announcements are ready across 10 dates. "
            "Review starts with the oldest date.",
            exact=False,
        ).wait_for()
        page.get_by_text(
            "1 paper announcement was added to a previously finished date.",
            exact=False,
        ).wait_for()
        with application.lock:
            application.sync_running = False
        page.get_by_role("button", name="Start review").click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()
        assert page.locator("article").count() == 20
        review_text = page.locator("#content").inner_text()
        for forbidden in (
            "Current announcement",
            "current daily feed",
            "Inferred from version history",
            "Submission date (UTC; daily-list date unavailable)",
            "OAI",
        ):
            assert forbidden not in review_text
        unresolved = page.locator("article").nth(1)
        unresolved.get_by_text("Version not confirmed", exact=False).wait_for()
        unresolved.get_by_role(
            "link", name="Abstract on arXiv", exact=True
        ).wait_for()
        unresolved.get_by_role(
            "link", name="PDF on arXiv", exact=True
        ).wait_for()
        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/library/pdf")
        ):
            unresolved.get_by_role(
                "button",
                name="Download PDF",
                exact=True,
            ).click()
        unresolved.get_by_role(
            "button", name="Downloaded", exact=True
        ).wait_for()
        unresolved.get_by_text("PDF downloaded.", exact=True).wait_for()
        assert (
            "download_status",
            {"job_id": "download_1234"},
        ) in application.dashboard_calls
        completed_status_requests = sum(
            operation == "download_status"
            for operation, _payload in application.dashboard_calls
        )
        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/library/pdf")
        ):
            unresolved.get_by_role(
                "button",
                name="Save + PDF",
                exact=True,
            ).click()
        unresolved.get_by_role(
            "button", name="Saved + downloaded", exact=True
        ).wait_for()
        unresolved.get_by_text(
            "Paper saved and PDF downloaded.", exact=True
        ).wait_for()
        assert sum(
            operation == "download_status"
            for operation, _payload in application.dashboard_calls
        ) == completed_status_requests + 1
        unresolved.get_by_role("button", name="Save", exact=True).click()
        unresolved.get_by_role("button", name="Saved", exact=True).wait_for()
        assert (
            "library_save",
            {"arxiv_id": "2608.00002", "version": 4},
        ) in application.dashboard_calls
        assert (
            "library_pdf",
            {
                "arxiv_id": "2608.00002",
                "version": 4,
                "save_first": False,
                "save_version": None,
            },
        ) in application.dashboard_calls
        assert (
            "library_pdf",
            {
                "arxiv_id": "2608.00002",
                "version": 4,
                "save_first": True,
                "save_version": 4,
            },
        ) in application.dashboard_calls
        page.get_by_text(
            "200 unreviewed paper announcements for this date · Page 1 of 10",
            exact=True,
        ).wait_for()

        page.get_by_role("button", name="Next page").click()
        page.get_by_text(
            "200 unreviewed paper announcements for this date · Page 2 of 10",
            exact=True,
        ).wait_for()
        assert application.saved_anchor == 21
        page.reload()
        page.get_by_role("button", name="Start review").click()
        page.get_by_text(
            "200 unreviewed paper announcements for this date · Page 2 of 10",
            exact=True,
        ).wait_for()

        page.get_by_role("button", name="Next date", exact=True).click()
        page.get_by_role(
            "heading", name="Review 2026-08-31", exact=True
        ).wait_for()
        with application.lock:
            application.saved_anchor = 181
        page.get_by_role("navigation", name="Review dates").get_by_role(
            "button", name="Back to Review overview", exact=True
        ).click()
        page.get_by_role("button", name="Start review", exact=True).click()
        page.get_by_role(
            "heading", name="Review 2026-07-31", exact=True
        ).wait_for()

        page.get_by_role("button", name="Finish date").click()
        with page.expect_response(
            lambda response: response.url.endswith("/review/date/finish")
        ):
            page.get_by_role("button", name="Confirm finish").click()
        page.get_by_role(
            "heading", name="Review 2026-09-01", exact=True
        ).wait_for()
        assert application.finishes == [
            {
                "date": "2026-07-31",
                "snapshot_revision": 77,
                "profile_revision": 5,
                "projection_revision": 9,
            }
        ]

        assert all(
            urlsplit(url).hostname in {"127.0.0.1", None}
            for url in requests
        )
