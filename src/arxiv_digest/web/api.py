"""Strict, versioned JSON routing for the authenticated loopback server."""

from __future__ import annotations

import hmac
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from arxiv_digest.maintenance import (
    MaintenanceBarrier, MaintenanceTimeoutError, UpdateInProgressError, WorkActiveError,
)
from arxiv_digest.sources.xml import parse_arxiv_id


JSON_BODY_LIMIT = 64 * 1024
BACKUP_BODY_LIMIT = 64 * 1024 * 1024
QUERY_TEXT_LIMIT = 200
QUERY_STRING_LIMIT = 2048
_UPDATE_CONTROL_OPERATIONS = frozenset({
    "status", "update", "update_start", "update_job", "update_commit",
    "update_handoff_ack", "update_receipt", "update_receipt_ack",
})

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
Handler = Callable[[dict[str, Any]], Any]
KnownPaper = Callable[[str], bool]
Validator = Callable[[Any], bool]


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApiRequest:
    method: str
    target: str
    headers: Mapping[str, str]
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class BinaryPayload:
    data: bytes
    filename: str = "arxiv-digest-backup.zip"


@dataclass(frozen=True, slots=True)
class JsonPayload:
    status: int
    data: Any


@dataclass(frozen=True, slots=True)
class ReviewPagePayload:
    """A review page plus the snapshot boundary used for discovery labels."""

    page: Any
    last_finished_revision: int | None
    latest_known_versions: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SetupDraftPayload:
    """Marker for the browser-safe projection of a durable setup draft."""

    draft: Any
    corpus_can_resume: bool = False
    corpus_job: Mapping[str, Any] | None = None
    coverage_min: date | None = None
    coverage_max: date | None = None


@dataclass(frozen=True, slots=True)
class RouteSpec:
    method: Literal["GET", "POST", "PUT"]
    template: str
    operation: str
    body_kind: Literal["none", "json", "zip"] = "none"
    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    validators: Mapping[str, Validator] | None = None
    query_required: frozenset[str] = frozenset()
    query_optional: frozenset[str] = frozenset()
    query_validators: Mapping[str, Validator] | None = None
    known_paper_field: str | None = None


def _is_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and value >= 1


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= QUERY_TEXT_LIMIT


def _is_optional_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= QUERY_TEXT_LIMIT


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9_-]{8,128}", value
    ) is not None


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"[0-9a-f]{64}", value
    ) is not None


def _is_arxiv_id(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        base, version = parse_arxiv_id(value)
    except (TypeError, ValueError):
        return False
    return base == value and version is None


def _is_optional_version(value: Any) -> bool:
    return value is None or _is_positive_int(value)


def _is_string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 2_000
        and all(_is_text(item) for item in value)
    )


def _is_category_selections(value: Any) -> bool:
    return (
        isinstance(value, list)
        and 0 < len(value) <= 200
        and all(
            isinstance(item, dict)
            and set(item) == {"category", "set_spec"}
            and _is_text(item["category"])
            and _is_text(item["set_spec"])
            for item in value
        )
    )


def _is_category_configs(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 200
        and all(
            isinstance(item, dict)
            and set(item) == {"category", "set_spec", "coverage_start"}
            and _is_text(item["category"])
            and _is_text(item["set_spec"])
            and _is_iso_date(item["coverage_start"])
            for item in value
        )
    )


def _is_destination_choice(value: Any) -> bool:
    return value in {"downloads", "documents"} or (
        isinstance(value, str)
        and re.fullmatch(r"picker_[A-Za-z0-9_-]{8,120}", value) is not None
    )


def _is_tested_destination_token(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"destination_[A-Za-z0-9_-]{8,112}", value
    ) is not None


def _is_offset_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and value.isdigit()
        and int(value) <= 1_000_000
    )


COMMON_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _route(
    method: Literal["GET", "POST", "PUT"],
    template: str,
    operation: str,
    *,
    body_kind: Literal["none", "json", "zip"] = "none",
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
    validators: Mapping[str, Validator] | None = None,
    query_required: tuple[str, ...] = (),
    query_optional: tuple[str, ...] = (),
    query_validators: Mapping[str, Validator] | None = None,
    known_paper_field: str | None = None,
) -> RouteSpec:
    return RouteSpec(
        method=method,
        template=template,
        operation=operation,
        body_kind=body_kind,
        required=frozenset(required),
        optional=frozenset(optional),
        validators=validators,
        query_required=frozenset(query_required),
        query_optional=frozenset(query_optional),
        query_validators=query_validators,
        known_paper_field=known_paper_field,
    )


