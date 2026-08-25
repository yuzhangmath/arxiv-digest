from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from itertools import permutations
from zoneinfo import ZoneInfo

import pytest

from arxiv_digest.models import (
    AnnounceType,
    EvidenceSource,
    PaperVersion,
    ReconciledEvent,
    SourceObservation,
    VersionResolution,
)
from arxiv_digest.reconciliation import reconcile_paper


NOW = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)


def observation(
    key: str,
    source: EvidenceSource,
    **values: object,
) -> SourceObservation:
    return SourceObservation(
        source_key=key,
        arxiv_id="2608.00001",
        source=source,
        category=values.get("category", "math.AG"),
        announce_type=values.get("announce_type"),
        daily_list_date=values.get("daily_list_date"),
        announced_version=values.get("announced_version"),
        list_position=values.get("list_position"),
        oai_datestamp=values.get("oai_datestamp"),
        response_sha256="a" * 64,
        observed_at=NOW,
    )


def test_atom_and_oai_without_catchup_create_no_canonical_event() -> None:
    versions = (
        PaperVersion(1, datetime(2026, 8, 20, 16, tzinfo=timezone.utc)),
    )
    observations = (
        observation(
            "atom:math.AG:2608.00001:v1",
            EvidenceSource.ATOM,
            announce_type=AnnounceType.NEW,
            announced_version=1,
        ),
        observation(
            "oai:2608.00001:v1",
            EvidenceSource.OAI,
            announced_version=1,
        ),
    )

    assert reconcile_paper("2608.00001", observations, versions).events == ()


def test_reconciliation_rejects_observations_for_another_paper() -> None:
    catchup = observation(
        "catchup:math.AG:2026-08-24:2608.00002:0",
        EvidenceSource.CATCHUP,
        daily_list_date=date(2026, 8, 24),
        announce_type=AnnounceType.NEW,
        list_position=0,
    )
    catchup = replace(catchup, arxiv_id="2608.00002")

    with pytest.raises(ValueError, match="same paper"):
        reconcile_paper("2608.00001", (catchup,), ())


def test_catchup_observation_creates_an_unconfirmed_event() -> None:
    daily_list_date = date(2026, 8, 24)
    catchup = observation(
        "catchup:math.AG:2026-08-24:2608.00001:0",
        EvidenceSource.CATCHUP,
        announce_type=AnnounceType.NEW,
        daily_list_date=daily_list_date,
        list_position=0,
    )

    result = reconcile_paper("2608.00001", (catchup,), ())

    assert result.events == (
        ReconciledEvent(
            arxiv_id="2608.00001",
            daily_list_date=daily_list_date,
            announced_version=None,
            version_resolution=VersionResolution.UNCONFIRMED,
            observation_keys=(catchup.source_key,),
        ),
    )


def test_one_daily_list_event_chooses_the_newest_eligible_version() -> None:
    daily_list_date = date(2026, 8, 25)
    catchup = observation(
        "catchup:math.AG:2026-08-25:2608.00001:0",
        EvidenceSource.CATCHUP,
        announce_type=AnnounceType.NEW,
        daily_list_date=daily_list_date,
        list_position=0,
    )
    versions = (
        PaperVersion(2, datetime(2026, 8, 24, 12, tzinfo=timezone.utc)),
        PaperVersion(3, datetime(2026, 8, 25, 17, 59, tzinfo=timezone.utc)),
    )

    event = reconcile_paper("2608.00001", (catchup,), versions).events[0]

    assert event.announced_version == 3
    assert event.version_resolution is VersionResolution.CHRONOLOGY_MATCHED


def test_chronology_match_links_the_corresponding_oai_observation() -> None:
    daily_list_date = date(2026, 8, 25)
    catchup = observation(
        "catchup:math.AG:2026-08-25:2608.00001:0",
        EvidenceSource.CATCHUP,
        announce_type=AnnounceType.NEW,
        daily_list_date=daily_list_date,
        list_position=0,
    )
    oai = observation(
        "oai:2608.00001:v1",
        EvidenceSource.OAI,
        category=None,
        announced_version=1,
        oai_datestamp=daily_list_date,
    )

    event = reconcile_paper(
        "2608.00001",
        (catchup, oai),
        (
            PaperVersion(
                1,
                datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
            ),
        ),
    ).events[0]

    assert event.observation_keys == tuple(
        sorted((catchup.source_key, oai.source_key))
    )


