from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace


TOKEN = "A" * 43
HOST = "127.0.0.1:43123"
ORIGIN = f"http://{HOST}"


def _request(
    method: str,
    target: str,
    *,
    token: str | None = TOKEN,
    origin: str | None = None,
    body: bytes = b"",
    content_type: str | None = None,
):
    from arxiv_digest.web.api import ApiRequest

    headers = {"Host": HOST}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    if content_type is not None:
        headers["Content-Type"] = content_type
    return ApiRequest(method=method, target=target, headers=headers, body=body)


def _json(response):
    return json.loads(response.body)


def test_every_api_call_requires_the_lifetime_bearer_token() -> None:
    from arxiv_digest.web.api import ApiRouter

    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"status": lambda payload: {"state": "ready"}},
    )

    rejected = router.dispatch(_request("GET", "/api/v1/status", token=None))
    accepted = router.dispatch(_request("GET", "/api/v1/status"))

    assert rejected.status == 401
    assert _json(rejected)["error"]["code"] == "authentication_required"
    assert accepted.status == 200
    assert _json(accepted) == {
        "api_version": "v1",
        "ok": True,
        "data": {"state": "ready"},
    }


def test_host_must_match_the_exact_bound_loopback_authority() -> None:
    from arxiv_digest.web.api import ApiRequest, ApiRouter

    router = ApiRouter(token=TOKEN, host=HOST, handlers={})
    request = _request("GET", "/api/v1/status")
    forged = ApiRequest(
        method=request.method,
        target=request.target,
        headers={**request.headers, "Host": "localhost:43123"},
    )

    response = router.dispatch(forged)

    assert response.status == 400
    assert _json(response)["error"]["code"] == "invalid_host"
    assert not any(name.casefold().startswith("access-control-") for name in response.headers)


def test_mutations_require_the_exact_local_origin() -> None:
    from arxiv_digest.web.api import ApiRouter

    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"sync_start": lambda payload: {"job_id": "job_1"}},
    )

    missing = router.dispatch(_request("POST", "/api/v1/sync/start"))
    forged = router.dispatch(
        _request(
            "POST",
            "/api/v1/sync/start",
            origin="http://localhost:43123",
        )
    )
    accepted = router.dispatch(
        _request("POST", "/api/v1/sync/start", origin=ORIGIN)
    )

    assert missing.status == 403
    assert forged.status == 403
    assert _json(forged)["error"]["code"] == "origin_required"
    assert accepted.status == 200


def test_sync_start_accepts_a_scoped_failed_date_retry() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={
            "sync_start": lambda payload: seen.append(payload)
            or {"job_id": "sync_retry_1234"}
        },
    )

    response = router.dispatch(
        _request(
            "POST",
            "/api/v1/sync/start",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"retry_failed_dates":true}',
        )
    )

    assert response.status == 200
    assert seen == [{"retry_failed_dates": True}]


def test_settings_coverage_requires_profile_revision() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"settings_coverage": lambda payload: seen.append(payload) or {}},
    )

    missing = router.dispatch(
        _request(
            "PUT",
            "/api/v1/settings/coverage",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"category":"cs.SE","new_start":"2026-07-01"}',
        )
    )
    accepted = router.dispatch(
        _request(
            "PUT",
            "/api/v1/settings/coverage",
            origin=ORIGIN,
            content_type="application/json",
            body=(
                b'{"category":"cs.SE","new_start":"2026-07-01",'
                b'"expected_revision":7}'
            ),
        )
    )

    assert missing.status == 400
    assert accepted.status == 200
    assert seen == [
        {
            "category": "cs.SE",
            "new_start": "2026-07-01",
            "expected_revision": 7,
        }
    ]


def test_json_mutations_reject_duplicate_keys_and_unknown_fields() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"library_save": lambda payload: seen.append(payload) or {}},
        known_paper=lambda arxiv_id: arxiv_id == "2608.02001",
    )

    duplicate = router.dispatch(
        _request(
            "POST",
            "/api/v1/library/save",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"arxiv_id":"2608.02001","arxiv_id":"2608.02001"}',
        )
    )
    unknown = router.dispatch(
        _request(
            "POST",
            "/api/v1/library/save",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"arxiv_id":"2608.02001","extra":true}',
        )
    )
    accepted = router.dispatch(
        _request(
            "POST",
            "/api/v1/library/save",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"arxiv_id":"2608.02001","version":2}',
        )
    )

    assert duplicate.status == 400
    assert _json(duplicate)["error"]["code"] == "duplicate_json_key"
    assert unknown.status == 400
    assert _json(unknown)["error"]["code"] == "invalid_request"
    assert accepted.status == 200
    assert seen == [{"arxiv_id": "2608.02001", "version": 2}]


def test_json_request_body_limit_is_enforced_before_decoding() -> None:
    from arxiv_digest.web.api import JSON_BODY_LIMIT, ApiRouter

    router = ApiRouter(token=TOKEN, host=HOST, handlers={})

    response = router.dispatch(
        _request(
            "POST",
            "/api/v1/library/save",
            origin=ORIGIN,
            content_type="application/json",
            body=b"{" + b" " * JSON_BODY_LIMIT + b"}",
        )
    )

    assert response.status == 413
    assert _json(response)["error"]["code"] == "body_too_large"


def test_nonfinite_json_and_nested_interest_fields_are_rejected() -> None:
    from arxiv_digest.web.api import ApiRouter

    router = ApiRouter(token=TOKEN, host=HOST, handlers={})
    nonfinite = router.dispatch(
        _request(
            "POST",
            "/api/v1/tabs/connect",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"tab_id":NaN}',
        )
    )
    forged_config = router.dispatch(
        _request(
            "PUT",
            "/api/v1/interests",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"expected_revision":1,"category_configs":[{"category":"cs.SE","set_spec":"cs:SE","coverage_start":"2026-07-23","path":"forged"}]}',
        )
    )

    assert nonfinite.status == 400
    assert _json(nonfinite)["error"]["code"] == "invalid_json"
    assert forged_config.status == 400


def test_unwired_or_crashing_domain_handlers_return_redacted_envelopes() -> None:
    from arxiv_digest.web.api import ApiRouter

    unavailable = ApiRouter(token=TOKEN, host=HOST, handlers={}).dispatch(
        _request("GET", "/api/v1/status")
    )
    crashing = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={
            "status": lambda payload: (_ for _ in ()).throw(
                RuntimeError(
                    "/"
                    + "/".join(
                        (
                            "redacted-root",
                            "sensitive-" + "segment",
                            "credential=" + "value",
                        )
                    )
                )
            )
        },
    ).dispatch(_request("GET", "/api/v1/status"))

    assert unavailable.status == 503
    assert _json(unavailable)["error"]["code"] == "service_unavailable"
    assert crashing.status == 500
    assert _json(crashing)["error"]["code"] == "internal_error"
    assert b"sensitive-segment" not in crashing.body


def test_maintenance_conflicts_return_structured_safe_errors() -> None:
    from arxiv_digest.maintenance import (
        MaintenanceTimeoutError,
        WorkActiveError,
    )
    from arxiv_digest.web.api import ApiRouter

    def response_for(error: Exception):
        router = ApiRouter(
            token=TOKEN,
            host=HOST,
            handlers={
                "settings_cache_clear": lambda payload: (
                    _ for _ in ()
                ).throw(error)
            },
        )
        return router.dispatch(
            _request(
                "POST",
                "/api/v1/settings/cache/clear",
                origin=ORIGIN,
            )
        )

    active = response_for(WorkActiveError("private worker detail"))
    timed_out = response_for(
        MaintenanceTimeoutError("private timeout detail")
    )

    assert active.status == 409
    assert _json(active)["error"] == {
        "code": "work_active",
        "message": "Background work is still active; cancel it and retry.",
    }
    assert timed_out.status == 503
    assert _json(timed_out)["error"] == {
        "code": "maintenance_timeout",
        "message": "Maintenance could not start before its safety timeout.",
    }
    assert b"private" not in active.body
    assert b"private" not in timed_out.body


