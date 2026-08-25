"""Application composition and command orchestration."""

from __future__ import annotations

import shutil
import secrets
import os
import stat
import sys
import tempfile
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from arxiv_digest import __version__
from arxiv_digest.maintenance import MaintenanceBarrier
from arxiv_digest.paths import AppPaths, resolve_paths
from arxiv_digest.profile import ProfileRepository
from arxiv_digest.setup import SetupRecoveryError
from arxiv_digest.storage.database import CorruptDatabaseError
from arxiv_digest.web.lifecycle import (
    ExistingInstance,
    LifecycleController,
    SingleInstance,
)
from arxiv_digest.web.server import LoopbackServer, StaticAsset


_MAX_ACTIVE_BACKGROUND_JOBS = 8
_MAX_RETAINED_TERMINAL_JOBS = 128


class Application:
    def __init__(
        self,
        *,
        paths: Any,
        profile_exists: Callable[[], bool],
        instance_factory: Callable[[], Any],
        server_factory: Callable[[Mapping[str, Callable]], Any],
        handlers_factory: Callable[[], Mapping[str, Callable]],
        resolve_restore_journal: Callable[[], None],
        open_database: Callable[[], Any],
        start_sync: Callable[[], Any],
        browser_open: Callable[[str], bool],
        wait_for_server: Callable[[Any], None],
        output: Callable[[str], None] = print,
        doctor_action: Callable[[], int] | None = None,
        export_action: Callable[[Path], int] | None = None,
        import_action: Callable[[Path], int] | None = None,
        install_launcher_action: Callable[[], int] | None = None,
    ) -> None:
        self.paths = paths
        self.profile_exists = profile_exists
        self.instance_factory = instance_factory
        self.server_factory = server_factory
        self.handlers_factory = handlers_factory
        self.resolve_restore_journal = resolve_restore_journal
        self.open_database_action = open_database
        self.start_sync = start_sync
        self.browser_open = browser_open
        self.wait_for_server = wait_for_server
        self.output = output
        self.doctor_action = doctor_action
        self.export_action = export_action
        self.import_action = import_action
        self.install_launcher_action = install_launcher_action

    def _view(self, intent: str, initialized: bool) -> str:
        if intent == "library":
            return "library"
        if intent == "config":
            return "settings"
        if intent == "init":
            return "interests" if initialized else "setup"
        if intent == "default":
            return "review" if initialized else "setup"
        raise ValueError("unsupported dashboard intent")

    def open_dashboard(self, intent: str) -> int:
        initialized = self.profile_exists()
        view = self._view(intent, initialized)
        self.paths.ensure()
        instances = self.instance_factory()
        claim = instances.acquire()
        if isinstance(claim, ExistingInstance):
            descriptor = claim.descriptor
            url = (
                f"http://127.0.0.1:{descriptor.port}/"
                f"#token={descriptor.token}&view={view}"
            )
            if not self.browser_open(url):
                self.output(url)
            return 0
        server = None
        database = None
        try:
            self.resolve_restore_journal()
            initialized = self.profile_exists()
            view = self._view(intent, initialized)
            try:
                database = self.open_database_action()
            except (CorruptDatabaseError, SetupRecoveryError, OSError, ValueError):
                self.output(
                    "The local database could not be opened safely. Its bytes "
                    "were preserved; use a verified backup or run "
                    "arxiv-digest doctor for redacted recovery guidance."
                )
                return 2
            server = self.server_factory(self.handlers_factory())
            server.start()
            claim.publish(
                port=server.port,
                startup_nonce=server.startup_nonce,
                token=server.token,
            )
            if intent == "default" and initialized:
                self.start_sync()
            url = server.launch_url(view)
            if not self.browser_open(url):
                self.output(url)
            self.wait_for_server(server)
            return 0
        finally:
            try:
                if server is not None:
                    server.stop()
            finally:
                try:
                    close = getattr(database, "close", None)
                    if callable(close):
                        close()
                finally:
                    instances.release()

    def doctor(self) -> int:
        return 0 if self.doctor_action is None else self.doctor_action()

    def export_backup(self, destination: Path) -> int:
        if destination.exists():
            raise FileExistsError("refusing to overwrite an existing backup")
        if self.export_action is None:
            raise RuntimeError("backup export is unavailable")
        self.paths.ensure()
        instances = self.instance_factory()
        claim = instances.acquire()
        if isinstance(claim, ExistingInstance):
            raise RuntimeError(
                "The dashboard is running; export from Settings instead."
            )
        try:
            self.resolve_restore_journal()
            return self.export_action(destination)
        finally:
            instances.release()

    def import_backup(self, source: Path) -> int:
        if self.import_action is None:
            raise RuntimeError("backup restore is unavailable")
        self.paths.ensure()
        instances = self.instance_factory()
        claim = instances.acquire()
        if isinstance(claim, ExistingInstance):
            raise RuntimeError(
                "The dashboard is running; Quit it before importing a backup."
            )
        try:
            self.resolve_restore_journal()
            return self.import_action(source)
        finally:
            instances.release()

    def install_launcher(self) -> int:
        if self.install_launcher_action is None:
            raise RuntimeError("launcher installation is unavailable")
        return self.install_launcher_action()


_STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ttf": "font/ttf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

_RESTORE_UPLOAD_PREFIX = ".arxiv-digest-restore-"
_BROWSER_RESTORE_WAIT_SECONDS = 45.0
_PENDING_RESTORE_TTL_SECONDS = 15 * 60
_MAX_PENDING_RESTORES = 2
_MAX_PENDING_RESTORE_BYTES = 64 * 1024 * 1024


def _packaged_static_assets() -> dict[str, StaticAsset]:
    """Materialize the packaged tree into a fixed URL allowlist."""

    root = files("arxiv_digest.web").joinpath("static")
    assets: dict[str, StaticAsset] = {}

    def visit(resource: Any, prefix: str = "") -> None:
        for child in resource.iterdir():
            name = f"{prefix}/{child.name}"
            if child.is_dir():
                visit(child, name)
                continue
            content_type = (
                "text/plain; charset=utf-8"
                if child.name == "LICENSE"
                else _STATIC_CONTENT_TYPES.get(Path(child.name).suffix)
            )
            if content_type is not None:
                assets[name] = StaticAsset(content_type, child.read_bytes())

    visit(root)
    return assets


def _category_from_set_spec(set_spec: str) -> str:
    """Extract the category label while retaining the exact OAI set pair."""

    if not isinstance(set_spec, str) or not set_spec.strip():
        raise ValueError("OAI set specification must be nonblank")
    parts = set_spec.split(":")
    if len(parts) == 1:
        return set_spec
    if any(not part for part in parts):
        raise ValueError("OAI set specification has no category component")
    leaf = parts[-1]
    if "." in leaf or parts[0].casefold() == "arxiv":
        return leaf
    if len(parts) >= 3:
        return f"{parts[-2]}.{leaf}"
    prefix = parts[0]
    if prefix == leaf:
        return leaf
    if prefix in {"cs", "math", "stat", "econ", "eess", "q-bio", "q-fin"}:
        return f"{prefix}.{leaf}"
    return leaf