def test_two_ordered_events_choose_versions_two_then_three() -> None:
    observations = tuple(
        observation(
            f"catchup:math.AG:{day.isoformat()}:2608.00001:0",
            EvidenceSource.CATCHUP,
            announce_type=AnnounceType.REPLACE,
            daily_list_date=day,
            list_position=0,
        )
        for day in (date(2026, 8, 24), date(2026, 8, 25))
    )
    versions = (
        PaperVersion(2, datetime(2026, 8, 20, 12, tzinfo=timezone.utc)),
        PaperVersion(3, datetime(2026, 8, 21, 12, tzinfo=timezone.utc)),
    )

    result = reconcile_paper("2608.00001", observations, versions)

    assert [event.announced_version for event in result.events] == [2, 3]


def test_two_ordered_events_choose_the_newest_pair_from_three_versions() -> None:
    observations = tuple(
        observation(
            f"catchup:math.AG:{day.isoformat()}:2608.00001:0",
            EvidenceSource.CATCHUP,
            announce_type=AnnounceType.REPLACE,
            daily_list_date=day,
            list_position=0,
        )
        for day in (date(2026, 8, 24), date(2026, 8, 25))
    )
    versions = tuple(
        PaperVersion(number, datetime(2026, 8, 20, 12, tzinfo=timezone.utc))
        for number in (2, 3, 4)
    )

    result = reconcile_paper("2608.00001", observations, versions)

    assert [event.announced_version for event in result.events] == [3, 4]


@pytest.mark.parametrize(
    "daily_list_date",
    (date(2026, 1, 15), date(2026, 8, 25)),
    ids=("EST", "EDT"),
)
def test_version_eligibility_uses_the_exact_eastern_1400_cutoff(
    daily_list_date: date,
) -> None:
    eastern = ZoneInfo("America/New_York")
    cutoff = datetime(
        daily_list_date.year,
        daily_list_date.month,
        daily_list_date.day,
        14,
        tzinfo=eastern,
    ).astimezone(timezone.utc)
    catchup = observation(
        f"catchup:math.AG:{daily_list_date.isoformat()}:2608.00001:0",
        EvidenceSource.CATCHUP,
        announce_type=AnnounceType.NEW,
        daily_list_date=daily_list_date,
        list_position=0,
    )
    versions = (
        PaperVersion(1, cutoff),
        PaperVersion(2, cutoff + timedelta(microseconds=1)),
    )

    event = reconcile_paper("2608.00001", (catchup,), versions).events[0]

    assert event.announced_version == 1


def test_replacement_support_excludes_version_one() -> None:
    daily_list_date = date(2026, 8, 25)
    catchup = observation(
        "catchup:math.AG:2026-08-25:2608.00001:0",
        EvidenceSource.CATCHUP,
        announce_type=AnnounceType.REPLACE,
        daily_list_date=daily_list_date,
        list_position=0,
    )
    versions = (
        PaperVersion(1, datetime(2026, 8, 20, 12, tzinfo=timezone.utc)),
    )

    event = reconcile_paper("2608.00001", (catchup,), versions).events[0]

    assert event.announced_version is None
    assert event.version_resolution is VersionResolution.UNCONFIRMED


@pytest.mark.parametrize(
    ("submitted_at", "daily_list_date"),
    (
        (
            datetime(2026, 8, 21, 18, tzinfo=timezone.utc),
            date(2026, 8, 24),
        ),
        (
            datetime(2026, 1, 2, 12, tzinfo=timezone.utc),
            date(2026, 8, 25),
        ),
    ),
    ids=("weekend", "delayed"),
)
def test_eligible_versions_have_no_maximum_delay_rejection(
    submitted_at: datetime,
    daily_list_date: date,
) -> None:
    catchup = observation(
        f"catchup:math.AG:{daily_list_date.isoformat()}:2608.00001:0",
        EvidenceSource.CATCHUP,
        announce_type=AnnounceType.NEW,
        daily_list_date=daily_list_date,
        list_position=0,
    )

    event = reconcile_paper(
        "2608.00001",
        (catchup,),
        (PaperVersion(1, submitted_at),),
    ).events[0]

    assert event.announced_version == 1


def test_same_version_can_be_reused_on_different_dates_across_categories() -> None:
    observations = (
        observation(
            "catchup:math.AG:2026-08-24:2608.00001:0",
            EvidenceSource.CATCHUP,
            category="math.AG",
            announce_type=AnnounceType.NEW,
            daily_list_date=date(2026, 8, 24),
            list_position=0,
        ),
        observation(
            "catchup:math.CO:2026-08-25:2608.00001:0",
            EvidenceSource.CATCHUP,
            category="math.CO",
            announce_type=AnnounceType.CROSS,
            daily_list_date=date(2026, 8, 25),
            list_position=0,
        ),
    )
    versions = (
        PaperVersion(1, datetime(2026, 8, 20, 12, tzinfo=timezone.utc)),
    )

    result = reconcile_paper("2608.00001", observations, versions)

    assert [event.announced_version for event in result.events] == [1, 1]