def test_backup_binary_response_is_bounded() -> None:
    from arxiv_digest.web.api import BACKUP_BODY_LIMIT, ApiRouter, BinaryPayload

    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={
            "backup_export": lambda payload: BinaryPayload(
                b"x" * (BACKUP_BODY_LIMIT + 1)
            )
        },
    )

    response = router.dispatch(_request("GET", "/api/v1/backup/export"))

    assert response.status == 500
    assert _json(response)["error"]["code"] == "invalid_response"


def test_known_route_wrong_method_is_405_and_unknown_route_is_404() -> None:
    from arxiv_digest.web.api import ApiRouter

    router = ApiRouter(token=TOKEN, host=HOST, handlers={})

    wrong_method = router.dispatch(
        _request("GET", "/api/v1/library/save")
    )
    unknown = router.dispatch(_request("GET", "/api/v1/not-a-route"))

    assert wrong_method.status == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert _json(wrong_method)["error"]["code"] == "method_not_allowed"
    assert unknown.status == 404


def test_review_finish_all_route_dispatches_the_confirmed_projection_snapshot() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={
            "review_finish_all": lambda payload: seen.append(payload) or {
                "reviewed_count": 2,
                "through_revision": 9,
            }
        },
    )

    response = router.dispatch(
        _request(
            "POST",
            "/api/v1/review/finish",
            origin=ORIGIN,
            content_type="application/json",
            body=(
                b'{"snapshot_revision":9,"profile_revision":4,'
                b'"projection_revision":6}'
            ),
        )
    )

    assert response.status == 200
    assert seen == [
        {
            "snapshot_revision": 9,
            "profile_revision": 4,
            "projection_revision": 6,
        }
    ]
    assert _json(response)["data"] == {
        "reviewed_count": 2,
        "through_revision": 9,
    }


def test_pdf_route_keeps_nullable_save_version_separate_from_download_version() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={
            "library_pdf": lambda payload: seen.append(payload)
            or {"job_id": "download_1234"}
        },
        known_paper=lambda arxiv_id: arxiv_id == "2608.02001",
    )

    missing = router.dispatch(
        _request(
            "POST",
            "/api/v1/library/pdf",
            origin=ORIGIN,
            content_type="application/json",
            body=(
                b'{"arxiv_id":"2608.02001","version":3,'
                b'"save_first":true}'
            ),
        )
    )
    accepted = router.dispatch(
        _request(
            "POST",
            "/api/v1/library/pdf",
            origin=ORIGIN,
            content_type="application/json",
            body=(
                b'{"arxiv_id":"2608.02001","version":3,'
                b'"save_first":true,"save_version":null}'
            ),
        )
    )

    assert missing.status == 400
    assert accepted.status == 200
    assert seen == [
        {
            "arxiv_id": "2608.02001",
            "version": 3,
            "save_first": True,
            "save_version": None,
        }
    ]


def test_stale_review_projection_is_an_explicit_conflict() -> None:
    from arxiv_digest.storage.store import ReviewSnapshotConflict
    from arxiv_digest.web.api import ApiRouter

    def stale(_payload):
        raise ReviewSnapshotConflict("the active Review projection changed")

    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"review_finish": stale},
    )
    response = router.dispatch(
        _request(
            "POST",
            "/api/v1/review/date/finish",
            origin=ORIGIN,
            content_type="application/json",
            body=(
                b'{"date":"2026-08-22","snapshot_revision":9,'
                b'"profile_revision":4,"projection_revision":6}'
            ),
        )
    )

    assert response.status == 409
    assert _json(response)["error"] == {
        "code": "review_snapshot_stale",
        "message": "The active Review projection changed; reload and try again.",
    }


def test_review_date_mutations_require_all_opened_snapshot_revisions() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={
            "review_position": lambda payload: seen.append(payload) or {},
            "review_finish": lambda payload: seen.append(payload) or {},
        },
    )
    position = (
        b'{"date":"2026-08-22","snapshot_revision":9,'
        b'"profile_revision":4,"projection_revision":6,'
        b'"anchor_event_id":7}'
    )
    finish = (
        b'{"date":"2026-08-22","snapshot_revision":9,'
        b'"profile_revision":4,"projection_revision":6}'
    )

    for method, target, body in (
        ("PUT", "/api/v1/review/date/position", position),
        ("POST", "/api/v1/review/date/finish", finish),
    ):
        missing = json.loads(body)
        del missing["projection_revision"]
        rejected = router.dispatch(
            _request(
                method,
                target,
                origin=ORIGIN,
                content_type="application/json",
                body=json.dumps(missing).encode(),
            )
        )
        accepted = router.dispatch(
            _request(
                method,
                target,
                origin=ORIGIN,
                content_type="application/json",
                body=body,
            )
        )
        assert rejected.status == 400
        assert accepted.status == 200

    assert seen == [json.loads(position), json.loads(finish)]


def test_api_v1_route_surface_is_exact() -> None:
    from arxiv_digest.web.api import API_ROUTE_SURFACE

    assert API_ROUTE_SURFACE == frozenset(
        {
            ("GET", "/api/v1/status"),
            ("GET", "/api/v1/categories"),
            ("GET", "/api/v1/setup/draft"),
            ("PUT", "/api/v1/setup/draft"),
            ("POST", "/api/v1/setup/corpus"),
            ("POST", "/api/v1/setup/corpus/accept"),
            ("GET", "/api/v1/setup/jobs/{job_id}"),
            ("GET", "/api/v1/setup/candidates/papers"),
            ("GET", "/api/v1/setup/candidates/terms"),
            ("GET", "/api/v1/setup/candidates/authors"),
            ("POST", "/api/v1/setup/papers/lookup"),
            ("POST", "/api/v1/setup/folder/pick"),
            ("POST", "/api/v1/setup/folder/test"),
            ("POST", "/api/v1/setup/complete"),
            ("POST", "/api/v1/tabs/connect"),
            ("POST", "/api/v1/tabs/heartbeat"),
            ("POST", "/api/v1/tabs/disconnect"),
            ("POST", "/api/v1/sync/start"),
            ("POST", "/api/v1/sync/cancel"),
            ("GET", "/api/v1/review/summary"),
            ("POST", "/api/v1/review/finish"),
            ("GET", "/api/v1/review/calendar"),
            ("GET", "/api/v1/review/date"),
            ("PUT", "/api/v1/review/date/position"),
            ("POST", "/api/v1/review/date/finish"),
            ("GET", "/api/v1/library"),
            ("POST", "/api/v1/library/save"),
            ("POST", "/api/v1/library/remove"),
            ("POST", "/api/v1/library/pdf"),
            ("GET", "/api/v1/downloads/{job_id}"),
            ("GET", "/api/v1/interests"),
            ("PUT", "/api/v1/interests"),
            ("GET", "/api/v1/settings"),
            ("PUT", "/api/v1/settings/coverage"),
            ("POST", "/api/v1/settings/cache/clear"),
            ("POST", "/api/v1/settings/folder/pick"),
            ("POST", "/api/v1/settings/folder/test"),
            ("PUT", "/api/v1/settings/folder"),
            ("POST", "/api/v1/settings/folder/open"),
            ("GET", "/api/v1/settings/doctor"),
            ("GET", "/api/v1/settings/launcher"),
            ("POST", "/api/v1/settings/launcher/create"),
            ("POST", "/api/v1/settings/launcher/not-now"),
            ("POST", "/api/v1/settings/launcher/remove"),
            ("GET", "/api/v1/backup/export"),
            ("POST", "/api/v1/backup/inspect"),
            ("POST", "/api/v1/backup/restore"),
            ("POST", "/api/v1/application/quit"),
        }
    )


def test_routes_decode_query_path_and_json_fields_for_domain_handlers() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={
            "review_calendar": lambda payload: seen.append(
                ("calendar", payload)
            )
            or {},
            "download_status": lambda payload: seen.append(
                ("download", payload)
            )
            or {},
            "tabs_connect": lambda payload: seen.append(("tab", payload)) or {},
        },
    )

    calendar = router.dispatch(
        _request(
            "GET",
            "/api/v1/review/calendar?start=2026-08-01&end=2026-08-22",
        )
    )
    download = router.dispatch(
        _request("GET", "/api/v1/downloads/job_abcd1234")
    )
    tab = router.dispatch(
        _request(
            "POST",
            "/api/v1/tabs/connect",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"tab_id":"tab_abcd1234"}',
        )
    )

    assert (calendar.status, download.status, tab.status) == (200, 200, 200)
    assert seen == [
        ("calendar", {"start": "2026-08-01", "end": "2026-08-22"}),
        ("download", {"job_id": "job_abcd1234"}),
        ("tab", {"tab_id": "tab_abcd1234"}),
    ]


