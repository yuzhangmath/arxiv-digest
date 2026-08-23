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

    def finish(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.finishes.append(dict(payload))
        return {"reviewed_count": 20, "through_revision": 77}

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
            papers.append(
                {
                    "suggestion_id": f"paper_suggestion_{index + 1}",
                    "title": f"{display_category} candidate {index + 1:02d}",
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
        page.get_by_text(category_labels[0], exact=True).wait_for()
        for label in category_labels:
            page.get_by_text(label, exact=True).click()
        _continue(page)
        page.get_by_role("button", name="Use recommended 30 days").click()
        _continue(page)
        page.get_by_role("button", name="Generate corpus").click()
        page.get_by_text("Corpus ready", exact=True).wait_for()
        _continue(page)

        page.get_by_text(
            f"{category_labels[0]} candidate 01", exact=True
        ).wait_for()
        suggestion_rows = page.locator(".suggestion-list label")
        assert suggestion_rows.count() == int(raw["visible_count"]) == 30
        for label in category_labels:
            assert suggestion_rows.filter(has_text=label).count() == 10
        selected_paper = f"{category_labels[0]} candidate 01"
        page.get_by_text(selected_paper, exact=True).click()
        page.get_by_role("button", name="Add custom paper id").click()
        page.locator(".custom-entries input").fill("2608.09999")
        _continue(page)

        page.get_by_text(str(raw["recurring_keyword"]), exact=True).click()
        page.get_by_text(str(raw["recurring_phrase"]), exact=True).click()
        page.get_by_role("button", name="Add custom keyword").click()
        page.locator(".custom-entries").nth(0).locator("input").fill(
            str(explicit["custom_keyword"])
        )
        page.get_by_role("button", name="Add custom phrase").click()
        page.locator(".custom-entries").nth(1).locator("input").fill(
            str(explicit["custom_phrase"])
        )
        _continue(page)

        page.get_by_text(str(raw["recurring_author"]), exact=True).click()
        page.get_by_role("button", name="Add custom author").click()
        page.locator(".custom-entries input").fill(
            str(explicit["custom_author"])
        )
        _continue(page)

        page.get_by_role("button", name="Use Documents").click()
        assert page.locator('input[value="documents"]').is_checked()
        assert page.locator('input[type="text"]').count() == 0
        page.get_by_role("button", name="Test selected destination").click()
        page.get_by_text("destination is writable", exact=False).wait_for()
        _continue(page)
        page.get_by_text("Categories", exact=True).wait_for()
        _continue(page)
        page.get_by_text("launcher", exact=True).wait_for()

        launcher_continue = page.get_by_role("button", name="Continue")
        assert launcher_continue.is_disabled()
        page.get_by_role("button", name="Not now").click()
        assert launcher_continue.is_enabled()
        launcher_continue.click()
        page.get_by_role("button", name="Start review").wait_for()

        assert application.launcher_calls == 0
        assert application.completions == [
            {"draft_revision": 8, "launcher_choice": "not_now"}
        ]
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
            "accepted_suggestion_ids": ["paper_suggestion_1"],
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

        page.get_by_text("200 papers across 10 dates", exact=False).wait_for()
        page.get_by_text("1 are newly discovered", exact=False).wait_for()
        page.get_by_role("button", name="Start review").click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()
        assert page.locator("article").count() == 20
        page.get_by_text("Page 1 of 10", exact=True).wait_for()

        page.get_by_role("button", name="Next page").click()
        page.get_by_text("Page 2 of 10", exact=True).wait_for()
        assert application.saved_anchor == 21
        page.reload()
        page.get_by_role("button", name="Start review").click()
        page.get_by_text("Page 2 of 10", exact=True).wait_for()

        page.get_by_role("button", name="Finish date").click()
        with page.expect_response(
            lambda response: response.url.endswith("/review/date/finish")
        ):
            page.get_by_role("button", name="Confirm finish").click()
        assert application.finishes == [
            {"date": "2026-07-31", "snapshot_revision": 77}
        ]

        assert all(
            urlsplit(url).hostname in {"127.0.0.1", None}
            for url in requests
        )
