from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256

from arxiv_digest.models import (
    CategoryConfig,
    OaiArticle,
    PaperMetadata,
    PaperVersion,
)
from arxiv_digest.sources.oai import OaiPage, OaiProtocolError


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
CATEGORIES = (
    CategoryConfig("synthetic.alpha", "synthetic:alpha", date(2010, 1, 1)),
    CategoryConfig("synthetic.beta", "synthetic:beta", date(2010, 1, 1)),
    CategoryConfig("synthetic.gamma", "synthetic:gamma", date(2010, 1, 1)),
)


def _article(
    arxiv_id: str,
    category: str,
    *,
    submitted: date,
    oai_modified: date | None = None,
    authors: tuple[str, ...] = ("Fixture Author",),
    abstract: str = "latent orchard coupling recurs in this synthetic corpus",
) -> OaiArticle:
    return OaiArticle(
        oai_identifier=f"oai:arXiv.org:{arxiv_id}",
        oai_datestamp=oai_modified or submitted,
        set_specs=(f"synthetic:{category.rsplit('.', 1)[-1]}",),
        metadata=PaperMetadata(
            arxiv_id=arxiv_id,
            title=f"Fixture constellation {arxiv_id}",
            authors=authors,
            abstract=abstract,
            primary_category=category,
            categories=(category,),
        ),
        versions=(
            PaperVersion(
                number=1,
                submitted_at=datetime.combine(
                    submitted,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ),
            ),
        ),
    )


class NinetyDaySource:
    def __init__(self) -> None:
        self.first_calls: list[tuple[str, date, date]] = []
        self.next_calls: list[str] = []

    def sample_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        *,
        cancelled=None,
    ) -> OaiPage:
        self.first_calls.append((set_spec, from_date, until_date))
        category_index = [config.oai_set_spec for config in CATEGORIES].index(set_spec)
        stratum = (from_date - date(2026, 5, 25)).days // 5
        category = CATEGORIES[category_index].category
        serial = 1_000 + category_index * 1_000 + stratum * 2
        records = [
            _article(f"2608.{serial:05d}", category, submitted=from_date),
            _article(f"2608.{serial + 1:05d}", category, submitted=until_date),
            # OAI modification time can be current while every actual version
            # is old; the builder must reject this locally.
            _article(
                "2501.00001",
                category,
                submitted=date(2020, 1, 1),
                oai_modified=from_date,
            ),
        ]
        if stratum == 0 and category_index in (0, 1):
            records.append(
                _article(
                    "2608.09999",
                    category,
                    submitted=from_date,
                    authors=("Cross Category Author",),
                )
            )
        digest = sha256(f"{set_spec}:{from_date}:{until_date}".encode()).hexdigest()
        return OaiPage(NOW, tuple(records), None, digest)

    def next_page(self, token: str, *, cancelled=None) -> OaiPage:
        self.next_calls.append(token)
        raise AssertionError("this fixture has no continuation pages")


class TwoPageSource:
    def __init__(self) -> None:
        self.first_calls: list[tuple[str, date, date]] = []
        self.next_calls: list[str] = []
        self.pending: dict[str, tuple[str, date, int]] = {}

    def sample_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        *,
        cancelled=None,
    ) -> OaiPage:
        self.first_calls.append((set_spec, from_date, until_date))
        category_index = [config.oai_set_spec for config in CATEGORIES].index(set_spec)
        category = CATEGORIES[category_index].category
        stratum = (from_date - date(2026, 5, 25)).days // 5
        serial = 10_000 + category_index * 1_000 + stratum * 2
        token = f"opaque:{category_index}:{stratum}:page-2"
        self.pending[token] = (category, until_date, serial + 1)
        digest = sha256(f"first:{token}".encode()).hexdigest()
        return OaiPage(
            NOW,
            (_article(f"2608.{serial:05d}", category, submitted=from_date),),
            token,
            digest,
        )

    def next_page(self, token: str, *, cancelled=None) -> OaiPage:
        self.next_calls.append(token)
        category, submitted, serial = self.pending[token]
        digest = sha256(f"next:{token}".encode()).hexdigest()
        return OaiPage(
            NOW,
            (_article(f"2608.{serial:05d}", category, submitted=submitted),),
            None,
            digest,
        )