def test_library_api_keeps_paper_and_local_pdf_availability_separate() -> None:
    from arxiv_digest.library import LibraryItem
    from arxiv_digest.models import PaperMetadata
    from arxiv_digest.web.api import ApiRouter

    item = LibraryItem(
        metadata=PaperMetadata(
            arxiv_id="2608.01234",
            title="Synthetic Availability",
            authors=("Aster Example",),
            abstract="A fictional availability fixture.",
            primary_category="cs.SE",
            categories=("cs.SE",),
        ),
        saved_version=1,
        latest_version=2,
        paper_available=False,
        local_pdf_versions=(1,),
        new_version_available=True,
    )
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={
            "library": lambda payload: {
                "entries": (item,),
                "limit": 20,
                "offset": 0,
                "next_offset": None,
            }
        },
    )

    response = router.dispatch(_request("GET", "/api/v1/library"))
    entry = _json(response)["data"]["entries"][0]

    assert entry["paper_available"] is False
    assert entry["local_pdf_versions"] == [1]
    assert "unavailable" not in entry


def test_setup_draft_accepts_only_the_exact_discriminated_step_shapes() -> None:
    from arxiv_digest.web.api import ApiRouter

    accepted = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={
            "setup_draft_put": lambda payload: accepted.append(payload) or {}
        },
    )
    shapes = (
        {"step": "categories", "selections": [{"category": "cs.SE", "set_spec": "cs:SE"}]},
        {"step": "coverage", "coverage_start": "2026-07-23"},
        {"step": "seed_papers", "accepted_suggestion_ids": ["suggest_1234"], "custom_arxiv_ids": ["2608.02001"]},
        {"step": "terms", "accepted_keyword_suggestion_ids": [], "accepted_phrase_suggestion_ids": ["suggest_5678"], "custom_keywords": ["orbit"], "custom_phrases": ["lattice flow"]},
        {"step": "authors", "accepted_suggestion_ids": [], "custom_authors": ["Aster Vale"]},
        {"step": "pdf_destination", "tested_destination_token": "destination_abcd1234"},
        {"step": "review", "confirmed": True, "profile_summary_sha256": "a" * 64},
    )

    for shape in shapes:
        response = router.dispatch(
            _request(
                "PUT",
                "/api/v1/setup/draft",
                origin=ORIGIN,
                content_type="application/json",
                body=json.dumps({"revision": 3, **shape}).encode(),
            )
        )
        assert response.status == 200

    forged_transition = router.dispatch(
        _request(
            "PUT",
            "/api/v1/setup/draft",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"revision":3,"step":"candidate_corpus","corpus_hash":"bad"}',
        )
    )
    launcher_in_draft = router.dispatch(
        _request(
            "PUT",
            "/api/v1/setup/draft",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"revision":3,"step":"review","confirmed":true,"profile_summary_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","launcher_choice":"create"}',
        )
    )

    assert len(accepted) == len(shapes)
    assert forged_transition.status == 400
    assert launcher_in_draft.status == 400


def test_first_setup_categories_update_accepts_revision_zero() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"setup_draft_put": lambda payload: seen.append(payload) or {}},
    )

    response = router.dispatch(
        _request(
            "PUT",
            "/api/v1/setup/draft",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"revision":0,"step":"categories","selections":[{"category":"cs.SE","set_spec":"cs:SE"}]}',
        )
    )

    assert response.status == 200
    assert seen == [
        {
            "revision": 0,
            "step": "categories",
            "selections": [{"category": "cs.SE", "set_spec": "cs:SE"}],
        }
    ]


def test_review_page_projection_is_safe_complete_and_snapshot_relative() -> None:
    from arxiv_digest.models import (
        AnnounceType,
        EvidenceSource,
        PaperMetadata,
        ReviewEvent,
        SourceObservation,
        VersionResolution,
    )
    from arxiv_digest.ranking import RankedPaper, RankingReason, RankingTier
    from arxiv_digest.review import ReviewPage
    from arxiv_digest.web.api import ReviewPagePayload, project_review_page

    day = date(2026, 8, 22)
    observations = tuple(
        SourceObservation(
            source_key=f"catchup:{category}",
            arxiv_id="2608.02001",
            source=EvidenceSource.CATCHUP,
            category=category,
            announce_type=AnnounceType.REPLACE,
            daily_list_date=day,
            announced_version=None,
            list_position=index,
            oai_datestamp=None,
            response_sha256="a" * 64,
            observed_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        )
        for index, category in enumerate(("cs.SE", "cs.LG"), start=1)
    )
    event = ReviewEvent(
        event_id=7,
        arxiv_id="2608.02001",
        announced_version=2,
        daily_list_date=day,
        version_resolution=VersionResolution.CHRONOLOGY_MATCHED,
        observations=observations,
        queue_revision=7,
        reviewed_at=None,
    )
    paper = PaperMetadata(
        arxiv_id=event.arxiv_id,
        title="Safe synthetic title <script>",
        authors=("Aster Vale",),
        abstract="Safe synthetic abstract.",
        primary_category="cs.SE",
        categories=("cs.SE", "cs.LG"),
    )
    card = RankedPaper(
        event=event,
        paper=paper,
        tier=RankingTier.TOP,
        score=4.5,
        reasons=(RankingReason("author", "Matched Aster Vale", "authors"),),
    )
    page = ReviewPage(
        day=day,
        cards=(card,),
        snapshot_revision=7,
        profile_revision=4,
        projection_revision=9,
        anchor_event_id=7,
        previous_anchor_event_id=None,
        next_anchor_event_id=None,
        previous_date=date(2026, 8, 20),
        next_date=date(2026, 8, 25),
        next_unreviewed_date=date(2026, 8, 25),
        page_number=1,
        page_count=1,
        total_cards=1,
    )

    projected = project_review_page(
        ReviewPagePayload(
            page=page,
            last_finished_revision=5,
            latest_known_versions={event.arxiv_id: 3},
        )
    )

    assert projected["previous_date"] == "2026-08-20"
    assert projected["next_unreviewed_date"] == "2026-08-25"
    assert projected["profile_revision"] == 4
    assert projected["projection_revision"] == 9
    assert projected["cards"] == [
        {
            "event_id": 7,
            "arxiv_id": "2608.02001",
            "daily_list_date": "2026-08-22",
            "event_label": "Replacement",
            "version_resolution": "chronology_matched",
            "version_label": "Version v2 — matched by chronology",
            "support_categories": ["cs.LG", "cs.SE"],
            "resolved_announcement_version": 2,
            "latest_known_version": 3,
            "title": "Safe synthetic title <script>",
            "authors": ["Aster Vale"],
            "abstract": "Safe synthetic abstract.",
            "comments": "",
            "journal_ref": "",
            "doi": None,
            "newly_discovered": True,
            "reviewed": False,
            "tier": "top",
            "score": 4.5,
            "reasons": [
                {"kind": "author", "label": "Matched Aster Vale", "location": "authors"}
            ],
        }
    ]

    forbidden = {
        "announced_version",
        "announced_version_exact",
        "download_version",
        "effective_date",
        "date_basis",
        "date_label",
        "confidence",
        "confidence_label",
        "categories",
        "category_observations",
        "evidence",
    }
    assert forbidden.isdisjoint(projected["cards"][0])

    versionless = replace(
        event,
        announced_version=None,
        version_resolution=VersionResolution.UNCONFIRMED,
    )
    versionless_page = replace(
        page,
        cards=(replace(card, event=versionless),),
    )
    projected_versionless = project_review_page(
        ReviewPagePayload(
            page=versionless_page,
            last_finished_revision=5,
            latest_known_versions={event.arxiv_id: 3},
        )
    )
    unconfirmed = projected_versionless["cards"][0]
    assert unconfirmed["resolved_announcement_version"] is None
    assert unconfirmed["latest_known_version"] == 3
    assert unconfirmed["version_label"] == "Version not confirmed"
    assert unconfirmed["event_label"] == "Replacement"

    atom_confirmed = project_review_page(
        ReviewPagePayload(
            page=replace(
                page,
                cards=(
                    replace(
                        card,
                        event=replace(
                            event,
                            version_resolution=VersionResolution.ATOM_CONFIRMED,
                        ),
                    ),
                ),
            ),
            last_finished_revision=5,
            latest_known_versions={event.arxiv_id: 3},
        )
    )["cards"][0]
    assert atom_confirmed["version_label"] == "Announced v2 — Atom-confirmed"