def test_same_paper_and_date_merge_across_categories() -> None:
    daily_list_date = date(2026, 8, 25)
    observations = (
        observation(
            "catchup:math.CO:2026-08-25:2608.00001:1",
            EvidenceSource.CATCHUP,
            category="math.CO",
            announce_type=AnnounceType.CROSS,
            daily_list_date=daily_list_date,
            list_position=1,
        ),
        observation(
            "catchup:math.AG:2026-08-25:2608.00001:0",
            EvidenceSource.CATCHUP,
            category="math.AG",
            announce_type=AnnounceType.NEW,
            daily_list_date=daily_list_date,
            list_position=0,
        ),
    )

    result = reconcile_paper(
        "2608.00001",
        observations,
        (
            PaperVersion(
                1,
                datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
            ),
        ),
    )

    assert len(result.events) == 1
    assert result.events[0].announced_version == 1
    assert result.events[0].observation_keys == tuple(
        sorted(item.source_key for item in observations)
    )


def test_compatible_atom_version_wins_the_chronology_tie() -> None:
    daily_list_date = date(2026, 8, 25)
    catchup = observation(
        "catchup:math.AG:2026-08-25:2608.00001:0",
        EvidenceSource.CATCHUP,
        category="math.AG",
        announce_type=AnnounceType.REPLACE,
        daily_list_date=daily_list_date,
        list_position=0,
    )
    atom = observation(
        "atom:math.AG:2608.00001:v2",
        EvidenceSource.ATOM,
        category="math.AG",
        announce_type=AnnounceType.REPLACE,
        announced_version=2,
        list_position=0,
    )
    versions = tuple(
        PaperVersion(number, datetime(2026, 8, 20, 12, tzinfo=timezone.utc))
        for number in (2, 3)
    )

    event = reconcile_paper("2608.00001", (catchup, atom), versions).events[0]

    assert event.announced_version == 2
    assert event.version_resolution is VersionResolution.ATOM_CONFIRMED
    assert event.observation_keys == tuple(
        sorted((catchup.source_key, atom.source_key))
    )


def test_atom_feed_date_does_not_replace_the_only_canonical_date() -> None:
    catchup = observation(
        "catchup:math.AG:2026-08-25:2608.00001:0",
        EvidenceSource.CATCHUP,
        category="math.AG",
        announce_type=AnnounceType.REPLACE,
        daily_list_date=date(2026, 8, 25),
        list_position=0,
    )
    atom = observation(
        "atom:math.AG:2026-08-24:2608.00001:v2",
        EvidenceSource.ATOM,
        category="math.AG",
        announce_type=AnnounceType.REPLACE,
        daily_list_date=date(2026, 8, 24),
        announced_version=2,
        list_position=0,
    )
    versions = tuple(
        PaperVersion(number, datetime(2026, 8, 20, 12, tzinfo=timezone.utc))
        for number in (2, 3)
    )

    event = reconcile_paper("2608.00001", (catchup, atom), versions).events[0]

    assert event.daily_list_date == date(2026, 8, 25)
    assert event.announced_version == 2
    assert event.version_resolution is VersionResolution.ATOM_CONFIRMED


def test_contradictory_compatible_atom_versions_leave_event_unconfirmed() -> None:
    daily_list_date = date(2026, 8, 25)
    catchup = observation(
        "catchup:math.AG:2026-08-25:2608.00001:0",
        EvidenceSource.CATCHUP,
        category="math.AG",
        announce_type=AnnounceType.REPLACE,
        daily_list_date=daily_list_date,
        list_position=0,
    )
    atoms = tuple(
        observation(
            f"atom:math.AG:2608.00001:v{number}",
            EvidenceSource.ATOM,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            announced_version=number,
            list_position=0,
        )
        for number in (2, 3)
    )
    versions = tuple(
        PaperVersion(number, datetime(2026, 8, 20, 12, tzinfo=timezone.utc))
        for number in (2, 3)
    )

    result = reconcile_paper("2608.00001", (catchup, *atoms), versions)

    assert result.events == (
        ReconciledEvent(
            arxiv_id="2608.00001",
            daily_list_date=daily_list_date,
            announced_version=None,
            version_resolution=VersionResolution.UNCONFIRMED,
            observation_keys=tuple(
                sorted((catchup.source_key, *(atom.source_key for atom in atoms)))
            ),
            conflict_code="version_evidence_conflict",
        ),
    )
    assert result.diagnostic_codes == ("version_evidence_conflict",)