class _DefaultRuntime:
    """Lazy production service graph owned by one dashboard process."""

    def __init__(
        self,
        paths: AppPaths,
        profiles: ProfileRepository,
        maintenance: MaintenanceBarrier,
        lifecycle: LifecycleController,
        *,
        output: Callable[[str], None],
    ) -> None:
        self.paths = paths
        self.profiles = profiles
        self.maintenance = maintenance
        self.lifecycle = lifecycle
        self.output = output
        self.store: Any = None
        self.setup: Any = None
        self.review: Any = None
        self.library: Any = None
        self.downloads: Any = None
        self.sync: Any = None
        self.folder: Any = None
        self.launcher: Any = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._jobs_lock = threading.RLock()
        self._candidate_state_lock = threading.RLock()
        self._candidate_build: Any = None
        self._candidate_job_id: str | None = None
        self._candidate_job_revision: int | None = None
        self._suggestions: dict[str, tuple[int, str, str, Any]] = {}
        self._suggestion_ids: dict[tuple[int, str, str, str], str] = {}
        self._picker_choices: dict[str, Any] = {}
        self._tested_destinations: dict[str, tuple[int | None, Any]] = {}
        self._pending_restores: dict[str, Any] = {}
        self._pending_restores_lock = threading.RLock()
        self._pending_restore_reservations: dict[str, int] = {}
        self._restore_cleanup_timer: threading.Timer | None = None
        self._restore_shutdown = False
        self._sync_cancel = threading.Event()
        self._sync_start_lock = threading.Lock()
        self._active_sync_job: str | None = None
        self._sync_follow_up_requested = False
        self._last_sync_report: Any = None
        self._category_values: tuple[Any, ...] | None = None
        self._issued_category_pairs: set[tuple[str, str]] = set()

    @staticmethod
    def _launcher_manager() -> Any | None:
        from arxiv_digest.desktop_launcher import DesktopLauncherManager

        executable = shutil.which("arxiv-digest")
        if executable is None:
            return None
        return DesktopLauncherManager(
            platform=sys.platform,
            home=Path.home(),
            executable=Path(executable),
        )

    def open_database(self) -> Any:
        self.profiles.load()

        from arxiv_digest.downloads import DownloadManager
        from arxiv_digest.candidates import CandidateCache, CandidateCorpusBuilder
        from arxiv_digest.folders import FolderService
        from arxiv_digest.library import LibraryService
        from arxiv_digest.rate_limit import ArxivHttpClient
        from arxiv_digest.review import ReviewService
        from arxiv_digest.setup import SetupService
        from arxiv_digest.sources.atom import AtomSource
        from arxiv_digest.sources.catchup import CatchupSource
        from arxiv_digest.sources.oai import OaiSource
        from arxiv_digest.storage.database import open_database
        from arxiv_digest.storage.store import Store
        from arxiv_digest.sync import SyncService

        self._cleanup_orphaned_restore_uploads()
        connection = open_database(self.paths.database_path)
        # Service objects open short-lived, maintenance-leased connections on
        # demand. Keeping this bootstrap WAL connection for the server
        # lifetime would leave an unleased handle to the database that browser
        # restore replaces, so close it immediately after validation/migration.
        connection.close()
        self.store = Store(self.paths.database_path, maintenance=self.maintenance)
        self.folder = FolderService()
        self.launcher = self._launcher_manager()
        self.setup = SetupService(
            self.store,
            self.profiles,
            launcher_manager=self.launcher,
            maintenance=self.maintenance,
        )
        try:
            self.setup.recover()
        except Exception:
            connection.close()
            raise
        self.review = ReviewService(self.store, self.profiles)
        self.library = LibraryService(self.store)
        client = ArxivHttpClient(
            user_agent=f"arxiv-digest/{__version__}",
            contact_url="https://github.com/yuzhangmath/arxiv-digest",
        )
        oai = OaiSource(client)
        self.sync = SyncService(
            self.store,
            oai,
            AtomSource(client),
            CatchupSource(client),
            cancelled=self._sync_cancel.is_set,
        )
        self.downloads = DownloadManager(self.store, self.profiles, client)
        self._oai = oai
        self.candidates = CandidateCorpusBuilder(
            oai,
            CandidateCache(self.paths),
            local_document_provider=self._local_candidate_documents,
        )
        return SimpleNamespace(close=self._close_runtime)

    def _cleanup_orphaned_restore_uploads(self) -> None:
        cache = self.paths.cache_dir
        if not cache.exists():
            return
        for candidate in cache.iterdir():
            if not (
                candidate.name.startswith(_RESTORE_UPLOAD_PREFIX)
                and candidate.suffix == ".zip"
            ):
                continue
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                continue
            if info.st_mode & 0o077:
                continue
            candidate.unlink(missing_ok=True)

    def _close_runtime(self) -> None:
        with self._pending_restores_lock:
            self._restore_shutdown = True
            timer = self._restore_cleanup_timer
            self._restore_cleanup_timer = None
            pending = tuple(self._pending_restores.values())
            self._pending_restores.clear()
            self._pending_restore_reservations.clear()
        if timer is not None:
            timer.cancel()
        for _created, inspection in pending:
            inspection.path.unlink(missing_ok=True)

    def _require_open(self) -> None:
        if self.store is None:
            raise RuntimeError("application services are not initialized")

    def known_paper(self, arxiv_id: str) -> bool:
        self._require_open()
        try:
            self.store.article_metadata(arxiv_id)
        except KeyError:
            return False
        return True

    def status(self, payload: dict[str, Any]) -> dict[str, Any]:
        from arxiv_digest.doctor import _redacted_sync_error_code

        sync_job = None
        with self._jobs_lock:
            if self._active_sync_job is not None:
                sync_job = dict(self._jobs.get(self._active_sync_job, {}))
                sync_job.pop("result", None)
        retry = None if sync_job is None else sync_job.get("daily_list_retry")
        if not isinstance(retry, dict):
            retry_dates = self._retryable_sync_dates()
            retry = {
                "status": (
                    "running"
                    if sync_job is not None
                    and sync_job.get("status") == "running"
                    and sync_job.get("phase", "daily_list") == "daily_list"
                    and retry_dates
                    else "idle"
                ),
                "completed": 0,
                "total": len(
                    {
                        mailing_date
                        for dates in retry_dates.values()
                        for mailing_date in dates
                    }
                ),
            }
        else:
            retry = dict(retry)
        offline = bool(
            self._last_sync_report is not None
            and self._last_sync_report.offline
        )
        metadata_sync = None
        daily_list_progress = None
        progress_snapshot = getattr(getattr(self, "sync", None), "progress", None)
        if self.profiles.load() is not None and callable(progress_snapshot):
            report = progress_snapshot(self._sync_configs(), offline=offline)
            metadata_categories = []
            daily_list_categories = []
            for category_progress in report.categories:
                metadata = getattr(category_progress, "metadata_sync", None)
                if metadata is not None:
                    metadata_error = getattr(
                        metadata, "last_error_code", None
                    )
                    metadata_categories.append(
                        {
                            "category": category_progress.category,
                            "status": metadata.status,
                            "synchronized_through": (
                                None
                                if metadata.completed_through_utc is None
                                else metadata.completed_through_utc.isoformat()
                            ),
                            "error_codes": (
                                []
                                if metadata_error is None
                                else [
                                    _redacted_sync_error_code(metadata_error)
                                ]
                            ),
                        }
                    )
                if not hasattr(category_progress, "target_dates"):
                    continue
                daily_list_categories.append(
                    {
                        "category": category_progress.category,
                        "target_dates": len(category_progress.target_dates),
                        "checked_dates": len(category_progress.checked_dates),
                        "dates_with_papers": len(
                            category_progress.dates_with_papers
                        ),
                        "empty_dates": len(category_progress.empty_dates),
                        "failed_dates": len(category_progress.failed_dates),
                        "pending_dates": len(category_progress.pending_dates),
                        "unavailable_dates": len(
                            category_progress.unavailable_dates
                        ),
                        "status": category_progress.daily_list_status,
                        "error_codes": sorted(
                            {
                                _redacted_sync_error_code(error_code)
                                for _day, error_code in getattr(
                                    category_progress,
                                    "daily_list_errors",
                                    (),
                                )
                                if error_code is not None
                            }
                        ),
                    }
                )
            metadata_sync = {
                "status": (
                    "complete"
                    if bool(getattr(report, "metadata_complete", False))
                    else "incomplete"
                ),
                "categories": metadata_categories,
            }
            daily_list_progress = {
                "target_dates": report.target_dates,
                "checked_dates": report.checked_dates,
                "dates_with_papers": report.dates_with_papers,
                "empty_dates": report.empty_dates,
                "failed_dates": report.failed_dates,
                "pending_dates": report.pending_dates,
                "unavailable_dates": report.unavailable_dates,
                "status": report.daily_list_status,
            }
            if daily_list_categories:
                daily_list_progress["categories"] = daily_list_categories
        return {
            "state": "ready",
            "initialized": self.profiles.load() is not None,
            "sync": sync_job,
            "daily_list_retry": retry,
            "metadata_sync": metadata_sync,
            "daily_list_progress": daily_list_progress,
            "offline": offline,
        }

    def _review_date(self, payload: dict[str, Any]) -> Any:
        from arxiv_digest.web.api import ReviewPagePayload

        day = date.fromisoformat(payload["date"])
        page = self.review.open_date(
            day,
            anchor_event_id=payload.get("anchor_event_id"),
            from_start=payload.get("from_start", False),
        )
        versions: dict[str, int] = {}
        for card in page.cards:
            article_versions = self.store.article_versions(card.paper.arxiv_id)
            if article_versions:
                versions[card.paper.arxiv_id] = article_versions[-1].number
        return ReviewPagePayload(
            page,
            page.last_finished_revision,
            latest_known_versions=versions,
        )

    def _review_position(self, payload: dict[str, Any]) -> Any:
        return self.review.record_position(
            date.fromisoformat(payload["date"]),
            snapshot_revision=payload["snapshot_revision"],
            anchor_event_id=payload["anchor_event_id"],
            profile_revision=payload["profile_revision"],
            projection_revision=payload["projection_revision"],
        )

    def _review_finish(self, payload: dict[str, Any]) -> Any:
        day = date.fromisoformat(payload["date"])
        result = self.review.finish_date(
            day,
            through_revision=payload["snapshot_revision"],
            profile_revision=payload["profile_revision"],
            projection_revision=payload["projection_revision"],
            finished_at=datetime.now(timezone.utc),
        )
        next_date = self.review.next_later_unreviewed_date(day)
        return {
            "reviewed_count": result.reviewed_count,
            "through_revision": result.through_revision,
            "next_later_unreviewed_date": (
                None if next_date is None else next_date.isoformat()
            ),
        }

    def _review_finish_all(self, payload: dict[str, Any]) -> Any:
        return self.review.finish_all(
            through_revision=payload["snapshot_revision"],
            profile_revision=payload["profile_revision"],
            projection_revision=payload["projection_revision"],
            finished_at=datetime.now(timezone.utc),
        )

    def _library_page(self, payload: dict[str, Any]) -> dict[str, Any]:
        page = self.library.search(
            payload.get("q", ""),
            limit=20,
            offset=payload.get("offset", 0),
        )
        return {
            "query": payload.get("q", ""),
            "entries": page.entries,
            "limit": page.limit,
            "offset": page.offset,
            "previous_offset": max(0, page.offset - page.limit)
            if page.offset
            else None,
            "next_offset": page.next_offset,
        }

    def _new_job(
        self,
        prefix: str,
        kind: str,
        operation: Callable[[], Any],
        *,
        job_id: str | None = None,
        request_cancel: Callable[[], None] | None = None,
        initial_fields: Mapping[str, Any] | None = None,
    ) -> str:
        job_id = job_id or f"{prefix}_{secrets.token_urlsafe(12)}"
        with self._jobs_lock:
            active_jobs = sum(
                value.get("status") == "running"
                for value in self._jobs.values()
            )
            if active_jobs >= _MAX_ACTIVE_BACKGROUND_JOBS:
                raise ValueError("too many active background jobs")
            if job_id in self._jobs:
                raise ValueError("background job identifier already exists")
            job = {
                "job_id": job_id,
                "status": "running",
                "complete": False,
                "failed": False,
            }
            job.update(initial_fields or {})
            self._jobs[job_id] = job

        worker_registered = threading.Event()

        def prune_terminal_jobs() -> None:
            terminal_ids = [
                identifier
                for identifier, value in self._jobs.items()
                if value.get("status") != "running"
            ]
            for identifier in terminal_ids[:-_MAX_RETAINED_TERMINAL_JOBS]:
                self._jobs.pop(identifier, None)

        def record_failure(error: Exception) -> None:
            code = getattr(error, "code", None)
            with self._jobs_lock:
                self._jobs[job_id].update(
                    status="failed",
                    complete=False,
                    failed=True,
                    error_code=(
                        code if isinstance(code, str) else "job_failed"
                    ),
                    message="The background operation did not complete.",
                )
                prune_terminal_jobs()

        def run() -> None:
            try:
                with self.maintenance.worker(
                    job_id,
                    request_cancel or (lambda: None),
                ):
                    worker_registered.set()
                    self.lifecycle.worker_started(kind, job_id)
                    try:
                        try:
                            result = operation()
                        except Exception as error:
                            record_failure(error)
                        else:
                            with self._jobs_lock:
                                self._jobs[job_id].update(
                                    status="completed",
                                    complete=True,
                                    failed=False,
                                    result=result,
                                )
                                prune_terminal_jobs()
                    finally:
                        self.lifecycle.worker_finished(kind, job_id)
            except Exception as error:
                record_failure(error)
            finally:
                worker_registered.set()

        threading.Thread(
            target=run,
            name=f"arxiv-digest-{prefix}",
            daemon=True,
        ).start()
        # Do not expose a running job ID until it is registered. Otherwise a
        # restore can acquire exclusive maintenance in the thread-start gap
        # and let the stale job begin after replacement.
        worker_registered.wait()
        return job_id

    def _job_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._jobs_lock:
            job = self._jobs.get(payload["job_id"])
            if job is None:
                raise KeyError("unknown job")
            result = dict(job)
        value = result.pop("result", None)
        if value is not None:
            if hasattr(value, "diagnostics"):
                diagnostics = value.diagnostics
                result.update(
                    corpus_complete=diagnostics.complete,
                    corpus_hash=diagnostics.corpus_hash,
                    reduced_breadth=diagnostics.reduced_breadth,
                    setup_ready=diagnostics.setup_ready,
                    minimum_met=diagnostics.minimum_met,
                    pages_fetched=diagnostics.pages_fetched,
                    can_resume=diagnostics.can_resume,
                    progress=diagnostics.progress,
                )
            else:
                result["value"] = value
        return result

    @staticmethod
    def _category_configs(draft: Any) -> tuple[Any, ...]:
        from arxiv_digest.models import CategoryConfig

        if draft.coverage_start is None:
            raise ValueError("select initial coverage before building candidates")
        return tuple(
            CategoryConfig(
                item.category,
                item.set_spec,
                draft.coverage_start,
            )
            for item in draft.categories
        )

    def _local_candidate_documents(
        self,
        configs: tuple[Any, ...],
        window_start: date,
        window_end: date,
    ) -> tuple[Any, ...]:
        from arxiv_digest.candidates import CandidateDocument

        documents = []
        for config in configs:
            evidence_by_id: dict[str, list[date]] = {}
            for arxiv_id, mailing_date in self.store.candidate_mailing_evidence(
                config.category,
                window_start,
                window_end,
            ):
                evidence_by_id.setdefault(arxiv_id, []).append(mailing_date)
            for arxiv_id in sorted(evidence_by_id):
                documents.append(
                    CandidateDocument(
                        paper=self.store.article_metadata(arxiv_id),
                        versions=self.store.article_versions(arxiv_id),
                        eligible_categories=(config.category,),
                        evidence_dates=tuple(
                            sorted(set(evidence_by_id[arxiv_id]))
                        ),
                    )
                )
        return tuple(documents)

    def _start_candidate_corpus(self, payload: dict[str, Any]) -> dict[str, str]:
        with self._candidate_state_lock:
            draft = self.setup.load_draft()
            if draft is None or draft.revision != payload["draft_revision"]:
                from arxiv_digest.setup import SetupRevisionError

                raise SetupRevisionError(
                    payload["draft_revision"],
                    None if draft is None else draft.revision,
                )
            configs = self._category_configs(draft)
            cancellation = threading.Event()

            def build() -> Any:
                result = (
                    self.candidates.retry(
                        configs,
                        cancelled=cancellation.is_set,
                    )
                    if payload["mode"] == "restart"
                    else self.candidates.resume(
                        configs,
                        cancelled=cancellation.is_set,
                    )
                )
                self._candidate_build = result
                self._suggestions.clear()
                self._suggestion_ids.clear()
                return result

            with self._jobs_lock:
                current = (
                    None
                    if self._candidate_job_id is None
                    else self._jobs.get(self._candidate_job_id)
                )
            if (
                self._candidate_job_id is not None
                and self._candidate_job_revision == draft.revision
                and current is not None
                and current.get("status") == "running"
            ):
                return {"job_id": self._candidate_job_id}
            job_id = f"setup_{secrets.token_urlsafe(12)}"
            self._candidate_job_id = job_id
            self._candidate_job_revision = draft.revision
            try:
                self._new_job(
                    "setup",
                    "sync",
                    build,
                    job_id=job_id,
                    request_cancel=cancellation.set,
                )
            except Exception:
                if self._candidate_job_id == job_id:
                    self._candidate_job_id = None
                    self._candidate_job_revision = None
                raise
            return {"job_id": job_id}

    def _accept_candidate_corpus(self, payload: dict[str, Any]) -> Any:
        with self._candidate_state_lock:
            draft = self.setup.load_draft()
            if draft is None or draft.revision != payload["draft_revision"]:
                from arxiv_digest.setup import SetupRevisionError

                raise SetupRevisionError(
                    payload["draft_revision"],
                    None if draft is None else draft.revision,
                )
            with self._jobs_lock:
                active = (
                    None
                    if self._candidate_job_id is None
                    else self._jobs.get(self._candidate_job_id)
                )
            if (
                self._candidate_job_revision == draft.revision
                and active is not None
                and active.get("status") == "running"
            ):
                raise ValueError("candidate corpus generation is still running")
            build = self._candidate_build
            if build is None or build.corpus_hash != payload["corpus_hash"]:
                raise ValueError("candidate corpus changed; resume it again")
            if not build.complete and build.minimum_met:
                build = self.candidates.resume(
                    self._category_configs(draft),
                    accept_reduced_breadth=True,
                )
                if build.corpus_hash != payload["corpus_hash"]:
                    raise ValueError("candidate corpus changed; inspect it again")
                self._candidate_build = build
            revised = self.setup.accept_candidate_corpus(
                draft.revision,
                build,
                corpus_hash=payload["corpus_hash"],
            )
            return self._setup_payload(revised)

    @staticmethod
    def _suggestion_identity(kind: str, value: Any) -> str:
        if kind == "paper":
            return value.paper.arxiv_id
        if kind == "author":
            return value.name
        return f"{value.kind}:{value.value}"

    def _register_suggestion(
        self,
        draft: Any,
        kind: str,
        value: Any,
    ) -> str:
        if draft.corpus_hash is None:
            raise ValueError("candidate corpus is not accepted")
        key = (
            draft.revision,
            draft.corpus_hash,
            kind,
            self._suggestion_identity(kind, value),
        )
        suggestion_id = self._suggestion_ids.get(key)
        if suggestion_id is None:
            suggestion_id = f"suggest_{secrets.token_urlsafe(12)}"
            self._suggestion_ids[key] = suggestion_id
            self._suggestions[suggestion_id] = (
                draft.revision,
                draft.corpus_hash,
                kind,
                value,
            )
        return suggestion_id

    def _resolve_suggestions(
        self,
        draft: Any,
        identifiers: list[str],
        kind: str,
    ) -> tuple[Any, ...]:
        values = []
        for identifier in identifiers:
            registered = self._suggestions.get(identifier)
            if (
                registered is None
                or registered[:3]
                != (draft.revision, draft.corpus_hash, kind)
            ):
                raise ValueError("suggestion is missing, stale, or from another step")
            values.append(registered[3])
        if len(values) != len({self._suggestion_identity(kind, value) for value in values}):
            raise ValueError("suggestions must be unique")
        return tuple(values)

    def _candidate_suggestions(self) -> Any:
        from arxiv_digest.candidates import build_suggestions

        draft = self.setup.load_draft()
        if draft is None:
            raise ValueError("setup draft is unavailable")
        build = self._accepted_candidate_build(draft)
        if build is None:
            raise ValueError("candidate corpus is unavailable")
        corpus = build.corpus
        corpus_ids = {
            document.paper.arxiv_id for document in corpus.documents
        }
        return build_suggestions(
            corpus,
            tuple(
                document.paper.arxiv_id
                for document in draft.seed_papers
                if document.paper.arxiv_id in corpus_ids
            ),
            (*draft.keywords, *draft.phrases),
            draft.authors,
        )

    def _accepted_candidate_build(self, draft: Any) -> Any | None:
        """Return only the corpus bound to the draft's persisted accepted hash."""

        expected_hash = getattr(draft, "corpus_hash", None)
        if expected_hash is None:
            return None
        selected = tuple(item.category for item in draft.categories)
        current = self._candidate_build
        if (
            current is not None
            and getattr(current, "corpus_hash", None) == expected_hash
            and tuple(current.corpus.categories) == selected
        ):
            return current
        corpus = self.candidates.cache.load_accepted_corpus(
            self._category_configs(draft),
            expected_hash=expected_hash,
        )
        if corpus is None:
            return None
        current = SimpleNamespace(corpus=corpus, corpus_hash=expected_hash)
        self._candidate_build = current
        self._suggestions.clear()
        self._suggestion_ids.clear()
        return current

    @staticmethod
    def _paper_value(document: Any) -> dict[str, Any]:
        paper = document.paper
        return {
            "arxiv_id": paper.arxiv_id,
            "title": paper.title,
            "authors": list(paper.authors),
            "abstract": paper.abstract,
            "primary_category": paper.primary_category,
            "categories": list(paper.categories),
        }

    def _candidate_papers(self, payload: dict[str, Any]) -> dict[str, Any]:
        from arxiv_digest.candidates import search_candidate_papers

        draft = self.setup.load_draft()
        if draft is None:
            raise ValueError("candidate corpus is unavailable")
        build = self._accepted_candidate_build(draft)
        if build is None:
            raise ValueError("candidate corpus is unavailable")
        documents = search_candidate_papers(
            build.corpus,
            payload.get("q", ""),
            offset=payload.get("offset", 0),
            limit=30,
        )
        items = []
        for document in documents:
            suggestion_id = self._register_suggestion(
                draft, "paper", document
            )
            items.append(
                {
                    "suggestion_id": suggestion_id,
                    **self._paper_value(document),
                }
            )
        return {
            "items": items,
            "offset": payload.get("offset", 0),
            "next_offset": (
                payload.get("offset", 0) + len(items)
                if len(items) == 30
                else None
            ),
        }

    def _candidate_terms(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = self.setup.load_draft()
        if draft is None:
            raise ValueError("setup draft is unavailable")
        suggestions = self._candidate_suggestions()

        def value(item: Any) -> dict[str, Any]:
            return {
                "suggestion_id": self._register_suggestion(draft, "term", item),
                "value": item.value,
                "kind": item.kind,
                "score": item.score,
                "reasons": list(item.reasons),
            }

        return {
            "keywords": [
                value(item) for item in suggestions.terms if item.kind == "keyword"
            ],
            "phrases": [
                value(item) for item in suggestions.terms if item.kind == "phrase"
            ],
        }

    def _candidate_authors(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = self.setup.load_draft()
        if draft is None:
            raise ValueError("setup draft is unavailable")
        query = payload.get("q", "").casefold()
        items = []
        for item in self._candidate_suggestions().authors:
            if query and query not in item.name.casefold():
                continue
            items.append(
                {
                    "suggestion_id": self._register_suggestion(
                        draft, "author", item
                    ),
                    "name": item.name,
                    "score": item.score,
                    "reasons": list(item.reasons),
                }
            )
        return {"items": items}

    def _lookup_candidate_paper(self, payload: dict[str, Any]) -> dict[str, Any]:
        draft = self.setup.load_draft()
        if draft is None or draft.revision != payload["draft_revision"]:
            raise ValueError("setup draft revision changed")
        document = self.candidates.lookup(payload["arxiv_id"])
        return {
            "suggestion_id": self._register_suggestion(draft, "paper", document),
            **self._paper_value(document),
        }

    def _supported_coverage_bounds(self) -> tuple[date, date]:
        from arxiv_digest.sync import (
            DEFAULT_CATCHUP_FINALIZATION_HOUR,
            SUPPORTED_CATCHUP_WINDOW_DAYS,
            daily_list_coverage_bounds,
        )

        sync_bounds = getattr(getattr(self, "sync", None), "coverage_bounds", None)
        if callable(sync_bounds):
            return sync_bounds()
        clock = getattr(getattr(self, "setup", None), "clock", None)
        observed_at = (
            clock() if callable(clock) else datetime.now(timezone.utc)
        )
        window_days = getattr(
            getattr(self, "sync", None),
            "catchup_window_days",
            SUPPORTED_CATCHUP_WINDOW_DAYS,
        )
        if type(window_days) is not int or not (
            1 <= window_days <= SUPPORTED_CATCHUP_WINDOW_DAYS
        ):
            window_days = SUPPORTED_CATCHUP_WINDOW_DAYS
        finalization_hour = getattr(
            getattr(self, "sync", None),
            "catchup_finalization_hour",
            DEFAULT_CATCHUP_FINALIZATION_HOUR,
        )
        if type(finalization_hour) is not int or not 0 <= finalization_hour <= 23:
            finalization_hour = DEFAULT_CATCHUP_FINALIZATION_HOUR
        return daily_list_coverage_bounds(
            observed_at,
            window_days=window_days,
            finalization_hour=finalization_hour,
        )

    def _setup_payload(self, draft: Any) -> Any:
        from arxiv_digest.web.api import SetupDraftPayload

        coverage_min, coverage_max = self._supported_coverage_bounds()
        return SetupDraftPayload(
            draft,
            corpus_can_resume=self._candidate_cache_can_resume(draft),
            corpus_job=self._candidate_job_for_draft(draft),
            coverage_min=coverage_min,
            coverage_max=coverage_max,
        )

    def _candidate_job_for_draft(self, draft: Any) -> dict[str, Any] | None:
        state_lock = getattr(self, "_candidate_state_lock", None)
        if state_lock is None:
            return None
        with state_lock:
            if (
                getattr(self, "_candidate_job_id", None) is None
                or getattr(self, "_candidate_job_revision", None)
                != getattr(draft, "revision", None)
            ):
                return None
            job_id = self._candidate_job_id
            try:
                return self._job_status({"job_id": job_id})
            except KeyError:
                return None

    def _candidate_cache_can_resume(self, draft: Any) -> bool:
        from arxiv_digest.candidates import CandidateCacheError
        from arxiv_digest.setup import SetupStep

        if (
            getattr(draft, "current_step", None)
            is not SetupStep.CANDIDATE_CORPUS
            or getattr(draft, "corpus_hash", None) is not None
        ):
            return False
        candidates = getattr(self, "candidates", None)
        if candidates is None:
            return False
        observed_at = candidates.clock()
        window_end = observed_at.date()
        window_start = window_end - timedelta(days=89)
        for selection in draft.categories:
            try:
                shard = candidates.cache.load_shard(
                    selection.category,
                    selection.set_spec,
                    window_start=window_start,
                    window_end=window_end,
                    now=observed_at,
                )
            except (CandidateCacheError, OSError, ValueError):
                continue
            if shard is not None:
                return True
        return False

    def _setup_draft_put(self, payload: dict[str, Any]) -> Any:
        from arxiv_digest.candidates import (
            validate_custom_author,
            validate_custom_keyword,
            validate_custom_phrase,
        )
        from arxiv_digest.setup import CategorySelection
        from arxiv_digest.web.api import project_setup_draft

        revision = payload["revision"]
        step = payload["step"]
        if step == "categories":
            requested_pairs = {
                (item["category"], item["set_spec"])
                for item in payload["selections"]
            }
            if not requested_pairs <= self._issued_category_pairs:
                raise ValueError(
                    "category selections must come from current category browsing"
                )
            draft = self.setup.select_categories(
                revision,
                tuple(
                    CategorySelection(item["category"], item["set_spec"])
                    for item in payload["selections"]
                ),
            )
        elif step == "coverage":
            earliest = self._oai.identify().earliest_datestamp
            draft = self.setup.set_initial_coverage(
                revision,
                date.fromisoformat(payload["coverage_start"]),
                earliest_datestamp=earliest,
                coverage_bounds=self._supported_coverage_bounds(),
            )
        elif step == "seed_papers":
            current = self.setup.load_draft()
            if current is None or current.revision != revision:
                raise ValueError("setup draft revision changed")
            accepted = self._resolve_suggestions(
                current,
                payload["accepted_suggestion_ids"],
                "paper",
            )
            custom = tuple(
                self.candidates.lookup(arxiv_id)
                for arxiv_id in payload["custom_arxiv_ids"]
            )
            draft = self.setup.select_seed_papers(
                revision, (*accepted, *custom)
            )
        elif step == "terms":
            current = self.setup.load_draft()
            if current is None or current.revision != revision:
                raise ValueError("setup draft revision changed")
            keyword_values = self._resolve_suggestions(
                current,
                payload["accepted_keyword_suggestion_ids"],
                "term",
            )
            phrase_values = self._resolve_suggestions(
                current,
                payload["accepted_phrase_suggestion_ids"],
                "term",
            )
            if any(item.kind != "keyword" for item in keyword_values) or any(
                item.kind != "phrase" for item in phrase_values
            ):
                raise ValueError("suggestion kind does not match the setup field")
            draft = self.setup.select_terms(
                revision,
                keywords=(
                    *(item.value for item in keyword_values),
                    *(
                        validate_custom_keyword(value)
                        for value in payload["custom_keywords"]
                    ),
                ),
                phrases=(
                    *(item.value for item in phrase_values),
                    *(
                        validate_custom_phrase(value)
                        for value in payload["custom_phrases"]
                    ),
                ),
            )
        elif step == "authors":
            current = self.setup.load_draft()
            if current is None or current.revision != revision:
                raise ValueError("setup draft revision changed")
            accepted = self._resolve_suggestions(
                current,
                payload["accepted_suggestion_ids"],
                "author",
            )
            draft = self.setup.select_authors(
                revision,
                (
                    *(item.name for item in accepted),
                    *(
                        validate_custom_author(value)
                        for value in payload["custom_authors"]
                    ),
                ),
            )
        elif step == "pdf_destination":
            registered = self._tested_destinations.pop(
                payload["tested_destination_token"], None
            )
            if registered is None or registered[0] != revision:
                raise ValueError("tested destination is missing, stale, or already used")
            draft = self.setup.set_pdf_destination(
                revision, registered[1], tested=True
            )
        elif step == "review":
            current = self.setup.load_draft()
            if current is None or current.revision != revision:
                raise ValueError("setup draft revision changed")
            projection = project_setup_draft(self._setup_payload(current))
            if payload["profile_summary_sha256"] != projection[
                "profile_summary_sha256"
            ]:
                raise ValueError("the displayed setup review changed")
            draft = self.setup.confirm_review(revision)
        else:
            raise ValueError("unsupported setup step")
        return self._setup_payload(draft)

    def _resolve_folder_choice(self, value: str) -> Any:
        if value in {"downloads", "documents"}:
            choices = {item.kind.value: item for item in self.folder.standard_choices()}
            return choices[value]
        choice = self._picker_choices.pop(value, None)
        if choice is None:
            raise ValueError("folder choice is missing, stale, or already used")
        return choice

    def _folder_pick(self, payload: dict[str, Any]) -> dict[str, Any]:
        from arxiv_digest.folders import PickerStatus

        result = self.folder.pick_custom()
        if result.status is PickerStatus.CANCELLED:
            return {"cancelled": True}
        if result.status is PickerStatus.UNAVAILABLE:
            return {"unavailable": True}
        if result.status is not PickerStatus.SELECTED or result.choice is None:
            raise ValueError("the native folder picker did not complete")
        identifier = f"picker_{secrets.token_urlsafe(12)}"
        self._picker_choices[identifier] = result.choice
        return {
            "picker_result_id": identifier,
            "destination_choice": identifier,
            "display_name": result.choice.display_name,
        }

    def _folder_test(
        self,
        payload: dict[str, Any],
        *,
        revision_field: str | None,
    ) -> dict[str, Any]:
        revision = None if revision_field is None else payload[revision_field]
        destination = self.folder.validate(
            self._resolve_folder_choice(payload["destination_choice"])
        )
        token = f"destination_{secrets.token_urlsafe(12)}"
        self._tested_destinations[token] = (revision, destination)
        return {
            "tested_destination_token": token,
            "destination_kind": destination.kind,
        }

    def _categories(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._category_values is None:
            self._category_values = self._oai.list_sets()
        query = payload.get("q", "").casefold()
        items = []
        seen: set[str] = set()
        for item in self._category_values:
            if ":" not in item.set_spec:
                continue
            category = _category_from_set_spec(item.set_spec)
            if query and query not in (
                f"{category} {item.display_name} {item.set_spec}".casefold()
            ):
                continue
            if category in seen:
                continue
            seen.add(category)
            if len(items) == 200:
                break
            items.append(
                {
                    "category": category,
                    "set_spec": item.set_spec,
                    "display_name": item.display_name,
                }
            )
            self._issued_category_pairs.add((category, item.set_spec))
        return {"categories": items}

    @staticmethod
    def _profile_value(profile: Any, store: Any) -> dict[str, Any]:
        categories = []
        coverage_by_category = {
            item.category: item.coverage_start
            for item in getattr(profile, "category_coverage", ())
        }
        for category in profile.categories:
            state = store.category_sync_state(category)
            categories.append(
                {
                    "category": category,
                    "set_spec": state.set_spec,
                    "coverage_start": coverage_by_category.get(
                        category, state.coverage_start
                    ).isoformat(),
                }
            )
        seed_paper_details = []
        article_metadata = getattr(store, "article_metadata", None)
        if callable(article_metadata):
            for arxiv_id in profile.seed_papers:
                try:
                    paper = article_metadata(arxiv_id)
                except KeyError:
                    continue
                seed_paper_details.append(
                    {
                        "arxiv_id": paper.arxiv_id,
                        "title": paper.title,
                        "authors": list(paper.authors),
                    }
                )
        return {
            "schema_version": profile.schema_version,
            "revision": profile.revision,
            "categories": categories,
            "keywords": list(profile.keywords),
            "phrases": list(profile.phrases),
            "authors": list(profile.authors),
            "seed_papers": list(profile.seed_papers),
            "seed_paper_details": seed_paper_details,
            "pdf_destination": {"kind": profile.pdf_destination.kind},
        }

    def _complete_setup(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.setup.complete(
            payload["draft_revision"],
            launcher_choice=payload["launcher_choice"],
        )
        return self._profile_value(profile, self.store)

    def _retryable_sync_dates(
        self, configs: tuple[Any, ...] | None = None
    ) -> dict[str, tuple[date, ...]]:
        sync = getattr(self, "sync", None)
        profiles = getattr(self, "profiles", None)
        progress_snapshot = getattr(sync, "progress", None)
        if (
            sync is None
            or profiles is None
            or profiles.load() is None
            or not callable(progress_snapshot)
        ):
            return {}
        selected = self._sync_configs() if configs is None else configs
        offline = bool(
            self._last_sync_report is not None
            and self._last_sync_report.offline
        )
        report = progress_snapshot(selected, offline=offline)
        return {
            progress.category: tuple(progress.retryable_failed_exact_dates)
            for progress in report.categories
            if progress.retryable_failed_exact_dates
        }

    def _set_sync_phase(self, job_id: str, phase: str) -> None:
        if phase not in {"daily_list", "enrichment"}:
            raise ValueError("unknown synchronization phase")
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("status") != "running":
                return
            job["phase"] = phase
            if phase == "enrichment":
                job.pop("daily_list_retry", None)

    def _run_sync_pass(self, job_id: str, *, retry_failed_dates: bool) -> Any:
        configs = self._sync_configs()
        pending_daily_lists = getattr(
            self.sync,
            "has_pending_daily_list_work",
            None,
        )
        phase = (
            "daily_list"
            if retry_failed_dates
            or not callable(pending_daily_lists)
            or pending_daily_lists(configs)
            else "enrichment"
        )
        self._set_sync_phase(job_id, phase)
        retry_dates = self._retryable_sync_dates(configs)
        targets_by_date: dict[date, set[str]] = {}
        for category, dates in retry_dates.items():
            for mailing_date in dates:
                targets_by_date.setdefault(mailing_date, set()).add(category)
        attempted_pairs: set[tuple[str, date]] = set()

        def record_attempt(category: str, mailing_date: date) -> None:
            expected = targets_by_date.get(mailing_date)
            if expected is None or category not in expected:
                return
            attempted_pairs.add((category, mailing_date))
            completed = sum(
                all(
                    (category_name, day) in attempted_pairs
                    for category_name in categories
                )
                for day, categories in targets_by_date.items()
            )
            with self._jobs_lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["daily_list_retry"] = {
                        "status": "running",
                        "completed": completed,
                        "total": len(targets_by_date),
                    }

        if targets_by_date:
            with self._jobs_lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["daily_list_retry"] = {
                        "status": "running",
                        "completed": 0,
                        "total": len(targets_by_date),
                    }
        if retry_failed_dates:
            report = self.sync.retry_failed_dates(
                configs,
                retry_dates,
                attempted=record_attempt,
            )
        elif targets_by_date:
            report = self.sync.sync(
                configs,
                attempted=record_attempt,
                daily_list_complete=lambda: self._set_sync_phase(
                    job_id,
                    "enrichment",
                ),
            )
        else:
            report = self.sync.sync(
                configs,
                daily_list_complete=lambda: self._set_sync_phase(
                    job_id,
                    "enrichment",
                ),
            )
        self._last_sync_report = report
        return report

    def _run_sync(self, job_id: str, *, retry_failed_dates: bool) -> Any:
        try:
            while True:
                report = self._run_sync_pass(
                    job_id,
                    retry_failed_dates=retry_failed_dates,
                )
                with self._jobs_lock:
                    if not self._sync_follow_up_requested:
                        self._active_sync_job = None
                        return report
                    self._sync_follow_up_requested = False
                retry_failed_dates = False
        finally:
            with self._jobs_lock:
                if self._active_sync_job == job_id:
                    self._active_sync_job = None
                    self._sync_follow_up_requested = False

    def _start_sync_job(self, payload: dict[str, Any]) -> dict[str, str]:
        with self._sync_start_lock:
            with self._jobs_lock:
                if self._active_sync_job is not None:
                    current = self._jobs.get(self._active_sync_job)
                    if current is not None and current["status"] == "running":
                        if payload.get("follow_up") is True:
                            self._sync_follow_up_requested = True
                        return {"job_id": self._active_sync_job}
                job_id = f"sync_{secrets.token_urlsafe(12)}"
                self._active_sync_job = job_id
                self._sync_follow_up_requested = False
                # Clear a previous request before the worker can register. Once
                # registered, maintenance cancellation must never be erased by a
                # startup race inside the worker thread.
                self._sync_cancel.clear()
            try:
                pending_daily_lists = getattr(
                    getattr(self, "sync", None),
                    "has_pending_daily_list_work",
                    None,
                )
                initial_phase = (
                    "daily_list"
                    if payload.get("retry_failed_dates") is True
                    or not callable(pending_daily_lists)
                    or pending_daily_lists(self._sync_configs())
                    else "enrichment"
                )
                self._new_job(
                    "sync",
                    "sync",
                    lambda: self._run_sync(
                        job_id,
                        retry_failed_dates=payload.get("retry_failed_dates") is True,
                    ),
                    job_id=job_id,
                    request_cancel=self._sync_cancel.set,
                    initial_fields={"phase": initial_phase},
                )
            except Exception:
                with self._jobs_lock:
                    if self._active_sync_job == job_id:
                        self._active_sync_job = None
                raise
        return {"job_id": job_id}

    def _cancel_sync(self, payload: dict[str, Any]) -> dict[str, bool]:
        with self._jobs_lock:
            if payload["job_id"] != self._active_sync_job:
                raise KeyError("unknown active synchronization job")
        self._sync_cancel.set()
        return {"cancel_requested": True}

    def _start_download(self, payload: dict[str, Any]) -> dict[str, str]:
        cancellation = threading.Event()
        job_id = self._new_job(
            "download",
            "download",
            lambda: self.downloads.download(
                payload["arxiv_id"],
                payload["version"],
                save_first=payload["save_first"],
                save_version=payload.get("save_version"),
                cancelled=cancellation.is_set,
            ),
            request_cancel=cancellation.set,
        )
        return {"job_id": job_id}

    def _interests_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.profiles.load()
        if profile is None:
            raise KeyError("active profile")
        if payload.get("refresh") == "1":
            configs = self._sync_configs()
            with self.maintenance.operation():
                build = self.candidates.resume(configs)
            self._candidate_build = build
            self._suggestions.clear()
            self._suggestion_ids.clear()
        result = self._profile_value(profile, self.store)
        coverage_min, coverage_max = self._supported_coverage_bounds()
        result["coverage_min"] = coverage_min.isoformat()
        result["coverage_max"] = coverage_max.isoformat()
        result["suggestions"] = {
            "categories": self._interest_category_suggestions(profile),
            "seed_papers": [],
            "keywords": [],
            "phrases": [],
            "authors": [],
        }
        current = self._current_interest_corpus(profile)
        if current is None:
            return result

        from arxiv_digest.candidates import build_suggestions

        corpus, corpus_hash = current
        corpus_ids = {
            document.paper.arxiv_id for document in corpus.documents
        }
        suggestions = build_suggestions(
            corpus,
            tuple(
                arxiv_id
                for arxiv_id in profile.seed_papers
                if arxiv_id in corpus_ids
            ),
            (*profile.keywords, *profile.phrases),
            profile.authors,
        )
        binding = SimpleNamespace(
            revision=profile.revision,
            corpus_hash=corpus_hash,
        )

        def suggestion_id(kind: str, item: Any) -> str:
            return self._register_suggestion(binding, kind, item)

        result["suggestions_generated_at"] = corpus.created_at.isoformat()
        result["suggestions"]["seed_papers"] = [
            {
                "suggestion_id": suggestion_id("paper", item),
                "arxiv_id": item.paper.arxiv_id,
                "title": item.paper.title,
                "authors": list(item.paper.authors),
                "abstract": item.paper.abstract,
                "primary_category": item.paper.primary_category,
                "categories": list(item.paper.categories),
                "score": item.score,
                "reasons": list(item.reasons),
            }
            for item in suggestions.papers[:30]
        ]
        for kind in ("keyword", "phrase"):
            result["suggestions"][f"{kind}s"] = [
                {
                    "suggestion_id": suggestion_id("term", item),
                    "value": item.value,
                    "score": item.score,
                    "reasons": list(item.reasons),
                }
                for item in suggestions.terms
                if item.kind == kind
            ][:30]
        result["suggestions"]["authors"] = [
            {
                "suggestion_id": suggestion_id("author", item),
                "name": item.name,
                "score": item.score,
                "reasons": list(item.reasons),
            }
            for item in suggestions.authors[:30]
        ]
        return result

    def _current_interest_corpus(self, profile: Any) -> tuple[Any, str] | None:
        build = self._candidate_build
        if (
            build is not None
            and tuple(build.corpus.categories) == tuple(profile.categories)
        ):
            return build.corpus, build.corpus_hash

        shards = []
        try:
            for category in profile.categories:
                state = self.store.category_sync_state(category)
                shard = self.candidates.cache.load_shard(
                    category,
                    state.set_spec,
                )
                if shard is None:
                    return None
                shards.append(shard)
            corpus = self.candidates.cache.derive_corpus(
                tuple(shards),
                categories=tuple(profile.categories),
            )
        except (KeyError, OSError, ValueError):
            return None

        from arxiv_digest.candidates import candidate_corpus_hash

        return corpus, candidate_corpus_hash(corpus)

    def _interest_category_suggestions(self, profile: Any) -> list[dict[str, str]]:
        if self._category_values is None:
            from arxiv_digest.sources.oai import OaiError

            try:
                self._category_values = self._oai.list_sets()
            except (OaiError, OSError):
                return []
        selected = set(profile.categories)
        seen: set[str] = set()
        values = []
        for item in self._category_values:
            if ":" not in item.set_spec:
                continue
            try:
                category = _category_from_set_spec(item.set_spec)
            except ValueError:
                continue
            if category in selected or category in seen:
                continue
            seen.add(category)
            self._issued_category_pairs.add((category, item.set_spec))
            values.append(
                {
                    "category": category,
                    "set_spec": item.set_spec,
                    "display_name": item.display_name,
                }
            )
            if len(values) == 30:
                break
        return values

    def _interests_put(self, payload: dict[str, Any]) -> dict[str, Any]:
        from arxiv_digest.candidates import (
            validate_custom_arxiv_id,
            validate_custom_author,
            validate_custom_keyword,
            validate_custom_phrase,
        )
        from arxiv_digest.models import CategoryConfig
        from arxiv_digest.profile import Profile, ProfileCategory

        current = self.profiles.load()
        if current is None:
            raise KeyError("active profile")
        expected = payload["expected_revision"]
        if current.revision != expected:
            from arxiv_digest.profile import ProfileRevisionError

            raise ProfileRevisionError(expected, current.revision)
        selections = payload.get("categories")
        if selections is None:
            selections = [
                {
                    "category": category,
                    "set_spec": self.store.category_sync_state(category).set_spec,
                }
                for category in current.categories
            ]
        added_configs = payload.get("category_configs", [])
        added_by_category = {
            item["category"]: item for item in added_configs
        }
        if len(added_by_category) != len(added_configs):
            raise ValueError("category configuration additions must be unique")
        active_coverage = {
            item.category: item.coverage_start
            for item in current.category_coverage
        }
        configs = []
        used_added: set[str] = set()
        for selection in selections:
            category = selection["category"]
            if category not in active_coverage:
                item = added_by_category.get(category)
                if (
                    item is None
                    or item["set_spec"] != selection["set_spec"]
                    or (category, selection["set_spec"])
                    not in self._issued_category_pairs
                ):
                    raise ValueError(
                        "new categories require an exact coverage configuration"
                    )
                coverage_start = date.fromisoformat(item["coverage_start"])
                coverage_min, coverage_max = self._supported_coverage_bounds()
                if not coverage_min <= coverage_start <= coverage_max:
                    raise ValueError(
                        "category coverage is outside the supported recovery window"
                    )
                configs.append(
                    CategoryConfig(
                        category,
                        selection["set_spec"],
                        coverage_start,
                    )
                )
                used_added.add(category)
            else:
                state = self.store.category_sync_state(category)
                if state.set_spec != selection["set_spec"]:
                    raise ValueError("stored category set specification changed")
                configs.append(
                    CategoryConfig(
                        category,
                        state.set_spec,
                        active_coverage[category],
                    )
                )
        if used_added != set(added_by_category):
            raise ValueError("category configurations must belong to new selections")
        seeds = tuple(
            validate_custom_arxiv_id(value)
            for value in payload.get("seed_papers", current.seed_papers)
        )
        new_seed_documents = tuple(
            self.candidates.lookup(value)
            for value in seeds
            if value not in current.seed_papers and not self.known_paper(value)
        )
        profile = Profile(
            schema_version=2,
            revision=expected + 1,
            category_coverage=tuple(
                ProfileCategory(config.category, config.coverage_start)
                for config in configs
            ),
            keywords=tuple(
                validate_custom_keyword(value)
                for value in payload.get("keywords", current.keywords)
            ),
            phrases=tuple(
                validate_custom_phrase(value)
                for value in payload.get("phrases", current.phrases)
            ),
            authors=tuple(
                validate_custom_author(value)
                for value in payload.get("authors", current.authors)
            ),
            seed_papers=seeds,
            pdf_destination=current.pdf_destination,
        )
        self.setup.publish_profile(
            profile,
            tuple(configs),
            seed_papers=new_seed_documents,
            expected_revision=expected,
        )
        if used_added:
            self._start_sync_job({"follow_up": True})
        return self._profile_value(profile, self.store)

    def _settings_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        from arxiv_digest.web.api import _destination_display_path
        from arxiv_digest.doctor import _redacted_sync_error_code

        profile = self.profiles.load()
        if profile is None:
            raise KeyError("active profile")
        service_status = self.status({})
        active_sync = service_status.get("sync")
        coverage_min, coverage_max = self._supported_coverage_bounds()
        metadata_categories = []
        coverage_categories = []
        category_days: dict[str, dict[date, str]] = {}
        checkpoint_count = 0
        for profile_category in profile.category_coverage:
            category = profile_category.category
            state = self.store.category_sync_state(category)
            if state.completed_through_utc is not None:
                checkpoint_count += 1
            metadata_codes = []
            if state.last_error_code is not None:
                metadata_codes.append(
                    _redacted_sync_error_code(state.last_error_code)
                )
            metadata_categories.append(
                {
                    "category": category,
                    "status": state.status,
                    "synchronized_through": (
                        None
                        if state.completed_through_utc is None
                        else state.completed_through_utc.isoformat()
                    ),
                    "error_codes": sorted(set(metadata_codes)),
                }
            )

            records = tuple(
                record
                for record in self.store.catchup_day_records(category)
                if record.daily_list_date >= profile_category.coverage_start
            )
            category_days[category] = {
                record.daily_list_date: record.status.value
                for record in records
            }
            category_statuses = [record.status.value for record in records]
            category_unavailable = sum(
                self._daily_list_is_unavailable(
                    [record.status.value],
                    record.daily_list_date,
                    coverage_min,
                )
                for record in records
            )
            error_codes = sorted(
                {
                    _redacted_sync_error_code(record.error_code)
                    for record in records
                    if record.error_code is not None
                }
            )
            coverage_categories.append(
                {
                    "category": category,
                    "coverage_start": profile_category.coverage_start.isoformat(),
                    **self._daily_list_counts(
                        category_statuses,
                        unavailable=category_unavailable,
                    ),
                    "error_codes": error_codes,
                    "retryable_failed_dates": [
                        record.daily_list_date.isoformat()
                        for record in records
                        if record.status.value == "failed"
                        and coverage_min <= record.daily_list_date <= coverage_max
                    ],
                }
            )

        global_dates = sorted(
            {
                day
                for statuses in category_days.values()
                for day in statuses
            }
        )
        global_statuses = []
        global_unavailable = 0
        for day in global_dates:
            statuses = [
                category_days.get(item.category, {}).get(day, "pending")
                for item in profile.category_coverage
                if item.coverage_start <= day
            ]
            if statuses:
                global_statuses.append(self._global_daily_list_status(statuses))
                global_unavailable += self._daily_list_is_unavailable(
                    statuses, day, coverage_min
                )
        connection = self.store._connect()
        try:
            resolution_counts = {
                row[0]: int(row[1])
                for row in connection.execute(
                    """SELECT version_resolution, COUNT(*)
                       FROM canonical_events GROUP BY version_resolution"""
                )
            }
            canonical_count = sum(resolution_counts.values())
            saved_count = int(
                connection.execute("SELECT COUNT(*) FROM saved_papers").fetchone()[0]
            )
            pdf_count = int(
                connection.execute("SELECT COUNT(*) FROM download_files").fetchone()[0]
            )
        finally:
            connection.close()

        cache_root = self.candidates.cache.root
        cache_file_count = 0
        cache_status = "missing"
        try:
            root_info = cache_root.lstat()
            if stat.S_ISDIR(root_info.st_mode) and not cache_root.is_symlink():
                for walk_root, directory_names, file_names in os.walk(
                    cache_root, followlinks=False
                ):
                    directory_names[:] = [
                        name
                        for name in directory_names
                        if not (Path(walk_root) / name).is_symlink()
                    ]
                    cache_file_count += sum(
                        (Path(walk_root) / name).is_file()
                        and not (Path(walk_root) / name).is_symlink()
                        for name in file_names
                    )
                cache_status = "ready" if cache_file_count else "empty"
        except OSError:
            pass
        return {
            "revision": profile.revision,
            "online": not bool(service_status.get("offline")),
            "synchronizing": bool(
                active_sync is not None
                and active_sync.get("status") == "running"
            ),
            "daily_list_retry": service_status.get(
                "daily_list_retry",
                {"status": "idle", "completed": 0, "total": 0},
            ),
            "coverage_min": coverage_min.isoformat(),
            "coverage_max": coverage_max.isoformat(),
            "metadata_sync": {
                "checkpoint_count": checkpoint_count,
                "categories": metadata_categories,
            },
            "daily_list_coverage": {
                **self._daily_list_counts(
                    global_statuses,
                    unavailable=global_unavailable,
                ),
                "categories": coverage_categories,
            },
            "version_resolution": {
                "canonical_event_count": canonical_count,
                "atom_confirmed": resolution_counts.get("atom_confirmed", 0),
                "chronology_matched": resolution_counts.get(
                    "chronology_matched", 0
                ),
                "unconfirmed": resolution_counts.get("unconfirmed", 0),
            },
            "candidate_cache": {
                "status": cache_status,
                "file_count": cache_file_count,
            },
            "library": {"saved_paper_count": saved_count},
            "pdf_presence": {"downloaded_pdf_count": pdf_count},
            "pdf_destination": {
                "kind": profile.pdf_destination.kind,
                "display_path": _destination_display_path(
                    profile.pdf_destination.path
                ),
            },
            "launcher": self._launcher_value(),
        }

    @staticmethod
    def _daily_list_is_unavailable(
        statuses: list[str],
        day: date,
        coverage_min: date,
    ) -> bool:
        return day < coverage_min and any(
            status in {"failed", "pending"} for status in statuses
        )

    @staticmethod
    def _global_daily_list_status(
        statuses: list[str],
    ) -> str:
        if "failed" in statuses:
            return "failed"
        if "pending" in statuses:
            return "pending"
        if "complete" in statuses:
            return "complete"
        return "empty"

    @staticmethod
    def _daily_list_counts(
        statuses: list[str],
        *,
        unavailable: int = 0,
    ) -> dict[str, int]:
        with_papers = statuses.count("complete")
        empty = statuses.count("empty")
        failed = statuses.count("failed")
        pending = statuses.count("pending")
        return {
            "target": with_papers + empty + failed + pending,
            "checked": with_papers + empty + failed,
            "with_papers": with_papers,
            "empty": empty,
            "failed": failed,
            "pending": pending,
            "unavailable": unavailable,
            "gaps": failed + pending,
        }

    def _settings_coverage(self, payload: dict[str, Any]) -> dict[str, Any]:
        from arxiv_digest.models import CategoryConfig
        from arxiv_digest.profile import (
            Profile,
            ProfileCategory,
            ProfileRevisionError,
        )

        current = self.profiles.load()
        if current is None:
            raise KeyError("active profile")
        expected = payload["expected_revision"]
        if current.revision != expected:
            raise ProfileRevisionError(expected, current.revision)
        category = payload["category"]
        current_coverage = {
            item.category: item.coverage_start
            for item in current.category_coverage
        }
        if category not in current_coverage:
            raise ValueError("coverage can only be extended for an active category")
        new_start = date.fromisoformat(payload["new_start"])
        coverage_min, coverage_max = self._supported_coverage_bounds()
        if not coverage_min <= new_start <= coverage_max:
            raise ValueError("coverage is outside the supported recovery window")
        if new_start >= current_coverage[category]:
            raise ValueError("new coverage start must extend existing coverage")

        self.sync.extend_coverage(category, new_start)

        category_coverage = tuple(
            ProfileCategory(
                item.category,
                new_start if item.category == category else item.coverage_start,
            )
            for item in current.category_coverage
        )
        configs = tuple(
            CategoryConfig(
                item.category,
                self.store.category_sync_state(item.category).set_spec,
                item.coverage_start,
            )
            for item in category_coverage
        )
        profile = Profile(
            schema_version=2,
            revision=expected + 1,
            category_coverage=category_coverage,
            keywords=current.keywords,
            phrases=current.phrases,
            authors=current.authors,
            seed_papers=current.seed_papers,
            pdf_destination=current.pdf_destination,
        )
        self.setup.publish_profile(
            profile,
            configs,
            expected_revision=expected,
        )
        sync_job = self._start_sync_job({"follow_up": True})
        return {
            "revision": profile.revision,
            "category": category,
            "coverage_start": new_start.isoformat(),
            "sync_job_id": sync_job["job_id"],
        }

    def _settings_cache_clear(self, payload: dict[str, Any]) -> dict[str, bool]:
        root = self.candidates.cache.root
        try:
            root.relative_to(self.paths.cache_dir)
        except ValueError as error:
            raise RuntimeError("candidate cache root escaped the cache directory") from error
        if root.exists():
            shutil.rmtree(root)
        self._candidate_build = None
        self._suggestions.clear()
        self._suggestion_ids.clear()
        return {"cleared": True}

    def _settings_folder(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.maintenance.operation():
            return self._settings_folder_under_lease(payload)

    def _settings_folder_under_lease(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        from arxiv_digest.profile import Profile, ProfileCategory

        profile = self.profiles.load()
        if profile is None:
            raise KeyError("active profile")
        expected = payload["expected_revision"]
        if profile.revision != expected:
            from arxiv_digest.profile import ProfileRevisionError

            raise ProfileRevisionError(expected, profile.revision)
        registered = self._tested_destinations.pop(
            payload["tested_destination_token"], None
        )
        if registered is None or registered[0] is not None:
            raise ValueError("tested destination is missing, stale, or already used")
        configs = tuple(self._sync_configs())
        revised = Profile(
            schema_version=2,
            revision=expected + 1,
            category_coverage=tuple(
                ProfileCategory(config.category, config.coverage_start)
                for config in configs
            ),
            keywords=profile.keywords,
            phrases=profile.phrases,
            authors=profile.authors,
            seed_papers=profile.seed_papers,
            pdf_destination=registered[1],
        )
        self.setup.publish_profile(
            revised,
            configs,
            expected_revision=expected,
        )
        self.downloads.recompute_presence(revised.pdf_destination.path)
        return self._profile_value(revised, self.store)

    def _settings_folder_open(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.profiles.load()
        if profile is None:
            raise KeyError("active profile")
        result = self.folder.open_active(profile)
        return {"status": result.status.value, "message": result.message}

    def _launcher_value(self) -> dict[str, Any]:
        settings = self.setup.launcher_settings()
        installed = False
        if self.launcher is not None:
            from arxiv_digest.desktop_launcher import LauncherState

            installed = self.launcher.status().state is LauncherState.INSTALLED
        return {
            "operation": settings.operation,
            "error_code": settings.error_code,
            "retry_available": settings.retry_available,
            "installed": installed,
        }

    def _launcher_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.launcher is None:
            raise RuntimeError("desktop launcher is unavailable")
        self.launcher.install()
        if self.setup.launcher_settings().operation == "create_failed":
            self.setup.dismiss_launcher_failure()
        return self._launcher_value()

    def _launcher_not_now(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.setup.dismiss_launcher_failure()
        return self._launcher_value()

    def _launcher_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.launcher is None:
            raise RuntimeError("desktop launcher is unavailable")
        self.launcher.remove()
        return self._launcher_value()

    def _doctor(self, payload: dict[str, Any]) -> Any:
        from arxiv_digest.doctor import inspect_doctor

        return inspect_doctor(self.paths)

    def _private_temp(
        self,
        suffix: str,
        *,
        prefix: str = ".arxiv-digest-",
    ) -> Path:
        self.paths.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=prefix,
            suffix=suffix,
            dir=self.paths.cache_dir,
        )
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        return Path(name)

    def _backup_export(self, payload: dict[str, Any]) -> Any:
        from arxiv_digest.backup import export_backup
        from arxiv_digest.web.api import BACKUP_BODY_LIMIT, BinaryPayload

        temporary = self._private_temp(".zip")
        temporary.unlink()
        try:
            export_backup(
                self.paths,
                temporary,
                maintenance=self.maintenance,
            )
            if temporary.stat().st_size > BACKUP_BODY_LIMIT:
                raise ValueError("backup exceeds the browser download limit")
            return BinaryPayload(
                temporary.read_bytes(),
                f"arxiv-digest-{datetime.now(timezone.utc):%Y%m%d}.zip",
            )
        finally:
            temporary.unlink(missing_ok=True)

    def _expire_pending_restores(self) -> None:
        now = time.monotonic()
        expired_paths: list[Path] = []
        with self._pending_restores_lock:
            expired = [
                identifier
                for identifier, (created, _) in self._pending_restores.items()
                if now - created >= _PENDING_RESTORE_TTL_SECONDS
            ]
            for identifier in expired:
                pending = self._pending_restores.pop(identifier, None)
                if pending is not None:
                    _, inspection = pending
                    expired_paths.append(inspection.path)
            self._schedule_pending_restore_expiration_locked(now=now)
        for path in expired_paths:
            path.unlink(missing_ok=True)

    def _schedule_pending_restore_expiration_locked(
        self, *, now: float | None = None
    ) -> None:
        current = self._restore_cleanup_timer
        if current is not None:
            current.cancel()
        self._restore_cleanup_timer = None
        if self._restore_shutdown or not self._pending_restores:
            return
        current_time = time.monotonic() if now is None else now
        next_expiry = min(
            created + _PENDING_RESTORE_TTL_SECONDS
            for created, _inspection in self._pending_restores.values()
        )
        timer = threading.Timer(
            max(0.01, next_expiry - current_time),
            self._expire_pending_restores,
        )
        timer.daemon = True
        self._restore_cleanup_timer = timer
        timer.start()

    def _schedule_pending_restore_expiration(self) -> None:
        with self._pending_restores_lock:
            self._schedule_pending_restore_expiration_locked()

    def _pending_restore_usage_locked(self) -> tuple[int, int]:
        pending_bytes = sum(
            inspection.path.stat().st_size
            for _created, inspection in self._pending_restores.values()
        )
        return (
            len(self._pending_restores)
            + len(self._pending_restore_reservations),
            pending_bytes + sum(self._pending_restore_reservations.values()),
        )

    def _reserve_pending_restore_locked(
        self, identifier: str, archive_bytes: int
    ) -> None:
        if self._restore_shutdown:
            raise RuntimeError("application services are closing")
        if (
            identifier in self._pending_restores
            or identifier in self._pending_restore_reservations
        ):
            raise RuntimeError("pending restore identifier collision")
        count, total_bytes = self._pending_restore_usage_locked()
        if count >= _MAX_PENDING_RESTORES:
            raise ValueError("too many pending restore inspections")
        if total_bytes + archive_bytes > _MAX_PENDING_RESTORE_BYTES:
            raise ValueError("pending restore storage limit exceeded")
        self._pending_restore_reservations[identifier] = archive_bytes

    def _backup_inspect(self, payload: dict[str, Any]) -> dict[str, Any]:
        from arxiv_digest.backup import inspect_backup

        self._expire_pending_restores()
        identifier = f"restore_{secrets.token_urlsafe(12)}"
        with self._pending_restores_lock:
            self._reserve_pending_restore_locked(
                identifier,
                len(payload["archive"]),
            )
        temporary: Path | None = None
        try:
            temporary = self._private_temp(
                ".zip",
                prefix=_RESTORE_UPLOAD_PREFIX,
            )
            with temporary.open("wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload["archive"])
                handle.flush()
                os.fsync(handle.fileno())
            inspection = inspect_backup(temporary)
        except Exception:
            with self._pending_restores_lock:
                self._pending_restore_reservations.pop(identifier, None)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        try:
            with self._pending_restores_lock:
                reserved_bytes = self._pending_restore_reservations.pop(
                    identifier,
                    None,
                )
                if self._restore_shutdown:
                    raise RuntimeError("application services are closing")
                if reserved_bytes is None:
                    raise RuntimeError("pending restore reservation was lost")
                self._pending_restores[identifier] = (
                    time.monotonic(),
                    inspection,
                )
                self._schedule_pending_restore_expiration_locked()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        record_counts = {
            record_type: sum(
                record.record_type == record_type
                for record in inspection.records
            )
            for record_type in ("saved_paper", "canonical_event")
        }
        return {
            "pending_restore_id": identifier,
            "format_version": inspection.manifest.format_version,
            "application_version": inspection.manifest.application_version,
            "created_at": inspection.manifest.created_at.isoformat(),
            "profile_revision": inspection.profile.revision,
            "category_count": len(inspection.profile.categories),
            "record_count": len(inspection.records),
            "summary": {
                "categories": len(inspection.profile.categories),
                "saved_papers": record_counts["saved_paper"],
                "review_events": record_counts["canonical_event"],
                "profile_revision": inspection.profile.revision,
            },
        }

    def _backup_restore(self, payload: dict[str, Any]) -> dict[str, Any]:
        from arxiv_digest.backup import restore_backup
        from arxiv_digest.storage.database import open_database

        self._expire_pending_restores()
        identifier = payload["pending_restore_id"]
        with self._pending_restores_lock:
            pending = self._pending_restores.get(identifier)
            if pending is not None:
                _, reserved_inspection = pending
                reserved_bytes = reserved_inspection.path.stat().st_size
                self._pending_restores.pop(identifier)
                self._pending_restore_reservations[identifier] = reserved_bytes
            self._schedule_pending_restore_expiration_locked()
        if pending is None:
            raise KeyError("pending restore")
        _, inspection = pending
        choice_identifier = payload["destination_choice"]
        picker_choice = self._picker_choices.get(choice_identifier)
        try:
            destination = self.folder.validate(
                self._resolve_folder_choice(choice_identifier)
            )
            # Acquire candidate ownership before making maintenance exclusive.
            # A start that won this lock must register its cancellable worker
            # first; a restore that won it replaces state before another start
            # is allowed to capture a draft.
            with self._candidate_state_lock:
                with self.maintenance.exclusive(
                    cancel_active=payload["cancel_active"],
                    timeout=_BROWSER_RESTORE_WAIT_SECONDS,
                ):
                    result = restore_backup(
                        self.paths,
                        inspection,
                        destination,
                        maintenance=None,
                    )
                    # Reopen under the same exclusive lease so migrations,
                    # integrity checks, and interrupted-run reconciliation finish
                    # before any worker can observe the replacement database.
                    validation = open_database(self.paths.database_path)
                    validation.close()
                    self._candidate_build = None
                    self._candidate_job_id = None
                    self._candidate_job_revision = None
                    self._suggestions.clear()
                    self._suggestion_ids.clear()
                    self._last_sync_report = None
        except Exception:
            retain_pending = False
            with self._pending_restores_lock:
                self._pending_restore_reservations.pop(identifier, None)
                if not self._restore_shutdown:
                    self._pending_restores[identifier] = pending
                    self._schedule_pending_restore_expiration_locked()
                    retain_pending = True
            if not retain_pending:
                inspection.path.unlink(missing_ok=True)
            if picker_choice is not None:
                self._picker_choices.setdefault(choice_identifier, picker_choice)
            raise
        else:
            with self._pending_restores_lock:
                self._pending_restore_reservations.pop(identifier, None)
                self._schedule_pending_restore_expiration_locked()
            inspection.path.unlink(missing_ok=True)
            return {
                "profile_revision": result.profile_revision,
                "pre_restore_backup_created": result.pre_restore_path is not None,
            }

    def handlers(self) -> Mapping[str, Callable]:
        self._require_open()
        return {
            "status": self.status,
            "categories": self._categories,
            "setup_draft_get": lambda payload: self._setup_payload(
                self.setup.start()
            ),
            "setup_draft_put": self._setup_draft_put,
            "setup_corpus": self._start_candidate_corpus,
            "setup_corpus_accept": self._accept_candidate_corpus,
            "setup_job": self._job_status,
            "setup_candidate_papers": self._candidate_papers,
            "setup_candidate_terms": self._candidate_terms,
            "setup_candidate_authors": self._candidate_authors,
            "setup_paper_lookup": self._lookup_candidate_paper,
            "setup_folder_pick": self._folder_pick,
            "setup_folder_test": lambda payload: self._folder_test(
                payload, revision_field="draft_revision"
            ),
            "setup_complete": self._complete_setup,
            "sync_start": self._start_sync_job,
            "sync_cancel": self._cancel_sync,
            "review_summary": lambda payload: self.review.summary(),
            "review_finish_all": self._review_finish_all,
            "review_calendar": lambda payload: self.review.calendar(
                date.fromisoformat(payload["start"]),
                date.fromisoformat(payload["end"]),
            ),
            "review_date": self._review_date,
            "review_position": self._review_position,
            "review_finish": self._review_finish,
            "library": self._library_page,
            "library_save": lambda payload: self.library.save(
                payload["arxiv_id"], version=payload.get("version")
            )
            or {"saved": True},
            "library_remove": lambda payload: self.library.remove(
                payload["arxiv_id"]
            )
            or {"saved": False},
            "library_pdf": self._start_download,
            "download_status": self._job_status,
            "interests_get": self._interests_get,
            "interests_put": self._interests_put,
            "settings_get": self._settings_get,
            "settings_coverage": self._settings_coverage,
            "settings_cache_clear": self._settings_cache_clear,
            "settings_folder_pick": self._folder_pick,
            "settings_folder_test": lambda payload: self._folder_test(
                payload, revision_field=None
            ),
            "settings_folder": self._settings_folder,
            "settings_folder_open": self._settings_folder_open,
            "settings_doctor": self._doctor,
            "settings_launcher": lambda payload: self._launcher_value(),
            "settings_launcher_create": self._launcher_create,
            "settings_launcher_not_now": self._launcher_not_now,
            "settings_launcher_remove": self._launcher_remove,
            "backup_export": self._backup_export,
            "backup_inspect": self._backup_inspect,
            "backup_restore": self._backup_restore,
            "application_quit": lambda payload: self.lifecycle.request_quit()
            or {"quitting": True},
        }

    def server(self, handlers: Mapping[str, Callable]) -> LoopbackServer:
        return LoopbackServer(
            handlers=handlers,
            known_paper=self.known_paper,
            static_assets=_packaged_static_assets(),
            lifecycle=self.lifecycle,
        )

    def _sync_configs(self) -> tuple[Any, ...]:
        from arxiv_digest.models import CategoryConfig

        profile = self.profiles.load()
        if profile is None:
            return ()
        values = []
        for profile_category in profile.category_coverage:
            state = self.store.category_sync_state(profile_category.category)
            values.append(
                CategoryConfig(
                    profile_category.category,
                    state.set_spec,
                    profile_category.coverage_start,
                )
            )
        return tuple(values)

    def start_sync(self) -> None:
        if self.sync is None or self.profiles.load() is None:
            return
        self._start_sync_job({})


def _standalone_import(
    paths: AppPaths,
    source: Path,
    *,
    maintenance: MaintenanceBarrier,
    input_text: Callable[[str], str],
    output: Callable[[str], None],
) -> int:
    from arxiv_digest.backup import inspect_backup, restore_backup
    from arxiv_digest.folders import FolderService, PickerStatus

    inspection = inspect_backup(source)
    folders = FolderService()
    downloads, documents = folders.standard_choices()
    output(
        "Choose the restored PDF destination:\n"
        f"1. {downloads.display_name}\n"
        f"2. {documents.display_name}\n"
        "3. Choose another folder"
    )
    selected = input_text("Choice [1-3]: ").strip()
    if selected == "1":
        choice = downloads
    elif selected == "2":
        choice = documents
    elif selected == "3":
        picked = folders.pick_custom()
        if picked.status is not PickerStatus.SELECTED or picked.choice is None:
            raise RuntimeError("No PDF destination was selected.")
        choice = picked.choice
    else:
        raise ValueError("Choose 1, 2, or 3.")
    destination = folders.validate(choice)
    restore_backup(
        paths,
        inspection,
        destination,
        maintenance=maintenance,
    )
    output("Backup restored successfully.")
    return 0


def create_application(
    *,
    paths: AppPaths | None = None,
    browser_open: Callable[[str], bool] = webbrowser.open,
    input_text: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> Application:
    """Compose the installed application without creating persistent state."""

    from arxiv_digest.backup import export_backup, recover_restore
    from arxiv_digest.doctor import inspect_doctor, render_doctor

    resolved = resolve_paths() if paths is None else paths
    maintenance = MaintenanceBarrier()
    lifecycle = LifecycleController()
    profiles = ProfileRepository(
        resolved.profile_path,
        resolved.profile_lock_path,
        maintenance=maintenance,
    )
    runtime = _DefaultRuntime(
        resolved,
        profiles,
        maintenance,
        lifecycle,
        output=output,
    )

    def doctor_action() -> int:
        output(render_doctor(inspect_doctor(resolved)))
        return 0

    def export_action(destination: Path) -> int:
        export_backup(resolved, destination, maintenance=maintenance)
        return 0

    def launcher_action() -> int:
        manager = runtime._launcher_manager()
        if manager is None:
            raise RuntimeError("the installed arxiv-digest executable was not found")
        manager.install()
        return 0

    return Application(
        paths=resolved,
        profile_exists=lambda: resolved.profile_path.is_file(),
        instance_factory=lambda: SingleInstance(
            resolved.process_lock_path,
            resolved.runtime_descriptor_path,
        ),
        server_factory=runtime.server,
        handlers_factory=runtime.handlers,
        resolve_restore_journal=lambda: recover_restore(
            resolved, maintenance=maintenance
        ),
        open_database=runtime.open_database,
        start_sync=runtime.start_sync,
        browser_open=browser_open,
        wait_for_server=lambda server: server.wait(),
        output=output,
        doctor_action=doctor_action,
        export_action=export_action,
        import_action=lambda source: _standalone_import(
            resolved,
            source,
            maintenance=maintenance,
            input_text=input_text,
            output=output,
        ),
        install_launcher_action=launcher_action,
    )