class RestartableCandidateSource:
    exact_token = "opaque:synthetic-alpha:stratum-0:page-2"

    def __init__(self) -> None:
        self.first_calls: list[tuple[str, date, date]] = []
        self.next_calls: list[str] = []

    def sample_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        *,
        cancelled=None,
    ) -> OaiPage:
        assert set_spec == "synthetic:alpha"
        self.first_calls.append((set_spec, from_date, until_date))
        stratum = (from_date - date(2026, 5, 25)).days // 5
        token = self.exact_token if stratum == 0 else None
        digest = sha256(f"restart-first:{stratum}".encode()).hexdigest()
        return OaiPage(
            NOW,
            (
                _article(
                    f"2608.{41_000 + stratum:05d}",
                    "synthetic.alpha",
                    submitted=from_date,
                ),
            ),
            token,
            digest,
        )

    def next_page(self, token: str, *, cancelled=None) -> OaiPage:
        self.next_calls.append(token)
        assert token == self.exact_token
        return OaiPage(
            NOW,
            (
                _article(
                    "2608.41999",
                    "synthetic.alpha",
                    submitted=date(2026, 5, 29),
                ),
            ),
            None,
            sha256(b"restart-second:0").hexdigest(),
        )


class ExpiringTokenSource:
    def __init__(self) -> None:
        self.first_count: dict[int, int] = {}
        self.expired_once = False

    def sample_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        *,
        cancelled=None,
    ) -> OaiPage:
        assert set_spec == "synthetic:alpha"
        stratum = (from_date - date(2026, 5, 25)).days // 5
        count = self.first_count.get(stratum, 0) + 1
        self.first_count[stratum] = count
        token = f"opaque:{stratum}:attempt-{count}"
        digest = sha256(f"expired-first:{token}".encode()).hexdigest()
        return OaiPage(
            NOW,
            (
                _article(
                    f"2608.{30_000 + stratum:05d}",
                    "synthetic.alpha",
                    submitted=from_date,
                ),
            ),
            token,
            digest,
        )

    def next_page(self, token: str, *, cancelled=None) -> OaiPage:
        stratum = int(token.split(":")[1])
        if stratum == 0 and not self.expired_once:
            self.expired_once = True
            raise OaiProtocolError("badResumptionToken", "fixture token expired")
        digest = sha256(f"expired-next:{token}".encode()).hexdigest()
        return OaiPage(NOW, (), None, digest)


class ThreePageSource:
    def __init__(self) -> None:
        self.next_calls: list[str] = []

    def sample_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        *,
        cancelled=None,
    ) -> OaiPage:
        assert set_spec == "synthetic:alpha"
        stratum = (from_date - date(2026, 5, 25)).days // 5
        token = f"opaque:{stratum}:page-2"
        return OaiPage(
            NOW,
            (
                _article(
                    f"2608.{40_000 + stratum:05d}",
                    "synthetic.alpha",
                    submitted=from_date,
                ),
            ),
            token,
            sha256(f"three:first:{stratum}".encode()).hexdigest(),
        )

    def next_page(self, token: str, *, cancelled=None) -> OaiPage:
        self.next_calls.append(token)
        _, raw_stratum, raw_page = token.split(":")
        stratum = int(raw_stratum)
        page_number = int(raw_page.removeprefix("page-"))
        next_token = (
            None
            if page_number == 3
            else f"opaque:{stratum}:page-{page_number + 1}"
        )
        return OaiPage(
            NOW,
            (),
            next_token,
            sha256(f"three:next:{token}".encode()).hexdigest(),
        )


