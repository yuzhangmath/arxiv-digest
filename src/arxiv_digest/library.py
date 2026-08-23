from __future__ import annotations

from dataclasses import dataclass

from arxiv_digest.models import PaperMetadata
from arxiv_digest.storage.store import LibraryEntry, Store


@dataclass(frozen=True, slots=True)
class LibraryItem:
    metadata: PaperMetadata
    saved_version: int | None
    latest_version: int | None
    paper_available: bool
    local_pdf_versions: tuple[int, ...]
    new_version_available: bool


@dataclass(frozen=True, slots=True)
class LibraryPage:
    entries: tuple[LibraryItem, ...]
    limit: int
    offset: int
    next_offset: int | None


def _library_item(entry: LibraryEntry) -> LibraryItem:
    return LibraryItem(
        metadata=entry.metadata,
        saved_version=entry.saved_version,
        latest_version=entry.latest_version,
        paper_available=entry.paper_available,
        local_pdf_versions=entry.local_pdf_versions,
        new_version_available=(
            entry.paper_available
            and entry.saved_version is not None
            and entry.latest_version is not None
            and entry.latest_version > entry.saved_version
        ),
    )


class LibraryService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def save(self, arxiv_id: str, *, version: int | None) -> None:
        self.store.save_paper(arxiv_id, version)

    def remove(self, arxiv_id: str) -> None:
        self.store.remove_saved_paper(arxiv_id)

    def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
    ) -> LibraryPage:
        values = self.store.search_library(query, limit=limit, offset=offset)
        has_more = len(values) == limit and bool(
            self.store.search_library(query, limit=1, offset=offset + limit)
        )
        return LibraryPage(
            entries=tuple(_library_item(value) for value in values),
            limit=limit,
            offset=offset,
            next_offset=offset + limit if has_more else None,
        )
