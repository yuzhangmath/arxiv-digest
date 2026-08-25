from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from arxiv_digest.profile import ProfileRepository
from arxiv_digest.rate_limit import ArxivHttpClient, Interface
from arxiv_digest.sources.xml import parse_arxiv_id
from arxiv_digest.storage.store import DownloadFileRecord, Store


_RESERVED_FILENAME_CHARACTERS = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_MAX_FILENAME_BYTES = 180
_SAVE_DOWNLOAD_VERSION = object()


@dataclass(frozen=True, slots=True)
class DownloadResult:
    arxiv_id: str
    version: int
    filename: str
    byte_count: int
    sha256: str
    reused_existing: bool


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(DownloadError):
    code = "cancelled"


def _truncate_utf8(value: str, byte_limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _file_matches(path: Path, byte_count: int, digest: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if path.stat().st_size != byte_count:
        return False
    value = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest() == digest


def _verified_pdf(path: Path) -> tuple[int, str] | None:
    if path.is_symlink() or not path.is_file():
        return None
    digest = sha256()
    prefix = b""
    byte_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if len(prefix) < 5:
                prefix += chunk[: 5 - len(prefix)]
            byte_count += len(chunk)
            digest.update(chunk)
    if byte_count < 1 or not prefix.startswith(b"%PDF-"):
        return None
    return byte_count, digest.hexdigest()


def _numbered_filename(filename: str, number: int) -> str:
    suffix = f" ({number}).pdf"
    stem_budget = _MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
    stem = _truncate_utf8(Path(filename).stem, stem_budget).rstrip(" .")
    return f"{stem or 'paper'}{suffix}"


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_pdf_filename(arxiv_id: str, version: int, title: str) -> str:
    base_id, embedded_version = parse_arxiv_id(arxiv_id)
    if embedded_version is not None:
        raise ValueError("PDF filename requires an unversioned arXiv ID")
    if version < 1:
        raise ValueError("PDF version must be positive")
    normalized_title = unicodedata.normalize("NFKC", title)
    safe_title = " ".join(
        _RESERVED_FILENAME_CHARACTERS.sub(" ", normalized_title).split()
    ).strip(" .")
    if not safe_title:
        safe_title = "paper"
    encoded_id = quote(base_id, safe=".-")
    prefix = f"{encoded_id}v{version} - "
    suffix = ".pdf"
    title_budget = _MAX_FILENAME_BYTES - len((prefix + suffix).encode("utf-8"))
    safe_title = _truncate_utf8(safe_title, title_budget).rstrip(" .") or "paper"
    return f"{prefix}{safe_title}{suffix}"


def arxiv_pdf_url(arxiv_id: str, version: int) -> str:
    base_id, embedded_version = parse_arxiv_id(arxiv_id)
    if embedded_version is not None:
        raise ValueError("PDF URL requires an unversioned arXiv ID")
    if version < 1:
        raise ValueError("PDF version must be positive")
    path_id = quote(base_id, safe="/.-")
    return f"https://arxiv.org/pdf/{path_id}v{version}"


class DownloadManager:
    def __init__(
        self,
        store: Store,
        profiles: ProfileRepository,
        client: ArxivHttpClient,
        *,
        max_pdf_bytes: int = 256 * 1024 * 1024,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self.profiles = profiles
        self.client = client
        self.max_pdf_bytes = max_pdf_bytes
        self.clock = clock

    def recompute_presence(self, destination: Path) -> None:
        if not destination.is_absolute() or not destination.is_dir():
            raise DownloadError("the PDF destination is unavailable")
        verified_at = self.clock()
        records: list[DownloadFileRecord] = []
        for arxiv_id, version, title in self.store.download_file_candidates():
            filename = safe_pdf_filename(arxiv_id, version, title)
            verified = _verified_pdf(destination / filename)
            if verified is None:
                continue
            byte_count, digest = verified
            records.append(
                DownloadFileRecord(
                    arxiv_id=arxiv_id,
                    version=version,
                    filename=filename,
                    byte_count=byte_count,
                    sha256=digest,
                    last_verified_at=verified_at,
                )
            )
        self.store.replace_download_files(tuple(records))

    def download(
        self,
        arxiv_id: str,
        version: int,
        *,
        save_first: bool = False,
        save_version: int | None | object = _SAVE_DOWNLOAD_VERSION,
        cancelled: Callable[[], bool] | None = None,
    ) -> DownloadResult:
        def check_cancelled() -> None:
            if cancelled is not None and cancelled():
                raise DownloadCancelled("PDF download was cancelled")

        check_cancelled()
        metadata = self.store.article_metadata(arxiv_id)
        self.store.article_version(arxiv_id, version)
        if save_first:
            version_to_save = (
                version
                if save_version is _SAVE_DOWNLOAD_VERSION
                else save_version
            )
            if version_to_save is not None and type(version_to_save) is not int:
                raise TypeError("save_version must be an integer or null")
            self.store.save_paper(metadata.arxiv_id, version_to_save)
        profile = self.profiles.load()
        if profile is None:
            raise DownloadError("an active profile is required for PDF downloads")
        destination = profile.pdf_destination.path
        if not destination.is_absolute() or not destination.is_dir():
            raise DownloadError("the active PDF destination is unavailable")
        recorded = self.store.download_file(metadata.arxiv_id, version)
        if recorded is not None:
            recorded_path = destination / recorded.filename
            if _file_matches(
                recorded_path,
                recorded.byte_count,
                recorded.sha256,
            ):
                check_cancelled()
                self.store.record_download_file(
                    DownloadFileRecord(
                        arxiv_id=recorded.arxiv_id,
                        version=recorded.version,
                        filename=recorded.filename,
                        byte_count=recorded.byte_count,
                        sha256=recorded.sha256,
                        last_verified_at=self.clock(),
                    )
                )
                return DownloadResult(
                    arxiv_id=recorded.arxiv_id,
                    version=recorded.version,
                    filename=recorded.filename,
                    byte_count=recorded.byte_count,
                    sha256=recorded.sha256,
                    reused_existing=True,
                )
        request_options = {
            "interface": Interface.PDF,
            "accept": "application/pdf",
            "max_bytes": self.max_pdf_bytes,
        }
        if cancelled is not None:
            request_options["cancelled"] = cancelled
        response = self.client.get(
            arxiv_pdf_url(metadata.arxiv_id, version),
            **request_options,
        )
        check_cancelled()
        headers = {
            name.casefold(): value for name, value in response.headers.items()
        }
        media_type = (
            headers.get("content-type", "").partition(";")[0].strip().casefold()
        )
        if media_type != "application/pdf":
            raise DownloadError("arXiv response has an invalid PDF content type")
        body = response.body
        if len(body) > self.max_pdf_bytes:
            raise DownloadError("arXiv PDF exceeded the response size limit")
        if not body.startswith(b"%PDF-"):
            raise DownloadError("arXiv response has an invalid PDF signature")
        digest = sha256(body).hexdigest()
        base_filename = safe_pdf_filename(
            metadata.arxiv_id,
            version,
            metadata.title,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{base_filename}.",
            suffix=".tmp",
            dir=destination,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            check_cancelled()
            number = 1
            while True:
                filename = (
                    base_filename
                    if number == 1
                    else _numbered_filename(base_filename, number)
                )
                target = destination / filename
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if _file_matches(target, len(body), digest):
                        self.store.record_download_file(
                            DownloadFileRecord(
                                arxiv_id=metadata.arxiv_id,
                                version=version,
                                filename=filename,
                                byte_count=len(body),
                                sha256=digest,
                                last_verified_at=self.clock(),
                            )
                        )
                        return DownloadResult(
                            arxiv_id=metadata.arxiv_id,
                            version=version,
                            filename=filename,
                            byte_count=len(body),
                            sha256=digest,
                            reused_existing=True,
                        )
                    number += 1
                    continue
                temporary.unlink()
                temporary = None
                _fsync_directory(destination)
                break
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.store.record_download_file(
            DownloadFileRecord(
                arxiv_id=metadata.arxiv_id,
                version=version,
                filename=filename,
                byte_count=len(body),
                sha256=digest,
                last_verified_at=self.clock(),
            )
        )
        return DownloadResult(
            arxiv_id=metadata.arxiv_id,
            version=version,
            filename=filename,
            byte_count=len(body),
            sha256=digest,
            reused_existing=False,
        )