class EmptySource:
    def __init__(self) -> None:
        self.first_calls: list[str] = []

    def sample_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        *,
        cancelled=None,
    ) -> OaiPage:
        self.first_calls.append(set_spec)
        digest = sha256(f"empty:{set_spec}:{from_date}".encode()).hexdigest()
        return OaiPage(NOW, (), None, digest)

    def next_page(self, token: str, *, cancelled=None) -> OaiPage:
        raise AssertionError("empty pages never have continuations")


class CancellingAfterFetchSource:
    def __init__(self, cancel) -> None:
        self.cancel = cancel
        self.first_calls = 0

    def sample_first_page(
        self,
        set_spec: str,
        from_date: date,
        until_date: date,
        *,
        cancelled=None,
    ) -> OaiPage:
        self.first_calls += 1
        self.cancel()
        return OaiPage(
            NOW,
            (
                _article(
                    "2608.52001",
                    "synthetic.alpha",
                    submitted=from_date,
                ),
            ),
            None,
            sha256(b"cancel-after-fetch").hexdigest(),
        )

    def next_page(self, token: str, *, cancelled=None) -> OaiPage:
        raise AssertionError("cancelled first pages are never resumed inline")


def _local_evidence_document():
    from arxiv_digest.candidates import CandidateDocument

    article = _article(
        "2608.09998",
        "synthetic.gamma",
        submitted=date(2020, 1, 1),
        authors=("Local Evidence Author",),
    )
    return CandidateDocument(
        paper=article.metadata,
        versions=article.versions,
        eligible_categories=("synthetic.gamma",),
        evidence_dates=(date(2026, 8, 20),),
    )


def _local_document(arxiv_id: str, category: str):
    from arxiv_digest.candidates import CandidateDocument

    article = _article(arxiv_id, category, submitted=date(2020, 1, 1))
    return CandidateDocument(
        paper=article.metadata,
        versions=article.versions,
        eligible_categories=(category,),
        evidence_dates=(date(2026, 8, 20),),
    )


def test_balanced_90_day_corpus_filters_local_evidence_and_deduplicates(tmp_path) -> None:
    from arxiv_digest.candidates import (
        CandidateCache,
        CandidateCorpusBuilder,
        search_candidate_papers,
    )

    source = NinetyDaySource()
    cache = CandidateCache(tmp_path, clock=lambda: NOW)
    builder = CandidateCorpusBuilder(
        source,
        cache,
        clock=lambda: NOW,
        local_documents=(_local_evidence_document(),),
    )

    result = builder.build(CATEGORIES)

    assert result.complete
    assert result.setup_ready
    assert not result.reduced_breadth
    assert result.pages_fetched == 54
    assert len(result.corpus.documents) >= 60
    assert len(search_candidate_papers(result.corpus, "", limit=30)) == 30
    assert all(
        sum(category in document.eligible_categories for document in result.corpus.documents)
        >= 5
        for category in result.corpus.categories
    )
    ids = {document.paper.arxiv_id for document in result.corpus.documents}
    assert "2501.00001" not in ids
    assert "2608.09998" in ids
    cross = next(
        document
        for document in result.corpus.documents
        if document.paper.arxiv_id == "2608.09999"
    )
    assert cross.eligible_categories == ("synthetic.alpha", "synthetic.beta")
    assert len(source.first_calls) == 54
    for set_spec in {config.oai_set_spec for config in CATEGORIES}:
        ranges = [
            (start, end)
            for called_spec, start, end in source.first_calls
            if called_spec == set_spec
        ]
        assert ranges == [
            (date(2026, 5, 25).fromordinal(date(2026, 5, 25).toordinal() + 5 * i),
             date(2026, 5, 29).fromordinal(date(2026, 5, 29).toordinal() + 5 * i))
            for i in range(18)
        ]
    assert {path.name for path in tmp_path.iterdir()} == {"candidate-corpus"}