def test_destination_display_path_is_home_relative_and_component_aware() -> None:
    from arxiv_digest.web.api import _destination_display_path

    home = Path("/") / "synthetic-home"

    assert _destination_display_path(home, home=home) == "~"
    assert (
        _destination_display_path(home / "Documents" / "Papers", home=home)
        == "~/Documents/Papers"
    )
    outside = Path("/") / "synthetic-home-other" / "Papers"
    assert (
        _destination_display_path(outside, home=home)
        == str(outside.resolve(strict=False))
    )


def test_setup_draft_projection_uses_a_read_only_home_relative_destination_path() -> None:
    from arxiv_digest.profile import PdfDestination
    from arxiv_digest.setup import SetupDraft, SetupStep
    from arxiv_digest.web.api import SetupDraftPayload, project_setup_draft

    seed = SimpleNamespace(
        paper=SimpleNamespace(
            arxiv_id="2205.13427",
            title="A titled seed paper",
        )
    )
    destination = Path.home() / "Private Project" / "PDFs"
    draft = SetupDraft(
        schema_version=1,
        revision=6,
        current_step=SetupStep.REVIEW,
        categories=(),
        coverage_start=date(2026, 7, 1),
        coverage_warning=None,
        corpus_hash="a" * 64,
        corpus_categories=("cs.SE",),
        corpus_complete=True,
        corpus_reduced_breadth=False,
        seed_papers=(seed,),
        keywords=("testing",),
        phrases=(),
        authors=("Aster Vale",),
        pdf_destination=PdfDestination(
            "custom",
            destination,
        ),
        destination_tested=True,
        review_confirmed=False,
        launcher_choice=None,
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )

    corpus_job = {
        "job_id": "setup_reload1234",
        "status": "running",
        "complete": False,
        "failed": False,
    }
    projected = project_setup_draft(
        SetupDraftPayload(draft, corpus_job=corpus_job)
    )
    encoded = json.dumps(projected)

    assert projected["current_step"] == "review"
    assert projected["recommended_coverage_start"] == "2026-07-23"
    assert projected["coverage_min"] == "2026-05-25"
    assert projected["coverage_max"] == "2026-08-22"
    assert projected["seed_papers"] == ["2205.13427"]
    assert projected["profile_summary"]["seed_paper_details"] == [
        {
            "arxiv_id": "2205.13427",
            "title": "A titled seed paper",
        }
    ]
    assert projected["pdf_destination"] == {"kind": "custom"}
    assert projected["profile_summary"]["pdf_destination_kind"] == "custom"
    assert (
        projected["profile_summary"]["pdf_destination_display_path"]
        == "~/Private Project/PDFs"
    )
    assert len(projected["profile_summary_sha256"]) == 64
    assert projected["corpus_job"] == corpus_job
    assert str(destination) not in encoded


def test_setup_draft_get_reports_a_resumable_partial_candidate_cache() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.setup import CategorySelection, SetupDraft, SetupStep
    from arxiv_digest.web.api import project_setup_draft

    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    draft = SetupDraft(
        schema_version=1,
        revision=2,
        current_step=SetupStep.CANDIDATE_CORPUS,
        categories=(CategorySelection("synthetic.alpha", "synthetic:alpha"),),
        coverage_start=date(2026, 7, 1),
        coverage_warning=None,
        corpus_hash=None,
        corpus_categories=(),
        corpus_complete=False,
        corpus_reduced_breadth=False,
        seed_papers=(),
        keywords=(),
        phrases=(),
        authors=(),
        pdf_destination=None,
        destination_tested=False,
        review_confirmed=False,
        launcher_choice=None,
        created_at=now,
        updated_at=now,
    )
    runtime = object.__new__(_DefaultRuntime)
    runtime.store = object()
    runtime.setup = SimpleNamespace(start=lambda: draft)
    runtime.candidates = SimpleNamespace(
        clock=lambda: now,
        cache=SimpleNamespace(
            load_shard=lambda category, set_spec, **options: SimpleNamespace()
        )
    )

    payload = runtime.handlers()["setup_draft_get"]({})

    assert project_setup_draft(payload)["corpus_can_resume"] is True


def test_default_service_graph_accepts_the_revision_zero_categories_step(
    tmp_path,
) -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.maintenance import MaintenanceBarrier
    from arxiv_digest.paths import resolve_paths
    from arxiv_digest.profile import ProfileRepository
    from arxiv_digest.web.api import API_OPERATIONS, ApiRouter
    from arxiv_digest.web.lifecycle import LifecycleController

    paths = resolve_paths(
        home=tmp_path / "home",
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "state"),
        },
    )
    paths.ensure()
    maintenance = MaintenanceBarrier()
    profiles = ProfileRepository(
        paths.profile_path,
        paths.profile_lock_path,
        maintenance=maintenance,
    )
    runtime = _DefaultRuntime(
        paths,
        profiles,
        maintenance,
        LifecycleController(),
        output=lambda message: None,
    )
    connection = runtime.open_database()
    try:
        assert runtime.sync.oai_source.client._contact_url == (
            "https://github.com/yuzhangmath/arxiv-digest"
        )
        handlers = runtime.handlers()
        assert API_OPERATIONS - set(handlers) == {
            "tabs_connect",
            "tabs_disconnect",
            "tabs_heartbeat",
        }
        router = ApiRouter(token=TOKEN, host=HOST, handlers=handlers)
        started = router.dispatch(_request("GET", "/api/v1/setup/draft"))
        assert _json(started)["data"]["revision"] == 0
        runtime._issued_category_pairs.add(("cs.SE", "cs:SE"))
        response = router.dispatch(
            _request(
                "PUT",
                "/api/v1/setup/draft",
                origin=ORIGIN,
                content_type="application/json",
                body=b'{"revision":0,"step":"categories","selections":[{"category":"cs.SE","set_spec":"cs:SE"}]}',
            )
        )
        payload = _json(response)

        assert response.status == 200
        assert payload["data"]["revision"] == 1
        assert payload["data"]["current_step"] == "initial_coverage"
        assert runtime.setup.load_draft().revision == 1
    finally:
        connection.close()


def test_oai_set_specs_map_to_categories_without_changing_exact_pairs() -> None:
    from arxiv_digest.application import _category_from_set_spec

    assert _category_from_set_spec("arXiv:math.AG") == "math.AG"
    assert _category_from_set_spec("math:math.AG") == "math.AG"
    assert _category_from_set_spec("cs:cs.AI") == "cs.AI"
    assert _category_from_set_spec("physics:physics.acc-ph") == "physics.acc-ph"
    assert _category_from_set_spec("cs:SE") == "cs.SE"
    assert _category_from_set_spec("cs:cs") == "cs"
    assert _category_from_set_spec("cs:cs:AI") == "cs.AI"
    assert _category_from_set_spec("math:math:AG") == "math.AG"
    assert _category_from_set_spec("physics:astro-ph:CO") == "astro-ph.CO"
    assert _category_from_set_spec("eess:eess:AS") == "eess.AS"


def test_category_browsing_deduplicates_current_oai_hierarchy() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.web.api import ApiRouter

    runtime = object.__new__(_DefaultRuntime)
    runtime._category_values = (
        SimpleNamespace(set_spec="cs", display_name="Computer Science"),
        SimpleNamespace(set_spec="cs:cs", display_name="Computer Science"),
        SimpleNamespace(
            set_spec="cs:cs:AI",
            display_name="Artificial Intelligence",
        ),
        SimpleNamespace(
            set_spec="physics:gr-qc",
            display_name="General Relativity and Quantum Cosmology",
        ),
        SimpleNamespace(
            set_spec="physics:gr-qc",
            display_name="General Relativity and Quantum Cosmology",
        ),
    )
    runtime._issued_category_pairs = set()

    response = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"categories": runtime._categories},
    ).dispatch(_request("GET", "/api/v1/categories?q="))
    result = _json(response)["data"]

    assert response.status == 200
    assert [
        (item["category"], item["set_spec"])
        for item in result["categories"]
    ] == [
        ("cs", "cs:cs"),
        ("cs.AI", "cs:cs:AI"),
        ("gr-qc", "physics:gr-qc"),
    ]
    assert runtime._issued_category_pairs == {
        ("cs", "cs:cs"),
        ("cs.AI", "cs:cs:AI"),
        ("gr-qc", "physics:gr-qc"),
    }