_R = _route
_ROUTES = (
    _R("GET", "/api/v1/status", "status"),
    _R("GET", "/api/v1/update", "update"),
    _R("POST", "/api/v1/update/start", "update_start", body_kind="json", required=("target_version",), validators={"target_version": lambda value: isinstance(value, str) and len(value) <= 64 and re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value) is not None}),
    _R("GET", "/api/v1/update/jobs/{job_id}", "update_job"),
    _R("POST", "/api/v1/update/jobs/{job_id}/commit", "update_commit", body_kind="json"),
    _R("POST", "/api/v1/update/jobs/{job_id}/handoff-ack", "update_handoff_ack", body_kind="json"),
    _R("GET", "/api/v1/update/receipt", "update_receipt"),
    _R("POST", "/api/v1/update/receipt/{receipt_id}/ack", "update_receipt_ack", body_kind="json"),
    _R("GET", "/api/v1/categories", "categories", query_optional=("q",), query_validators={"q": _is_optional_text}),
    _R("GET", "/api/v1/setup/draft", "setup_draft_get"),
    _R("PUT", "/api/v1/setup/draft", "setup_draft_put", body_kind="json"),
    _R("POST", "/api/v1/setup/corpus", "setup_corpus", body_kind="json", required=("draft_revision", "mode"), validators={"draft_revision": _is_int, "mode": lambda value: value in {"resume", "restart"}}),
    _R("POST", "/api/v1/setup/corpus/accept", "setup_corpus_accept", body_kind="json", required=("draft_revision", "corpus_hash"), validators={"draft_revision": _is_int, "corpus_hash": _is_sha256}),
    _R("GET", "/api/v1/setup/jobs/{job_id}", "setup_job"),
    _R("GET", "/api/v1/setup/candidates/papers", "setup_candidate_papers", query_optional=("q", "offset"), query_validators={"q": _is_optional_text, "offset": _is_offset_text}),
    _R("GET", "/api/v1/setup/candidates/terms", "setup_candidate_terms"),
    _R("GET", "/api/v1/setup/candidates/authors", "setup_candidate_authors", query_optional=("q",), query_validators={"q": _is_optional_text}),
    _R("POST", "/api/v1/setup/papers/lookup", "setup_paper_lookup", body_kind="json", required=("draft_revision", "arxiv_id"), validators={"draft_revision": _is_int, "arxiv_id": _is_arxiv_id}),
    _R("POST", "/api/v1/setup/folder/pick", "setup_folder_pick", body_kind="json", required=("draft_revision",), validators={"draft_revision": _is_int}),
    _R("POST", "/api/v1/setup/folder/test", "setup_folder_test", body_kind="json", required=("draft_revision", "destination_choice"), validators={"draft_revision": _is_int, "destination_choice": _is_destination_choice}),
    _R("POST", "/api/v1/setup/complete", "setup_complete", body_kind="json", required=("draft_revision", "launcher_choice"), validators={"draft_revision": _is_int, "launcher_choice": lambda value: value in {"create", "not_now"}}),
    _R("POST", "/api/v1/tabs/connect", "tabs_connect", body_kind="json", required=("tab_id",), validators={"tab_id": _is_id}),
    _R("POST", "/api/v1/tabs/heartbeat", "tabs_heartbeat", body_kind="json", required=("tab_id",), validators={"tab_id": _is_id}),
    _R("POST", "/api/v1/tabs/disconnect", "tabs_disconnect", body_kind="json", required=("tab_id",), validators={"tab_id": _is_id}),
    _R(
        "POST",
        "/api/v1/sync/start",
        "sync_start",
        optional=("retry_failed_dates",),
        validators={"retry_failed_dates": lambda value: type(value) is bool},
    ),
    _R("POST", "/api/v1/sync/cancel", "sync_cancel", body_kind="json", required=("job_id",), validators={"job_id": _is_id}),
    _R("GET", "/api/v1/review/summary", "review_summary"),
    _R("POST", "/api/v1/review/finish", "review_finish_all", body_kind="json", required=("snapshot_revision", "profile_revision", "projection_revision"), validators={"snapshot_revision": _is_int, "profile_revision": _is_positive_int, "projection_revision": _is_int}),
    _R("GET", "/api/v1/review/calendar", "review_calendar", query_required=("start", "end"), query_validators={"start": _is_iso_date, "end": _is_iso_date}),
    _R("GET", "/api/v1/review/date", "review_date", query_required=("date",), query_optional=("anchor_event_id", "from_start"), query_validators={"date": _is_iso_date, "anchor_event_id": _is_offset_text, "from_start": lambda value: value in {"true", "false"}}),
    _R("PUT", "/api/v1/review/date/position", "review_position", body_kind="json", required=("date", "snapshot_revision", "profile_revision", "projection_revision", "anchor_event_id"), validators={"date": _is_iso_date, "snapshot_revision": _is_int, "profile_revision": _is_positive_int, "projection_revision": _is_int, "anchor_event_id": _is_positive_int}),
    _R("POST", "/api/v1/review/date/finish", "review_finish", body_kind="json", required=("date", "snapshot_revision", "profile_revision", "projection_revision"), validators={"date": _is_iso_date, "snapshot_revision": _is_int, "profile_revision": _is_positive_int, "projection_revision": _is_int}),
    _R("GET", "/api/v1/library", "library", query_optional=("q", "offset"), query_validators={"q": _is_optional_text, "offset": _is_offset_text}),
    _R("POST", "/api/v1/library/save", "library_save", body_kind="json", required=("arxiv_id",), optional=("version",), validators={"arxiv_id": _is_arxiv_id, "version": _is_optional_version}, known_paper_field="arxiv_id"),
    _R("POST", "/api/v1/library/remove", "library_remove", body_kind="json", required=("arxiv_id",), validators={"arxiv_id": _is_arxiv_id}, known_paper_field="arxiv_id"),
    _R("POST", "/api/v1/library/pdf", "library_pdf", body_kind="json", required=("arxiv_id", "version", "save_first", "save_version"), validators={"arxiv_id": _is_arxiv_id, "version": _is_positive_int, "save_first": lambda value: type(value) is bool, "save_version": _is_optional_version}, known_paper_field="arxiv_id"),
    _R("GET", "/api/v1/downloads/{job_id}", "download_status"),
    _R("GET", "/api/v1/interests", "interests_get", query_optional=("refresh",), query_validators={"refresh": lambda value: value == "1"}),
    _R("PUT", "/api/v1/interests", "interests_put", body_kind="json", required=("expected_revision",), optional=("categories", "keywords", "phrases", "authors", "seed_papers", "category_configs"), validators={"expected_revision": _is_positive_int, "categories": _is_category_selections, "keywords": _is_string_list, "phrases": _is_string_list, "authors": _is_string_list, "seed_papers": _is_string_list, "category_configs": _is_category_configs}),
    _R("GET", "/api/v1/settings", "settings_get"),
    _R("PUT", "/api/v1/settings/coverage", "settings_coverage", body_kind="json", required=("category", "new_start", "expected_revision"), validators={"category": _is_text, "new_start": _is_iso_date, "expected_revision": _is_positive_int}),
    _R("POST", "/api/v1/settings/cache/clear", "settings_cache_clear"),
    _R("POST", "/api/v1/settings/folder/pick", "settings_folder_pick"),
    _R("POST", "/api/v1/settings/folder/test", "settings_folder_test", body_kind="json", required=("destination_choice",), validators={"destination_choice": _is_destination_choice}),
    _R("PUT", "/api/v1/settings/folder", "settings_folder", body_kind="json", required=("expected_revision", "tested_destination_token"), validators={"expected_revision": _is_positive_int, "tested_destination_token": _is_tested_destination_token}),
    _R("POST", "/api/v1/settings/folder/open", "settings_folder_open"),
    _R("GET", "/api/v1/settings/doctor", "settings_doctor"),
    _R("GET", "/api/v1/settings/launcher", "settings_launcher"),
    _R("POST", "/api/v1/settings/launcher/create", "settings_launcher_create"),
    _R("POST", "/api/v1/settings/launcher/not-now", "settings_launcher_not_now"),
    _R("POST", "/api/v1/settings/launcher/remove", "settings_launcher_remove"),
    _R("GET", "/api/v1/backup/export", "backup_export"),
    _R("POST", "/api/v1/backup/inspect", "backup_inspect", body_kind="zip"),
    _R("POST", "/api/v1/backup/restore", "backup_restore", body_kind="json", required=("pending_restore_id", "destination_choice", "cancel_active"), validators={"pending_restore_id": _is_id, "destination_choice": _is_destination_choice, "cancel_active": lambda value: type(value) is bool}),
    _R("POST", "/api/v1/application/quit", "application_quit"),
)