def test_page_budget_persists_exact_tokens_accepts_visible_reduced_breadth_and_resumes(
    tmp_path,
) -> None:
    from arxiv_digest.candidates import CandidateCache, CandidateCorpusBuilder

    source = TwoPageSource()
    cache = CandidateCache(tmp_path, clock=lambda: NOW)
    builder = CandidateCorpusBuilder(source, cache, clock=lambda: NOW)

    partial = builder.build(CATEGORIES)

    assert partial.pages_fetched == 60
    assert partial.minimum_met
    assert not partial.complete
    assert not partial.setup_ready
    assert partial.can_resume
    assert len(source.first_calls) == 54
    assert len(source.next_calls) == 6

    accepted = builder.build(CATEGORIES, accept_reduced_breadth=True)
    assert accepted.pages_fetched == 0
    assert accepted.reduced_breadth
    assert accepted.setup_ready
    assert accepted.corpus_hash == partial.corpus_hash

    complete = builder.resume(CATEGORIES)
    assert complete.complete
    assert complete.pages_fetched == 48
    assert len(source.first_calls) == 54
    assert len(source.next_calls) == 54


def test_setup_restart_resumes_exact_shards_and_rehydrates_before_acceptance(
    tmp_path,
) -> None:
    from arxiv_digest.application import _DefaultRuntime
    from arxiv_digest.candidates import CandidateCache, CandidateCorpusBuilder
    from arxiv_digest.maintenance import MaintenanceBarrier
    from arxiv_digest.paths import resolve_paths
    from arxiv_digest.profile import ProfileRepository
    from arxiv_digest.setup import CategorySelection
    from arxiv_digest.web.api import ApiRequest, ApiRouter
    from arxiv_digest.web.lifecycle import LifecycleController

    token = "A" * 43
    host = "127.0.0.1:43123"
    origin = f"http://{host}"
    paths = resolve_paths(
        home=tmp_path / "home",
        environ={
            "ARXIV_DIGEST_TESTING": "1",
            "ARXIV_DIGEST_TEST_ROOT": str(tmp_path / "state"),
        },
    )
    paths.ensure()

    def open_runtime(source, *, page_budget: int):
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
            output=lambda _message: None,
        )
        closeable = runtime.open_database()
        runtime.candidates = CandidateCorpusBuilder(
            source,
            CandidateCache(paths, clock=lambda: NOW),
            clock=lambda: NOW,
            page_budget=page_budget,
        )
        return (
            runtime,
            ApiRouter(token=token, host=host, handlers=runtime.handlers()),
            closeable,
        )

    def dispatch(router: ApiRouter, method: str, target: str, value=None):
        headers = {"Host": host, "Authorization": f"Bearer {token}"}
        body = b""
        if value is not None:
            headers.update(
                Origin=origin,
                **{"Content-Type": "application/json"},
            )
            body = json.dumps(value).encode()
        response = router.dispatch(ApiRequest(method, target, headers, body))
        payload = json.loads(response.body)
        assert response.status == 200, payload
        return payload["data"]

    def await_job(router: ApiRouter, job_id: str):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = dispatch(
                router,
                "GET",
                f"/api/v1/setup/jobs/{job_id}",
            )
            if status["status"] != "running":
                assert status["status"] == "completed", status
                return status
            time.sleep(0.01)
        raise AssertionError("candidate job did not reach a terminal state")

    initial_source = RestartableCandidateSource()
    first_runtime, first_router, first_closeable = open_runtime(
        initial_source,
        page_budget=1,
    )
    try:
        draft = first_runtime.setup.start()
        draft = first_runtime.setup.select_categories(
            draft.revision,
            (CategorySelection("synthetic.alpha", "synthetic:alpha"),),
        )
        draft = first_runtime.setup.set_initial_coverage(
            draft.revision,
            date(2026, 7, 23),
            earliest_datestamp=date(2007, 1, 1),
        )
        started = dispatch(
            first_router,
            "POST",
            "/api/v1/setup/corpus",
            {"draft_revision": draft.revision, "mode": "restart"},
        )
        partial = await_job(first_router, started["job_id"])
        assert partial["pages_fetched"] == 1
        assert partial["can_resume"] is True
        assert partial["corpus_complete"] is False
    finally:
        first_closeable.close()

    cache = CandidateCache(paths, clock=lambda: NOW)
    persisted = cache.load_shard("synthetic.alpha", "synthetic:alpha")
    assert persisted is not None
    assert persisted.continuation_by_stratum == (
        (0, RestartableCandidateSource.exact_token),
    )
    accepted_first_page_ids = {
        document.paper.arxiv_id for document in persisted.documents
    }

    resumed_source = RestartableCandidateSource()
    second_runtime, second_router, second_closeable = open_runtime(
        resumed_source,
        page_budget=18,
    )
    try:
        assert second_runtime._candidate_build is None
        restarted_draft = dispatch(
            second_router,
            "GET",
            "/api/v1/setup/draft",
        )
        assert restarted_draft["current_step"] == "candidate_corpus"
        assert restarted_draft["corpus_hash"] is None
        assert restarted_draft["corpus_can_resume"] is True

        started = dispatch(
            second_router,
            "POST",
            "/api/v1/setup/corpus",
            {
                "draft_revision": restarted_draft["revision"],
                "mode": "resume",
            },
        )
        complete = await_job(second_router, started["job_id"])
        assert complete["corpus_complete"] is True
        assert complete["pages_fetched"] == 18
        assert resumed_source.next_calls == [
            RestartableCandidateSource.exact_token
        ]
        assert (
            "synthetic:alpha",
            date(2026, 5, 25),
            date(2026, 5, 29),
        ) not in resumed_source.first_calls
        assert accepted_first_page_ids <= {
            document.paper.arxiv_id
            for document in second_runtime._candidate_build.corpus.documents
        }
    finally:
        second_closeable.close()

    no_network_source = RestartableCandidateSource()
    third_runtime, third_router, third_closeable = open_runtime(
        no_network_source,
        page_budget=60,
    )
    try:
        complete_draft = dispatch(
            third_router,
            "GET",
            "/api/v1/setup/draft",
        )
        assert third_runtime._candidate_build is None
        assert complete_draft["corpus_hash"] is None
        assert complete_draft["corpus_can_resume"] is True

        started = dispatch(
            third_router,
            "POST",
            "/api/v1/setup/corpus",
            {
                "draft_revision": complete_draft["revision"],
                "mode": "resume",
            },
        )
        rehydrated = await_job(third_router, started["job_id"])
        assert rehydrated["corpus_complete"] is True
        assert rehydrated["pages_fetched"] == 0
        assert no_network_source.first_calls == []
        assert no_network_source.next_calls == []
        assert third_runtime._candidate_build.corpus_hash == complete["corpus_hash"]

        accepted = dispatch(
            third_router,
            "POST",
            "/api/v1/setup/corpus/accept",
            {
                "draft_revision": complete_draft["revision"],
                "corpus_hash": rehydrated["corpus_hash"],
            },
        )
        assert accepted["current_step"] == "seed_papers"
        assert accepted["corpus_complete"] is True
        assert accepted["corpus_hash"] == rehydrated["corpus_hash"]
    finally:
        third_closeable.close()