def test_category_browsing_searches_names_and_codes_case_insensitively() -> None:
    from arxiv_digest.application import _DefaultRuntime

    runtime = object.__new__(_DefaultRuntime)
    runtime._category_values = (
        SimpleNamespace(
            set_spec="arXiv:math.AG",
            display_name="Algebraic Geometry",
        ),
        SimpleNamespace(
            set_spec="arXiv:stat.ML",
            display_name="Machine Learning",
        ),
    )
    runtime._issued_category_pairs = set()

    for query in ("AG", "algebraic", "math.AG"):
        result = runtime._categories({"q": query})["categories"]
        assert [
            (item["category"], item["set_spec"])
            for item in result
        ] == [("math.AG", "arXiv:math.AG")]


def test_setup_authorizes_only_category_pairs_returned_to_the_browser() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.web.api import ApiRouter

    runtime = object.__new__(_DefaultRuntime)
    runtime._category_values = tuple(
        SimpleNamespace(
            set_spec=f"cs:ZZ{index:03d}",
            display_name=f"Synthetic category {index:03d}",
        )
        for index in range(201)
    )
    runtime._issued_category_pairs = set()
    mutations = []
    runtime.setup = SimpleNamespace(
        select_categories=lambda *args: mutations.append(args)
    )

    result = runtime._categories({})
    response = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"setup_draft_put": runtime._setup_draft_put},
    ).dispatch(
        _request(
            "PUT",
            "/api/v1/setup/draft",
            origin=ORIGIN,
            content_type="application/json",
            body=b'{"revision":0,"step":"categories","selections":[{"category":"cs.ZZ200","set_spec":"cs:ZZ200"}]}',
        )
    )

    assert len(result["categories"]) == 200
    assert ("cs.ZZ199", "cs:ZZ199") in runtime._issued_category_pairs
    assert ("cs.ZZ200", "cs:ZZ200") not in runtime._issued_category_pairs
    assert response.status == 400
    assert _json(response)["error"]["code"] == "domain_error"
    assert mutations == []


def test_failed_browser_restore_retains_validated_pending_inspection(
    tmp_path, monkeypatch
) -> None:
    import arxiv_digest.backup
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.maintenance import MaintenanceBarrier

    archive = tmp_path / "pending.zip"
    archive.write_bytes(b"validated")
    inspection = SimpleNamespace(path=archive)
    runtime = object.__new__(_DefaultRuntime)
    runtime._pending_restores_lock = threading.RLock()
    runtime._restore_cleanup_timer = None
    runtime._restore_shutdown = False
    runtime._pending_restore_reservations = {}
    runtime._pending_restores = {
        "restore_abcd1234": (time.monotonic(), inspection)
    }
    picker_choice = SimpleNamespace(kind="custom")
    runtime._picker_choices = {"picker_abcd1234": picker_choice}
    runtime.folder = SimpleNamespace(validate=lambda choice: "destination")
    runtime.paths = SimpleNamespace()
    runtime.maintenance = MaintenanceBarrier()
    runtime._candidate_state_lock = threading.RLock()
    runtime._expire_pending_restores = lambda: None
    monkeypatch.setattr(
        arxiv_digest.backup,
        "restore_backup",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("safe restore refusal")
        ),
    )

    with __import__("pytest").raises(ValueError, match="safe restore"):
        runtime._backup_restore(
            {
                "pending_restore_id": "restore_abcd1234",
                "destination_choice": "picker_abcd1234",
                "cancel_active": False,
            }
        )

    assert "restore_abcd1234" in runtime._pending_restores
    assert runtime._picker_choices["picker_abcd1234"] is picker_choice
    assert archive.read_bytes() == b"validated"


def test_browser_backup_inspection_returns_safe_renderable_summary(
    tmp_path, monkeypatch
) -> None:
    import arxiv_digest.backup
    from arxiv_digest.application import _DefaultRuntime

    runtime = object.__new__(_DefaultRuntime)
    runtime.paths = SimpleNamespace(cache_dir=tmp_path)
    runtime._pending_restores_lock = threading.RLock()
    runtime._restore_cleanup_timer = None
    runtime._restore_shutdown = False
    runtime._pending_restore_reservations = {}
    runtime._pending_restores = {}
    manifest = SimpleNamespace(
        format_version=2,
        application_version="0.2.0",
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )
    profile = SimpleNamespace(
        revision=4,
        categories=("cs.SE", "cs.LG"),
    )

    def inspect(path):
        return SimpleNamespace(
            path=path,
            manifest=manifest,
            profile=profile,
            records=(
                SimpleNamespace(record_type="saved_paper"),
                SimpleNamespace(record_type="canonical_event"),
                SimpleNamespace(record_type="canonical_event"),
            ),
        )

    monkeypatch.setattr(arxiv_digest.backup, "inspect_backup", inspect)

    result = runtime._backup_inspect({"archive": b"synthetic zip bytes"})

    assert result["summary"] == {
        "categories": 2,
        "saved_papers": 1,
        "review_events": 2,
        "profile_revision": 4,
    }
    pending = runtime._pending_restores[result["pending_restore_id"]][1]
    pending.path.unlink()


def test_browser_backup_inspection_bounds_concurrent_pending_archives(
    tmp_path, monkeypatch
) -> None:
    import arxiv_digest.application as application
    import arxiv_digest.backup
    from arxiv_digest.application import _DefaultRuntime

    runtime = object.__new__(_DefaultRuntime)
    runtime.paths = SimpleNamespace(cache_dir=tmp_path)
    runtime._pending_restores_lock = threading.RLock()
    runtime._restore_cleanup_timer = None
    runtime._restore_shutdown = False
    runtime._pending_restore_reservations = {}
    runtime._pending_restores = {}
    manifest = SimpleNamespace(
        format_version=2,
        application_version="0.2.0",
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )
    profile = SimpleNamespace(revision=1, categories=())
    ready = threading.Barrier(2)
    inspected: list[object] = []

    def inspect(path):
        inspected.append(path)
        ready.wait(timeout=2)
        return SimpleNamespace(
            path=path,
            manifest=manifest,
            profile=profile,
            records=(),
        )

    monkeypatch.setattr(arxiv_digest.backup, "inspect_backup", inspect)
    monkeypatch.setattr(application, "_MAX_PENDING_RESTORES", 2)
    outcomes: list[str] = []

    def submit() -> None:
        try:
            runtime._backup_inspect({"archive": b"validated"})
        except ValueError as error:
            outcomes.append(str(error))
        else:
            outcomes.append("accepted")

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert outcomes.count("accepted") == 2
    assert outcomes.count("too many pending restore inspections") == 4
    assert len(inspected) == 2
    assert len(runtime._pending_restores) == 2
    assert len(tuple(tmp_path.glob(".arxiv-digest-restore-*.zip"))) == 2
    runtime._close_runtime()


def test_browser_backup_inspection_bounds_total_pending_archive_bytes(
    tmp_path, monkeypatch
) -> None:
    import arxiv_digest.application as application
    import arxiv_digest.backup
    from arxiv_digest.application import _DefaultRuntime

    runtime = object.__new__(_DefaultRuntime)
    runtime.paths = SimpleNamespace(cache_dir=tmp_path)
    runtime._pending_restores_lock = threading.RLock()
    runtime._restore_cleanup_timer = None
    runtime._restore_shutdown = False
    runtime._pending_restore_reservations = {}
    runtime._pending_restores = {}
    manifest = SimpleNamespace(
        format_version=2,
        application_version="0.2.0",
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
    )
    profile = SimpleNamespace(revision=1, categories=())
    monkeypatch.setattr(
        arxiv_digest.backup,
        "inspect_backup",
        lambda path: SimpleNamespace(
            path=path,
            manifest=manifest,
            profile=profile,
            records=(),
        ),
    )
    monkeypatch.setattr(application, "_MAX_PENDING_RESTORE_BYTES", 12)

    runtime._backup_inspect({"archive": b"12345678"})
    with __import__("pytest").raises(
        ValueError, match="pending restore storage limit exceeded"
    ):
        runtime._backup_inspect({"archive": b"abcdefgh"})

    assert len(runtime._pending_restores) == 1
    assert len(tuple(tmp_path.glob(".arxiv-digest-restore-*.zip"))) == 1
    runtime._close_runtime()


