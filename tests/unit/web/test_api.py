from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


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


@pytest.mark.parametrize("operation", ["inspect", "restore"])
def test_backup_import_is_available_only_through_the_cli(operation: str) -> None:
    from arxiv_digest.web.api import ApiRouter

    called = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={f"backup_{operation}": lambda payload: called.append(payload) or {}},
    )
    response = router.dispatch(_request(
        "POST", f"/api/v1/backup/{operation}", origin=ORIGIN,
        body=b"synthetic archive" if operation == "inspect" else b"{}",
        content_type="application/zip" if operation == "inspect" else "application/json",
    ))

    assert response.status == 404
    assert called == []


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


def test_sync_start_accepts_scoped_retry_modes_and_requires_booleans() -> None:
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

    for mode in ("retry_failed_dates", "retry_missing_abstracts"):
        response = router.dispatch(
            _request(
                "POST",
                "/api/v1/sync/start",
                origin=ORIGIN,
                content_type="application/json",
                body=json.dumps({mode: True}).encode(),
            )
        )
        assert response.status == 200
        for invalid in ("true", 1, None):
            rejected = router.dispatch(
                _request(
                    "POST",
                    "/api/v1/sync/start",
                    origin=ORIGIN,
                    content_type="application/json",
                    body=json.dumps({mode: invalid}).encode(),
                )
            )
            assert rejected.status == 400

    assert seen == [{"retry_failed_dates": True}, {"retry_missing_abstracts": True}]


def test_sync_start_accepts_one_failed_date_and_validates_its_fields() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(token=TOKEN, host=HOST, handlers={
        "sync_start": lambda payload: seen.append(payload) or {"job_id": "sync_fixture"},
    })
    payload = {
        "retry_failed_dates": True, "retry_category": "cs.CL",
        "retry_date": "2026-08-20",
    }
    def dispatch(value):
        return router.dispatch(_request(
            "POST", "/api/v1/sync/start", origin=ORIGIN,
            content_type="application/json", body=json.dumps(value).encode(),
        ))

    assert dispatch(payload).status == 200
    for invalid in ("2026-02-30", "today", 1, None):
        assert dispatch({**payload, "retry_date": invalid}).status == 400
    for invalid in ("", 1, None, "x" * 65):
        assert dispatch({**payload, "retry_category": invalid}).status == 400
    assert seen == [payload]


def test_sync_start_accepts_date_scoped_optional_abstract_retry() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(token=TOKEN, host=HOST, handlers={
        "sync_start": lambda payload: seen.append(payload) or {"job_id": "sync_fixture"},
    })
    payload = {"retry_missing_abstracts": True, "retry_date": "2026-08-20"}
    response = router.dispatch(_request(
        "POST", "/api/v1/sync/start", origin=ORIGIN,
        content_type="application/json", body=json.dumps(payload).encode(),
    ))
    assert response.status == 200
    assert seen == [payload]


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


@pytest.mark.parametrize("through_date", (None, "2026-08-19"))
def test_review_finish_all_route_dispatches_the_confirmed_projection_snapshot(
    through_date,
) -> None:
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

    payload = {
        "snapshot_revision": 9,
        "profile_revision": 4,
        "projection_revision": 6,
    }
    if through_date is not None:
        payload["through_date"] = through_date
    response = router.dispatch(
        _request(
            "POST",
            "/api/v1/review/finish",
            origin=ORIGIN,
            content_type="application/json",
            body=json.dumps(payload).encode(),
        )
    )

    assert response.status == 200
    assert seen == [payload]
    assert _json(response)["data"] == {
        "reviewed_count": 2,
        "through_revision": 9,
    }


def test_finish_all_route_preserves_confirmation_cutoff_through_runtime() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.web.api import ApiRouter

    seen = []
    runtime = object.__new__(_DefaultRuntime)
    runtime.review = SimpleNamespace(finish_all=lambda **kwargs: seen.append(kwargs))
    router = ApiRouter(
        token=TOKEN, host=HOST,
        handlers={"review_finish_all": runtime._review_finish_all},
    )
    response = router.dispatch(_request(
        "POST", "/api/v1/review/finish", origin=ORIGIN,
        content_type="application/json",
        body=json.dumps({
            "snapshot_revision": 9, "profile_revision": 4,
            "projection_revision": 6, "through_date": "2026-08-19",
        }).encode(),
    ))

    assert response.status == 200
    assert len(seen) == 1
    assert seen[0]["through_date"] == date(2026, 8, 19)
    assert seen[0]["through_revision"] == 9


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
            ("GET", "/api/v1/update"),
            ("GET", "/api/v1/categories"),
            ("GET", "/api/v1/setup/draft"),
            ("PUT", "/api/v1/setup/draft"),
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