def test_expired_token_replays_only_its_stratum_and_retains_other_documents(
    tmp_path,
) -> None:
    from arxiv_digest.candidates import CandidateCache, CandidateCorpusBuilder

    source = ExpiringTokenSource()
    cache = CandidateCache(tmp_path, clock=lambda: NOW)
    config = (CATEGORIES[0],)
    initial = CandidateCorpusBuilder(
        source,
        cache,
        clock=lambda: NOW,
        page_budget=2,
    ).build(config)
    assert {document.paper.arxiv_id for document in initial.corpus.documents} == {
        "2608.30000",
        "2608.30001",
    }

    resumed = CandidateCorpusBuilder(
        source,
        cache,
        clock=lambda: NOW,
    ).resume(config)

    assert resumed.complete
    assert source.first_count[0] == 2
    assert source.first_count[1] == 1
    assert all(source.first_count[index] == 1 for index in range(1, 18))
    ids = {document.paper.arxiv_id for document in resumed.corpus.documents}
    assert "2608.30000" in ids
    assert "2608.30001" in ids


def test_resume_continues_the_round_robin_at_the_least_inspected_stratum(
    tmp_path,
) -> None:
    from arxiv_digest.candidates import CandidateCache, CandidateCorpusBuilder

    source = ThreePageSource()
    cache = CandidateCache(tmp_path, clock=lambda: NOW)
    config = (CATEGORIES[0],)
    CandidateCorpusBuilder(
        source,
        cache,
        clock=lambda: NOW,
        page_budget=20,
    ).build(config)

    CandidateCorpusBuilder(
        source,
        cache,
        clock=lambda: NOW,
        page_budget=1,
    ).resume(config)

    assert source.next_calls[-1] == "opaque:2:page-2"


