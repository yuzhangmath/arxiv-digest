"""Pure reconciliation of source observations into daily-list events."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .models import (
    AnnounceType,
    EvidenceSource,
    PaperVersion,
    ReconciledEvent,
    ReconciliationResult,
    SourceObservation,
    VersionResolution,
)


_EASTERN = ZoneInfo("America/New_York")


def _observation_sort_key(
    observation: SourceObservation,
) -> tuple[date, str, int, str]:
    return (
        observation.daily_list_date or date.min,
        observation.category or "",
        -1 if observation.list_position is None else observation.list_position,
        observation.source_key,
    )


def _mailing_cutoff(daily_list_date: date) -> datetime:
    return datetime.combine(daily_list_date, time(14), tzinfo=_EASTERN)


def _section_compatible(
    version_number: int,
    observations: list[SourceObservation],
) -> bool:
    replacement_types = {AnnounceType.REPLACE, AnnounceType.REPLACE_CROSS}
    return version_number >= 2 or not any(
        item.announce_type in replacement_types for item in observations
    )


def _announcement_family(value: AnnounceType | None) -> str | None:
    if value in {AnnounceType.REPLACE, AnnounceType.REPLACE_CROSS}:
        return "replacement"
    return value.value if value is not None else None


def _compatible_atoms(
    announced_version: int | None,
    catchup_observations: list[SourceObservation],
    atom_observations: tuple[SourceObservation, ...],
    all_catchup_observations: tuple[SourceObservation, ...],
) -> tuple[SourceObservation, ...]:
    if announced_version is None:
        return ()
    return tuple(
        atom
        for atom in atom_observations
        if atom.announced_version == announced_version
        and _atom_matches_catchup(
            atom,
            catchup_observations,
            all_catchup_observations,
        )
    )


def _atom_matches_catchup(
    atom: SourceObservation,
    catchup_observations: list[SourceObservation],
    all_catchup_observations: tuple[SourceObservation, ...],
) -> bool:
    def same_sequence(catchup: SourceObservation) -> bool:
        return (
            atom.category == catchup.category
            and _announcement_family(atom.announce_type)
            == _announcement_family(catchup.announce_type)
        )

    if not any(same_sequence(catchup) for catchup in catchup_observations):
        return False
    if atom.daily_list_date is None:
        return True
    exact_slot_exists = any(
        same_sequence(catchup)
        and atom.daily_list_date == catchup.daily_list_date
        for catchup in all_catchup_observations
    )
    return not exact_slot_exists or any(
        same_sequence(catchup)
        and atom.daily_list_date == catchup.daily_list_date
        for catchup in catchup_observations
    )


def _eligible_version_numbers(
    daily_list_date: date,
    catchup_observations: list[SourceObservation],
    versions: tuple[PaperVersion, ...],
) -> tuple[int, ...]:
    return tuple(
        version.number
        for version in versions
        if version.submitted_at <= _mailing_cutoff(daily_list_date)
        and _section_compatible(version.number, catchup_observations)
    )


def _conflicting_atoms(
    daily_list_date: date,
    catchup_observations: list[SourceObservation],
    atom_observations: tuple[SourceObservation, ...],
    versions: tuple[PaperVersion, ...],
    all_catchup_observations: tuple[SourceObservation, ...],
) -> tuple[SourceObservation, ...]:
    eligible = set(
        _eligible_version_numbers(
            daily_list_date,
            catchup_observations,
            versions,
        )
    )
    compatible = tuple(
        atom
        for atom in atom_observations
        if atom.announced_version in eligible
        and _atom_matches_catchup(
            atom,
            catchup_observations,
            all_catchup_observations,
        )
    )
    catchup_by_date: dict[date, list[SourceObservation]] = {}
    for catchup in all_catchup_observations:
        assert catchup.daily_list_date is not None
        catchup_by_date.setdefault(catchup.daily_list_date, []).append(catchup)
    forced_here = tuple(
        atom
        for atom in compatible
        if {
            candidate_date
            for candidate_date, candidate_observations in catchup_by_date.items()
            if atom.announced_version
            in _eligible_version_numbers(
                candidate_date,
                candidate_observations,
                versions,
            )
            and _atom_matches_catchup(
                atom,
                candidate_observations,
                all_catchup_observations,
            )
        }
        == {daily_list_date}
    )
    if len({atom.announced_version for atom in forced_here}) > 1:
        return forced_here
    return ()


def _solve_assignments(
    dates: tuple[date, ...],
    catchup_by_date: dict[date, list[SourceObservation]],
    versions: tuple[PaperVersion, ...],
    atom_observations: tuple[SourceObservation, ...],
    all_catchup_observations: tuple[SourceObservation, ...],
    conflicts_by_date: dict[date, tuple[SourceObservation, ...]],
) -> tuple[int | None, ...]:
    candidates_by_date = tuple(
        (
            (None,)
            if daily_list_date in conflicts_by_date
            else (None,)
            + _eligible_version_numbers(
                daily_list_date,
                catchup_by_date[daily_list_date],
                versions,
            )
        )
        for daily_list_date in dates
    )
    categories_by_date = tuple(
        frozenset(
            item.category
            for item in catchup_by_date[daily_list_date]
            if item.category is not None
        )
        for daily_list_date in dates
    )
    best_assignments: tuple[int | None, ...] | None = None
    best_objective: tuple[int, int, tuple[int, ...]] | None = None

    def search(
        index: int,
        assignments: tuple[int | None, ...],
        last_by_category: dict[str, int],
    ) -> None:
        nonlocal best_assignments, best_objective
        if index == len(dates):
            compatible_atom_assignments = sum(
                len(
                    _compatible_atoms(
                        value,
                        catchup_by_date[daily_list_date],
                        atom_observations,
                        all_catchup_observations,
                    )
                )
                for daily_list_date, value in zip(dates, assignments)
            )
            assigned_observations = sum(
                len(catchup_by_date[daily_list_date])
                for daily_list_date, value in zip(dates, assignments)
                if value is not None
            )
            objective = (
                compatible_atom_assignments,
                assigned_observations,
                tuple(
                    -1 if value is None else value
                    for value in reversed(assignments)
                ),
            )
            if best_objective is None or objective > best_objective:
                best_assignments = assignments
                best_objective = objective
            return

        categories = categories_by_date[index]
        for value in candidates_by_date[index]:
            if value is not None and any(
                value <= last_by_category[category]
                for category in categories
                if category in last_by_category
            ):
                continue
            updated = last_by_category.copy()
            if value is not None:
                updated.update({category: value for category in categories})
            search(index + 1, assignments + (value,), updated)

    search(0, (), {})
    assert best_assignments is not None
    return best_assignments


def reconcile_paper(
    arxiv_id: str,
    observations: Iterable[SourceObservation],
    versions: Iterable[PaperVersion],
) -> ReconciliationResult:
    """Return the desired catch-up-backed events for one paper."""

    ordered_versions = tuple(sorted(versions, key=lambda item: item.number))
    ordered_observations = tuple(
        sorted(set(observations), key=_observation_sort_key)
    )
    if any(item.arxiv_id != arxiv_id for item in ordered_observations):
        raise ValueError("all observations must belong to the same paper")
    atom_observations = tuple(
        item
        for item in ordered_observations
        if item.source is EvidenceSource.ATOM
    )
    oai_observations = tuple(
        item
        for item in ordered_observations
        if item.source is EvidenceSource.OAI
    )
    catchup_by_date: dict[date, list[SourceObservation]] = {}
    for item in ordered_observations:
        if (
            item.source is EvidenceSource.CATCHUP
            and item.daily_list_date is not None
        ):
            catchup_by_date.setdefault(item.daily_list_date, []).append(item)

    dates = tuple(sorted(catchup_by_date))
    all_catchup_observations = tuple(
        item
        for daily_list_date in dates
        for item in catchup_by_date[daily_list_date]
    )
    conflicts_by_date = {
        daily_list_date: conflict_atoms
        for daily_list_date in dates
        if (
            conflict_atoms := _conflicting_atoms(
                daily_list_date,
                catchup_by_date[daily_list_date],
                atom_observations,
                ordered_versions,
                all_catchup_observations,
            )
        )
    }
    assignments = _solve_assignments(
        dates,
        catchup_by_date,
        ordered_versions,
        atom_observations,
        all_catchup_observations,
        conflicts_by_date,
    )
    events = tuple(
        ReconciledEvent(
            arxiv_id=arxiv_id,
            daily_list_date=daily_list_date,
            announced_version=announced_version,
            version_resolution=(
                VersionResolution.ATOM_CONFIRMED
                if compatible_atoms
                else (
                    VersionResolution.CHRONOLOGY_MATCHED
                    if announced_version is not None
                    else VersionResolution.UNCONFIRMED
                )
            ),
            observation_keys=tuple(
                sorted(
                    item.source_key
                    for item in (
                        *catchup_by_date[daily_list_date],
                        *(
                            conflicts_by_date.get(daily_list_date)
                            or (*compatible_atoms, *compatible_oai)
                        ),
                    )
                )
            ),
            conflict_code=(
                "version_evidence_conflict"
                if daily_list_date in conflicts_by_date
                else None
            ),
        )
        for daily_list_date, announced_version in zip(dates, assignments)
        for compatible_atoms in (
            _compatible_atoms(
                announced_version,
                catchup_by_date[daily_list_date],
                atom_observations,
                all_catchup_observations,
            ),
        )
        for compatible_oai in (
            tuple(
                item
                for item in oai_observations
                if announced_version is not None
                and item.announced_version == announced_version
            ),
        )
    )
    diagnostic_codes = (
        ("version_evidence_conflict",) if conflicts_by_date else ()
    )
    return ReconciliationResult(arxiv_id, events, diagnostic_codes)