def test_review_date_from_start_query_is_a_strict_boolean() -> None:
    from arxiv_digest.web.api import ApiRouter

    seen = []
    router = ApiRouter(
        token=TOKEN,
        host=HOST,
        handlers={"review_date": lambda payload: seen.append(payload) or {}},
    )

    enabled = router.dispatch(
        _request(
            "GET",
            "/api/v1/review/date?date=2026-08-22&from_start=true",
        )
    )
    disabled = router.dispatch(
        _request(
            "GET",
            "/api/v1/review/date?date=2026-08-22&from_start=false",
        )
    )
    invalid = router.dispatch(
        _request(
            "GET",
            "/api/v1/review/date?date=2026-08-22&from_start=1",
        )
    )

    assert (enabled.status, disabled.status, invalid.status) == (200, 200, 400)
    assert seen == [
        {"date": "2026-08-22", "from_start": True},
        {"date": "2026-08-22", "from_start": False},
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
    from arxiv_digest.ranking import (
        RankedPaper,
        RankingReason,
        RankingReference,
        RankingTier,
    )
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
        categories=("cs.SE", "cs.LG", "stat.ML"),
    )
    card = RankedPaper(
        event=event,
        paper=paper,
        tier=RankingTier.TOP,
        score=4.5,
        reasons=(
            RankingReason("author", "Matched Aster Vale", "authors"),
            RankingReason(
                "seed_similarity",
                "Related to selected seed paper",
                None,
                RankingReference("2501.00001", "Named seed paper"),
            ),
        ),
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
        page_number=1,
        page_count=1,
        total_cards=1,
        abstracts_ready=1,
        missing_abstracts=0,
    )

    projected = project_review_page(
        ReviewPagePayload(
            page=page,
            last_finished_revision=5,
            latest_known_versions={event.arxiv_id: 3},
        )
    )

    assert projected["previous_date"] == "2026-08-20"
    assert "next_unreviewed_date" not in projected
    assert projected["profile_revision"] == 4
    assert projected["projection_revision"] == 9
    assert projected["abstracts_ready"] == 1
    assert projected["missing_abstracts"] == 0
    assert projected["cards"] == [
        {
            "event_id": 7,
            "arxiv_id": "2608.02001",
            "daily_list_date": "2026-08-22",
            "event_label": "Replacement",
            "version_resolution": "chronology_matched",
            "version_label": "Version v2",
            "support_categories": ["cs.LG", "cs.SE"],
            "subjects": ["cs.SE", "cs.LG", "stat.ML"],
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
                {
                    "kind": "author",
                    "label": "Matched Aster Vale",
                    "location": "authors",
                },
                {
                    "kind": "seed_similarity",
                    "label": "Related to selected seed paper",
                    "location": None,
                    "reference": {
                        "arxiv_id": "2501.00001",
                        "title": "Named seed paper",
                    },
                },
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
    assert atom_confirmed["version_label"] == "Version v2"

    first_version_new_event = replace(
        event,
        announced_version=1,
        version_resolution=VersionResolution.ATOM_CONFIRMED,
        observations=tuple(
            replace(observation, announce_type=AnnounceType.NEW)
            for observation in observations
        ),
    )
    first_version_new = project_review_page(
        ReviewPagePayload(
            page=replace(
                page,
                cards=(replace(card, event=first_version_new_event),),
            ),
            last_finished_revision=5,
            latest_known_versions={event.arxiv_id: 1},
        )
    )["cards"][0]
    assert first_version_new["version_label"] == ""
    assert "event_label" not in first_version_new

    cross_list_event = replace(
        first_version_new_event,
        observations=tuple(
            replace(observation, announce_type=AnnounceType.CROSS)
            for observation in observations
        ),
    )
    cross_list = project_review_page(
        ReviewPagePayload(
            page=replace(
                page,
                cards=(replace(card, event=cross_list_event),),
            ),
            last_finished_revision=5,
            latest_known_versions={event.arxiv_id: 1},
        )
    )["cards"][0]
    assert cross_list["version_label"] == ""
    assert cross_list["event_label"] == "Cross-list"


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
            arxiv_id="2608.49003",
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

    projected = project_setup_draft(SetupDraftPayload(draft))
    encoded = json.dumps(projected)

    assert projected["current_step"] == "review"
    assert projected["recommended_coverage_start"] == "2026-07-23"
    assert projected["coverage_min"] == "2026-05-25"
    assert projected["coverage_max"] == "2026-08-22"
    assert projected["seed_papers"] == ["2608.49003"]
    assert projected["profile_summary"]["seed_paper_details"] == [
        {
            "arxiv_id": "2608.49003",
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
    assert "corpus_job" not in projected
    assert str(destination) not in encoded


def test_setup_draft_get_does_not_require_or_load_candidate_data() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.setup import CategorySelection, SetupDraft, SetupStep
    from arxiv_digest.web.api import project_setup_draft

    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    draft = SetupDraft(
        schema_version=1,
        revision=2,
        current_step=SetupStep.PDF_DESTINATION,
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

    assert project_setup_draft(payload)["current_step"] == "pdf_destination"
    assert "corpus_can_resume" not in project_setup_draft(payload)


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


def test_application_quit_handler_adapts_shutdown_acceptance_to_api_payload() -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.web.lifecycle import LifecycleController

    runtime = object.__new__(_DefaultRuntime)
    runtime.store = object()
    runtime.lifecycle = LifecycleController()

    assert runtime.handlers()["application_quit"]({}) == {"quitting": True}
    assert runtime.lifecycle.is_closing


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


@pytest.mark.parametrize("has_previous_build", [False, True])
def test_fresh_interests_suggestions_resume_the_current_profile_candidate_corpus(
    monkeypatch, has_previous_build,
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
    ) if has_previous_build else None
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