def test_five_minute_budget_stops_below_minimum_with_resume_retry_state(
    tmp_path,
) -> None:
    from arxiv_digest.candidates import CandidateCache, CandidateCorpusBuilder

    source = TwoPageSource()
    times = iter((NOW, NOW, NOW + timedelta(minutes=5)))
    builder = CandidateCorpusBuilder(
        source,
        CandidateCache(tmp_path, clock=lambda: NOW),
        clock=lambda: next(times),
    )

    result = builder.build(CATEGORIES)

    assert result.pages_fetched == 1
    assert not result.minimum_met
    assert not result.setup_ready
    assert result.can_resume


def test_five_minute_deadline_cancels_an_in_flight_source_before_cache_mutation(
    tmp_path,
) -> None:
    from arxiv_digest.candidates import (
        CandidateBuildCancelled,
        CandidateCache,
        CandidateCorpusBuilder,
    )
    from arxiv_digest.rate_limit import ArxivRequestCancelled

    current = {"value": NOW}

    class DeadlineAwareSource:
        def sample_first_page(
            self,
            set_spec: str,
            from_date: date,
            until_date: date,
            *,
            cancelled=None,
        ) -> OaiPage:
            assert cancelled is not None
            current["value"] = NOW + timedelta(minutes=5, seconds=1)
            if cancelled():
                raise ArxivRequestCancelled("fixture deadline")
            raise AssertionError("candidate deadline was not forwarded")

    cache = CandidateCache(tmp_path, clock=lambda: NOW)
    builder = CandidateCorpusBuilder(
        DeadlineAwareSource(),
        cache,
        clock=lambda: current["value"],
    )

    import pytest

    with pytest.raises(CandidateBuildCancelled):
        builder.build((CATEGORIES[0],))

    shard = cache.load_shard(
        CATEGORIES[0].category,
        CATEGORIES[0].oai_set_spec,
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        now=NOW,
    )
    assert shard is not None
    assert shard.pages_fetched == 0


def test_category_selection_changes_reuse_unchanged_fresh_shards(tmp_path) -> None:
    from arxiv_digest.candidates import CandidateCache, CandidateCorpusBuilder

    source = NinetyDaySource()
    builder = CandidateCorpusBuilder(
        source,
        CandidateCache(tmp_path, clock=lambda: NOW),
        clock=lambda: NOW,
    )

    alpha = builder.build((CATEGORIES[0],))
    expanded = builder.build(CATEGORIES[:2])
    reduced = builder.build((CATEGORIES[0],))

    assert alpha.pages_fetched == 18
    assert expanded.pages_fetched == 18
    assert reduced.pages_fetched == 0
    assert sum(call[0] == "synthetic:alpha" for call in source.first_calls) == 18
    assert sum(call[0] == "synthetic:beta" for call in source.first_calls) == 18


