from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from arxiv_digest.models import (
    CategoryConfig,
    CatchupDay,
    CatchupDayStatus,
    EnrichmentStatus,
)
from arxiv_digest.rate_limit import ArxivRequestCancelled
from arxiv_digest.sources.atom import atom_observations
from arxiv_digest.sources.catchup import catchup_observations
from arxiv_digest.sources.oai import (
    OaiPage,
    OaiProtocolError,
    durable_protocol_error_code,
    oai_observations,
)
from arxiv_digest.storage.store import CategorySyncRecord, Store


_MAILING_TIME_ZONE = ZoneInfo("America/New_York")
SUPPORTED_CATCHUP_WINDOW_DAYS = 90
DEFAULT_CATCHUP_FINALIZATION_HOUR = 20


def daily_list_coverage_bounds(
    observed_at: datetime,
    *,
    window_days: int = SUPPORTED_CATCHUP_WINDOW_DAYS,
    finalization_hour: int = DEFAULT_CATCHUP_FINALIZATION_HOUR,
    mailing_today: date | None = None,
) -> tuple[date, date]:
    """Return recoverable bounds under the New York mailing-day policy."""

    if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
        raise ValueError("coverage clock must return UTC")
    if type(window_days) is not int or not (
        1 <= window_days <= SUPPORTED_CATCHUP_WINDOW_DAYS
    ):
        raise ValueError("catch-up window must be between 1 and 90 days")
    if type(finalization_hour) is not int or not 0 <= finalization_hour <= 23:
        raise ValueError("catch-up finalization hour must be between 0 and 23")
    local_now = observed_at.astimezone(_MAILING_TIME_ZONE)
    today = local_now.date() if mailing_today is None else mailing_today
    if type(today) is not date:
        raise TypeError("mailing today must be a calendar date")
    latest_finalized = (
        today
        if local_now.hour >= finalization_hour
        else today - timedelta(days=1)
    )
    return (
        today - timedelta(days=window_days - 1),
        latest_finalized,
    )


@dataclass(frozen=True, slots=True)
class MetadataSyncProgress:
    status: str
    completed_through_utc: date | None
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True, slots=True)
class HistoricalBackfillProgress:
    status: str
    pending_start: date | None
    pending_until: date | None
    last_error_code: str | None
    last_error_message: str | None


