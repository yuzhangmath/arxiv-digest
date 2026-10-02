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
        self.interest_values = {field: [] for field in self.interest_values}
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

    def interests(self, payload: dict[str, object]) -> dict[str, object]:
        result = super().interests(payload)
        raw = _fixture()["candidate_corpus"]
        categories = raw["categories"]
        papers = []
        for index in range(int(raw["visible_count"])):
            category = str(categories[index % len(categories)]["category"])
            label = category.replace("synthetic.", "Synthetic ").title()
            papers.append({
                "arxiv_id": f"2608.{41001 + index:05d}",
                "title": f"{label} candidate {index + 1:02d}",
            })
        result["categories"] = categories
        result["suggestions"] = {
            "categories": [],
            "seed_papers": papers,
            "keywords": [{"value": str(raw["recurring_keyword"])}],
            "phrases": [{"value": str(raw["recurring_phrase"])}],
            "authors": [{"name": str(raw["recurring_author"])}],
        }
        return result

    def update_interests(self, payload: dict[str, object]) -> dict[str, object]:
        projection_revision = self.review_projection_revision
        result = super().update_interests(payload)
        # Preference edits change the profile revision without changing category projection.
        self.review_projection_revision = projection_revision
        return result

    def review_date(self, payload: dict[str, object]) -> dict[str, object]:
        result = super().review_date(payload)
        result["cards"][0]["title"] = "Synthetic $K$-theory <script>not markup</script>"
        return result

    def handlers(self) -> dict[str, object]:
        handlers = super().handlers()
        raw = _fixture()["candidate_corpus"]
        categories = [
            {
                "category": str(item["category"]),
                "set_spec": str(item["set_spec"]),
                "label": str(item["category"]).replace("synthetic.", "Synthetic ").title(),
            }
            for item in raw["categories"]
        ]
        handlers.update({
            "categories": lambda payload: [
                item for item in categories
                if str(payload.get("q", "")).casefold() in item["label"].casefold()
            ],
            "review_finish": self.finish,
        })
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
        initial_categories = page.locator(".category-groups").element_handle()
        assert initial_categories is not None
        page.get_by_label("Search by category name or code").fill("Synthetic")
        page.get_by_role("button", name="Search", exact=True).click()
        # These labels also exist before Search replaces the category list.
        page.wait_for_function(
            "categories => !categories.isConnected", arg=initial_categories
        )
        page.get_by_text(category_setup_labels[0], exact=True).wait_for()
        assert page.locator('.suggestion-list input:checked').count() == 0
        assert application.submissions == []
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
            {"draft_revision": 4, "launcher_choice": "not_now"}
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
        assert [payload["step"] for payload in application.submissions] == [
            "categories", "coverage", "pdf_destination", "review",
        ]
        assert not any("/setup/corpus" in url or "/setup/candidates/" in url for url in requests)

        page.get_by_role("button", name="Interests", exact=True).click()
        page.get_by_role("heading", name="Interests", exact=True).wait_for()
        save = page.get_by_role("button", name="Update interests", exact=True)
        assert save.is_disabled()
        page.get_by_role("button", name="Refresh suggestions", exact=True).click()
        page.get_by_text("Suggestions refreshed", exact=False).wait_for()
        assert save.is_disabled()
        page.get_by_role("button", name="Add seed paper", exact=True).click()
        suggestion_rows = page.locator('[data-interest-add="seed_papers"] .interest-suggestion')
        assert suggestion_rows.count() == int(raw["visible_count"]) == 30
        assert suggestion_rows.locator('input:checked').count() == 0
        for label in category_labels:
            assert suggestion_rows.filter(has_text=label).count() == 10
        selected_checkbox = suggestion_rows.nth(1).locator('input[type="checkbox"]')
        selected_checkbox.focus()
        selected_checkbox.press("Space")
        assert selected_checkbox.is_checked()
        assert selected_checkbox.evaluate("checkbox => document.activeElement === checkbox")
        page.get_by_label("Add custom seed papers", exact=True).fill("2608.09999")
        page.get_by_role("button", name="Add custom seed paper", exact=True).click()

        page.get_by_role("button", name="Add terms", exact=True).click()
        page.get_by_text(str(raw["recurring_keyword"]), exact=True).click()
        page.get_by_text(str(raw["recurring_phrase"]), exact=True).click()
        page.get_by_label("Add custom term", exact=True).fill(str(explicit["custom_keyword"]))
        page.get_by_role("button", name="Add custom term", exact=True).click()
        page.get_by_label("Add custom term", exact=True).fill(str(explicit["custom_phrase"]))
        page.get_by_role("button", name="Add custom term", exact=True).click()
        page.get_by_role("button", name="Add author", exact=True).click()
        page.get_by_text(str(raw["recurring_author"]), exact=True).click()
        page.get_by_label("Add custom authors", exact=True).fill(str(explicit["custom_author"]))
        page.get_by_role("button", name="Add custom author", exact=True).click()
        assert not any(operation == "interests_put" for operation, _ in application.dashboard_calls)
        save.click()
        page.get_by_text("Interests updated", exact=False).wait_for()
        submitted = [payload for operation, payload in application.dashboard_calls if operation == "interests_put"]
        assert len(submitted) == 1
        assert submitted[0]["expected_revision"] == 5
        assert submitted[0]["seed_papers"] == ["2608.41002", "2608.09999"]
        assert submitted[0]["keywords"] == [raw["recurring_keyword"], explicit["custom_keyword"]]
        assert submitted[0]["phrases"] == [raw["recurring_phrase"], explicit["custom_phrase"]]
        assert submitted[0]["authors"] == [raw["recurring_author"], explicit["custom_author"]]
        assert not any(operation == "library_save" for operation, _ in application.dashboard_calls)
        page.get_by_role("button", name="Review", exact=True).click()

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
        assert page.locator("article").first.locator(".katex").count() == 1
        assert page.locator("article script").count() == 0
        assert "<script>not markup</script>" in page.locator("article").first.inner_text()
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
                "profile_revision": 6,
                "projection_revision": 9,
            }
        ]

        assert all(
            urlsplit(url).hostname in {"127.0.0.1", None}
            for url in requests
        )