def test_changed_local_evidence_invalidates_only_its_category_shard(tmp_path) -> None:
    from arxiv_digest.candidates import CandidateCache, CandidateCorpusBuilder

    source = EmptySource()
    cache = CandidateCache(tmp_path, clock=lambda: NOW)
    stable_beta = _local_document("2608.50001", "synthetic.beta")
    initial_alpha = _local_document("2608.50002", "synthetic.alpha")
    CandidateCorpusBuilder(
        source,
        cache,
        clock=lambda: NOW,
        local_documents=(initial_alpha, stable_beta),
    ).build(CATEGORIES[:2])

    added_alpha = _local_document("2608.50003", "synthetic.alpha")
    refreshed = CandidateCorpusBuilder(
        source,
        cache,
        clock=lambda: NOW,
        local_documents=(initial_alpha, added_alpha, stable_beta),
    ).build(CATEGORIES[:2])

    assert refreshed.pages_fetched == 18
    assert "2608.50003" in {
        document.paper.arxiv_id for document in refreshed.corpus.documents
    }
    assert source.first_calls.count("synthetic:alpha") == 36
    assert source.first_calls.count("synthetic:beta") == 18


def test_builder_refreshes_local_mailing_evidence_for_each_generation(
    tmp_path,
) -> None:
    from arxiv_digest.candidates import CandidateCache, CandidateCorpusBuilder

    source = EmptySource()
    documents = [_local_document("2608.51001", "synthetic.alpha")]
    calls = []

    def local_provider(configs, window_start, window_end):
        calls.append((configs, window_start, window_end))
        return tuple(documents)

    builder = CandidateCorpusBuilder(
        source,
        CandidateCache(tmp_path, clock=lambda: NOW),
        clock=lambda: NOW,
        local_document_provider=local_provider,
    )
    first = builder.build((CATEGORIES[0],))
    documents.append(_local_document("2608.51002", "synthetic.alpha"))
    second = builder.build((CATEGORIES[0],))

    assert calls == [
        ((CATEGORIES[0],), date(2026, 5, 25), date(2026, 8, 22)),
        ((CATEGORIES[0],), date(2026, 5, 25), date(2026, 8, 22)),
    ]
    assert {item.paper.arxiv_id for item in first.corpus.documents} == {
        "2608.51001"
    }
    assert {item.paper.arxiv_id for item in second.corpus.documents} == {
        "2608.51001",
        "2608.51002",
    }


def test_candidate_build_cancellation_stops_before_post_fetch_cache_mutation(
    tmp_path,
) -> None:
    from arxiv_digest.candidates import (
        CandidateBuildCancelled,
        CandidateCache,
        CandidateCorpusBuilder,
    )

    cancellation = {"requested": False}
    source = CancellingAfterFetchSource(
        lambda: cancellation.update(requested=True)
    )
    cache = CandidateCache(tmp_path, clock=lambda: NOW)
    builder = CandidateCorpusBuilder(source, cache, clock=lambda: NOW)

    import pytest

    with pytest.raises(CandidateBuildCancelled):
        builder.build(
            (CATEGORIES[0],),
            cancelled=lambda: cancellation["requested"],
        )

    shard = cache.load_shard(
        CATEGORIES[0].category,
        CATEGORIES[0].oai_set_spec,
        window_start=date(2026, 5, 25),
        window_end=date(2026, 8, 22),
        now=NOW,
    )
    assert source.first_calls == 1
    assert shard is not None
    assert shard.pages_fetched == 0
    assert shard.documents == ()