API_ROUTE_SURFACE = frozenset((route.method, route.template) for route in _ROUTES)
API_OPERATIONS = frozenset(route.operation for route in _ROUTES)
_DYNAMIC_PATTERN = re.compile(
    r"(?P<prefix>/api/v1/(?:setup/jobs|downloads))/(?P<job_id>[A-Za-z0-9_-]{8,128})"
)
_UPDATE_JOB_PATTERN = re.compile(r"/api/v1/update/jobs/(?P<job_id>[a-f0-9]{64})(?P<suffix>/(?:commit|handoff-ack))?")
_UPDATE_RECEIPT_PATTERN = re.compile(r"/api/v1/update/receipt/(?P<receipt_id>[a-f0-9]{64})/ack")


_SETUP_DRAFT_FIELDS: dict[
    str, tuple[frozenset[str], Mapping[str, Validator]]
] = {
    "categories": (frozenset({"selections"}), {"selections": _is_category_selections}),
    "coverage": (frozenset({"coverage_start"}), {"coverage_start": _is_iso_date}),
    "seed_papers": (frozenset({"accepted_suggestion_ids", "custom_arxiv_ids"}), {"accepted_suggestion_ids": _is_string_list, "custom_arxiv_ids": lambda values: _is_string_list(values) and all(_is_arxiv_id(value) for value in values)}),
    "terms": (frozenset({"accepted_keyword_suggestion_ids", "accepted_phrase_suggestion_ids", "custom_keywords", "custom_phrases"}), {"accepted_keyword_suggestion_ids": _is_string_list, "accepted_phrase_suggestion_ids": _is_string_list, "custom_keywords": _is_string_list, "custom_phrases": _is_string_list}),
    "authors": (frozenset({"accepted_suggestion_ids", "custom_authors"}), {"accepted_suggestion_ids": _is_string_list, "custom_authors": _is_string_list}),
    "pdf_destination": (frozenset({"tested_destination_token"}), {"tested_destination_token": _is_tested_destination_token}),
    "review": (frozenset({"confirmed", "profile_summary_sha256"}), {"confirmed": lambda value: value is True, "profile_summary_sha256": _is_sha256}),
}


