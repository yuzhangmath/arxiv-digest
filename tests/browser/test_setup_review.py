from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, sync_playwright

from arxiv_digest.web.server import LoopbackServer, StaticAsset
from arxiv_digest.web.lifecycle import LifecycleController


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
        self.corpus_start_gate: threading.Event | None = None
        self.corpus_start_entered = threading.Event()
        self.corpus_start_requests = 0
        self.corpus_status_requests = 0
        self.corpus_status_gate: threading.Event | None = None
        self.corpus_status_entered = threading.Event()
        self.draft_requests = 0
        self.category_failures = 0
        self.setup_already_complete = False
        self.draft_gate_request: int | None = None
        self.draft_gate: threading.Event | None = None
        self.draft_gate_entered = threading.Event()
        self.quit_gate: threading.Event | None = None
        self.quit_entered = threading.Event()
        self.expose_corpus_job = False
        self.corpus_can_resume = False
        self.folder_pick_result: dict[str, object] = {
            "destination_choice": "picker_abcd1234",
            "display_name": "Research PDFs",
        }
        self.folder_test_result: dict[str, object] = {
            "tested_destination_token": "destination_12345678"
        }
        self.settings_folder_pick_result: dict[str, object] = {
            "destination_choice": "picker_settings1234",
            "display_name": "Research PDFs",
            "cancelled": False,
        }
        self.settings_folder_test_result: dict[str, object] = {
            "tested_destination_token": "destination_12345678"
        }
        self.settings_folder_save_failures = 0
        self.library_empty = False
        self.sync_starts_running = False
        self.sync_running = False
        self.sync_phase = "daily_list"
        self.sync_status_requests = 0
        self.sync_status_failures = 0
        self.sync_status_gate: threading.Event | None = None
        self.sync_status_entered = threading.Event()
        self.update_status: dict[str, object] = {
            "status": "current",
            "automatic_update": False,
            "installed_version": "0.2.1",
        }
        self.update_status_sequence: list[dict[str, object]] = []
        self.update_requests = 0
        self.settings_missing_exact_dates: list[str] = []
        self.settings_failed_daily_list_dates: list[str] = []
        self.settings_retryable_failed_daily_list_dates: list[str] = []
        self.daily_list_retry_total: int | None = None
        self.daily_list_retry_completed = 0
        self.daily_list_target_dates = 0
        self.daily_list_checked_dates = 0
        self.daily_list_dates_with_papers = 0
        self.daily_list_empty_dates = 0
        self.daily_list_failed_dates = 0
        self.daily_list_pending_dates = 0
        self.daily_list_unavailable_dates = 0
        self.review_ready = True
        self.review_profile_revision = 5
        self.review_projection_revision = 9
        self.review_support_categories = ("math.AG", "math.CO")
        self.review_unconfirmed_latest_version = 4
        self.active_categories = ["math.AG"]
        self.category_coverage_starts = {"math.AG": "2026-01-01"}
        self.review_summary_requests = 0
        self.review_summary_gate: threading.Event | None = None
        self.review_summary_entered = threading.Event()
        self.review_summary_returned = threading.Event()
        self.review_summary_failures = 0
        self.review_finish_requests = 0
        self.review_finish_next_later_unreviewed_date: str | None = None
        self.review_finish_gate: threading.Event | None = None
        self.review_finish_entered = threading.Event()
        self.review_finish_failures = 0
        self.review_finish_refresh_failures = 0
        self.review_finish_all_requests = 0
        self.review_finish_all_gate: threading.Event | None = None
        self.review_finish_all_entered = threading.Event()
        self.review_finish_all_failures = 0
        self.review_finish_all_refresh_failures = 0
        self.review_date_requests = 0
        self.review_date_gate_request: int | None = None
        self.review_date_gate: threading.Event | None = None
        self.review_date_gate_entered = threading.Event()
        self.review_metadata_tracks_sync = False
        self.missing_review_dates: set[str] = set()
        self.library_save_requests = 0
        self.library_save_gate: threading.Event | None = None
        self.library_save_entered = threading.Event()
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

    def start_corpus(self, _payload: dict[str, object]) -> dict[str, str]:
        with self.lock:
            self.corpus_start_requests += 1
            self.expose_corpus_job = True
        self.corpus_start_entered.set()
        if self.corpus_start_gate is not None:
            assert self.corpus_start_gate.wait(timeout=5)
        return {"job_id": "corpus_job_1234"}

    def corpus_job(self, _payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.corpus_status_requests += 1
            request_number = self.corpus_status_requests
            result = dict(self.candidate_job)
        if request_number == 1 and self.corpus_status_gate is not None:
            self.corpus_status_entered.set()
            assert self.corpus_status_gate.wait(timeout=5)
        return result

    def record_dashboard(
        self, operation: str, payload: dict[str, object], result: object
    ) -> object:
        with self.lock:
            self.dashboard_calls.append((operation, dict(payload)))
        return result

    def settings_folder_save(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.dashboard_calls.append(("settings_folder", dict(payload)))
            if self.settings_folder_save_failures > 0:
                self.settings_folder_save_failures -= 1
                raise ValueError("profile revision changed")
        return {"revision": 4}

    def draft(self, _payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.draft_requests += 1
            request_number = self.draft_requests
            already_complete = self.setup_already_complete
        if already_complete:
            error = ValueError("setup is complete; open Interests")
            error.code = "already_configured"  # type: ignore[attr-defined]
            raise error
        if (
            request_number == self.draft_gate_request
            and self.draft_gate is not None
        ):
            self.draft_gate_entered.set()
            assert self.draft_gate.wait(timeout=5)
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
            "corpus_can_resume": self.corpus_can_resume,
            "recommended_coverage_start": "2026-07-23",
        }
        if self.step == "review":
            value.update(
                {
                    "profile_summary": {
                        "categories": ["math.AG"],
                        "seed_papers": ["2608.01234", "2608.09999"],
                        "seed_paper_details": [
                            {
                                "arxiv_id": "2608.01234",
                                "title": "Selected geometry",
                            },
                            {
                                "arxiv_id": "2608.09999",
                                "title": "Custom seed paper",
                            },
                        ],
                        "keywords": ["derived geometry", "custom keyword"],
                        "phrases": ["mirror symmetry", "custom phrase"],
                        "authors": ["Ada Example", "Custom Author"],
                        "pdf_destination_kind": "custom",
                        "pdf_destination_display_path": (
                            "~/Documents/Research PDFs"
                        ),
                    },
                    "profile_summary_sha256": "b" * 64,
                }
            )
        if self.step == "candidate_corpus" and self.expose_corpus_job:
            value["corpus_job"] = dict(self.candidate_job)
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

    def status(self, _payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.sync_status_requests += 1
        self.sync_status_entered.set()
        if self.sync_status_gate is not None:
            assert self.sync_status_gate.wait(timeout=5)
        with self.lock:
            if self.sync_status_failures > 0:
                self.sync_status_failures -= 1
                raise RuntimeError("synthetic synchronization status failure")
            running = self.sync_running
            sync_phase = self.sync_phase
            retryable_dates = set(self.settings_retryable_failed_daily_list_dates)
            retry_total = (
                len(retryable_dates)
                if self.daily_list_retry_total is None
                else self.daily_list_retry_total
            )
            retry_completed = self.daily_list_retry_completed
            daily_list_progress = {
                "target_dates": self.daily_list_target_dates,
                "checked_dates": self.daily_list_checked_dates,
                "dates_with_papers": self.daily_list_dates_with_papers,
                "empty_dates": self.daily_list_empty_dates,
                "failed_dates": self.daily_list_failed_dates,
                "pending_dates": self.daily_list_pending_dates,
                "unavailable_dates": self.daily_list_unavailable_dates,
            }
        return {
            "state": "ready",
            "initialized": True,
            "sync": (
                {
                    "job_id": "sync_initial_1234",
                    "status": "running",
                    "complete": False,
                    "failed": False,
                    "phase": sync_phase,
                }
                if running
                else None
            ),
            "daily_list_retry": {
                "status": (
                    "running"
                    if running and sync_phase == "daily_list" and retry_total
                    else "idle"
                ),
                "completed": retry_completed if running else 0,
                "total": retry_total if running else len(retryable_dates),
            },
            "daily_list_progress": daily_list_progress,
        }

    def start_sync(self, payload: dict[str, object]) -> dict[str, str]:
        self.record_dashboard(
            "sync_start", payload, {"job_id": "sync_initial_1234"}
        )
        with self.lock:
            if self.sync_starts_running:
                self.sync_running = True
                self.sync_phase = "daily_list"
                if payload.get("retry_failed_dates") is True:
                    self.daily_list_retry_total = len(
                        set(self.settings_retryable_failed_daily_list_dates)
                    )
                    self.daily_list_retry_completed = 0
                else:
                    self.review_ready = False
        return {"job_id": "sync_initial_1234"}

    def settings(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.dashboard_calls.append(("settings_get", dict(payload)))
            synchronizing = self.sync_running
            failed_dates = list(self.settings_failed_daily_list_dates)
            retryable_failed_dates = list(
                self.settings_retryable_failed_daily_list_dates
            )
            target = max(
                self.daily_list_target_dates,
                len(set(self.settings_missing_exact_dates)),
            )
            failed = max(self.daily_list_failed_dates, len(set(failed_dates)))
            checked = max(self.daily_list_checked_dates, failed)
            with_papers = self.daily_list_dates_with_papers
            empty = self.daily_list_empty_dates
            pending = max(
                self.daily_list_pending_dates,
                target - with_papers - empty - failed,
            )
        return {
            "revision": 3,
            "online": False,
            "synchronizing": synchronizing,
            "daily_list_retry": {
                "status": (
                    "running"
                    if synchronizing and self.daily_list_retry_total
                    else "idle"
                ),
                "completed": (
                    self.daily_list_retry_completed if synchronizing else 0
                ),
                "total": (
                    self.daily_list_retry_total
                    if synchronizing and self.daily_list_retry_total is not None
                    else len(set(retryable_failed_dates))
                ),
            },
            "pdf_destination": {"kind": "downloads"},
            "coverage_min": "2026-07-01",
            "coverage_max": "2026-08-24",
            "metadata_sync": {
                "checkpoint_count": 1,
                "categories": [
                    {
                        "category": "math.AG",
                        "synchronized_through": "2026-08-21",
                        "error_codes": ["offline"],
                    }
                ],
            },
            "daily_list_coverage": {
                "target": target,
                "checked": checked,
                "with_papers": with_papers,
                "empty": empty,
                "failed": failed,
                "pending": pending,
                "unavailable": self.daily_list_unavailable_dates,
                "categories": [
                    {
                        "category": "math.AG",
                        "coverage_start": "2026-07-01",
                        "target": target,
                        "checked": checked,
                        "with_papers": with_papers,
                        "empty": empty,
                        "failed": failed,
                        "pending": pending,
                        "unavailable": self.daily_list_unavailable_dates,
                        "error_codes": (
                            ["catchup_fetch_failed"] if failed else []
                        ),
                        "retryable_failed_dates": retryable_failed_dates,
                    }
                ],
            },
            "version_resolution": {
                "canonical_event_count": 200,
                "atom_confirmed": 180,
                "chronology_matched": 19,
                "unconfirmed": 1,
            },
            "candidate_cache": {"status": "ready", "file_count": 1},
            "library": {"saved_paper_count": 1},
            "pdf_presence": {"downloaded_pdf_count": 0},
        }

    def _active_support(self, day: str) -> list[str]:
        return [
            category
            for category in self.review_support_categories
            if category in self.active_categories
            and self.category_coverage_starts.get(category, "9999-12-31") <= day
        ]

    def review_summary(self, _payload: dict[str, object]) -> dict[str, object]:
        # A real summary query can finish after the selected-date request. The
        # shell must not start both when Calendar chooses a date.
        with self.lock:
            self.review_summary_requests += 1
        self.review_summary_entered.set()
        if self.review_summary_gate is not None:
            assert self.review_summary_gate.wait(timeout=5)
        time.sleep(0.15)
        with self.lock:
            if self.review_summary_failures > 0:
                self.review_summary_failures -= 1
                raise RuntimeError("synthetic review summary failure")
            ready = self.review_ready and bool(
                self._active_support("2026-07-31")
            )
            refresh_failures = (
                self.review_finish_refresh_failures
                + self.review_finish_all_refresh_failures
            )
            if not ready and refresh_failures > 0:
                if self.review_finish_refresh_failures > 0:
                    self.review_finish_refresh_failures -= 1
                else:
                    self.review_finish_all_refresh_failures -= 1
                raise RuntimeError("synthetic review refresh failure")
        self.review_summary_returned.set()
        if not ready:
            return {
                "unreviewed_dates": 0,
                "unreviewed_papers": 0,
                "newly_discovered": 0,
                "oldest_unreviewed_date": None,
                "snapshot_revision": 77,
                "profile_revision": self.review_profile_revision,
                "projection_revision": self.review_projection_revision,
            }
        return {
            "unreviewed_dates": 10,
            "unreviewed_papers": 200,
            "newly_discovered": 1,
            "oldest_unreviewed_date": "2026-07-31",
            "snapshot_revision": 77,
            "profile_revision": self.review_profile_revision,
            "projection_revision": self.review_projection_revision,
        }

    def finish_all_reviews(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        with self.lock:
            self.review_finish_all_requests += 1
            self.review_ready = False
            self.dashboard_calls.append(("review_finish_all", dict(payload)))
        self.review_finish_all_entered.set()
        if self.review_finish_all_gate is not None:
            assert self.review_finish_all_gate.wait(timeout=5)
        with self.lock:
            if self.review_finish_all_failures > 0:
                self.review_finish_all_failures -= 1
                raise RuntimeError("synthetic finish-all failure")
        return {
            "reviewed_count": 200,
            "through_revision": 77,
        }

    def finish_review_date(
        self, payload: dict[str, object]
    ) -> dict[str, object]:
        with self.lock:
            self.review_finish_requests += 1
            self.dashboard_calls.append(("review_finish", dict(payload)))
            if self.review_finish_refresh_failures > 0:
                self.review_ready = False
        self.review_finish_entered.set()
        if self.review_finish_gate is not None:
            assert self.review_finish_gate.wait(timeout=5)
        with self.lock:
            if self.review_finish_failures > 0:
                self.review_finish_failures -= 1
                raise RuntimeError("synthetic finish failure")
        return {
            "reviewed_count": 20,
            "through_revision": 77,
            "next_later_unreviewed_date": (
                self.review_finish_next_later_unreviewed_date
            ),
        }

    def save_to_library(self, payload: dict[str, object]) -> dict[str, bool]:
        with self.lock:
            self.library_save_requests += 1
            request_number = self.library_save_requests
            self.dashboard_calls.append(("library_save", dict(payload)))
        if request_number == 1 and self.library_save_gate is not None:
            self.library_save_entered.set()
            assert self.library_save_gate.wait(timeout=5)
        return {"saved": True}

    def review_date(self, payload: dict[str, object]) -> dict[str, object]:
        day = str(payload["date"])
        with self.lock:
            self.review_date_requests += 1
            self.dashboard_calls.append(("review_date", dict(payload)))
            request_number = self.review_date_requests
            recovered_metadata = (
                self.review_metadata_tracks_sync and not self.sync_running
            )
        if (
            request_number == self.review_date_gate_request
            and self.review_date_gate is not None
        ):
            self.review_date_gate_entered.set()
            assert self.review_date_gate.wait(timeout=5)
        if day in self.missing_review_dates:
            raise KeyError(day)
        active_support = self._active_support(day)
        if not active_support:
            raise KeyError(day)
        from_start = payload.get("from_start") is True
        requested_anchor = payload.get("anchor_event_id")
        anchor = (
            1
            if from_start
            else int(requested_anchor or self.saved_anchor or 1)
        )
        page_number = min(10, max(1, (anchor - 1) // 20 + 1))
        start = (page_number - 1) * 20 + 1
        cards = []
        for event_id in range(start, start + 20):
            tier = "top" if event_id == start else "possible" if event_id == start + 1 else "other"
            title = f"Paper {event_id} <script>not markup</script>"
            is_carlsson = event_id == start + 1
            carlsson_is_resolved = is_carlsson and recovered_metadata
            if is_carlsson:
                title = (
                    "Carlsson's Conjecture and the Generalized Total Rank Conjecture "
                    "in Characteristic Two"
                )
            cards.append(
                {
                    "event_id": event_id,
                    "arxiv_id": f"2608.{event_id:05d}",
                    "resolved_announcement_version": (
                        None if is_carlsson and not carlsson_is_resolved else 2
                    ),
                    "latest_known_version": (
                        self.review_unconfirmed_latest_version
                        if is_carlsson
                        else 2
                    ),
                    "version_resolution": (
                        "unconfirmed"
                        if is_carlsson and not carlsson_is_resolved
                        else "chronology_matched"
                        if is_carlsson
                        else "atom_confirmed"
                    ),
                    "version_label": (
                        "Version not confirmed"
                        if is_carlsson and not carlsson_is_resolved
                        else "Version v2"
                    ),
                    "title": title,
                    "authors": ["Ada Example"],
                    "abstract": "An accessible collapsed abstract.",
                    "daily_list_date": day,
                    "support_categories": active_support,
                    "subjects": list(self.review_support_categories),
                    "event_label": "Replacement" if is_carlsson else None,
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
            "profile_revision": self.review_profile_revision,
            "projection_revision": self.review_projection_revision,
            "anchor_event_id": start,
            "previous_anchor_event_id": None if page_number == 1 else start - 20,
            "next_anchor_event_id": None if page_number == 10 else start + 20,
            "previous_date": "2026-06-30",
            "next_date": "2026-08-31",
            "page_number": page_number,
            "page_count": 10,
            "total_cards": 200,
        }

    def record_position(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.saved_anchor = int(payload["anchor_event_id"])
            self.dashboard_calls.append(("review_position", dict(payload)))
        return dict(payload)

    def categories(self, payload: dict[str, object]) -> list[dict[str, str]]:
        with self.lock:
            fail = self.category_failures > 0
            if fail:
                self.category_failures -= 1
        if fail:
            raise RuntimeError("synthetic category search failure")
        values = [
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
            {
                "category": "stat.ML",
                "set_spec": "arXiv:stat.ML",
                "label": "Machine Learning",
            },
        ]
        query = str(payload.get("q", "")).casefold()
        return [
            item
            for item in values
            if not query
            or query
            in f"{item['category']} {item['set_spec']} {item['label']}".casefold()
        ]

    @staticmethod
    def _set_spec(category: str) -> str:
        return f"arXiv:{category}"

    def review_calendar(self, payload: dict[str, object]) -> list[dict[str, object]]:
        with self.lock:
            visible = bool(self._active_support("2026-08-03"))
        if not visible:
            return []
        entries = [
            {
                "day": "2026-08-01",
                "total_papers": 14,
                "unreviewed_papers": 0,
                "newly_discovered": 0,
                "finished": True,
            },
            {
                "day": "2026-08-02",
                "total_papers": 16,
                "unreviewed_papers": 16,
                "newly_discovered": 0,
                "finished": False,
            },
            {
                "day": "2026-08-03",
                "total_papers": 4,
                "unreviewed_papers": 2,
                "newly_discovered": 1,
                "finished": False,
            },
            {
                "day": "2026-09-01",
                "total_papers": 5,
                "unreviewed_papers": 5,
                "newly_discovered": 0,
                "finished": False,
            },
        ]
        start = str(payload["start"])
        end = str(payload["end"])
        return [entry for entry in entries if start <= str(entry["day"]) <= end]

    def interests(self, payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.dashboard_calls.append(("interests_get", dict(payload)))
            active = list(self.active_categories)
            revision = self.review_profile_revision
        known = ("math.AG", "math.CO", "stat.ML", "math.NT")
        return {
            "revision": revision,
            "categories": [
                {
                    "category": category,
                    "set_spec": self._set_spec(category),
                }
                for category in active
            ],
            "coverage_min": "2026-07-01",
            "coverage_max": "2026-08-24",
            "keywords": ["derived geometry"],
            "phrases": ["mirror symmetry"],
            "authors": ["Ada Example"],
            "seed_papers": ["2608.01234"],
            "seed_paper_details": [
                {
                    "arxiv_id": "2608.01234",
                    "title": "Selected geometry",
                    "authors": ["Ada Example"],
                }
            ],
            "suggestions_generated_at": "2026-08-22T12:00:00Z",
            "suggestions": {
                "categories": [
                    {
                        "category": category,
                        "set_spec": self._set_spec(category),
                    }
                    for category in known
                    if category not in active
                ],
                "keywords": [{"value": "spectral sequence"}],
                "phrases": [],
                "authors": [],
                "seed_papers": [],
            },
        }

    def update_interests(self, payload: dict[str, object]) -> dict[str, object]:
        selections = payload.get("categories", [])
        if not isinstance(selections, list):
            raise TypeError("categories must be a list")
        selected = [
            str(item["category"])
            for item in selections
            if isinstance(item, dict) and "category" in item
        ]
        configs = payload.get("category_configs", [])
        if not isinstance(configs, list):
            raise TypeError("category_configs must be a list")
        configured_starts = {
            str(item["category"]): str(item["coverage_start"])
            for item in configs
            if isinstance(item, dict)
            and "category" in item
            and "coverage_start" in item
        }
        with self.lock:
            prior = set(self.active_categories)
            for category in set(selected) - prior:
                if category not in configured_starts:
                    raise ValueError("new categories require coverage")
                self.category_coverage_starts[category] = configured_starts[category]
            self.active_categories = selected
            self.review_profile_revision += 1
            self.review_projection_revision += 1
            self.dashboard_calls.append(("interests_put", dict(payload)))
            revision = self.review_profile_revision
        return {"revision": revision}

    def release_update(self, _payload: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.update_requests += 1
            if self.update_status_sequence:
                return dict(self.update_status_sequence.pop(0))
            return dict(self.update_status)

    def handlers(self) -> dict[str, object]:
        empty = lambda _payload: {}
        return {
            "status": self.status,
            "update": self.release_update,
            "categories": self.categories,
            "setup_draft_get": self.draft,
            "setup_draft_put": self.update_draft,
            "setup_corpus": self.start_corpus,
            "setup_job": self.corpus_job,
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
                    },
                    *(
                        {
                            "suggestion_id": f"phrase_suggestion_{index}",
                            "label": label,
                        }
                        for index, label in enumerate(
                            (
                                "k theory",
                                "homotopy theory",
                                "persistent homology",
                                "homotopy type",
                                "homotopy groups",
                                "homotopy equivalent",
                                "data analysis",
                                "vector bundles",
                                "spectral sequence",
                                "topological data",
                                "simplicial complexes",
                                "topological data analysis",
                                "characteristicallylongtoken extraordinarilylongtoken topology",
                            ),
                            start=2,
                        )
                    ),
                ],
            },
            "setup_candidate_authors": lambda _payload: {
                "items": [
                    {
                        "suggestion_id": "author_suggestion_1",
                        "label": "Ada Example",
                    },
                    *(
                        {
                            "suggestion_id": f"author_suggestion_{index}",
                            "label": label,
                        }
                        for index, label in enumerate(
                            (
                                "Nova Recurring",
                                "Casey Crosscategory",
                                "Morgan Manual",
                                "Taylor Spectral",
                                "Jordan Homotopy",
                                "Avery Topological",
                                "Riley Simplicial",
                                "Cameron Persistent",
                                "Quinn Mathematical",
                                "Characteristicallylonggivenname Extraordinarilylongfamilyname",
                            ),
                            start=2,
                        )
                    ),
                ]
            },
            "setup_folder_test": lambda _payload: dict(self.folder_test_result),
            "setup_folder_pick": lambda _payload: dict(self.folder_pick_result),
            "setup_complete": self.complete,
            "sync_start": self.start_sync,
            "tabs_connect": empty,
            "tabs_heartbeat": empty,
            "tabs_disconnect": empty,
            "review_summary": self.review_summary,
            "review_finish_all": self.finish_all_reviews,
            "review_calendar": self.review_calendar,
            "review_date": self.review_date,
            "review_position": self.record_position,
            "review_finish": self.finish_review_date,
            "library_save": self.save_to_library,
            "library": lambda payload: self.record_dashboard(
                "library",
                payload,
                {
                    "query": payload.get("q", ""),
                    "limit": 20,
                    "offset": int(payload.get("offset", 0)),
                    "previous_offset": None,
                    "next_offset": None,
                    "entries": [] if self.library_empty else [
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
            "interests_get": self.interests,
            "interests_put": self.update_interests,
            "settings_get": self.settings,
            "settings_doctor": lambda payload: self.record_dashboard(
                "settings_doctor",
                payload,
                {
                    "application_version": "0.2.0",
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
                dict(self.settings_folder_test_result),
            ),
            "settings_folder": self.settings_folder_save,
            "settings_folder_open": lambda payload: self.record_dashboard(
                "settings_folder_open", payload, {"status": "opened"}
            ),
            "settings_folder_pick": lambda payload: self.record_dashboard(
                "settings_folder_pick",
                payload,
                dict(self.settings_folder_pick_result),
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


class FixtureLifecycle(LifecycleController):
    def __init__(self, application: FixtureApplication) -> None:
        super().__init__()
        self.application = application

    def request_quit(self) -> None:
        self.application.quit_entered.set()
        if self.application.quit_gate is not None:
            assert self.application.quit_gate.wait(timeout=5)
        super().request_quit()


@contextmanager
def running_fixture() -> Iterator[tuple[LoopbackServer, FixtureApplication]]:
    application = FixtureApplication()
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


def navigate_with_history(page: Page, view: str) -> None:
    page.evaluate(
        """
        view => {
          history.pushState({ view }, "", `?view=${encodeURIComponent(view)}`);
          dispatchEvent(new PopStateEvent("popstate", { state: { view } }));
        }
        """,
        view,
    )


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_startup_shows_a_link_when_a_new_release_is_available(engine: str) -> None:
    with (
        running_fixture() as (server, application),
        browser_page(engine) as page,
    ):
        available = {
            "status": "available_manual",
            "automatic_update": False,
            "installed_version": "0.2.1",
            "available_version": "0.3.0",
            "release_notes_url": (
                "https://github.com/yuzhangmath/arxiv-digest/"
                "releases/tag/v0.3.0"
            ),
        }
        application.update_status = available
        application.update_status_sequence = [
            {"status": "checking", "automatic_update": False},
            available,
        ]

        page.goto(server.launch_url("setup"))

        link = page.get_by_role("link", name="View update instructions")
        link.wait_for()
        assert application.update_requests >= 2
        assert link.get_attribute("href") == (
            "https://github.com/yuzhangmath/arxiv-digest/releases/tag/v0.3.0"
        )
        assert link.get_attribute("target") == "_blank"
        assert link.get_attribute("rel") == "noopener noreferrer"
        assert link.get_attribute("aria-label") == (
            "View update instructions (opens in a new tab)"
        )

        for color_scheme in ("light", "dark"):
            page.emulate_media(color_scheme=color_scheme)
            link.focus()
            contrasts = link.evaluate(
                """element => {
                  const channel = value => {
                    const normalized = value / 255;
                    return normalized <= 0.04045
                      ? normalized / 12.92
                      : ((normalized + 0.055) / 1.055) ** 2.4;
                  };
                  const luminance = value => {
                    const channels = value.match(/[0-9.]+/g)
                      .slice(0, 3).map(Number).map(channel);
                    return 0.2126 * channels[0] +
                      0.7152 * channels[1] + 0.0722 * channels[2];
                  };
                  const ratio = (first, second) =>
                    (Math.max(first, second) + 0.05) /
                    (Math.min(first, second) + 0.05);
                  const noticeStyle = getComputedStyle(element.closest("aside"));
                  const linkStyle = getComputedStyle(element);
                  const background = luminance(noticeStyle.backgroundColor);
                  return {
                    text: ratio(luminance(linkStyle.color), background),
                    focus: ratio(luminance(linkStyle.outlineColor), background),
                  };
                }"""
            )
            assert contrasts["text"] >= 4.5
            assert contrasts["focus"] >= 3


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
@pytest.mark.parametrize(("width", "columns"), [(360, 1), (1280, 2)])
def test_category_choices_are_spaced_responsive_cards(
    engine: str,
    width: int,
    columns: int,
) -> None:
    with running_fixture() as (server, _application), browser_page(engine) as page:
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(server.launch_url("setup"))
        page.get_by_text("Algebraic Geometry · math.AG", exact=True).wait_for()

        rows = page.locator(
            '.setup-view[data-step="categories"] '
            ".category-group-mathematics .suggestion-list .suggestion"
        )
        assert rows.count() == 2
        boxes = [rows.nth(index).bounding_box() for index in range(rows.count())]
        assert all(box is not None for box in boxes)
        first, second = boxes
        assert first is not None
        assert second is not None
        assert first["height"] >= 40
        assert second["height"] >= 40
        if columns == 1:
            assert abs(first["x"] - second["x"]) < 1
            assert second["y"] >= first["y"] + first["height"] + 6
        else:
            assert abs(first["y"] - second["y"]) < 1
            assert second["x"] >= first["x"] + first["width"] + 6
        assert page.evaluate(
            "document.documentElement.scrollWidth <= "
            "document.documentElement.clientWidth"
        )


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_terms_guidance_uses_available_width_responsively(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.revision = 4
        application.step = "keywords_and_phrases"
        page.set_viewport_size({"width": 1161, "height": 900})
        page.goto(server.launch_url("setup"))

        guidance = page.get_by_text(
            "Optional. Choose or add terms that describe work you want to prioritize. "
            "You can change them later in Interests.",
            exact=True,
        )
        guidance.wait_for()
        line_count = guidance.evaluate(
            """
            element => {
              const range = document.createRange();
              range.selectNodeContents(element);
              return new Set(
                [...range.getClientRects()].map((rect) => Math.round(rect.top)),
              ).size;
            }
            """
        )

        assert line_count == 1
        page.set_viewport_size({"width": 360, "height": 900})
        mobile_line_count = guidance.evaluate(
            """
            element => {
              const range = document.createRange();
              range.selectNodeContents(element);
              return new Set(
                [...range.getClientRects()].map((rect) => Math.round(rect.top)),
              ).size;
            }
            """
        )
        assert mobile_line_count > 1
        assert page.evaluate(
            "document.documentElement.scrollWidth <= "
            "document.documentElement.clientWidth"
        )


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_term_suggestions_wrap_only_between_complete_options(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.revision = 4
        application.step = "keywords_and_phrases"
        page.set_viewport_size({"width": 720, "height": 900})
        page.goto(server.launch_url("setup"))

        short_phrase = page.get_by_text("spectral sequence", exact=True)
        long_phrase = page.get_by_text(
            "characteristicallylongtoken extraordinarilylongtoken topology",
            exact=True,
        )
        short_phrase.wait_for()

        def text_line_count(label: Locator) -> int:
            return label.evaluate(
                """
                element => {
                  const range = document.createRange();
                  range.selectNodeContents(element);
                  return new Set(
                    [...range.getClientRects()].map((rect) => Math.round(rect.top)),
                  ).size;
                }
                """
            )

        assert text_line_count(short_phrase) == 1
        page.set_viewport_size({"width": 360, "height": 900})
        assert text_line_count(short_phrase) == 1
        assert text_line_count(long_phrase) > 1
        assert page.locator(
            '.setup-view[data-step="terms"] .suggestion'
        ).evaluate_all(
            """
            options => options.every((option) => {
              const checkbox = option.querySelector('input').getBoundingClientRect();
              const label = option.querySelector('span').getBoundingClientRect();
              return Math.min(checkbox.bottom, label.bottom) >
                Math.max(checkbox.top, label.top);
            })
            """
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= "
            "document.documentElement.clientWidth"
        )


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_custom_terms_have_contextual_names_and_preserve_focus(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.revision = 4
        application.step = "keywords_and_phrases"
        page.goto(server.launch_url("setup"))

        add = page.get_by_role("button", name="Add custom term", exact=True)
        add.click()
        first = page.get_by_role("textbox", name="Custom term 1", exact=True)
        assert first.count() == 1
        assert first.evaluate("input => document.activeElement === input")
        page.get_by_role(
            "button", name="Remove custom term 1", exact=True
        ).wait_for()

        add.click()
        second = page.get_by_role("textbox", name="Custom term 2", exact=True)
        assert second.evaluate("input => document.activeElement === input")
        page.get_by_role(
            "button", name="Remove custom term 2", exact=True
        ).click()
        assert first.evaluate("input => document.activeElement === input")

        page.get_by_role(
            "button", name="Remove custom term 1", exact=True
        ).click()
        assert add.evaluate("button => document.activeElement === button")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_author_suggestions_wrap_only_between_complete_options(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.revision = 5
        application.step = "authors"
        page.set_viewport_size({"width": 720, "height": 900})
        page.goto(server.launch_url("setup"))

        short_name = page.get_by_text("Taylor Spectral", exact=True)
        long_name = page.get_by_text(
            "Characteristicallylonggivenname Extraordinarilylongfamilyname",
            exact=True,
        )
        short_name.wait_for()

        def text_line_count(label: Locator) -> int:
            return label.evaluate(
                """
                element => {
                  const range = document.createRange();
                  range.selectNodeContents(element);
                  return new Set(
                    [...range.getClientRects()].map((rect) => Math.round(rect.top)),
                  ).size;
                }
                """
            )

        assert text_line_count(short_name) == 1
        page.set_viewport_size({"width": 360, "height": 900})
        assert text_line_count(short_name) == 1
        assert text_line_count(long_name) > 1
        assert page.locator(
            '.setup-view[data-step="authors"] .suggestion'
        ).evaluate_all(
            """
            options => options.every((option) => {
              const checkbox = option.querySelector('input').getBoundingClientRect();
              const label = option.querySelector('span').getBoundingClientRect();
              return Math.min(checkbox.bottom, label.bottom) >
                Math.max(checkbox.top, label.top);
            })
            """
        )
        assert page.evaluate(
            "document.documentElement.scrollWidth <= "
            "document.documentElement.clientWidth"
        )


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_category_search_groups_results_and_preserves_selected_counts(
    engine: str,
) -> None:
    with running_fixture() as (server, _application), browser_page(engine) as page:
        page.goto(server.launch_url("setup"))
        page.get_by_role(
            "heading", name="Choose categories to monitor", exact=True
        ).wait_for()
        more = page.locator("details.category-group-more")
        assert not more.get_attribute("open")

        search = page.get_by_label("Search by category name or code")
        search.fill("AG")
        page.get_by_role("button", name="Search", exact=True).click()
        page.get_by_text("Algebraic Geometry · math.AG", exact=True).wait_for()
        page.get_by_text("Combinatorics · math.CO", exact=True).wait_for(
            state="detached"
        )

        search = page.get_by_label("Search by category name or code")
        search.fill("ML")
        page.get_by_role("button", name="Search", exact=True).click()
        page.get_by_text("Machine Learning · stat.ML", exact=True).wait_for()
        more = page.locator("details.category-group-more")
        assert more.get_attribute("open") is not None
        page.get_by_text("Machine Learning · stat.ML", exact=True).click()
        page.get_by_role(
            "button", name="Continue with 1 selected category", exact=True
        ).wait_for()
        assert more.locator("summary").inner_text() == "More categories"

        search = page.get_by_label("Search by category name or code")
        search.fill("")
        page.get_by_role("button", name="Search", exact=True).click()
        page.get_by_text("Combinatorics · math.CO", exact=True).wait_for()
        more = page.locator("details.category-group-more")
        assert more.get_attribute("open") is None
        assert more.locator("summary").inner_text() == "More categories"
        more.locator("summary").click()
        assert more.get_by_role(
            "checkbox", name="Machine Learning · stat.ML"
        ).is_checked()
        page.get_by_text("Algebraic Geometry · math.AG", exact=True).click()
        page.get_by_role(
            "button", name="Continue with 2 selected categories", exact=True
        ).wait_for()
        page.get_by_text("Algebraic Geometry · math.AG", exact=True).click()
        more.get_by_text("Machine Learning · stat.ML", exact=True).click()
        continue_button = page.get_by_role("button", name="Continue", exact=True)
        assert continue_button.is_disabled()
        assert page.get_by_text(
            "Select at least one category to continue.", exact=True
        ).is_visible()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_primary_navigation_is_hidden_and_guarded_during_setup(engine: str) -> None:
    with running_fixture() as (server, _application), browser_page(engine) as page:
        page.goto(server.launch_url("setup"))
        page.get_by_role(
            "heading", name="Choose categories to monitor", exact=True
        ).wait_for()
        navigation = page.locator(".primary-navigation")
        assert navigation.is_hidden()
        assert page.get_by_role("button", name="Settings", exact=True).count() == 0
        assert page.get_by_role("button", name="Quit", exact=True).is_visible()

        page.get_by_text("Algebraic Geometry · math.AG", exact=True).click()
        page.locator('.primary-navigation [data-view="settings"]').evaluate(
            "control => control.click()"
        )
        assert page.url.endswith("?view=setup")
        assert page.get_by_role(
            "heading", name="Choose categories to monitor", exact=True
        ).is_visible()
        assert page.get_by_role(
            "checkbox", name="Algebraic Geometry · math.AG"
        ).is_checked()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_stale_setup_route_is_replaced_with_interests(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.setup_already_complete = True

        page.goto(server.launch_url("setup"))

        page.get_by_role("heading", name="Interests", exact=True).wait_for()
        assert page.url.endswith("?view=interests")
        assert page.evaluate("history.state?.view") == "interests"
        assert page.get_by_role("button", name="Retry", exact=True).count() == 0
        page.reload()
        page.get_by_role("heading", name="Interests", exact=True).wait_for()


def test_setup_retry_redirects_if_setup_completed_after_the_failure() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        page.goto(server.launch_url("setup"))
        page.get_by_role(
            "heading", name="Choose categories to monitor", exact=True
        ).wait_for()
        with application.lock:
            application.category_failures = 1
        page.get_by_label("Search by category name or code").fill("topology")
        page.get_by_role("button", name="Search", exact=True).click()
        retry = page.get_by_role("button", name="Retry", exact=True)
        retry.wait_for()
        with application.lock:
            application.setup_already_complete = True

        retry.click()

        page.get_by_role("heading", name="Interests", exact=True).wait_for()
        assert page.url.endswith("?view=interests")
        assert page.get_by_role("button", name="Retry", exact=True).count() == 0
        assert page_errors == []


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_complete_setup_records_only_explicit_selections_and_not_now(
    engine: str,
) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        page.goto(server.launch_url("setup"))
        page.get_by_text("Algebraic Geometry · math.AG", exact=True).wait_for()
        assert page.get_by_text(
            "Algebraic Geometry · math.AG", exact=True
        ).count() == 1, page.locator("body").inner_text()
        category_search = page.get_by_label("Search by category name or code")
        category_search.fill("geometry")
        page.get_by_role("button", name="Search").click()
        page.get_by_text("Algebraic Geometry · math.AG", exact=True).click()
        page.get_by_role(
            "button", name="Continue with 1 selected category", exact=True
        ).click()
        page.get_by_role("button", name="Use recommended 30 days").click()
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Generate corpus").click()
        page.get_by_text("Corpus ready").wait_for()
        page.get_by_role(
            "button", name="Use this corpus and continue", exact=True
        ).click()

        page.get_by_role(
            "button", name="Continue without seed papers", exact=True
        ).wait_for()
        page.get_by_text("Selected geometry").click()
        page.get_by_role(
            "button", name="Continue with selected seed papers", exact=True
        ).wait_for()
        page.get_by_role("button", name="Add custom paper id").click()
        page.locator(".custom-entries input").fill("2608.09999")
        page.get_by_role(
            "button", name="Continue with selected seed papers", exact=True
        ).click()

        page.get_by_role(
            "button", name="Continue without terms", exact=True
        ).wait_for()
        page.get_by_text("derived geometry", exact=True).click()
        page.get_by_text("mirror symmetry").click()
        page.get_by_role(
            "button", name="Continue with selected terms", exact=True
        ).wait_for()
        page.get_by_role("button", name="Add custom term").click()
        page.locator(".custom-entries input").nth(0).fill("topology")
        page.get_by_role("button", name="Add custom term").click()
        page.locator(".custom-entries input").nth(1).fill("custom phrase")
        page.get_by_role(
            "button", name="Continue with selected terms", exact=True
        ).click()

        page.get_by_role(
            "button", name="Continue without author preferences", exact=True
        ).wait_for()
        page.get_by_text("Ada Example").click()
        page.get_by_role(
            "button", name="Continue with selected authors", exact=True
        ).wait_for()
        page.get_by_role("button", name="Add custom author").click()
        page.locator(".custom-entries input").fill("Custom Author")
        page.get_by_role(
            "button", name="Continue with selected authors", exact=True
        ).click()

        page.get_by_text(
            "Choose the folder where arXiv Digest will place paper PDFs",
            exact=False,
        ).wait_for()
        assert page.get_by_text("Setup will not download any PDFs", exact=False).count() == 1
        assert page.get_by_role("button", name="Use Downloads").count() == 0
        assert page.get_by_role("button", name="Use Documents").count() == 0
        assert page.locator('input[type="text"]').count() == 0
        assert page.get_by_role("button", name="Test selected destination").count() == 0
        page.get_by_role("button", name="Choose PDF folder").click()
        page.get_by_text("Selected folder: Research PDFs", exact=True).wait_for()
        page.get_by_role("button", name="Test selected destination").click()
        page.get_by_text("selected folder is writable", exact=False).wait_for()
        page.get_by_text("temporary test file was removed", exact=False).wait_for()
        page.get_by_role("button", name="Continue").click()

        page.get_by_text("Categories", exact=True).wait_for()
        seed_rows = page.locator(".setup-summary-seed-paper")
        assert seed_rows.count() == 2
        assert "2608.01234" in seed_rows.nth(0).inner_text()
        assert "Selected geometry" in seed_rows.nth(0).inner_text()
        assert "2608.09999" in seed_rows.nth(1).inner_text()
        assert "Custom seed paper" in seed_rows.nth(1).inner_text()
        first_seed_box = seed_rows.nth(0).bounding_box()
        second_seed_box = seed_rows.nth(1).bounding_box()
        assert first_seed_box is not None
        assert second_seed_box is not None
        assert second_seed_box["y"] >= first_seed_box["y"] + first_seed_box["height"]
        page.get_by_text("PDF download folder", exact=True).wait_for()
        destination_path = page.locator(".setup-summary-destination-path")
        assert destination_path.inner_text() == "~/Documents/Research PDFs"
        assert destination_path.evaluate("element => element.tagName") == "CODE"
        page.get_by_text("Tested and ready for PDF downloads.", exact=True).wait_for()
        assert page.locator(".setup-summary-destination input").count() == 0
        assert page.get_by_text("custom", exact=True).count() == 0
        page.get_by_role(
            "button", name="Confirm profile and continue", exact=True
        ).click()
        page.get_by_text("launcher", exact=True).wait_for()
        launcher_continue = page.get_by_role(
            "button", name="Finish setup", exact=True
        )
        assert launcher_continue.is_disabled()
        page.get_by_role("button", name="Not now").click()
        assert launcher_continue.is_enabled()
        assert page.locator(".primary-navigation").is_hidden()
        stale_launcher_continue = launcher_continue.element_handle()
        assert stale_launcher_continue is not None
        launcher_continue.click()
        page.get_by_role("button", name="Start review").wait_for()
        assert page.locator(".primary-navigation").is_visible()
        assert page.locator(".primary-navigation [data-view]").count() == 5
        assert page.url.endswith("?view=review")
        assert page.evaluate("history.state") == {"view": "review"}
        assert page.get_by_role("button", name="Retry", exact=True).count() == 0
        stale_launcher_continue.evaluate("control => control.click()")
        page.wait_for_timeout(200)

        assert application.launcher_calls == 0
        assert len(application.completions) == 1
        assert application.completions[-1]["launcher_choice"] == "not_now"
        assert [
            call
            for call in application.dashboard_calls
            if call == ("sync_start", {})
        ] == [("sync_start", {})]
        category = next(item for item in application.submissions if item.get("step") == "categories")
        assert category["selections"] == [
            {"category": "math.AG", "set_spec": "arXiv:math.AG"}
        ]
        seeds = next(item for item in application.submissions if item.get("step") == "seed_papers")
        assert seeds["accepted_suggestion_ids"] == ["paper_suggestion_1"]
        assert seeds["custom_arxiv_ids"] == ["2608.09999"]
        terms = next(item for item in application.submissions if item.get("step") == "terms")
        assert terms["accepted_keyword_suggestion_ids"] == [
            "keyword_suggestion_1"
        ]
        assert terms["accepted_phrase_suggestion_ids"] == ["phrase_suggestion_1"]
        assert terms["custom_keywords"] == ["topology"]
        assert terms["custom_phrases"] == ["custom phrase"]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_optional_interest_steps_can_continue_with_no_selections(
    engine: str,
) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.revision = 3
        application.step = "seed_papers"
        page.goto(server.launch_url("setup"))

        page.get_by_role("button", name="Add custom paper id").click()
        page.locator(".custom-entries input").fill("   ")
        page.get_by_role(
            "button", name="Continue without seed papers", exact=True
        ).click()
        page.get_by_role("button", name="Add custom term").click()
        page.locator(".custom-entries input").fill("   ")
        page.get_by_role(
            "button", name="Continue without terms", exact=True
        ).click()
        page.get_by_role("button", name="Add custom author").click()
        page.locator(".custom-entries input").fill("   ")
        page.get_by_role(
            "button", name="Continue without author preferences", exact=True
        ).click()
        page.get_by_role("button", name="Choose PDF folder").wait_for()
        assert page.get_by_role("button", name="Test selected destination").count() == 0

        optional = {
            str(item["step"]): item
            for item in application.submissions
            if item.get("step") in {"seed_papers", "terms", "authors"}
        }
        assert optional["seed_papers"]["accepted_suggestion_ids"] == []
        assert optional["seed_papers"]["custom_arxiv_ids"] == []
        assert optional["terms"]["accepted_keyword_suggestion_ids"] == []
        assert optional["terms"]["accepted_phrase_suggestion_ids"] == []
        assert optional["terms"]["custom_keywords"] == []
        assert optional["terms"]["custom_phrases"] == []
        assert optional["authors"]["accepted_suggestion_ids"] == []
        assert optional["authors"]["custom_authors"] == []


def test_review_reports_initial_sync_and_refreshes_when_papers_arrive() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 8
        application.step = "desktop_launcher"
        application.sync_starts_running = True
        application.daily_list_target_dates = 32
        application.daily_list_checked_dates = 18
        application.daily_list_dates_with_papers = 10
        application.daily_list_empty_dates = 8
        application.daily_list_failed_dates = 2
        application.daily_list_pending_dates = 12

        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Not now", exact=True).click()
        page.get_by_role("button", name="Finish setup", exact=True).click()

        page.get_by_text(
            "Historical daily-list recovery is in progress. Confirmed daily-list announcements will appear as dates are recovered, and this page will update automatically.",
            exact=True,
        ).wait_for()
        progress_text = (
            "Checking historical daily lists: 18 of 32 dates checked · "
            "10 with papers · 8 empty · 2 failed · 12 remaining."
        )
        page.get_by_text(progress_text, exact=True).wait_for()
        progress = page.get_by_role("progressbar", name=progress_text)
        assert progress.get_attribute("value") == "18"
        assert progress.get_attribute("max") == "32"
        assert page.get_by_text("You are caught up", exact=False).count() == 0
        assert page.get_by_role("button", name="Start review").count() == 0

        with application.lock:
            application.review_ready = True

        page.get_by_role("button", name="Start review").wait_for(timeout=5_000)
        page.get_by_text(
            "200 unreviewed paper announcements are ready across 10 dates. "
            "Review starts with the oldest date.",
            exact=False,
        ).wait_for()
        page.get_by_text(
            "1 paper announcement was added to a previously finished date.",
            exact=False,
        ).wait_for()
        page.get_by_text(
            "Synchronization is still in progress",
            exact=False,
        ).wait_for()
        assert page.get_by_text(progress_text, exact=True).is_visible()

        with application.lock:
            application.sync_phase = "enrichment"
            application.daily_list_checked_dates = 32
            application.daily_list_dates_with_papers = 20
            application.daily_list_empty_dates = 10
            application.daily_list_failed_dates = 2
            application.daily_list_pending_dates = 0

        recent_data = page.get_by_text(
            "Syncing recent paper data…",
            exact=True,
        )
        recent_data.wait_for(timeout=5_000)
        enrichment_progress = page.get_by_role(
            "progressbar",
            name="Syncing recent paper data…",
            exact=True,
        )
        assert enrichment_progress.get_attribute("value") is None
        assert enrichment_progress.get_attribute("max") is None
        assert page.locator(".review-home").get_attribute("aria-busy") == "false"
        assert page.get_by_role("button", name="Start review").is_enabled()
        assert page.get_by_role(
            "button", name="Mark all as reviewed"
        ).is_enabled()
        assert page.get_by_text(
            "Synchronization is still in progress",
            exact=False,
        ).count() == 0

        with application.lock:
            application.sync_running = False

        enrichment_progress.wait_for(state="hidden", timeout=5_000)
        assert application.sync_status_requests >= 2


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_review_can_mark_the_whole_current_backlog_reviewed(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        page.goto(server.launch_url("review"))

        page.get_by_role("button", name="Mark all as reviewed").click()
        assert application.review_finish_all_requests == 0
        page.get_by_text(
            "Mark all 200 currently unreviewed papers across 10 dates as reviewed? "
            "Papers imported later will remain unreviewed.",
            exact=True,
        ).wait_for()

        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/review/finish")
        ) as response_info:
            page.get_by_role(
                "button", name="Confirm mark all as reviewed"
            ).click()

        assert response_info.value.status == 200
        page.get_by_text("You are caught up.", exact=False).wait_for()
        assert application.review_finish_all_requests == 1
        assert (
            "review_finish_all",
            {
                "snapshot_revision": 77,
                "profile_revision": 5,
                "projection_revision": 9,
            },
        ) in application.dashboard_calls
        assert page.get_by_role("button", name="Mark all as reviewed").count() == 0
        page.get_by_text("Marked 200 papers as reviewed.", exact=True).wait_for()
        assert page.locator("#content").evaluate(
            "element => document.activeElement === element"
        )


def test_successful_bulk_review_is_not_retried_when_refresh_fails() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_finish_all_refresh_failures = 1
        page.goto(server.launch_url("review"))

        page.get_by_role("button", name="Mark all as reviewed").click()
        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/review/finish")
        ) as response_info:
            page.get_by_role(
                "button", name="Confirm mark all as reviewed"
            ).click()

        assert response_info.value.status == 200
        page.locator("#content").get_by_text(
            "The papers were marked as reviewed, but the Review summary could not be refreshed.",
            exact=True,
        ).wait_for()
        assert application.review_finish_all_requests == 1
        assert page.get_by_role(
            "button", name="Confirm mark all as reviewed"
        ).count() == 0
        assert page.get_by_role("button", name="Marked as reviewed").is_disabled()


def test_bulk_review_refresh_failure_does_not_cross_into_another_view() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Mark all as reviewed").wait_for()
        application.review_summary_entered.clear()
        application.review_summary_gate = threading.Event()
        application.review_finish_all_refresh_failures = 1

        try:
            page.get_by_role("button", name="Mark all as reviewed").click()
            with page.expect_response(
                lambda response: response.url.endswith("/api/v1/review/finish")
            ):
                page.get_by_role(
                    "button", name="Confirm mark all as reviewed"
                ).click()
            assert application.review_summary_entered.wait(timeout=5)

            page.get_by_role("button", name="Library", exact=True).click()
            page.get_by_role("heading", name="Library", exact=True).wait_for()
            application.review_summary_gate.set()
            page.wait_for_timeout(500)

            assert page.locator("#content").get_by_text(
                "The papers were marked as reviewed, but the Review summary could not be refreshed.",
                exact=True,
            ).count() == 0
        finally:
            application.review_summary_gate.set()


@pytest.mark.parametrize("destination", ["review-date", "library"])
def test_late_bulk_finish_cannot_replace_destination_or_leak_success_status(
    destination: str,
) -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_finish_all_gate = threading.Event()

        try:
            page.goto(server.launch_url("review"))
            page.get_by_role("button", name="Mark all as reviewed").click()
            page.get_by_role(
                "button", name="Confirm mark all as reviewed"
            ).click()
            assert application.review_finish_all_entered.wait(timeout=5)

            if destination == "review-date":
                page.get_by_role("button", name="Start review", exact=True).click()
                page.get_by_role("heading", name="Review 2026-07-31").wait_for()
            else:
                page.get_by_role("button", name="Library", exact=True).click()
                page.get_by_role(
                    "heading", name="Library", exact=True
                ).wait_for()

            with page.expect_response(
                lambda response: response.url.endswith("/api/v1/review/finish")
            ):
                application.review_finish_all_gate.set()
            page.wait_for_timeout(300)

            if destination == "review-date":
                assert page.get_by_role(
                    "heading", name="Review 2026-07-31"
                ).is_visible()
            else:
                assert page.get_by_role(
                    "heading", name="Library", exact=True
                ).is_visible()
            assert (
                "Marked 200 papers as reviewed."
                not in page.locator("#status").inner_text()
            )
            assert application.review_finish_all_requests == 1
        finally:
            application.review_finish_all_gate.set()


def test_late_failed_bulk_finish_does_not_leak_status_into_library() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_finish_all_gate = threading.Event()
        application.review_finish_all_failures = 1

        try:
            page.goto(server.launch_url("review"))
            page.get_by_role("button", name="Mark all as reviewed").click()
            page.get_by_role(
                "button", name="Confirm mark all as reviewed"
            ).click()
            assert application.review_finish_all_entered.wait(timeout=5)

            page.get_by_role("button", name="Library", exact=True).click()
            page.get_by_role("heading", name="Library", exact=True).wait_for()

            with page.expect_response(
                lambda response: response.url.endswith("/api/v1/review/finish")
            ):
                application.review_finish_all_gate.set()
            page.wait_for_timeout(300)

            assert page.get_by_role(
                "heading", name="Library", exact=True
            ).is_visible()
            assert page.locator("#status").inner_text() == ""
        finally:
            application.review_finish_all_gate.set()


def test_review_reads_terminal_sync_status_before_its_final_summary() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.sync_running = True
        application.review_ready = False
        application.sync_status_gate = threading.Event()

        try:
            page.goto(server.launch_url("review"))
            assert application.sync_status_entered.wait(timeout=5)
            time.sleep(0.25)
            with application.lock:
                application.review_ready = True
                application.sync_running = False
            application.sync_status_gate.set()

            page.get_by_role("button", name="Start review").wait_for(timeout=5_000)
            assert application.review_summary_requests == 1
        finally:
            application.sync_status_gate.set()


def test_leaving_review_ignores_a_late_summary_response() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_summary_gate = threading.Event()

        try:
            page.goto(server.launch_url("review"))
            assert application.review_summary_entered.wait(timeout=5)
            page.get_by_role("button", name="Library", exact=True).click()
            page.get_by_role("heading", name="Library", exact=True).wait_for()

            application.review_summary_gate.set()
            page.wait_for_timeout(300)

            assert page.get_by_role(
                "heading", name="Library", exact=True
            ).is_visible()
            assert page.get_by_role(
                "heading", name="Review", exact=True
            ).count() == 0
        finally:
            application.review_summary_gate.set()


def test_late_failed_review_render_does_not_cross_into_library() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_summary_gate = threading.Event()
        application.review_summary_failures = 1

        try:
            page.goto(server.launch_url("review"))
            assert application.review_summary_entered.wait(timeout=5)

            page.get_by_role("button", name="Library", exact=True).click()
            page.get_by_role("heading", name="Library", exact=True).wait_for()

            with page.expect_response(
                lambda response: response.url.endswith(
                    "/api/v1/review/summary"
                )
            ):
                application.review_summary_gate.set()
            page.wait_for_timeout(300)

            assert page.get_by_role(
                "heading", name="Library", exact=True
            ).is_visible()
            assert page.locator(".error-banner").count() == 0
            assert "did not complete" not in page.locator("#status").inner_text()
        finally:
            application.review_summary_gate.set()


def test_review_polling_preserves_focus_and_exposes_live_status() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.sync_running = True
        application.sync_phase = "enrichment"
        application.review_ready = True

        page.goto(server.launch_url("review"))
        start = page.get_by_role("button", name="Start review", exact=True)
        start.wait_for()
        phase_status = page.get_by_text(
            "Syncing recent paper data…", exact=True
        )
        assert phase_status.get_attribute("role") == "status"
        assert phase_status.get_attribute("aria-live") == "polite"
        assert page.locator(".review-home-status").get_attribute("role") is None
        phase_status.evaluate("node => { window.__reviewPhaseStatus = node; }")

        start.focus()
        page.wait_for_timeout(1_400)

        assert page.evaluate("document.activeElement?.textContent") == "Start review"
        assert phase_status.evaluate(
            "node => node === window.__reviewPhaseStatus"
        )
        assert application.sync_status_requests >= 2


def test_late_failed_review_poll_does_not_leak_status_after_leaving() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.sync_running = True
        application.review_ready = True

        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review", exact=True).wait_for()
        application.review_summary_entered.clear()
        application.review_summary_gate = threading.Event()
        application.review_summary_failures = 1

        try:
            assert application.review_summary_entered.wait(timeout=5)
            page.get_by_role("button", name="Library", exact=True).click()
            page.get_by_role("heading", name="Library", exact=True).wait_for()

            with page.expect_response(
                lambda response: response.url.endswith(
                    "/api/v1/review/summary"
                )
            ):
                application.review_summary_gate.set()
            page.wait_for_timeout(300)

            assert page.get_by_role(
                "heading", name="Library", exact=True
            ).is_visible()
            assert page.locator("#status").inner_text() == ""
        finally:
            application.review_summary_gate.set()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_review_sync_progress_becomes_determinate_when_targets_are_known(
    engine: str,
) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.sync_running = True
        application.review_ready = True

        page.goto(server.launch_url("review"))

        home = page.locator(".review-home")
        progress = page.locator("progress.review-sync-progress")
        progress.wait_for()
        assert progress.get_attribute("aria-label") == "Synchronization in progress"
        assert progress.get_attribute("value") is None
        assert home.get_attribute("aria-busy") == "true"
        progress.evaluate("element => { window.__initialReviewProgress = element; }")

        with application.lock:
            application.daily_list_target_dates = 32
            application.daily_list_checked_dates = 18
            application.daily_list_dates_with_papers = 10
            application.daily_list_empty_dates = 8
            application.daily_list_failed_dates = 2
            application.daily_list_pending_dates = 12

        progress_text = (
            "Checking historical daily lists: 18 of 32 dates checked · "
            "10 with papers · 8 empty · 2 failed · 12 remaining."
        )
        page.get_by_text(progress_text, exact=True).wait_for(timeout=5_000)
        page.get_by_role("progressbar", name=progress_text).wait_for()
        assert progress.get_attribute("value") == "18"
        assert progress.get_attribute("max") == "32"
        assert progress.evaluate(
            "element => element === window.__initialReviewProgress"
        )

        with application.lock:
            application.daily_list_checked_dates = 24
            application.daily_list_dates_with_papers = 13
            application.daily_list_empty_dates = 9
            application.daily_list_failed_dates = 2
            application.daily_list_pending_dates = 6

        page.get_by_text(
            "Checking historical daily lists: 24 of 32 dates checked · "
            "13 with papers · 9 empty · 2 failed · 6 remaining.",
            exact=True,
        ).wait_for(timeout=5_000)
        assert progress.get_attribute("value") == "24"

        with application.lock:
            application.sync_running = False

        progress.wait_for(state="hidden", timeout=5_000)
        assert home.get_attribute("aria-busy") == "false"


def test_enrichment_activity_remains_clear_with_reduced_motion() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.sync_running = True
        application.sync_phase = "enrichment"
        application.review_ready = True
        page.emulate_media(reduced_motion="reduce")

        page.goto(server.launch_url("review"))

        assert page.evaluate(
            "window.matchMedia('(prefers-reduced-motion: reduce)').matches"
        )
        page.get_by_text("Syncing recent paper data…", exact=True).wait_for()
        progress = page.get_by_role(
            "progressbar",
            name="Syncing recent paper data…",
            exact=True,
        )
        assert progress.is_visible()
        assert progress.get_attribute("value") is None
        assert page.locator(".review-home").get_attribute("aria-busy") == "false"


def test_review_start_is_independent_of_failed_daily_list_retry() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.settings_retryable_failed_daily_list_dates = [
            "2026-08-11",
            "2026-08-12",
        ]
        application.sync_starts_running = True

        page.goto(server.launch_url("review"))
        start = page.get_by_role("button", name="Start review", exact=True)
        retry = page.get_by_role(
            "button",
            name="Retry 2 failed daily-list dates",
            exact=True,
        )
        start.wait_for()
        retry.wait_for()

        with page.expect_response(
            lambda response: "/api/v1/review/date?" in response.url
        ):
            start.click()
        page.get_by_role(
            "heading", name="Review 2026-07-31", exact=True
        ).wait_for()
        assert not any(
            operation == "sync_start"
            for operation, _payload in application.dashboard_calls
        )

        page.get_by_role("navigation", name="Review dates").get_by_role(
            "button", name="Back to Review overview", exact=True
        ).click()
        retry.wait_for()
        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/sync/start")
        ):
            retry.click()

        page.get_by_role(
            "button",
            name="Retrying failed daily-list dates… 0 of 2 completed",
            exact=True,
        ).wait_for()
        with application.lock:
            application.daily_list_retry_completed = 1
        page.get_by_role(
            "button",
            name="Retrying failed daily-list dates… 1 of 2 completed",
            exact=True,
        ).wait_for(timeout=5_000)

        with application.lock:
            application.sync_running = False
            application.review_ready = True

        page.get_by_role("button", name="Start review", exact=True).wait_for(
            timeout=5_000
        )
        assert page.get_by_role(
            "heading", name="Review 2026-07-31", exact=True
        ).count() == 0
        assert (
            "sync_start",
            {"retry_failed_dates": True},
        ) in application.dashboard_calls


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_open_review_date_refreshes_once_when_synchronization_finishes(
    engine: str,
) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.sync_running = True
        application.review_ready = True
        application.review_metadata_tracks_sync = True
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review", exact=True).click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()
        unresolved = page.locator("article .paper-labels").nth(1)
        assert "Version not confirmed" in unresolved.inner_text()
        assert "daily-list date" not in unresolved.inner_text()

        save = page.locator("article").first.get_by_role(
            "button", name="Save", exact=True
        )
        save.focus()
        page.wait_for_timeout(1_300)
        assert page.evaluate("document.activeElement?.textContent") == "Save"
        assert "Version not confirmed" in unresolved.inner_text()
        assert application.review_date_requests == 1

        with application.lock:
            application.sync_running = False

        unresolved.get_by_text("Version v2", exact=False).wait_for(timeout=5_000)
        page.get_by_text(
            "Review updated after synchronization finished.", exact=True
        ).wait_for()
        assert application.review_date_requests == 2
        assert page_errors == []


def test_finishing_date_wins_over_inflight_terminal_sync_refresh() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.sync_running = True
        application.review_ready = True
        application.review_metadata_tracks_sync = True
        application.review_date_gate_request = 2
        application.review_date_gate = threading.Event()
        application.review_finish_gate = threading.Event()
        application.saved_anchor = 181

        try:
            page.goto(server.launch_url("review"))
            page.get_by_role("button", name="Start review", exact=True).click()
            page.get_by_role("heading", name="Review 2026-07-31").wait_for()

            with application.lock:
                application.sync_running = False
            assert application.review_date_gate_entered.wait(timeout=5)

            page.get_by_role("button", name="Finish date", exact=True).click()
            page.get_by_role("button", name="Confirm finish", exact=True).click()
            assert application.review_finish_entered.wait(timeout=5)

            with page.expect_response(
                lambda response: "/api/v1/review/date?" in response.url
            ):
                application.review_date_gate.set()
            with page.expect_response(
                lambda response: response.url.endswith(
                    "/api/v1/review/date/finish"
                )
            ):
                application.review_finish_gate.set()
            page.wait_for_timeout(300)

            assert page.get_by_role(
                "button", name="Start review", exact=True
            ).is_visible()
            assert page.get_by_role(
                "heading", name="Review 2026-07-31"
            ).count() == 0
            assert page.locator("#status").inner_text() == (
                "Finished review for 2026-07-31."
            )
            assert application.review_finish_requests == 1
        finally:
            application.review_date_gate.set()
            application.review_finish_gate.set()


def test_review_polling_retries_after_a_transient_status_failure() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.sync_running = True
        application.review_ready = True

        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review", exact=True).wait_for()
        with application.lock:
            application.sync_status_failures = 1

        page.wait_for_timeout(2_700)

        assert application.sync_status_requests >= 3
        page.get_by_role("button", name="Start review", exact=True).wait_for()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_review_home_page_navigation_safe_metadata_and_resume(engine: str) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        page.set_viewport_size({"width": 1800, "height": 1000})
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review").click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()

        assert page.locator("article").count() == 20
        second_title = page.locator("article h3").nth(1)
        assert second_title.evaluate(
            """
            element => {
              const range = document.createRange();
              range.selectNodeContents(element);
              return new Set(
                [...range.getClientRects()].map((rect) => Math.round(rect.top)),
              ).size;
            }
            """
        ) == 1
        second_labels = page.locator("article .paper-labels").nth(1).inner_text()
        assert second_labels.startswith(
            "Version not confirmed · Replacement · Subjects: math.AG, math.CO"
        )
        assert "Announced v2" not in second_labels
        assert "daily-list date" not in second_labels
        assert "Recovered under" not in second_labels
        assert "Submission date" not in second_labels
        assert "Inferred from version history" not in second_labels
        second = page.locator("article").nth(1)
        assert second.get_by_role(
            "link", name="Abstract on arXiv", exact=True
        ).get_attribute("href").endswith("/abs/2608.00002v4")
        assert second.get_by_role(
            "link", name="PDF on arXiv", exact=True
        ).get_attribute("href").endswith("/pdf/2608.00002v4.pdf")
        second.get_by_role(
            "button",
            name="Download PDF",
            exact=True,
        ).wait_for()
        second.get_by_role(
            "button",
            name="Save + PDF",
            exact=True,
        ).wait_for()
        for tier in ("Top", "Possible", "Other"):
            page.get_by_role("heading", name=tier).wait_for()
        assert page.locator("script", has_text="not markup").count() == 0
        assert page.get_by_text("<script>not markup</script>", exact=False).count() >= 1
        first = page.locator("article").first
        abstract_box = first.get_by_role(
            "link", name="Abstract on arXiv", exact=True
        ).bounding_box()
        pdf_box = first.get_by_role(
            "link", name="PDF on arXiv", exact=True
        ).bounding_box()
        assert abstract_box is not None
        assert pdf_box is not None
        assert pdf_box["x"] >= abstract_box["x"] + abstract_box["width"] + 12
        ranking = first.locator("details.ranking-explanation")
        first.get_by_text("Why this ranking", exact=True).wait_for()
        assert ranking.get_attribute("open") is None
        first.get_by_text("Version v2", exact=False).wait_for()
        first.get_by_text("Newly discovered").wait_for()
        first.get_by_text("Why this ranking", exact=True).click()
        assert ranking.get_attribute("open") is not None
        first.get_by_text("Why this ranking", exact=True).click()
        assert ranking.get_attribute("open") is None

        page_navigation = page.get_by_role(
            "navigation", name="Pages for this date", exact=True
        )
        assert page_navigation.evaluate(
            """
            navigation => {
              const papers = [...document.querySelectorAll("article.paper-card")];
              const lastPaper = papers.at(-1);
              return lastPaper !== undefined && Boolean(
                lastPaper.compareDocumentPosition(navigation) &
                  Node.DOCUMENT_POSITION_FOLLOWING
              );
            }
            """
        )
        page_navigation.get_by_role(
            "button", name="Next page", exact=True
        ).click()
        page.get_by_text("Page 2 of 10").wait_for()
        assert application.saved_anchor == 21
        assert (
            "review_position",
            {
                "date": "2026-07-31",
                "snapshot_revision": 77,
                "profile_revision": 5,
                "projection_revision": 9,
                "anchor_event_id": 21,
            },
        ) in application.dashboard_calls
        page.reload()
        page.get_by_role("button", name="Start review").click()
        page.get_by_text("Page 2 of 10").wait_for()

        page.get_by_role("button", name="Previous date").click()
        page.get_by_role("heading", name="Review 2026-06-30").wait_for()
        page.get_by_role("button", name="Next date").click()
        page.get_by_role("heading", name="Review 2026-08-31").wait_for()
        assert page.evaluate("document.activeElement?.textContent") == (
            "Review 2026-08-31"
        )
        assert page.get_by_role("button", name="Next unreviewed").count() == 0

        assert page.get_by_role("button", name="Finish date").count() == 0


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_review_date_has_an_explicit_overview_exit(engine: str) -> None:
    with running_fixture() as (server, _application), browser_page(engine) as page:
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review").click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()

        page.get_by_role("navigation", name="Review dates").get_by_role(
            "button", name="Back to Review overview", exact=True
        ).click()

        page.get_by_role("button", name="Start review", exact=True).wait_for()
        assert page.url.endswith("?view=review")
        assert page.evaluate("history.state") == {"view": "review"}


def test_bottom_review_exit_reports_pending_summary_load() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review", exact=True).click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()
        application.review_summary_entered.clear()
        application.review_summary_gate = threading.Event()

        try:
            exit_button = page.get_by_role(
                "navigation", name="Leave this review date"
            ).get_by_role(
                "button", name="Back to Review overview", exact=True
            )
            exit_button.click()
            assert application.review_summary_entered.wait(timeout=5)

            loading = page.get_by_role("button", name="Loading…", exact=True)
            assert loading.is_disabled()
            assert loading.get_attribute("aria-busy") == "true"

            application.review_summary_gate.set()
            page.get_by_role("button", name="Start review", exact=True).wait_for()
        finally:
            application.review_summary_gate.set()


def test_failed_review_exit_focuses_its_retry() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review", exact=True).click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()
        application.review_summary_entered.clear()
        application.review_summary_gate = threading.Event()
        application.review_summary_failures = 1

        try:
            page.get_by_role("navigation", name="Review dates").get_by_role(
                "button", name="Back to Review overview", exact=True
            ).click()
            assert application.review_summary_entered.wait(timeout=5)
            application.review_summary_gate.set()

            retry = page.get_by_role("button", name="Retry", exact=True)
            retry.wait_for()
            assert retry.evaluate("element => document.activeElement === element")
        finally:
            application.review_summary_gate.set()


def test_review_date_navigation_updates_and_clears_history() -> None:
    with running_fixture() as (server, _application), browser_page("chromium") as page:
        page.clock.install(time="2026-08-31T12:00:00Z")
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Calendar").click()
        page.get_by_role("heading", name="Calendar", exact=True).wait_for()
        page.get_by_role(
            "button",
            name="2026-08-03: 4 papers, partial",
        ).click()
        page.get_by_role("heading", name="Review 2026-08-03").wait_for()

        page.get_by_role("button", name="Next date", exact=True).click()

        page.get_by_role("heading", name="Review 2026-08-31").wait_for()
        assert page.evaluate("history.state") == {
            "view": "review",
            "reviewDate": "2026-08-31",
        }

        page.get_by_role("navigation", name="Review dates").get_by_role(
            "button", name="Back to Review overview", exact=True
        ).click()
        page.get_by_role("button", name="Start review", exact=True).wait_for()
        assert page.evaluate("history.state") == {"view": "review"}

        page.reload()
        page.get_by_role("button", name="Start review", exact=True).wait_for()
        assert page.get_by_role(
            "heading", name="Review 2026-08-31"
        ).count() == 0


def test_stale_next_date_recovers_without_an_unhandled_browser_error() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.missing_review_dates.add("2026-08-31")
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review").click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()

        page.get_by_role("button", name="Next date", exact=True).click()

        page.get_by_role("button", name="Start review", exact=True).wait_for()
        page.get_by_text(
            "Review dates changed while synchronization was finishing. The overview has been refreshed.",
            exact=True,
        ).wait_for()
        assert page_errors == []


def test_finishing_a_date_opens_the_next_later_date_from_its_beginning() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_finish_next_later_unreviewed_date = "2026-09-01"
        application.saved_anchor = 181
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review").click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()
        with application.lock:
            application.saved_anchor = 21

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.get_by_role("button", name="Finish date", exact=True).click()
        page.get_by_role("button", name="Confirm finish", exact=True).click()

        page.get_by_role(
            "heading", name="Review 2026-09-01", exact=True
        ).wait_for()
        page.get_by_text("Page 1 of 10", exact=False).wait_for()
        page.get_by_text(
            "Finished review for 2026-07-31. Opening 2026-09-01.", exact=True
        ).wait_for()
        assert page.url.endswith("?view=review")
        assert page.evaluate("history.state") == {"view": "review"}
        assert page.evaluate("document.activeElement?.textContent") == (
            "Review 2026-09-01"
        )
        assert page.locator(".review-view h1").evaluate(
            "node => node.getBoundingClientRect().top"
        ) < 100
        assert (
            "review_finish",
            {
                "date": "2026-07-31",
                "snapshot_revision": 77,
                "profile_revision": 5,
                "projection_revision": 9,
            },
        ) in application.dashboard_calls
        assert (
            "review_date",
            {"date": "2026-09-01", "from_start": True},
        ) in application.dashboard_calls


def test_finishing_without_a_later_date_returns_to_the_review_overview() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_finish_next_later_unreviewed_date = None
        application.saved_anchor = 181
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review").click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()

        page.get_by_role("button", name="Finish date", exact=True).click()
        page.get_by_role("button", name="Confirm finish", exact=True).click()

        page.get_by_role("heading", name="Review", exact=True).wait_for()
        page.get_by_role("button", name="Start review", exact=True).wait_for()
        assert page.locator(".review-view").count() == 0
        assert page.url.endswith("?view=review")


def test_successful_date_finish_is_not_retried_when_overview_refresh_fails() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_finish_refresh_failures = 1
        application.saved_anchor = 181
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review", exact=True).click()
        page.get_by_role("heading", name="Review 2026-07-31").wait_for()

        page.get_by_role("button", name="Finish date", exact=True).click()
        page.get_by_role("button", name="Confirm finish", exact=True).click()

        page.get_by_text(
            "Finished review for 2026-07-31, but the Review overview could not be refreshed.",
            exact=True,
        ).wait_for()
        assert page.locator("article.paper-card").count() == 0
        assert application.review_finish_requests == 1

        page.get_by_role("button", name="Retry", exact=True).click()

        page.get_by_text("You are caught up.", exact=False).wait_for()
        page.get_by_text(
            "Finished review for 2026-07-31.", exact=True
        ).wait_for()
        assert application.review_finish_requests == 1


def test_late_date_finish_cannot_replace_library_or_leak_success_status() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_finish_gate = threading.Event()
        application.saved_anchor = 181

        try:
            page.goto(server.launch_url("review"))
            page.get_by_role("button", name="Start review", exact=True).click()
            page.get_by_role("heading", name="Review 2026-07-31").wait_for()
            page.get_by_role("button", name="Finish date", exact=True).click()
            page.get_by_role("button", name="Confirm finish", exact=True).click()
            assert application.review_finish_entered.wait(timeout=5)

            page.get_by_role("button", name="Library", exact=True).click()
            page.get_by_role("heading", name="Library", exact=True).wait_for()

            with page.expect_response(
                lambda response: response.url.endswith(
                    "/api/v1/review/date/finish"
                )
            ):
                application.review_finish_gate.set()
            page.wait_for_timeout(300)

            assert page.get_by_role(
                "heading", name="Library", exact=True
            ).is_visible()
            assert "Finished review for" not in page.locator("#status").inner_text()
            assert application.review_finish_requests == 1
        finally:
            application.review_finish_gate.set()


def test_late_failed_date_finish_does_not_leak_status_into_library() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.review_finish_gate = threading.Event()
        application.review_finish_failures = 1
        application.saved_anchor = 181

        try:
            page.goto(server.launch_url("review"))
            page.get_by_role("button", name="Start review", exact=True).click()
            page.get_by_role("heading", name="Review 2026-07-31").wait_for()
            page.get_by_role("button", name="Finish date", exact=True).click()
            page.get_by_role("button", name="Confirm finish", exact=True).click()
            assert application.review_finish_entered.wait(timeout=5)

            page.get_by_role("button", name="Library", exact=True).click()
            page.get_by_role("heading", name="Library", exact=True).wait_for()

            with page.expect_response(
                lambda response: response.url.endswith(
                    "/api/v1/review/date/finish"
                )
            ):
                application.review_finish_gate.set()
            page.wait_for_timeout(300)

            assert page.get_by_role(
                "heading", name="Library", exact=True
            ).is_visible()
            assert page.locator("#status").inner_text() == ""
        finally:
            application.review_finish_gate.set()


def test_review_save_confirms_the_paper_was_added_to_library() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review").click()
        first = page.locator("article").first

        first.get_by_role("button", name="Save", exact=True).click()

        saved = first.get_by_role("button", name="Saved", exact=True)
        saved.wait_for()
        assert saved.is_disabled()
        first.get_by_text("Paper saved to Library.", exact=True).wait_for()
        assert (
            "library_save",
            {"arxiv_id": "2608.00001", "version": 2},
        ) in application.dashboard_calls


def test_saving_two_review_cards_does_not_cancel_the_first_save() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.library_save_gate = threading.Event()
        try:
            page.goto(server.launch_url("review"))
            page.get_by_role("button", name="Start review").click()
            first = page.locator("article").nth(0)
            second = page.locator("article").nth(1)

            first.get_by_role("button", name="Save", exact=True).click()
            assert application.library_save_entered.wait(timeout=5)
            second.get_by_role("button", name="Save", exact=True).click()
            second.get_by_role("button", name="Saved", exact=True).wait_for()

            application.library_save_gate.set()
            first.get_by_role("button", name="Saved", exact=True).wait_for()
            assert application.library_save_requests == 2
        finally:
            application.library_save_gate.set()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_calendar_navigation_opens_and_keeps_the_selected_date(engine: str) -> None:
    with running_fixture() as (server, _application), browser_page(engine) as page:
        page.clock.install(time="2026-08-31T12:00:00Z")
        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Calendar").click()
        page.get_by_role("heading", name="Calendar", exact=True).wait_for()
        assert "view=calendar" in page.url
        page.reload()
        page.get_by_role("heading", name="Calendar", exact=True).wait_for()
        assert page.evaluate(
            "sessionStorage.getItem('arxiv-digest.session-token')"
        ) == server.token
        calendar_day = page.get_by_role(
            "button",
            name="2026-08-03: 4 papers, partial",
        )
        assert "4 papers" in calendar_day.inner_text()
        assert "Partial" in calendar_day.inner_text()
        assert calendar_day.get_attribute("role") is None
        assert calendar_day.locator("xpath=..").get_attribute("role") == "listitem"
        calendar_day.focus()
        page.keyboard.press("Enter")
        page.get_by_role("heading", name="Review 2026-08-03").wait_for()
        page.wait_for_timeout(250)

        assert page.get_by_role(
            "heading", name="Review 2026-08-03"
        ).is_visible()
        assert "view=review" in page.url


def test_calendar_keeps_recent_dates_visible_across_a_month_boundary() -> None:
    with running_fixture() as (server, _application), browser_page("chromium") as page:
        page.clock.install(time="2026-09-01T12:00:00Z")
        page.goto(server.launch_url("calendar"))

        page.get_by_role("heading", name="Calendar", exact=True).wait_for()
        page.get_by_role(
            "button", name="2026-08-03: 4 papers, partial"
        ).wait_for()
        page.get_by_role(
            "button", name="2026-09-01: 5 papers, unreviewed"
        ).wait_for()


def test_calendar_outlines_distinguish_review_states_in_light_and_dark() -> None:
    with running_fixture() as (server, _application), browser_page("chromium") as page:
        page.clock.install(time="2026-08-31T12:00:00Z")
        page.goto(server.launch_url("calendar"))
        page.get_by_role("heading", name="Calendar", exact=True).wait_for()
        cards = {
            "reviewed": page.get_by_role(
                "button", name="2026-08-01: 14 papers, reviewed"
            ),
            "unreviewed": page.get_by_role(
                "button", name="2026-08-02: 16 papers, unreviewed"
            ),
            "partial": page.get_by_role(
                "button", name="2026-08-03: 4 papers, partial"
            ),
        }
        assert "✓ Reviewed" in cards["reviewed"].inner_text()
        assert "Unreviewed" in cards["unreviewed"].inner_text()
        assert "Partial" in cards["partial"].inner_text()

        theme_colors: dict[str, dict[str, str]] = {}
        for color_scheme in ("light", "dark"):
            page.emulate_media(color_scheme=color_scheme)
            styles = {
                status: card.evaluate(
                    """element => {
                        const style = getComputedStyle(element);
                        return {
                            borderColor: style.borderTopColor,
                            borderStyle: style.borderTopStyle,
                            borderWidth: style.borderTopWidth,
                            color: style.color,
                            opacity: style.opacity,
                        };
                    }"""
                )
                for status, card in cards.items()
            }
            assert styles["reviewed"]["borderStyle"] == "solid"
            assert styles["reviewed"]["borderWidth"] == "1px"
            assert styles["reviewed"]["borderColor"] == styles["reviewed"]["color"]
            assert styles["unreviewed"]["borderStyle"] == "solid"
            assert styles["unreviewed"]["borderWidth"] == "2px"
            assert styles["partial"]["borderStyle"] == "dashed"
            assert styles["partial"]["borderWidth"] == "2px"
            assert (
                styles["unreviewed"]["borderColor"]
                == styles["partial"]["borderColor"]
            )
            assert (
                styles["unreviewed"]["borderColor"]
                != styles["reviewed"]["borderColor"]
            )
            assert {style["opacity"] for style in styles.values()} == {"1"}
            theme_colors[color_scheme] = {
                status: style["borderColor"] for status, style in styles.items()
            }

        assert theme_colors["light"] != theme_colors["dark"]


def test_category_lifecycle_hides_active_views_but_retains_library_state() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.active_categories = ["math.AG", "math.CO", "stat.ML"]
        application.category_coverage_starts = {
            "math.AG": "2026-07-01",
            "math.CO": "2026-07-01",
            "stat.ML": "2026-07-01",
        }

        page.goto(server.launch_url("review"))
        page.get_by_role("button", name="Start review", exact=True).click()
        page.get_by_role("heading", name="Review 2026-07-31", exact=True).wait_for()
        page.locator("article").first.get_by_text(
            "Subjects: math.AG, math.CO", exact=False
        ).wait_for()

        page.get_by_role("button", name="Interests", exact=True).click()
        page.get_by_role("button", name="Remove math.AG", exact=True).click()
        page.get_by_role("button", name="Update interests", exact=True).click()
        page.get_by_text("Interests updated.", exact=True).wait_for()

        page.get_by_role("button", name="Review", exact=True).click()
        page.get_by_role("button", name="Start review", exact=True).click()
        page.locator("article").first.get_by_text(
            "Subjects: math.AG, math.CO", exact=False
        ).wait_for()

        page.get_by_role("button", name="Interests", exact=True).click()
        page.get_by_role("button", name="Remove math.CO", exact=True).click()
        page.get_by_role("button", name="Update interests", exact=True).click()
        page.get_by_text("Interests updated.", exact=True).wait_for()

        page.get_by_role("button", name="Review", exact=True).click()
        page.get_by_text("You are caught up.", exact=False).wait_for()
        assert page.get_by_role("button", name="Start review", exact=True).count() == 0
        page.get_by_role("button", name="Calendar", exact=True).click()
        page.get_by_role("heading", name="Calendar", exact=True).wait_for()
        assert page.get_by_role("listitem").count() == 0

        page.get_by_role("button", name="Library", exact=True).click()
        page.get_by_text("A dashboard library paper", exact=True).wait_for()

        page.get_by_role("button", name="Interests", exact=True).click()
        page.get_by_role("button", name="Add category", exact=True).click()
        page.get_by_label("Coverage start for math.AG", exact=True).fill(
            "2026-08-24"
        )
        page.get_by_role("button", name="Add math.AG", exact=True).click()
        page.get_by_role("button", name="Update interests", exact=True).click()
        page.get_by_text("Interests updated.", exact=True).wait_for()

        page.get_by_role("button", name="Review", exact=True).click()
        page.get_by_text("You are caught up.", exact=False).wait_for()
        assert page.get_by_role("button", name="Start review", exact=True).count() == 0
        page.get_by_role("button", name="Calendar", exact=True).click()
        assert page.get_by_role("listitem").count() == 0
        page.get_by_role("button", name="Library", exact=True).click()
        page.get_by_text("A dashboard library paper", exact=True).wait_for()

        interests_updates = [
            payload
            for operation, payload in application.dashboard_calls
            if operation == "interests_put"
        ]
        assert interests_updates[-1]["category_configs"] == [
            {
                "category": "math.AG",
                "set_spec": "arXiv:math.AG",
                "coverage_start": "2026-08-24",
            }
        ]


def test_cancelled_folder_picker_keeps_setup_unselected_and_sends_no_path() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 7
        application.step = "pdf_destination"
        application.folder_pick_result = {"cancelled": True}
        seen_bodies: list[str] = []
        page.on(
            "request",
            lambda request: seen_bodies.append(request.post_data or "")
            if "/setup/folder" in request.url
            else None,
        )
        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Choose PDF folder").click()
        page.get_by_text("selection was cancelled").wait_for()
        page.get_by_text("no folder was selected", exact=False).wait_for()
        assert page.locator('input[type="radio"]').count() == 0
        assert page.get_by_role("button", name="Test selected destination").count() == 0
        assert page.get_by_role("button", name="Continue").is_disabled()
        assert all("/Users/" not in body and "C:\\" not in body for body in seen_bodies)


def test_failed_folder_test_requires_a_new_picker_choice() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 7
        application.step = "pdf_destination"
        application.folder_test_result = {}
        page.goto(server.launch_url("setup"))

        page.get_by_role("button", name="Choose PDF folder").click()
        page.get_by_text("Selected folder: Research PDFs", exact=True).wait_for()
        page.get_by_role("button", name="Test selected destination").click()

        page.get_by_text("The folder could not be used", exact=False).wait_for()
        assert page.get_by_text("Selected folder: Research PDFs", exact=True).count() == 0
        page.get_by_role("button", name="Choose PDF folder").wait_for()
        assert page.get_by_role("button", name="Test selected destination").count() == 0
        assert page.get_by_role("button", name="Continue").is_disabled()


def test_capped_candidate_job_requires_resume_or_restart_before_continuing() -> None:
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
        page.get_by_role("button", name="Resume corpus", exact=True).wait_for()

        assert page.get_by_role(
            "button", name="Restart corpus", exact=True
        ).is_visible()
        assert page.get_by_role("button", name="Continue", exact=True).is_disabled()
        page.get_by_text("below the required minimum", exact=False).wait_for()

        application.candidate_job.update(
            minimum_met=True,
            corpus_hash="b" * 64,
            message="Minimum candidate breadth reached",
        )
        page.get_by_role("button", name="Resume corpus", exact=True).click()
        accept = page.get_by_role(
            "button", name="Accept reduced breadth and continue", exact=True
        )
        accept.wait_for()
        assert accept.is_enabled()
        page.get_by_text("explicitly accept this reduced breadth", exact=False).wait_for()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_corpus_generation_is_busy_before_start_returns_and_through_polling(
    engine: str,
) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.corpus_start_gate = threading.Event()
        application.candidate_job = {
            "job_id": "corpus_job_1234",
            "status": "running",
            "complete": False,
            "failed": False,
        }

        try:
            page.goto(server.launch_url("setup"))
            page.get_by_role("button", name="Generate corpus").click()
            assert application.corpus_start_entered.wait(timeout=2)

            starting = page.get_by_text(
                "Starting corpus generation… This can take up to five minutes.",
                exact=True,
            )
            starting.wait_for(timeout=2_000)
            assert page.locator(
                'progress[aria-label="Corpus generation in progress"]'
            ).is_visible()
            assert page.locator('.setup-view[aria-busy="true"]').is_visible()
            assert page.get_by_role("button", name="Generate corpus").count() == 0
            navigation = page.locator(".primary-navigation")
            assert navigation.is_hidden()
            assert page.get_by_role(
                "button", name="Settings", exact=True
            ).count() == 0
            assert page.get_by_role("button", name="Quit", exact=True).is_visible()

            application.corpus_start_gate.set()
            working = page.get_by_text(
                "Generating corpus… This can take up to five minutes.",
                exact=True,
            )
            working.wait_for(timeout=2_000)
            deadline = time.monotonic() + 3
            while application.corpus_status_requests < 2 and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert application.corpus_status_requests >= 2
            assert working.is_visible()
            assert page.get_by_role("button", name="Generate corpus").count() == 0
            assert navigation.is_hidden()
            assert page.get_by_role(
                "button", name="Settings", exact=True
            ).count() == 0
            assert page.get_by_role("button", name="Quit", exact=True).is_visible()

            application.expose_corpus_job = True
            page.reload()
            working.wait_for(timeout=2_000)
            assert page.locator('.setup-view[aria-busy="true"]').is_visible()
            assert page.get_by_role("button", name="Generate corpus").count() == 0

            application.candidate_job = {
                "job_id": "corpus_job_1234",
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
            page.get_by_text("Corpus ready", exact=True).wait_for(timeout=3_000)
            assert application.corpus_start_requests == 1
        finally:
            application.corpus_start_gate.set()


def test_corpus_polling_never_replaces_a_view_opened_during_generation() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.candidate_job = {
            "job_id": "corpus_job_1234",
            "status": "running",
            "complete": False,
            "failed": False,
        }

        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Generate corpus").click()
        page.get_by_text(
            "Generating corpus… This can take up to five minutes.", exact=True
        ).wait_for()
        navigate_with_history(page, "settings")
        page.get_by_role("heading", name="Settings", exact=True).wait_for()

        page.wait_for_timeout(750)
        assert page.get_by_role("heading", name="Settings", exact=True).is_visible()
        assert page.get_by_role(
            "heading", name="Set up your arXiv digest", exact=True
        ).count() == 0


def test_steady_corpus_polling_does_not_repeat_live_region_announcements() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.candidate_job = {
            "job_id": "corpus_job_1234",
            "status": "running",
            "complete": False,
            "failed": False,
        }

        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Generate corpus").click()
        page.get_by_text(
            "Generating corpus… This can take up to five minutes.", exact=True
        ).wait_for()
        starting_requests = application.corpus_status_requests
        page.evaluate(
            """
            () => {
              window.__corpusMutationCounts = { status: 0, content: 0 };
              const statusObserver = new MutationObserver((records) => {
                window.__corpusMutationCounts.status += records.length;
              });
              statusObserver.observe(document.querySelector("#status"), {
                childList: true,
                characterData: true,
                subtree: true,
              });
              const contentObserver = new MutationObserver((records) => {
                window.__corpusMutationCounts.content += records.filter(
                  (record) => record.target === document.querySelector("#content"),
                ).length;
              });
              contentObserver.observe(document.querySelector("#content"), {
                childList: true,
              });
              window.__corpusObservers = [statusObserver, contentObserver];
            }
            """
        )

        deadline = time.monotonic() + 2
        while (
            application.corpus_status_requests < starting_requests + 3
            and time.monotonic() < deadline
        ):
            page.wait_for_timeout(50)
        assert application.corpus_status_requests >= starting_requests + 3
        assert page.evaluate("window.__corpusMutationCounts") == {
            "status": 0,
            "content": 0,
        }


def test_corpus_start_response_does_not_replace_a_view_opened_while_starting() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.corpus_start_gate = threading.Event()
        application.candidate_job = {
            "job_id": "corpus_job_1234",
            "status": "running",
            "complete": False,
            "failed": False,
        }

        try:
            page.goto(server.launch_url("setup"))
            page.get_by_role("button", name="Generate corpus").click()
            assert application.corpus_start_entered.wait(timeout=2)
            navigate_with_history(page, "settings")
            page.get_by_role("heading", name="Settings", exact=True).wait_for()

            application.corpus_start_gate.set()
            page.wait_for_timeout(500)
            assert page.get_by_role(
                "heading", name="Settings", exact=True
            ).is_visible()
            assert page.get_by_role(
                "heading", name="Set up your arXiv digest", exact=True
            ).count() == 0
            assert application.corpus_status_requests == 0
        finally:
            application.corpus_start_gate.set()


def test_corpus_start_response_does_not_replace_the_closed_screen() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.corpus_start_gate = threading.Event()

        try:
            page.goto(server.launch_url("setup"))
            page.get_by_role("button", name="Generate corpus").click()
            assert application.corpus_start_entered.wait(timeout=2)
            page.get_by_role("button", name="Quit", exact=True).click()
            page.get_by_role(
                "heading", name="arXiv Digest is closed", exact=True
            ).wait_for()

            application.corpus_start_gate.set()
            page.wait_for_timeout(500)
            assert page.get_by_role(
                "heading", name="arXiv Digest is closed", exact=True
            ).is_visible()
            assert page.get_by_role(
                "heading", name="Set up your arXiv digest", exact=True
            ).count() == 0
            assert application.corpus_status_requests == 0
        finally:
            application.corpus_start_gate.set()


def test_quit_immediately_prevents_a_new_corpus_start() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.quit_gate = threading.Event()
        application.corpus_start_gate = threading.Event()

        try:
            page.goto(server.launch_url("setup"))
            generate = page.get_by_role("button", name="Generate corpus", exact=True)
            page.get_by_role("button", name="Quit", exact=True).click()
            assert application.quit_entered.wait(timeout=2)

            generate.evaluate("control => control.click()")
            page.wait_for_timeout(200)
            assert not application.corpus_start_entered.is_set()

            application.quit_gate.set()
            page.get_by_role(
                "heading", name="arXiv Digest is closed", exact=True
            ).wait_for()
            assert application.corpus_start_requests == 0
            assert application.corpus_status_requests == 0
        finally:
            application.quit_gate.set()
            application.corpus_start_gate.set()


def test_pagehide_does_not_cancel_an_explicit_quit_request() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        page.add_init_script(
            """
            (() => {
              const originalFetch = globalThis.fetch;
              globalThis.fetch = function(url, options = {}) {
                const parsed = new URL(String(url), location.href);
                if (parsed.pathname !== "/api/v1/application/quit") {
                  return Reflect.apply(originalFetch, globalThis, [url, options]);
                }
                return new Promise((resolve, reject) => {
                  let settled = false;
                  const abort = () => {
                    if (settled) return;
                    settled = true;
                    const error = new Error("Aborted");
                    error.name = "AbortError";
                    reject(error);
                  };
                  options.signal?.addEventListener("abort", abort, { once: true });
                  window.__quitFetchHeld = true;
                  window.__quitFetchKeepalive = options.keepalive === true;
                  window.__releaseQuitFetch = () => {
                    if (settled) return;
                    settled = true;
                    options.signal?.removeEventListener("abort", abort);
                    Reflect.apply(originalFetch, globalThis, [url, options]).then(
                      resolve,
                      reject,
                    );
                  };
                });
              };
            })();
            """
        )
        page.goto(server.launch_url("setup"))

        page.evaluate(
            """
            () => {
              document.querySelector("#quit").click();
              dispatchEvent(new PageTransitionEvent("pagehide", { persisted: false }));
            }
            """
        )
        assert page.evaluate("window.__quitFetchHeld") is True
        assert page.evaluate("window.__quitFetchKeepalive") is True
        page.evaluate("window.__releaseQuitFetch()")

        assert application.quit_entered.wait(timeout=2)
        page.get_by_role(
            "heading", name="arXiv Digest is closed", exact=True
        ).wait_for()


def test_webkit_back_forward_cache_restore_reactivates_the_page() -> None:
    with running_fixture() as (server, application), browser_page("webkit") as page:
        page.goto(server.launch_url("setup"))
        initial_draft_requests = application.draft_requests
        deadline = time.monotonic() + 2
        while application.update_requests == 0 and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert application.update_requests > 0
        initial_update_requests = application.update_requests

        page.evaluate(
            """
            () => {
              dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true }));
              dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
            }
            """
        )
        deadline = time.monotonic() + 2
        while (
            (
                application.draft_requests <= initial_draft_requests
                or application.update_requests <= initial_update_requests
            )
            and time.monotonic() < deadline
        ):
            page.wait_for_timeout(50)
        assert application.draft_requests > initial_draft_requests
        assert application.update_requests > initial_update_requests

        navigate_with_history(page, "settings")
        page.get_by_role("heading", name="Settings", exact=True).wait_for()


def test_failed_corpus_refresh_does_not_replace_a_newly_opened_view() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.candidate_job = {
            "job_id": "corpus_job_1234",
            "status": "running",
            "complete": False,
            "failed": False,
        }

        try:
            page.goto(server.launch_url("setup"))
            page.get_by_role("button", name="Generate corpus").click()
            page.get_by_text(
                "Generating corpus… This can take up to five minutes.",
                exact=True,
            ).wait_for()

            application.draft_gate_request = application.draft_requests + 1
            application.draft_gate = threading.Event()
            application.expose_corpus_job = True
            application.candidate_job = {
                "job_id": "corpus_job_1234",
                "status": "failed",
                "complete": False,
                "failed": True,
                "error_code": "job_failed",
                "message": "The background operation did not complete.",
            }
            assert application.draft_gate_entered.wait(timeout=3)
            assert page.locator("#status").inner_text() == "Checking corpus status…"

            navigate_with_history(page, "settings")
            page.get_by_role("heading", name="Settings", exact=True).wait_for()
            application.draft_gate.set()
            page.wait_for_timeout(500)
            assert page.get_by_role(
                "heading", name="Settings", exact=True
            ).is_visible()
            assert page.get_by_role(
                "heading", name="Set up your arXiv digest", exact=True
            ).count() == 0
        finally:
            if application.draft_gate is not None:
                application.draft_gate.set()


def test_corpus_polling_restarts_after_returning_during_an_inflight_poll() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.expose_corpus_job = True
        application.corpus_status_gate = threading.Event()
        application.candidate_job = {
            "job_id": "corpus_job_1234",
            "status": "running",
            "complete": False,
            "failed": False,
        }

        try:
            page.goto(server.launch_url("setup"))
            assert application.corpus_status_entered.wait(timeout=2)

            navigate_with_history(page, "settings")
            page.get_by_role("heading", name="Settings", exact=True).wait_for()
            page.go_back()
            page.get_by_text(
                "Generating corpus… This can take up to five minutes.",
                exact=True,
            ).wait_for()

            deadline = time.monotonic() + 2
            while (
                application.corpus_status_requests < 3
                and time.monotonic() < deadline
            ):
                page.wait_for_timeout(50)
            assert application.corpus_status_requests >= 3

            application.candidate_job = {
                "job_id": "corpus_job_1234",
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
            page.get_by_text("Corpus ready", exact=True).wait_for(timeout=3_000)
        finally:
            application.corpus_status_gate.set()


def test_terminal_poll_reconciles_the_current_candidate_job_before_actions() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.corpus_status_gate = threading.Event()
        application.candidate_job = {
            "job_id": "corpus_job_1234",
            "status": "completed",
            "complete": True,
            "failed": False,
            "corpus_complete": True,
            "minimum_met": True,
            "setup_ready": True,
            "can_resume": False,
            "corpus_hash": "a" * 64,
            "reduced_breadth": False,
            "message": "Old corpus ready",
        }

        try:
            page.goto(server.launch_url("setup"))
            page.get_by_role("button", name="Generate corpus").click()
            assert application.corpus_status_entered.wait(timeout=2)

            application.candidate_job = {
                "job_id": "corpus_job_5678",
                "status": "running",
                "complete": False,
                "failed": False,
            }
            application.corpus_status_gate.set()
            deadline = time.monotonic() + 3
            while application.corpus_status_requests < 2 and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert application.corpus_status_requests >= 2
            assert page.locator('.setup-view[aria-busy="true"]').is_visible()
            assert page.get_by_text("Old corpus ready", exact=True).count() == 0
            assert page.get_by_role(
                "button", name="Use this corpus and continue", exact=True
            ).count() == 0

            application.candidate_job = {
                "job_id": "corpus_job_5678",
                "status": "completed",
                "complete": True,
                "failed": False,
                "corpus_complete": True,
                "minimum_met": True,
                "setup_ready": True,
                "can_resume": False,
                "corpus_hash": "b" * 64,
                "reduced_breadth": False,
                "message": "Current corpus ready",
            }
            page.get_by_text("Current corpus ready", exact=True).wait_for(
                timeout=3_000
            )
        finally:
            application.corpus_status_gate.set()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_failed_corpus_generation_preserves_resumable_cached_work(
    engine: str,
) -> None:
    with running_fixture() as (server, application), browser_page(engine) as page:
        application.revision = 2
        application.step = "candidate_corpus"
        application.candidate_job = {
            "job_id": "corpus_job_1234",
            "status": "running",
            "complete": False,
            "failed": False,
        }

        page.goto(server.launch_url("setup"))
        page.get_by_role("button", name="Generate corpus").click()
        page.get_by_text(
            "Generating corpus… This can take up to five minutes.", exact=True
        ).wait_for()

        application.corpus_can_resume = True
        application.expose_corpus_job = True
        application.candidate_job = {
            "job_id": "corpus_job_1234",
            "status": "failed",
            "complete": False,
            "failed": True,
            "error_code": "job_failed",
            "message": "The background operation did not complete.",
        }

        page.get_by_role("button", name="Resume corpus", exact=True).wait_for(
            timeout=3_000
        )
        assert page.get_by_role(
            "button", name="Restart corpus", exact=True
        ).is_visible()
        assert page.get_by_role("button", name="Retry corpus", exact=True).count() == 0


def test_create_launcher_is_explicit_and_dispatched_once() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.revision = 8
        application.step = "desktop_launcher"
        page.goto(server.launch_url("setup"))
        continue_button = page.get_by_role(
            "button", name="Finish setup", exact=True
        )
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
        page.get_by_text(
            "Leave the search blank to show all saved papers below.", exact=True
        ).wait_for()
        page.get_by_text("A dashboard library paper", exact=True).wait_for()
        abstract_link = page.get_by_role(
            "link", name="Abstract on arXiv", exact=True
        )
        pdf_link = page.get_by_role("link", name="PDF on arXiv", exact=True)
        assert abstract_link.get_attribute("href").endswith(
            "/abs/2608.01234v2"
        )
        assert pdf_link.get_attribute("href").endswith(
            "/pdf/2608.01234v2.pdf"
        )
        page.get_by_role("button", name="Download v2 PDF", exact=True).click()
        page.get_by_text("PDF download complete").wait_for()

        page.get_by_role("button", name="Interests").click()
        page.locator(
            ".interest-current-values li",
            has_text="Selected geometry (2608.01234)",
        ).wait_for()
        page.get_by_role("button", name="Add terms", exact=True).click()
        page.get_by_text("spectral sequence", exact=True).wait_for()
        page.get_by_text("spectral sequence", exact=True).click()
        page.get_by_label("Add custom term", exact=True).fill("custom topology")
        page.get_by_role("button", name="Add custom term", exact=True).click()
        page.get_by_role("button", name="Refresh suggestions", exact=True).click()
        page.get_by_text("Suggestions refreshed").wait_for()
        page.get_by_role("button", name="Update interests", exact=True).click()
        page.get_by_text("Interests updated").wait_for()

        page.get_by_role("button", name="Settings").click()
        page.get_by_text("Synchronization offline").wait_for()
        page.get_by_role("button", name="Choose PDF folder", exact=True).click()
        page.get_by_text("Selected folder: Research PDFs", exact=True).wait_for()
        page.get_by_role("button", name="Test and use folder", exact=True).click()
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
        ]
        assert interests["phrases"] == ["mirror symmetry", "custom topology"]


def test_library_distinguishes_no_saved_papers_from_no_search_matches() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.library_empty = True
        page.goto(server.launch_url("library"))
        page.get_by_text(
            "No saved papers yet. Save a paper from Review to add it here.",
            exact=True,
        ).wait_for()

        page.get_by_label("Search saved papers", exact=True).fill("topology")
        page.get_by_role("button", name="Search", exact=True).click()
        page.get_by_text("No saved papers match this search.", exact=True).wait_for()


def test_settings_uses_fallback_only_when_the_native_picker_is_unavailable() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.settings_folder_pick_result = {"unavailable": True}
        application.daily_list_target_dates = 32
        application.daily_list_checked_dates = 18
        application.daily_list_dates_with_papers = 10
        application.daily_list_empty_dates = 8
        application.daily_list_failed_dates = 2
        application.daily_list_pending_dates = 12
        application.daily_list_unavailable_dates = 1
        page.goto(server.launch_url("settings"))
        page.get_by_text("Synchronization offline").wait_for()
        page.get_by_role(
            "heading", name="Metadata synchronization", exact=True
        ).wait_for()
        page.get_by_role(
            "heading", name="Historical daily-list coverage", exact=True
        ).wait_for()
        page.get_by_text(
            "Target dates: 32 · 18 checked · 10 with papers · 8 empty · "
            "2 failed · 12 pending · 1 unavailable.",
            exact=True,
        ).wait_for()
        settings_text = page.locator("#content").inner_text()
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

        page.get_by_role("button", name="Choose PDF folder", exact=True).click()
        page.locator(".picker-status").get_by_text(
            "The native folder picker is unavailable", exact=False
        ).wait_for()
        page.get_by_role(
            "button", name="Test and use Downloads fallback", exact=True
        ).click()
        page.get_by_text("PDF destination saved.", exact=True).wait_for()

        assert (
            "settings_folder_test",
            {"destination_choice": "downloads"},
        ) in application.dashboard_calls


def test_settings_retries_failed_daily_list_dates_and_refreshes_when_complete() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.settings_missing_exact_dates = [
            "2026-08-11",
            "2026-08-12",
        ]
        application.settings_failed_daily_list_dates = [
            "2026-08-11",
            "2026-08-12",
        ]
        application.settings_retryable_failed_daily_list_dates = [
            "2026-08-11",
            "2026-08-12",
        ]
        application.sync_starts_running = True

        page.goto(server.launch_url("settings"))
        page.get_by_text(
            "Coverage starts 2026-07-01. 2 of 2 checked · 0 with papers · "
            "0 empty · 2 failed · 0 pending · 0 unavailable.",
            exact=True,
        ).wait_for()
        retry = page.get_by_role(
            "button", name="Retry 2 failed daily-list dates", exact=True
        )
        assert retry.is_enabled()

        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/sync/start"),
            timeout=2_000,
        ) as response_info:
            retry.click()

        assert response_info.value.status == 200
        page.get_by_text("Synchronization is running", exact=False).wait_for()
        retrying = page.get_by_role(
            "button",
            name="Retrying failed daily-list dates… 0 of 2 completed",
            exact=True,
        )
        assert retrying.is_disabled()
        assert [
            operation for operation, _payload in application.dashboard_calls
        ].count("sync_start") == 1

        with application.lock:
            application.daily_list_retry_completed = 1
        page.get_by_role(
            "button",
            name="Retrying failed daily-list dates… 1 of 2 completed",
            exact=True,
        ).wait_for(timeout=5_000)

        with application.lock:
            application.settings_missing_exact_dates = []
            application.settings_failed_daily_list_dates = []
            application.settings_retryable_failed_daily_list_dates = []
            application.sync_running = False

        page.get_by_text("Synchronization finished.", exact=True).wait_for(
            timeout=5_000
        )
        assert page.get_by_role(
            "button", name="Retry 2 failed daily-list dates", exact=True
        ).count() == 0
        assert [
            operation for operation, _payload in application.dashboard_calls
        ].count("settings_get") >= 2


def test_settings_refreshes_after_a_stale_folder_save_and_requires_a_new_pick() -> None:
    with running_fixture() as (server, application), browser_page("chromium") as page:
        application.settings_folder_save_failures = 1
        page.goto(server.launch_url("settings"))
        page.get_by_text("Synchronization offline").wait_for()

        page.get_by_role("button", name="Choose PDF folder", exact=True).click()
        page.get_by_text("Selected folder: Research PDFs", exact=True).wait_for()
        page.get_by_role("button", name="Test and use folder", exact=True).click()
        page.get_by_text("Choose the folder again", exact=False).wait_for()
        assert page.get_by_text(
            "Selected folder: Research PDFs", exact=True
        ).count() == 0
        assert page.get_by_role(
            "button", name="Test and use folder", exact=True
        ).count() == 0

        page.get_by_role("button", name="Choose PDF folder", exact=True).click()
        page.get_by_role("button", name="Test and use folder", exact=True).click()
        page.get_by_text("PDF destination saved.", exact=True).wait_for()

        operations = [operation for operation, _ in application.dashboard_calls]
        assert operations.count("settings_get") >= 3
        assert operations.count("settings_folder") == 2