def test_runtime_shutdown_removes_pending_backup_uploads(tmp_path) -> None:
    from arxiv_digest.application import _DefaultRuntime

    archive = tmp_path / ".arxiv-digest-restore-pending.zip"
    archive.write_bytes(b"sensitive portable backup")
    archive.chmod(0o600)
    timer = SimpleNamespace(cancelled=False)
    timer.cancel = lambda: setattr(timer, "cancelled", True)
    runtime = object.__new__(_DefaultRuntime)
    runtime._pending_restores_lock = threading.RLock()
    runtime._restore_shutdown = False
    runtime._pending_restore_reservations = {}
    runtime._restore_cleanup_timer = timer
    runtime._pending_restores = {
        "restore_abcd1234": (
            time.monotonic(),
            SimpleNamespace(path=archive),
        )
    }

    runtime._close_runtime()

    assert not archive.exists()
    assert runtime._pending_restores == {}
    assert timer.cancelled


def test_runtime_startup_removes_only_owned_private_restore_uploads(
    tmp_path,
) -> None:
    from arxiv_digest.application import _DefaultRuntime

    stale = tmp_path / ".arxiv-digest-restore-stale.zip"
    stale.write_bytes(b"stale sensitive backup")
    stale.chmod(0o600)
    unsafe = tmp_path / ".arxiv-digest-restore-unsafe.zip"
    unsafe.write_bytes(b"not owned by the private-temp contract")
    unsafe.chmod(0o644)
    unrelated = tmp_path / "candidate-cache.json"
    unrelated.write_text("{}", encoding="utf-8")
    runtime = object.__new__(_DefaultRuntime)
    runtime.paths = SimpleNamespace(cache_dir=tmp_path)

    runtime._cleanup_orphaned_restore_uploads()

    assert not stale.exists()
    assert unsafe.exists()
    assert unrelated.exists()


def test_interests_api_projects_bounded_current_corpus_suggestions_without_saving(
    monkeypatch,
) -> None:
    import arxiv_digest.candidates
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.web.api import ApiRouter

    profile = SimpleNamespace(
        schema_version=1,
        revision=7,
        categories=("cs.SE",),
        keywords=("testing",),
        phrases=("software quality",),
        authors=("Aster Vale",),
        seed_papers=("2608.00001", "2608.99999"),
        pdf_destination=SimpleNamespace(kind="downloads"),
    )
    state = SimpleNamespace(
        set_spec="cs:SE",
        coverage_start=date(2026, 7, 1),
    )
    candidate_paper = SimpleNamespace(
        arxiv_id="2608.00001",
        title="Existing seed",
        authors=("Aster Vale",),
    )
    stored_papers = {
        "2608.00001": candidate_paper,
        "2608.99999": SimpleNamespace(
            arxiv_id="2608.99999",
            title="Older stored seed",
            authors=("Legacy Researcher",),
        ),
    }
    suggested_paper = SimpleNamespace(
        arxiv_id="2608.00002",
        title="Suggested paper",
        authors=("Beta Researcher",),
        abstract="Suggestion abstract",
        primary_category="cs.SE",
        categories=("cs.SE",),
    )
    corpus = SimpleNamespace(
        categories=("cs.SE",),
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        documents=(SimpleNamespace(paper=candidate_paper),),
    )
    calls = []

    def build_suggestions(value, seeds, terms, authors):
        calls.append((value, seeds, terms, authors))
        return SimpleNamespace(
            papers=(
                SimpleNamespace(
                    paper=suggested_paper,
                    score=3.5,
                    reasons=("Related to a selected seed",),
                ),
            ),
            terms=(
                SimpleNamespace(
                    value="property testing",
                    kind="keyword",
                    score=2.5,
                    reasons=("Distinctive in the candidate corpus",),
                ),
                SimpleNamespace(
                    value="fault localization",
                    kind="phrase",
                    score=2.0,
                    reasons=("Recurring candidate phrase",),
                ),
            ),
            authors=(
                SimpleNamespace(
                    name="Beta Researcher",
                    score=1.5,
                    reasons=("Author of a related candidate",),
                ),
            ),
        )

    monkeypatch.setattr(
        arxiv_digest.candidates, "build_suggestions", build_suggestions
    )
    runtime = object.__new__(_DefaultRuntime)
    runtime.profiles = SimpleNamespace(load=lambda: profile)
    runtime.store = SimpleNamespace(
        category_sync_state=lambda category: state,
        article_metadata=lambda arxiv_id: stored_papers[arxiv_id],
    )
    runtime._candidate_build = SimpleNamespace(
        corpus=corpus,
        corpus_hash="b" * 64,
    )
    runtime._category_values = (
        SimpleNamespace(set_spec="cs", display_name="Computer Science"),
        SimpleNamespace(set_spec="cs:cs", display_name="Computer Science"),
        *(
            SimpleNamespace(
                set_spec=f"cs:ZZ{index:02d}",
                display_name=f"Synthetic category {index:02d}",
            )
            for index in range(35)
        ),
    )
    runtime._issued_category_pairs = set()
    runtime._suggestions = {}
    runtime._suggestion_ids = {}
    runtime.setup = SimpleNamespace(
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        publish_profile=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("GET /interests must not publish a profile")
        )
    )
    runtime.sync = SimpleNamespace(catchup_window_days=60)

    response = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"interests_get": runtime._interests_get},
    ).dispatch(_request("GET", "/api/v1/interests"))
    result = _json(response)["data"]

    assert response.status == 200
    assert result["revision"] == 7
    assert result["coverage_min"] == "2026-06-24"
    assert result["coverage_max"] == "2026-08-21"
    assert result["seed_paper_details"] == [
        {
            "arxiv_id": "2608.00001",
            "title": "Existing seed",
            "authors": ["Aster Vale"],
        },
        {
            "arxiv_id": "2608.99999",
            "title": "Older stored seed",
            "authors": ["Legacy Researcher"],
        },
    ]
    assert result["suggestions_generated_at"] == "2026-08-22T12:00:00+00:00"
    assert len(result["suggestions"]["categories"]) == 30
    assert result["suggestions"]["categories"][0] == {
        "category": "cs",
        "set_spec": "cs:cs",
        "display_name": "Computer Science",
    }
    assert result["suggestions"]["seed_papers"][0]["arxiv_id"] == "2608.00002"
    assert result["suggestions"]["keywords"][0]["value"] == "property testing"
    assert result["suggestions"]["phrases"][0]["value"] == "fault localization"
    assert result["suggestions"]["authors"][0]["name"] == "Beta Researcher"
    assert all(
        item["suggestion_id"].startswith("suggest_")
        for field in ("seed_papers", "keywords", "phrases", "authors")
        for item in result["suggestions"][field]
    )
    assert calls == [
        (
            corpus,
            ("2608.00001",),
            ("testing", "software quality"),
            ("Aster Vale",),
        )
    ]
    assert ("cs.ZZ00", "cs:ZZ00") in runtime._issued_category_pairs