def _header(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == folded),
        None,
    )


def _review_event_label(event: Any) -> str | None:
    from arxiv_digest.models import AnnounceType, EvidenceSource

    announce_types = {
        item.announce_type
        for item in event.observations
        if item.source is EvidenceSource.CATCHUP
        and item.announce_type is not None
    }
    for announce_type, label in (
        (AnnounceType.REPLACE, "Replacement"),
        (AnnounceType.REPLACE_CROSS, "Replacement cross-list"),
        (AnnounceType.CROSS, "Cross-list"),
    ):
        if announce_type in announce_types:
            return label
    return None


def _review_version_label(event: Any) -> str:
    if event.announced_version is None:
        return "Version not confirmed"
    if event.announced_version == 1:
        return ""
    return f"Version v{event.announced_version}"


def project_review_page(payload: ReviewPagePayload) -> dict[str, JsonValue]:
    """Expose the intentionally public, browser-safe review card schema."""

    page = payload.page
    cards: list[JsonValue] = []
    for ranked in page.cards:
        event = ranked.event
        paper = ranked.paper
        card: dict[str, JsonValue] = {
            "event_id": event.event_id,
            "arxiv_id": paper.arxiv_id,
            "daily_list_date": event.daily_list_date.isoformat(),
            "version_resolution": event.version_resolution.value,
            "version_label": _review_version_label(event),
            "support_categories": sorted(
                {
                    item.category
                    for item in event.observations
                    if item.category is not None
                },
                key=str.casefold,
            ),
            "subjects": list(paper.categories),
            "resolved_announcement_version": event.announced_version,
            "latest_known_version": payload.latest_known_versions.get(
                paper.arxiv_id,
                event.announced_version,
            ),
            "title": paper.title,
            "authors": list(paper.authors),
            "abstract": paper.abstract,
            "comments": paper.comments,
            "journal_ref": paper.journal_ref,
            "doi": paper.doi,
            "newly_discovered": (
                event.reviewed_at is None
                and payload.last_finished_revision is not None
                and event.queue_revision > payload.last_finished_revision
            ),
            "reviewed": event.reviewed_at is not None,
            "tier": ranked.tier.value,
            "score": ranked.score,
            "reasons": [
                {
                    "kind": reason.kind,
                    "label": reason.label,
                    "location": reason.location,
                    **(
                        {
                            "reference": {
                                "arxiv_id": reason.reference.arxiv_id,
                                "title": reason.reference.title,
                            }
                        }
                        if reason.reference is not None
                        else {}
                    ),
                }
                for reason in ranked.reasons
            ],
        }
        event_label = _review_event_label(event)
        if event_label is not None:
            card["event_label"] = event_label
        cards.append(card)
    return {
        "day": page.day.isoformat(),
        "snapshot_revision": page.snapshot_revision,
        "profile_revision": page.profile_revision,
        "projection_revision": page.projection_revision,
        "anchor_event_id": page.anchor_event_id,
        "previous_anchor_event_id": page.previous_anchor_event_id,
        "next_anchor_event_id": page.next_anchor_event_id,
        "previous_date": (
            None if page.previous_date is None else page.previous_date.isoformat()
        ),
        "next_date": None if page.next_date is None else page.next_date.isoformat(),
        "page_number": page.page_number,
        "page_count": page.page_count,
        "total_cards": page.total_cards,
        "cards": cards,
    }