@dataclass(frozen=True, slots=True)
class CategoryProgress:
    category: str
    metadata_sync: MetadataSyncProgress
    historical_backfill: HistoricalBackfillProgress
    exact_start: date | None
    exact_end: date | None
    missing_exact_dates: tuple[date, ...]
    failed_exact_dates: tuple[date, ...]
    retryable_failed_exact_dates: tuple[date, ...]
    target_dates: tuple[date, ...] = ()
    checked_dates: tuple[date, ...] = ()
    dates_with_papers: tuple[date, ...] = ()
    empty_dates: tuple[date, ...] = ()
    failed_dates: tuple[date, ...] = ()
    pending_dates: tuple[date, ...] = ()
    unavailable_dates: tuple[date, ...] = ()
    daily_list_status: str = "idle"
    daily_list_errors: tuple[tuple[date, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SyncReport:
    categories: tuple[CategoryProgress, ...]
    offline: bool
    metadata_complete: bool
    exact_start: date | None
    exact_end: date | None
    missing_exact_dates: tuple[date, ...]
    target_dates: int = 0
    checked_dates: int = 0
    dates_with_papers: int = 0
    empty_dates: int = 0
    failed_dates: int = 0
    pending_dates: int = 0
    unavailable_dates: int = 0
    daily_list_status: str = "idle"


class SyncCancelled(Exception):
    pass


class SyncService:
    """Coordinate durable source phases without letting adapters own state."""

    def __init__(
        self,
        store: Store,
        oai_source: object,
        atom_source: object,
        catchup_source: object,
        *,
        clock: Callable[[], datetime] | None = None,
        today: Callable[[], date] | None = None,
        cancelled: Callable[[], bool] | None = None,
        catchup_window_days: int = SUPPORTED_CATCHUP_WINDOW_DAYS,
        catchup_finalization_hour: int = DEFAULT_CATCHUP_FINALIZATION_HOUR,
    ) -> None:
        if catchup_window_days < 1 or catchup_window_days > 90:
            raise ValueError("catch-up window must be between 1 and 90 days")
        if not 0 <= catchup_finalization_hour <= 23:
            raise ValueError("catch-up finalization hour must be between 0 and 23")
        self.store = store
        self.oai_source = oai_source
        self.atom_source = atom_source
        self.catchup_source = catchup_source
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._today_override = today
        self._today = today or (
            lambda: self._now().astimezone(_MAILING_TIME_ZONE).date()
        )
        self._cancelled = cancelled or (lambda: False)
        self.catchup_window_days = catchup_window_days
        self.catchup_finalization_hour = catchup_finalization_hour

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("synchronization clock must return UTC")
        return value

    def _check_cancelled(self) -> None:
        if self._cancelled():
            raise SyncCancelled("synchronization was cancelled")

    def _daily_list_date_is_finalized(self, value: date) -> bool:
        return value <= self.coverage_bounds()[1]

    def coverage_bounds(self) -> tuple[date, date]:
        """Return the recoverable start and latest finalized mailing date."""

        observed_at = self._now()
        mailing_today = (
            observed_at.astimezone(_MAILING_TIME_ZONE).date()
            if self._today_override is None
            else self._today()
        )
        return daily_list_coverage_bounds(
            observed_at,
            window_days=self.catchup_window_days,
            finalization_hour=self.catchup_finalization_hour,
            mailing_today=mailing_today,
        )

    @staticmethod
    def _raise_cancelled(error: Exception) -> None:
        if isinstance(error, SyncCancelled):
            raise error
        if isinstance(error, ArxivRequestCancelled):
            raise SyncCancelled("synchronization was cancelled") from error

    @staticmethod
    def _error(error: Exception, phase: str) -> tuple[str, str]:
        if isinstance(error, (SyncCancelled, ArxivRequestCancelled)):
            return "cancelled", "Synchronization was cancelled."
        if isinstance(error, OaiProtocolError):
            return (
                durable_protocol_error_code(error.code),
                "The arXiv OAI service rejected the request.",
            )
        code = getattr(error, "code", None)
        safe_message = getattr(error, "safe_message", None)
        if isinstance(code, str) and code.strip() and isinstance(
            safe_message, str
        ):
            return code, safe_message
        return f"{phase}_failed", f"The arXiv {phase} phase did not complete."

    @staticmethod
    def _is_expired_token(error: Exception) -> bool:
        return isinstance(error, OaiProtocolError) and error.code in {
            "badResumptionToken",
            "bad_resumption_token",
        }

    def sync(
        self,
        configs: tuple[CategoryConfig, ...],
        *,
        catchup_dates: Mapping[str, Iterable[date]] | None = None,
        attempted: Callable[[str, date], None] | None = None,
    ) -> SyncReport:
        if not configs:
            return SyncReport((), False, True, None, None, ())
        if len({config.category for config in configs}) != len(configs):
            raise ValueError("selected categories must be unique")
        for config in configs:
            self.store.ensure_category_state(
                config.category, config.oai_set_spec, config.coverage_start
            )

        network_successes = 0
        network_failures = 0

        # Exact daily-list recovery leads the network work so Review can fill
        # while the hidden metadata sources are still running.
        requested_by_category: dict[str, tuple[date, ...]] = {}
        for config in configs:
            requested = self._eligible_catchup_dates(config, catchup_dates)
            requested_by_category[config.category] = requested
            self.store.ensure_catchup_targets(config.category, requested)
        target_dates = sorted(
            {
                mailing_date
                for requested in requested_by_category.values()
                for mailing_date in requested
            }
        )
        for mailing_date in target_dates:
            for config in configs:
                if mailing_date not in requested_by_category[config.category]:
                    continue
                self._check_cancelled()
                if self._sync_catchup_day(config, mailing_date):
                    network_successes += 1
                else:
                    network_failures += 1
                if attempted is not None:
                    attempted(config.category, mailing_date)

        # Atom and OAI enrich confirmed events but never create visible ones.
        for config in configs:
            self._check_cancelled()
            if self._sync_atom(config):
                network_successes += 1
            else:
                network_failures += 1

        for config in configs:
            self._check_cancelled()
            if self._sync_incremental(config):
                network_successes += 1
            else:
                network_failures += 1

        for config in configs:
            self._check_cancelled()
            state = self.store.category_sync_state(config.category)
            if state.pending_backfill_start is None:
                continue
            if self._sync_backfill(config, state):
                network_successes += 1
            else:
                network_failures += 1

        return self.progress(
            configs,
            offline=network_failures > 0 and network_successes == 0,
        )

    def retry_failed_dates(
        self,
        configs: tuple[CategoryConfig, ...],
        dates: Mapping[str, Iterable[date]],
        *,
        attempted: Callable[[str, date], None] | None = None,
    ) -> SyncReport:
        """Retry only the supplied exact daily-list dates."""

        if len({config.category for config in configs}) != len(configs):
            raise ValueError("selected categories must be unique")
        network_successes = 0
        network_failures = 0
        for config in configs:
            self.store.ensure_category_state(
                config.category, config.oai_set_spec, config.coverage_start
            )
            requested = self._eligible_catchup_dates(config, dates)
            self.store.ensure_catchup_targets(config.category, requested)
            for mailing_date in requested:
                self._check_cancelled()
                if self._sync_catchup_day(config, mailing_date):
                    network_successes += 1
                else:
                    network_failures += 1
                if attempted is not None:
                    attempted(config.category, mailing_date)
        return self.progress(
            configs,
            offline=network_failures > 0 and network_successes == 0,
        )

    def _sync_incremental(self, config: CategoryConfig) -> bool:
        state = self.store.category_sync_state(config.category)
        requested_from = (
            state.coverage_start
            if state.completed_through_utc is None
            else max(
                state.coverage_start,
                state.completed_through_utc - timedelta(days=1),
            )
        )
        restarted = False
        while True:
            run_id = self.store.begin_sync_run(
                config.category,
                "incremental",
                requested_from,
                None,
                self._now(),
            )
            try:
                self._check_cancelled()
                page = self.oai_source.first_page(
                    config.oai_set_spec,
                    requested_from,
                    cancelled=self._cancelled,
                )
                final_response_at = self._apply_oai_chain(
                    run_id,
                    config,
                    page,
                )
                completed_at = self._now()
                self.store.complete_incremental_run(
                    run_id,
                    final_response_at.date(),
                    final_response_at,
                    completed_at,
                )
                return True
            except Exception as error:
                code, message = self._error(error, "metadata synchronization")
                self.store.fail_sync_run(run_id, code, message, self._now())
                self._raise_cancelled(error)
                if self._is_expired_token(error) and not restarted:
                    restarted = True
                    continue
                return False

    def _apply_oai_chain(
        self,
        run_id: int,
        config: CategoryConfig,
        first_page: OaiPage,
    ) -> datetime:
        page = first_page
        while True:
            self._check_cancelled()
            self.store.apply_oai_page(
                run_id,
                config.category,
                page.records,
                oai_observations(page),
                page.raw_sha256,
                page.response_date,
            )
            if page.resumption_token is None:
                return page.response_date
            self._check_cancelled()
            page = self.oai_source.next_page(
                page.resumption_token,
                cancelled=self._cancelled,
            )

    def _sync_atom(self, config: CategoryConfig) -> bool:
        try:
            self._check_cancelled()
            batch = self.atom_source.fetch(
                config.category,
                cancelled=self._cancelled,
            )
            self._check_cancelled()
            if batch.category != config.category:
                raise ValueError("Atom category does not match the request")
            self.store.apply_atom_batch(batch, atom_observations(batch))
            return True
        except Exception as error:
            self._raise_cancelled(error)
            return False

    def extend_coverage(self, category: str, new_start: date) -> CategorySyncRecord:
        state = self.store.category_sync_state(category)
        if new_start >= state.coverage_start:
            if new_start == state.coverage_start:
                return state
            raise ValueError("coverage can only be extended earlier")
        if (
            state.pending_backfill_start is not None
            and new_start >= state.pending_backfill_start
        ):
            return state
        earliest, latest_finalized = self.coverage_bounds()
        if not earliest <= new_start <= latest_finalized:
            raise ValueError(
                "requested coverage is outside the catch-up recovery window"
            )
        identify = self.oai_source.identify(cancelled=self._cancelled)
        if new_start < identify.earliest_datestamp:
            raise ValueError("requested coverage predates the OAI repository")
        self.store.set_pending_backfill(
            category, new_start, state.coverage_start
        )
        return self.store.category_sync_state(category)

    def _sync_backfill(
        self, config: CategoryConfig, state: CategorySyncRecord
    ) -> bool:
        pending_start = state.pending_backfill_start
        pending_until = state.pending_backfill_until
        if pending_start is None or pending_until is None:
            raise ValueError("pending backfill interval is incomplete")

        run_id = self.store.begin_sync_run(
            config.category,
            "coverage_backfill",
            pending_start,
            pending_until,
            self._now(),
        )
        try:
            self._check_cancelled()
            page = self.oai_source.backfill_first_page(
                config.oai_set_spec,
                pending_start,
                pending_until,
                cancelled=self._cancelled,
            )
            final_response_at = self._apply_oai_chain(
                run_id,
                config,
                page,
            )
            self.store.complete_backfill_run(
                run_id, pending_start, final_response_at, self._now()
            )
            return True
        except Exception as error:
            code, message = self._error(error, "historical backfill")
            self.store.fail_sync_run(run_id, code, message, self._now())
            self._raise_cancelled(error)
            return False

    def _eligible_catchup_dates(
        self,
        config: CategoryConfig,
        supplied: Mapping[str, Iterable[date]] | None,
    ) -> tuple[date, ...]:
        today = self._today()
        earliest = today - timedelta(days=self.catchup_window_days - 1)
        if supplied is None:
            start = max(config.coverage_start, earliest)
            requested = (
                start + timedelta(days=offset)
                for offset in range((today - start).days + 1)
            )
        else:
            requested = supplied.get(config.category, ())
        successful = {
            record.daily_list_date
            for record in self.store.catchup_day_records(config.category)
            if record.status
            in {CatchupDayStatus.COMPLETE, CatchupDayStatus.EMPTY}
            and self._daily_list_date_is_finalized(record.daily_list_date)
        }
        return tuple(
            day
            for day in sorted(set(requested))
            if earliest <= day <= today and day not in successful
        )

    def _sync_catchup_day(
        self, config: CategoryConfig, mailing_date: date
    ) -> bool:
        try:
            self._check_cancelled()
            result = self.catchup_source.fetch_day(
                config.category,
                mailing_date,
                cancelled=self._cancelled,
            )
            self._check_cancelled()
            if (
                result.category != config.category
                or result.mailing_date != mailing_date
            ):
                raise ValueError("catch-up response does not match the request")
            if (
                result.status == EnrichmentStatus.EMPTY
                and not self._daily_list_date_is_finalized(mailing_date)
            ):
                return True
            attempted_at = self._now()
            self.store.apply_catchup_day(
                result,
                catchup_observations(result, attempted_at),
                attempted_at,
            )
            return result.status != EnrichmentStatus.FAILED
        except Exception as error:
            self._raise_cancelled(error)
            code, message = self._error(error, "catch-up")
            result = CatchupDay(
                category=config.category,
                mailing_date=mailing_date,
                status=EnrichmentStatus.FAILED,
                pages=(),
                error_code=code,
                error_message=message,
            )
            self.store.apply_catchup_day(result, (), self._now())
            return False

    def progress(
        self, configs: tuple[CategoryConfig, ...], *, offline: bool = False
    ) -> SyncReport:
        values: list[CategoryProgress] = []
        today = self._today()
        retry_earliest = today - timedelta(days=self.catchup_window_days - 1)
        statuses_by_category: dict[
            str, dict[date, CatchupDayStatus]
        ] = {}
        for config in configs:
            state = self.store.category_sync_state(config.category)
            backfill = self.store.latest_sync_run(
                config.category, "coverage_backfill"
            )
            records = tuple(
                record
                for record in self.store.catchup_day_records(config.category)
                if record.daily_list_date >= config.coverage_start
            )
            statuses = {
                record.daily_list_date: record.status for record in records
            }
            statuses_by_category[config.category] = statuses
            targets = tuple(sorted(statuses))
            with_papers = tuple(
                record.daily_list_date
                for record in records
                if record.status is CatchupDayStatus.COMPLETE
            )
            empty = tuple(
                record.daily_list_date
                for record in records
                if record.status is CatchupDayStatus.EMPTY
            )
            failed = tuple(
                record.daily_list_date
                for record in records
                if record.status is CatchupDayStatus.FAILED
            )
            pending = tuple(
                record.daily_list_date
                for record in records
                if record.status is CatchupDayStatus.PENDING
            )
            checked = tuple(sorted((*with_papers, *empty, *failed)))
            unavailable = tuple(
                day
                for day in (*failed, *pending)
                if day < retry_earliest
            )
            retryable_failed_exact = tuple(
                day for day in failed
                if retry_earliest <= day <= today
            )
            exact = set(with_papers) | set(empty)
            if exact:
                exact_start = min(exact)
                exact_end = max(exact)
            else:
                exact_start = None
                exact_end = None
            missing = tuple(sorted((*failed, *pending)))
            if state.pending_backfill_start is None:
                backfill_status = "idle"
                backfill_error_code = None
                backfill_error_message = None
            elif backfill is None:
                backfill_status = "pending"
                backfill_error_code = None
                backfill_error_message = None
            else:
                backfill_status = backfill.status
                backfill_error_code = backfill.error_code
                backfill_error_message = backfill.error_message
            if not targets:
                daily_list_status = "idle"
            elif pending:
                daily_list_status = "pending"
            elif failed:
                daily_list_status = "failed"
            else:
                daily_list_status = "complete"
            values.append(
                CategoryProgress(
                    category=config.category,
                    metadata_sync=MetadataSyncProgress(
                        status=state.status,
                        completed_through_utc=state.completed_through_utc,
                        last_error_code=state.last_error_code,
                        last_error_message=state.last_error_message,
                    ),
                    historical_backfill=HistoricalBackfillProgress(
                        status=backfill_status,
                        pending_start=state.pending_backfill_start,
                        pending_until=state.pending_backfill_until,
                        last_error_code=backfill_error_code,
                        last_error_message=backfill_error_message,
                    ),
                    exact_start=exact_start,
                    exact_end=exact_end,
                    missing_exact_dates=missing,
                    failed_exact_dates=failed,
                    retryable_failed_exact_dates=retryable_failed_exact,
                    target_dates=targets,
                    checked_dates=checked,
                    dates_with_papers=with_papers,
                    empty_dates=empty,
                    failed_dates=failed,
                    pending_dates=pending,
                    unavailable_dates=unavailable,
                    daily_list_status=daily_list_status,
                    daily_list_errors=tuple(
                        (record.daily_list_date, record.error_code)
                        for record in records
                        if record.error_code is not None
                    ),
                )
            )

        global_targets = sorted(
            {
                day
                for statuses in statuses_by_category.values()
                for day in statuses
            }
        )
        global_with_papers: list[date] = []
        global_empty: list[date] = []
        global_failed: list[date] = []
        global_pending: list[date] = []
        global_unavailable: list[date] = []
        config_by_category = {config.category: config for config in configs}
        for day in global_targets:
            applicable = tuple(
                statuses_by_category[category].get(
                    day, CatchupDayStatus.PENDING
                )
                for category, config in config_by_category.items()
                if config.coverage_start <= day
            )
            if not applicable or CatchupDayStatus.PENDING in applicable:
                global_pending.append(day)
            elif CatchupDayStatus.FAILED in applicable:
                global_failed.append(day)
            elif CatchupDayStatus.COMPLETE in applicable:
                global_with_papers.append(day)
            else:
                global_empty.append(day)
            if day < retry_earliest and any(
                status
                in {CatchupDayStatus.PENDING, CatchupDayStatus.FAILED}
                for status in applicable
            ):
                global_unavailable.append(day)

        global_checked = (
            len(global_with_papers) + len(global_empty) + len(global_failed)
        )
        global_exact = set(global_with_papers) | set(global_empty)
        global_start = min(global_exact) if global_exact else None
        global_end = max(global_exact) if global_exact else None
        global_missing = tuple(sorted((*global_failed, *global_pending)))
        if not global_targets:
            daily_list_status = "idle"
        elif global_pending:
            daily_list_status = "pending"
        elif global_failed:
            daily_list_status = "failed"
        else:
            daily_list_status = "complete"
        return SyncReport(
            categories=tuple(values),
            offline=offline,
            metadata_complete=all(
                value.metadata_sync.status == "idle"
                and value.metadata_sync.completed_through_utc is not None
                for value in values
            ),
            exact_start=global_start,
            exact_end=global_end,
            missing_exact_dates=global_missing,
            target_dates=len(global_targets),
            checked_dates=global_checked,
            dates_with_papers=len(global_with_papers),
            empty_dates=len(global_empty),
            failed_dates=len(global_failed),
            pending_dates=len(global_pending),
            unavailable_dates=len(global_unavailable),
            daily_list_status=daily_list_status,
        )