def test_atom_versions_on_distinct_feed_dates_align_to_distinct_events() -> None:
    days = (date(2026, 8, 24), date(2026, 8, 25))
    catchups = tuple(
        observation(
            f"catchup:math.AG:{day.isoformat()}:2608.00001:0",
            EvidenceSource.CATCHUP,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            daily_list_date=day,
            list_position=0,
        )
        for day in days
    )
    atoms = tuple(
        observation(
            f"atom:math.AG:{day.isoformat()}:2608.00001:v{number}",
            EvidenceSource.ATOM,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            daily_list_date=day,
            announced_version=number,
            list_position=0,
        )
        for day, number in zip(days, (2, 3))
    )
    versions = tuple(
        PaperVersion(number, datetime(2026, 8, 20, 12, tzinfo=timezone.utc))
        for number in (2, 3)
    )

    result = reconcile_paper(
        "2608.00001",
        (*catchups, *atoms),
        versions,
    )

    assert [event.announced_version for event in result.events] == [2, 3]
    assert all(
        event.version_resolution is VersionResolution.ATOM_CONFIRMED
        for event in result.events
    )
    assert result.diagnostic_codes == ()


def test_ordered_atom_versions_align_to_the_category_local_sequence() -> None:
    days = (date(2026, 8, 24), date(2026, 8, 25))
    catchups = tuple(
        observation(
            f"catchup:math.AG:{day.isoformat()}:2608.00001:0",
            EvidenceSource.CATCHUP,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            daily_list_date=day,
            list_position=0,
        )
        for day in days
    )
    atoms = tuple(
        observation(
            f"atom:math.AG:2608.00001:v{number}",
            EvidenceSource.ATOM,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            announced_version=number,
            list_position=0,
        )
        for number in (2, 3)
    )
    versions = tuple(
        PaperVersion(number, datetime(2026, 8, 20, 12, tzinfo=timezone.utc))
        for number in (2, 3)
    )

    result = reconcile_paper(
        "2608.00001",
        (*catchups, *atoms),
        versions,
    )

    assert [event.announced_version for event in result.events] == [2, 3]
    assert all(
        event.version_resolution is VersionResolution.ATOM_CONFIRMED
        for event in result.events
    )
    assert result.diagnostic_codes == ()


def test_every_observation_arrival_permutation_has_the_same_result() -> None:
    observations = (
        observation(
            "catchup:math.AG:2026-08-24:2608.00001:0",
            EvidenceSource.CATCHUP,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            daily_list_date=date(2026, 8, 24),
            list_position=0,
        ),
        observation(
            "catchup:math.AG:2026-08-25:2608.00001:0",
            EvidenceSource.CATCHUP,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            daily_list_date=date(2026, 8, 25),
            list_position=0,
        ),
        observation(
            "atom:math.AG:2608.00001:v3",
            EvidenceSource.ATOM,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            announced_version=3,
            list_position=0,
        ),
    )
    versions = tuple(
        PaperVersion(number, datetime(2026, 8, 20, 12, tzinfo=timezone.utc))
        for number in (2, 3)
    )
    expected = reconcile_paper("2608.00001", observations, versions)

    assert all(
        reconcile_paper("2608.00001", ordering, versions) == expected
        for ordering in permutations(observations)
    )


def test_repeating_every_observation_is_idempotent() -> None:
    observations = (
        observation(
            "catchup:math.AG:2026-08-25:2608.00001:0",
            EvidenceSource.CATCHUP,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            daily_list_date=date(2026, 8, 25),
            list_position=0,
        ),
        observation(
            "atom:math.AG:2608.00001:v2",
            EvidenceSource.ATOM,
            category="math.AG",
            announce_type=AnnounceType.REPLACE,
            announced_version=2,
            list_position=0,
        ),
    )
    versions = tuple(
        PaperVersion(number, datetime(2026, 8, 20, 12, tzinfo=timezone.utc))
        for number in (2, 3)
    )

    expected = reconcile_paper("2608.00001", observations, versions)

    assert reconcile_paper(
        "2608.00001",
        observations + observations,
        versions,
    ) == expected