def test_interests_edit_publishes_profile_v2_with_exact_active_coverage() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory

    coverage_start = date(2026, 7, 1)
    current = Profile(
        schema_version=2,
        revision=3,
        category_coverage=(ProfileCategory("cs.SE", coverage_start),),
        keywords=("testing",),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("downloads", Path("/tmp/downloads")),
    )
    state = SimpleNamespace(
        category="cs.SE",
        set_spec="cs:SE",
        coverage_start=date(2025, 1, 1),
    )
    published = []
    runtime = object.__new__(_DefaultRuntime)
    runtime.profiles = SimpleNamespace(load=lambda: current)
    runtime.store = SimpleNamespace(category_sync_state=lambda _category: state)
    runtime.candidates = SimpleNamespace()
    runtime.known_paper = lambda _arxiv_id: False
    runtime._issued_category_pairs = set()
    runtime.setup = SimpleNamespace(
        publish_profile=lambda profile, configs, **kwargs: published.append(
            (profile, configs, kwargs)
        )
    )
    runtime._profile_value = lambda profile, _store: {
        "revision": profile.revision
    }

    result = runtime._interests_put(
        {"expected_revision": 3, "keywords": ["verification"]}
    )

    assert result == {"revision": 4}
    assert len(published) == 1
    profile, configs, options = published[0]
    assert profile.schema_version == 2
    assert profile.category_coverage == (
        ProfileCategory("cs.SE", coverage_start),
    )
    assert tuple(
        (config.category, config.coverage_start) for config in configs
    ) == (("cs.SE", coverage_start),)
    assert options == {"seed_papers": (), "expected_revision": 3}


def test_profile_projection_reports_authoritative_profile_coverage() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory

    profile = Profile(
        schema_version=2,
        revision=3,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 7, 1)),
        ),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("downloads", Path("/tmp/downloads")),
    )
    state = SimpleNamespace(
        set_spec="cs:SE",
        coverage_start=date(2025, 1, 1),
    )
    store = SimpleNamespace(category_sync_state=lambda _category: state)

    projected = _DefaultRuntime._profile_value(profile, store)

    assert projected["categories"] == [
        {
            "category": "cs.SE",
            "set_spec": "cs:SE",
            "coverage_start": "2026-07-01",
        }
    ]


def test_interests_readd_requires_fresh_coverage_when_sync_state_is_retained(
) -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory

    current = Profile(
        schema_version=2,
        revision=4,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 7, 1)),
        ),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("downloads", Path("/tmp/downloads")),
    )
    states = {
        "cs.SE": SimpleNamespace(
            set_spec="cs:SE", coverage_start=date(2026, 7, 1)
        ),
        "cs.LG": SimpleNamespace(
            set_spec="cs:LG", coverage_start=date(2025, 1, 1)
        ),
    }
    published = []
    runtime = object.__new__(_DefaultRuntime)
    runtime.profiles = SimpleNamespace(load=lambda: current)
    runtime.store = SimpleNamespace(
        category_sync_state=lambda category: states[category]
    )
    runtime.candidates = SimpleNamespace()
    runtime.known_paper = lambda _arxiv_id: False
    runtime._issued_category_pairs = {("cs.LG", "cs:LG")}
    runtime.setup = SimpleNamespace(
        publish_profile=lambda *args, **kwargs: published.append((args, kwargs))
    )
    runtime._profile_value = lambda profile, _store: {
        "revision": profile.revision
    }

    with __import__("pytest").raises(
        ValueError, match="exact coverage configuration"
    ):
        runtime._interests_put(
            {
                "expected_revision": 4,
                "categories": [
                    {"category": "cs.SE", "set_spec": "cs:SE"},
                    {"category": "cs.LG", "set_spec": "cs:LG"},
                ],
            }
        )

    assert published == []


def test_interests_readd_rejects_coverage_outside_supported_window() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory

    current = Profile(
        schema_version=2,
        revision=4,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 7, 1)),
        ),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("downloads", Path("/tmp/downloads")),
    )
    published = []
    runtime = object.__new__(_DefaultRuntime)
    runtime.profiles = SimpleNamespace(load=lambda: current)
    runtime.store = SimpleNamespace(
        category_sync_state=lambda category: SimpleNamespace(
            set_spec="cs:LG" if category == "cs.LG" else "cs:SE",
            coverage_start=date(2025, 1, 1),
        )
    )
    runtime.candidates = SimpleNamespace()
    runtime.known_paper = lambda _arxiv_id: False
    runtime._issued_category_pairs = {("cs.LG", "cs:LG")}
    runtime.setup = SimpleNamespace(
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        publish_profile=lambda *args, **kwargs: published.append((args, kwargs)),
    )
    runtime.sync = SimpleNamespace(catchup_window_days=90)

    with __import__("pytest").raises(ValueError, match="recovery window"):
        runtime._interests_put(
            {
                "expected_revision": 4,
                "categories": [
                    {"category": "cs.SE", "set_spec": "cs:SE"},
                    {"category": "cs.LG", "set_spec": "cs:LG"},
                ],
                "category_configs": [
                    {
                        "category": "cs.LG",
                        "set_spec": "cs:LG",
                        "coverage_start": "2026-05-24",
                    }
                ],
            }
        )

    assert published == []


def test_interests_readd_uses_fresh_coverage_not_the_retained_boundary() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.profile import PdfDestination, Profile, ProfileCategory

    current = Profile(
        schema_version=2,
        revision=4,
        category_coverage=(
            ProfileCategory("cs.SE", date(2026, 7, 1)),
        ),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("downloads", Path("/tmp/downloads")),
    )
    states = {
        "cs.SE": SimpleNamespace(
            set_spec="cs:SE", coverage_start=date(2026, 7, 1)
        ),
        "cs.LG": SimpleNamespace(
            set_spec="cs:LG", coverage_start=date(2025, 1, 1)
        ),
    }
    published = []
    sync_starts = []
    runtime = object.__new__(_DefaultRuntime)
    runtime.profiles = SimpleNamespace(load=lambda: current)
    runtime.store = SimpleNamespace(
        category_sync_state=lambda category: states[category]
    )
    runtime.candidates = SimpleNamespace()
    runtime.known_paper = lambda _arxiv_id: False
    runtime._issued_category_pairs = {("cs.LG", "cs:LG")}
    runtime.setup = SimpleNamespace(
        publish_profile=lambda profile, configs, **kwargs: published.append(
            (profile, configs, kwargs)
        )
    )
    runtime._profile_value = lambda profile, _store: {
        "revision": profile.revision
    }
    runtime._start_sync_job = lambda payload: sync_starts.append(payload) or {
        "job_id": "sync_existing"
    }

    result = runtime._interests_put(
        {
            "expected_revision": 4,
            "categories": [
                {"category": "cs.SE", "set_spec": "cs:SE"},
                {"category": "cs.LG", "set_spec": "cs:LG"},
            ],
            "category_configs": [
                {
                    "category": "cs.LG",
                    "set_spec": "cs:LG",
                    "coverage_start": "2026-08-01",
                }
            ],
        }
    )

    assert result == {"revision": 5}
    profile, configs, options = published[0]
    assert profile.category_coverage == (
        ProfileCategory("cs.SE", date(2026, 7, 1)),
        ProfileCategory("cs.LG", date(2026, 8, 1)),
    )
    assert tuple(
        (config.category, config.oai_set_spec, config.coverage_start)
        for config in configs
    ) == (
        ("cs.SE", "cs:SE", date(2026, 7, 1)),
        ("cs.LG", "cs:LG", date(2026, 8, 1)),
    )
    assert options == {"seed_papers": (), "expected_revision": 4}
    assert sync_starts == [{"follow_up": True}]