def _destination_display_path(path: Path, *, home: Path | None = None) -> str:
    """Return a read-only display path, abbreviating the current home as ~."""

    destination = path.resolve(strict=False)
    resolved_home = (Path.home() if home is None else home).resolve(strict=False)
    try:
        relative = destination.relative_to(resolved_home)
    except ValueError:
        return str(destination)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"


def project_setup_draft(payload: SetupDraftPayload) -> dict[str, JsonValue]:
    """Project setup state without making browser-provided paths authoritative."""

    draft = payload.draft
    from arxiv_digest.setup import SUPPORTED_CATCHUP_WINDOW_DAYS

    coverage_max = payload.coverage_max or draft.updated_at.date()
    coverage_min = payload.coverage_min or (
        coverage_max - timedelta(days=SUPPORTED_CATCHUP_WINDOW_DAYS - 1)
    )
    destination = draft.pdf_destination
    summary: dict[str, JsonValue] | None = None
    summary_sha256: str | None = None
    if draft.coverage_start is not None and destination is not None:
        summary = {
            "categories": [item.category for item in draft.categories],
            "coverage_start": draft.coverage_start.isoformat(),
            "seed_papers": [
                item.paper.arxiv_id for item in draft.seed_papers
            ],
            "seed_paper_details": [
                {
                    "arxiv_id": item.paper.arxiv_id,
                    "title": item.paper.title,
                }
                for item in draft.seed_papers
            ],
            "keywords": list(draft.keywords),
            "phrases": list(draft.phrases),
            "authors": list(draft.authors),
            "pdf_destination_kind": destination.kind,
            "pdf_destination_display_path": _destination_display_path(
                destination.path
            ),
        }
        summary_sha256 = hashlib.sha256(
            json.dumps(
                summary,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    return {
        "schema_version": draft.schema_version,
        "revision": draft.revision,
        "current_step": draft.current_step.value,
        "categories": [
            {"category": item.category, "set_spec": item.set_spec}
            for item in draft.categories
        ],
        "coverage_start": (
            None
            if draft.coverage_start is None
            else draft.coverage_start.isoformat()
        ),
        "recommended_coverage_start": (
            draft.updated_at.date() - timedelta(days=30)
        ).isoformat(),
        "coverage_min": coverage_min.isoformat(),
        "coverage_max": coverage_max.isoformat(),
        "coverage_warning": draft.coverage_warning,
        "corpus_hash": draft.corpus_hash,
        "corpus_categories": list(draft.corpus_categories),
        "corpus_complete": draft.corpus_complete,
        "corpus_reduced_breadth": draft.corpus_reduced_breadth,
        "corpus_can_resume": payload.corpus_can_resume,
        "corpus_job": (
            None if payload.corpus_job is None else _jsonable(payload.corpus_job)
        ),
        "seed_papers": [item.paper.arxiv_id for item in draft.seed_papers],
        "keywords": list(draft.keywords),
        "phrases": list(draft.phrases),
        "authors": list(draft.authors),
        "pdf_destination": (
            None if destination is None else {"kind": destination.kind}
        ),
        "destination_tested": draft.destination_tested,
        "review_confirmed": draft.review_confirmed,
        "launcher_choice": draft.launcher_choice,
        "profile_summary": summary,
        "profile_summary_sha256": summary_sha256,
        "created_at": draft.created_at.isoformat(),
        "updated_at": draft.updated_at.isoformat(),
    }


def _jsonable(value: Any) -> JsonValue:
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError("non-finite numbers cannot be returned by the API")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        raise TypeError("filesystem paths cannot be returned by the API")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, ReviewPagePayload):
        return project_review_page(value)
    if isinstance(value, SetupDraftPayload):
        return project_setup_draft(value)
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON response mappings require string keys")
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise TypeError("domain response is not JSON serializable")


def _json_response(
    status: int,
    value: JsonValue,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> ApiResponse:
    return ApiResponse(
        status=status,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **COMMON_SECURITY_HEADERS,
            **({} if extra_headers is None else extra_headers),
        },
        body=(
            json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8"),
    )


def _error(
    status: int,
    code: str,
    message: str,
    *,
    extra_headers: Mapping[str, str] | None = None,
) -> ApiResponse:
    return _json_response(
        status,
        {
            "api_version": "v1",
            "ok": False,
            "error": {"code": code, "message": message},
        },
        extra_headers=extra_headers,
    )


def error_response(status: int, code: str, message: str) -> ApiResponse:
    """Build a security-header-complete API error for HTTP framing failures."""

    return _error(status, code, message)


def _strict_json_object(body: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateJsonKey(key)
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON number: {value}")

    value = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _match_route(
    method: str,
    path: str,
) -> tuple[RouteSpec | None, dict[str, Any], frozenset[str]]:
    path_routes = tuple(route for route in _ROUTES if route.template == path)
    dynamic = _DYNAMIC_PATTERN.fullmatch(path)
    path_values: dict[str, Any] = {}
    update_job = _UPDATE_JOB_PATTERN.fullmatch(path)
    update_receipt = _UPDATE_RECEIPT_PATTERN.fullmatch(path)
    if update_job is not None:
        template = "/api/v1/update/jobs/{job_id}" + (update_job.group("suffix") or "")
        path_routes = tuple(route for route in _ROUTES if route.template == template)
        path_values = {"job_id": update_job.group("job_id")}
    elif update_receipt is not None:
        path_routes = tuple(route for route in _ROUTES if route.operation == "update_receipt_ack")
        path_values = {"receipt_id": update_receipt.group("receipt_id")}
    if dynamic is not None:
        template = (
            "/api/v1/setup/jobs/{job_id}"
            if dynamic.group("prefix") == "/api/v1/setup/jobs"
            else "/api/v1/downloads/{job_id}"
        )
        path_routes = tuple(
            route for route in _ROUTES if route.template == template
        )
        path_values = {"job_id": dynamic.group("job_id")}
    allowed = frozenset(route.method for route in path_routes)
    return (
        next((route for route in path_routes if route.method == method), None),
        path_values,
        allowed,
    )


def request_body_limit(method: str, target: str) -> int:
    """Return the maximum body size without decoding or invoking a route."""

    split = urlsplit(target)
    route, _, _ = _match_route(method, split.path)
    return (
        BACKUP_BODY_LIMIT
        if route is not None and route.body_kind == "zip"
        else JSON_BODY_LIMIT
    )


class ApiRouter:
    def __init__(
        self,
        *,
        token: str,
        host: str,
        handlers: Mapping[str, Handler],
        known_paper: KnownPaper | None = None,
        maintenance: MaintenanceBarrier | None = None,
        allow_update_quit: Callable[[], bool] = lambda: False,
    ) -> None:
        if not token:
            raise ValueError("API token must not be blank")
        self.token = token
        self.host = host
        self.handlers = dict(handlers)
        self.known_paper = known_paper or (lambda arxiv_id: True)
        self.maintenance = maintenance
        self.allow_update_quit = allow_update_quit

    def _allowed_during_update(self, request: ApiRequest) -> bool:
        route, _, _ = _match_route(request.method, urlsplit(request.target).path)
        return route is not None and (
            route.operation in _UPDATE_CONTROL_OPERATIONS
            or route.operation == "application_quit" and self.allow_update_quit()
        )

    def preflight(self, request: ApiRequest) -> ApiResponse | None:
        """Validate request authority before an HTTP adapter reads its body."""

        split = urlsplit(request.target)
        if split.fragment or _header(request.headers, "Host") != self.host:
            return _error(
                400,
                "invalid_host",
                "The request host does not match this dashboard.",
            )
        authorization = _header(request.headers, "Authorization")
        expected = f"Bearer {self.token}"
        if authorization is None or not hmac.compare_digest(
            authorization, expected
        ):
            return _error(
                401,
                "authentication_required",
                "A valid dashboard token is required.",
            )
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and _header(request.headers, "Origin") != f"http://{self.host}"
        ):
            return _error(
                403,
                "origin_required",
                "Mutations require the dashboard's exact local origin.",
            )
        if (
            self.maintenance is not None and self.maintenance.update_active
            and not self._allowed_during_update(request)
        ):
            return _error(409, "update_in_progress", "An application update is in progress.")
        return None

    def _decode_query(
        self,
        route: RouteSpec,
        query: str,
    ) -> tuple[dict[str, Any] | None, ApiResponse | None]:
        if len(query) > QUERY_STRING_LIMIT:
            return None, _error(
                400, "invalid_request", "The query string is too long."
            )
        pairs = parse_qsl(query, keep_blank_values=True)
        keys = [key for key, _ in pairs]
        allowed = route.query_required | route.query_optional
        if (
            len(keys) != len(set(keys))
            or not route.query_required <= set(keys)
            or not set(keys) <= allowed
        ):
            return None, _error(
                400,
                "invalid_request",
                "The query fields do not match this route.",
            )
        payload: dict[str, Any] = dict(pairs)
        validators = route.query_validators or {}
        if any(
            not validators.get(key, lambda value: True)(value)
            for key, value in payload.items()
        ):
            return None, _error(
                400, "invalid_request", "A query field is invalid."
            )
        if route.operation in {"library", "setup_candidate_papers"}:
            payload.setdefault("q", "")
            payload.setdefault("offset", "0")
        if "offset" in payload:
            payload["offset"] = int(payload["offset"])
        if "anchor_event_id" in payload:
            payload["anchor_event_id"] = int(payload["anchor_event_id"])
        if "from_start" in payload:
            payload["from_start"] = payload["from_start"] == "true"
        if (
            route.operation == "review_calendar"
            and payload["start"] > payload["end"]
        ):
            return None, _error(
                400,
                "invalid_request",
                "Calendar start must not follow end.",
            )
        return payload, None

    def _decode_setup_draft(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, ApiResponse | None]:
        if (
            type(payload.get("revision")) is not int
            or payload["revision"] < 0
            or not isinstance(payload.get("step"), str)
        ):
            return None, _error(
                400,
                "invalid_request",
                "Draft revision and step are required.",
            )
        shape = _SETUP_DRAFT_FIELDS.get(payload["step"])
        if shape is None:
            return None, _error(
                400, "invalid_request", "The setup step is invalid."
            )
        additional, validators = shape
        if set(payload) != {"revision", "step"} | additional or any(
            not validators[key](payload[key]) for key in additional
        ):
            return None, _error(
                400,
                "invalid_request",
                "The setup step fields do not match its schema.",
            )
        return payload, None

    def _decode_json(
        self,
        route: RouteSpec,
        request: ApiRequest,
    ) -> tuple[dict[str, Any] | None, ApiResponse | None]:
        if not request.body:
            if route.required or route.operation in {"update_commit", "update_handoff_ack", "update_receipt_ack"}:
                return None, _error(
                    400, "invalid_request", "A JSON body is required."
                )
            return {}, None
        media_type = (
            (_header(request.headers, "Content-Type") or "")
            .split(";", 1)[0]
            .strip()
            .casefold()
        )
        if media_type != "application/json":
            return None, _error(
                415,
                "unsupported_media_type",
                "This route requires application/json.",
            )
        try:
            payload = _strict_json_object(request.body)
        except _DuplicateJsonKey:
            return None, _error(
                400,
                "duplicate_json_key",
                "JSON object keys must be unique.",
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None, _error(
                400, "invalid_json", "The JSON body is invalid."
            )
        if route.operation == "setup_draft_put":
            return self._decode_setup_draft(payload)
        allowed = route.required | route.optional
        if not route.required <= set(payload) or not set(payload) <= allowed:
            return None, _error(
                400,
                "invalid_request",
                "The request fields do not match this route.",
            )
        validators = route.validators or {}
        if any(
            not validators.get(key, lambda value: True)(value)
            for key, value in payload.items()
        ):
            return None, _error(
                400, "invalid_request", "A request field is invalid."
            )
        return payload, None

    def dispatch(self, request: ApiRequest) -> ApiResponse:
        preflight_error = self.preflight(request)
        if preflight_error is not None:
            return preflight_error
        if self.maintenance is None:
            return self._dispatch_admitted(request)
        try:
            with self.maintenance.handler(
                allow_during_update=self._allowed_during_update(request),
            ):
                return self._dispatch_admitted(request)
        except UpdateInProgressError:
            return _error(409, "update_in_progress", "An application update is in progress.")

    def _dispatch_admitted(self, request: ApiRequest) -> ApiResponse:
        split = urlsplit(request.target)
        path = split.path
        route, path_values, allowed = _match_route(request.method, path)
        if route is None:
            if allowed:
                return _error(
                    405,
                    "method_not_allowed",
                    "The method is not supported for this API route.",
                    extra_headers={"Allow": ", ".join(sorted(allowed))},
                )
            return _error(
                404, "route_not_found", "The API route does not exist."
            )
        body_limit = (
            BACKUP_BODY_LIMIT if route.body_kind == "zip" else JSON_BODY_LIMIT
        )
        if len(request.body) > body_limit:
            return _error(
                413,
                "body_too_large",
                "The request body exceeds this route's limit.",
            )
        query_payload, query_error = self._decode_query(route, split.query)
        if query_error is not None:
            return query_error
        assert query_payload is not None
        payload = {**path_values, **query_payload}
        if route.body_kind == "json" or (
            route.body_kind == "none" and request.body
        ):
            body_payload, body_error = self._decode_json(route, request)
            if body_error is not None:
                return body_error
            assert body_payload is not None
            payload.update(body_payload)
        elif route.body_kind == "zip":
            media_type = (
                (_header(request.headers, "Content-Type") or "")
                .split(";", 1)[0]
                .strip()
                .casefold()
            )
            if media_type != "application/zip":
                return _error(
                    415,
                    "unsupported_media_type",
                    "This route requires application/zip.",
                )
            if not request.body:
                return _error(
                    400, "invalid_request", "A backup archive is required."
                )
            payload["archive"] = request.body
        if (
            route.known_paper_field is not None
            and not self.known_paper(payload[route.known_paper_field])
        ):
            return _error(
                404,
                "paper_not_found",
                "The paper is not present in local application state.",
            )
        handler = self.handlers.get(route.operation)
        if handler is None:
            return _error(
                503,
                "service_unavailable",
                "This local service is not available yet.",
            )
        try:
            result = handler(payload)
            if isinstance(result, BinaryPayload):
                if len(result.data) > BACKUP_BODY_LIMIT or not re.fullmatch(
                    r"arxiv-digest-[A-Za-z0-9_.-]+\.zip", result.filename
                ):
                    return _error(
                        500,
                        "invalid_response",
                        "The backup response was unsafe.",
                    )
                return ApiResponse(
                    200,
                    {
                        "Content-Type": "application/zip",
                        "Content-Disposition": (
                            f'attachment; filename="{result.filename}"'
                        ),
                        **COMMON_SECURITY_HEADERS,
                    },
                    result.data,
                )
            response_status = 200
            if isinstance(result, JsonPayload):
                if result.status not in {200, 202}:
                    raise ValueError("invalid JSON success response status")
                response_status, result = result.status, result.data
            return _json_response(
                response_status,
                {
                    "api_version": "v1",
                    "ok": True,
                    "data": _jsonable(result),
                },
            )
        except UpdateInProgressError:
            return _error(409, "update_in_progress", "An application update is in progress.")
        except WorkActiveError:
            return _error(
                409,
                "work_active",
                "Background work is still active; cancel it and retry.",
            )
        except MaintenanceTimeoutError:
            return _error(
                503,
                "maintenance_timeout",
                "Maintenance could not start before its safety timeout.",
            )
        except KeyError:
            return _error(
                404,
                "domain_not_found",
                "The requested local object was not found.",
            )
        except ValueError as error:
            code = getattr(error, "code", None)
            safe_code = (
                code
                if isinstance(code, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", code)
                else "domain_error"
            )
            from arxiv_digest.update_coordinator import UpdateRequestError
            return _error(
                409 if isinstance(error, UpdateRequestError) else 400,
                safe_code,
                (
                    str(error)
                    if safe_code != "domain_error" and str(error)
                    else "The request was rejected."
                ),
            )
        except Exception as error:
            code = getattr(error, "code", None)
            if code == "review_snapshot_stale":
                return _error(
                    409,
                    "review_snapshot_stale",
                    "The active Review projection changed; reload and try again.",
                )
            if isinstance(code, str) and re.fullmatch(
                r"[a-z][a-z0-9_]{1,63}", code
            ):
                return _error(400, code, str(error) or "The request was rejected.")
            if type(error).__name__.endswith("RevisionError"):
                return _error(
                    409,
                    "revision_conflict",
                    "The local state changed; reload and try again.",
                )
            return _error(
                500,
                "internal_error",
                "The local service could not complete the request.",
            )