def test_fresh_interests_suggestions_resume_the_current_profile_candidate_corpus(
    monkeypatch,
) -> None:
    import arxiv_digest.candidates
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.maintenance import MaintenanceBarrier
    from arxiv_digest.web.api import ApiRouter

    profile = SimpleNamespace(
        schema_version=2,
        revision=8,
        categories=("cs.SE", "cs.LG"),
        category_coverage=(
            SimpleNamespace(
                category="cs.SE", coverage_start=date(2026, 7, 1)
            ),
            SimpleNamespace(
                category="cs.LG", coverage_start=date(2026, 8, 1)
            ),
        ),
        keywords=("testing",),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=SimpleNamespace(kind="downloads"),
    )
    states = {
        "cs.SE": SimpleNamespace(
            category="cs.SE",
            set_spec="cs:SE",
            coverage_start=date(2025, 7, 1),
        ),
        "cs.LG": SimpleNamespace(
            category="cs.LG",
            set_spec="cs:LG",
            coverage_start=date(2025, 8, 1),
        ),
    }
    corpus = SimpleNamespace(
        categories=("cs.SE", "cs.LG"),
        created_at=datetime(2026, 8, 22, 13, tzinfo=timezone.utc),
        documents=(),
    )
    rebuilt = SimpleNamespace(corpus=corpus, corpus_hash="c" * 64)
    resume_calls = []

    def resume(configs):
        resume_calls.append(tuple(configs))
        return rebuilt

    monkeypatch.setattr(
        arxiv_digest.candidates,
        "build_suggestions",
        lambda *_args: SimpleNamespace(papers=(), terms=(), authors=()),
    )
    runtime = object.__new__(_DefaultRuntime)
    runtime.profiles = SimpleNamespace(load=lambda: profile)
    runtime.store = SimpleNamespace(
        category_sync_state=lambda category: states[category]
    )
    runtime.candidates = SimpleNamespace(resume=resume)
    runtime.maintenance = MaintenanceBarrier()
    runtime._candidate_build = SimpleNamespace(
        corpus=SimpleNamespace(categories=("cs.SE",)),
        corpus_hash="b" * 64,
    )
    runtime._category_values = ()
    runtime._issued_category_pairs = set()
    runtime._suggestions = {}
    runtime._suggestion_ids = {}

    response = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"interests_get": runtime._interests_get},
    ).dispatch(_request("GET", "/api/v1/interests?refresh=1"))
    result = _json(response)["data"]

    assert response.status == 200
    assert result["suggestions_generated_at"] == "2026-08-22T13:00:00+00:00"
    assert len(resume_calls) == 1
    assert [
        (config.category, config.oai_set_spec, config.coverage_start)
        for config in resume_calls[0]
    ] == [
        ("cs.SE", "cs:SE", date(2026, 7, 1)),
        ("cs.LG", "cs:LG", date(2026, 8, 1)),
    ]
    assert runtime._candidate_build is rebuilt


def test_setup_suggestions_rerank_from_every_current_draft_selection(
    monkeypatch,
) -> None:
    import arxiv_digest.candidates
    from arxiv_digest.application import _DefaultRuntime

    seed = SimpleNamespace(paper=SimpleNamespace(arxiv_id="2608.00001"))
    corpus = SimpleNamespace(
        documents=(seed,), categories=("synthetic.alpha",)
    )
    accepted = {
        "corpus_hash": "a" * 64,
        "categories": (SimpleNamespace(category="synthetic.alpha"),),
    }
    drafts = iter(
        (
                SimpleNamespace(
                    **accepted,
                    seed_papers=(seed,),
                keywords=(),
                phrases=(),
                authors=(),
            ),
                SimpleNamespace(
                    **accepted,
                    seed_papers=(seed,),
                keywords=("testing",),
                phrases=("software quality",),
                authors=(),
            ),
                SimpleNamespace(
                    **accepted,
                    seed_papers=(seed,),
                keywords=("testing",),
                phrases=("software quality",),
                authors=("Aster Vale",),
            ),
        )
    )
    calls: list[tuple[object, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = []

    def build_suggestions(value, seeds, terms, authors):
        calls.append((value, seeds, terms, authors))
        return object()

    monkeypatch.setattr(
        arxiv_digest.candidates, "build_suggestions", build_suggestions
    )
    runtime = object.__new__(_DefaultRuntime)
    runtime._candidate_build = SimpleNamespace(
        corpus=corpus,
        corpus_hash="a" * 64,
    )
    runtime.setup = SimpleNamespace(load_draft=lambda: next(drafts))

    runtime._candidate_suggestions()
    runtime._candidate_suggestions()
    runtime._candidate_suggestions()

    assert calls == [
        (corpus, ("2608.00001",), (), ()),
        (
            corpus,
            ("2608.00001",),
            ("testing", "software quality"),
            (),
        ),
        (
            corpus,
            ("2608.00001",),
            ("testing", "software quality"),
            ("Aster Vale",),
        ),
    ]


def test_setup_candidate_endpoint_hydrates_the_accepted_cache_after_restart(
    tmp_path,
) -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.candidates import (
        CandidateCache,
        CandidateCategoryShard,
        CandidateCorpusBuilder,
        CandidateDocument,
        candidate_corpus_hash,
        derive_candidate_corpus,
    )
    from arxiv_digest.models import PaperMetadata, PaperVersion

    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    document = CandidateDocument(
        paper=PaperMetadata(
            arxiv_id="2608.00001",
            title="Restart-safe candidate",
            authors=("Aster Vale",),
            abstract="Persisted candidate corpus evidence.",
            primary_category="synthetic.alpha",
            categories=("synthetic.alpha",),
        ),
        versions=(PaperVersion(1, now),),
        eligible_categories=("synthetic.alpha",),
        evidence_dates=(date(2026, 8, 22),),
    )
    shard = CandidateCategoryShard(
        schema_version=1,
        category="synthetic.alpha",
        set_spec="synthetic:alpha",
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        created_at=now,
        source_hashes=("a" * 64,),
        documents=(document,),
        completed_strata=tuple(range(18)),
        continuation_by_stratum=(),
        pages_fetched=18,
        exhausted=True,
    )
    cache = CandidateCache(tmp_path, clock=lambda: now)
    cache.save_shard(shard)
    corpus = derive_candidate_corpus((shard,), categories=("synthetic.alpha",))
    draft = SimpleNamespace(
        revision=4,
        categories=(
            SimpleNamespace(
                category="synthetic.alpha",
                set_spec="synthetic:alpha",
            ),
        ),
        coverage_start=date(2026, 7, 1),
        corpus_hash=candidate_corpus_hash(corpus),
        seed_papers=(),
        keywords=(),
        phrases=(),
        authors=(),
    )
    runtime = object.__new__(_DefaultRuntime)
    runtime.setup = SimpleNamespace(load_draft=lambda: draft)
    runtime.candidates = CandidateCorpusBuilder(object(), cache, clock=lambda: now)
    runtime._candidate_build = None
    runtime._suggestions = {}
    runtime._suggestion_ids = {}

    result = runtime._candidate_papers({"q": "restart-safe", "offset": 0})

    assert result["items"][0]["arxiv_id"] == "2608.00001"
    assert result["items"][0]["suggestion_id"].startswith("suggest_")
    assert runtime._candidate_build.corpus_hash == draft.corpus_hash

    runtime._candidate_build = None
    terms = runtime._candidate_terms({})
    assert terms["keywords"] or terms["phrases"]

    runtime._candidate_build = None
    authors = runtime._candidate_authors({"q": "aster"})
    assert authors["items"][0]["name"] == "Aster Vale"


def test_runtime_projects_durable_mailing_evidence_into_candidate_documents() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.models import CategoryConfig, PaperMetadata, PaperVersion

    metadata = PaperMetadata(
        arxiv_id="2608.00001",
        title="Stored candidate",
        authors=("Aster Vale",),
        abstract="A stored abstract.",
        primary_category="cs.SE",
        categories=("cs.SE", "cs.LG"),
    )
    versions = (
        PaperVersion(
            number=1,
            submitted_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        ),
    )
    evidence_calls = []

    class FakeStore:
        def candidate_mailing_evidence(self, category, start, end):
            evidence_calls.append((category, start, end))
            return {
                "cs.SE": (
                    ("2608.00001", date(2026, 8, 20)),
                    ("2608.00001", date(2026, 8, 21)),
                ),
                "cs.LG": (("2608.00001", date(2026, 8, 19)),),
            }[category]

        def article_metadata(self, arxiv_id):
            assert arxiv_id == "2608.00001"
            return metadata

        def article_versions(self, arxiv_id):
            assert arxiv_id == "2608.00001"
            return versions

    runtime = object.__new__(_DefaultRuntime)
    runtime.store = FakeStore()
    configs = (
        CategoryConfig("cs.SE", "cs:SE", date(2026, 7, 1)),
        CategoryConfig("cs.LG", "cs:LG", date(2026, 7, 1)),
    )

    documents = runtime._local_candidate_documents(
        configs,
        date(2026, 5, 25),
        date(2026, 8, 22),
    )

    assert evidence_calls == [
        ("cs.SE", date(2026, 5, 25), date(2026, 8, 22)),
        ("cs.LG", date(2026, 5, 25), date(2026, 8, 22)),
    ]
    assert len(documents) == 2
    assert documents[0].paper is metadata
    assert documents[0].versions == versions
    assert documents[0].eligible_categories == ("cs.SE",)
    assert documents[0].evidence_dates == (
        date(2026, 8, 20),
        date(2026, 8, 21),
    )
    assert documents[1].eligible_categories == ("cs.LG",)
