"""Deterministic synthetic wheels shared by updater tests."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import stat
import zipfile
from pathlib import Path


def record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return "sha256=" + encoded.rstrip(b"=").decode("ascii")


def default_metadata(version: str) -> bytes:
    return (
        "Metadata-Version: 2.4\n"
        "Name: arxiv-digest\n"
        f"Version: {version}\n"
        "Requires-Python: >=3.11\n"
        "Requires-Dist: beautifulsoup4<5,>=4.12\n"
        "Requires-Dist: packaging<27,>=24\n"
        "\n"
    ).encode()


def default_wheel_metadata() -> bytes:
    return (
        b"Wheel-Version: 1.0\n"
        b"Generator: arxiv-digest-tests\n"
        b"Root-Is-Purelib: true\n"
        b"Tag: py3-none-any\n"
        b"\n"
    )


def regular_zip_info(
    name: str,
    *,
    compression: int = zipfile.ZIP_STORED,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = compression
    return info


def write_valid_wheel(
    path: Path,
    *,
    version: str = "0.3.0",
    dist_info_name: str | None = None,
    metadata_payload: bytes | None = None,
    metadata_padding: int = 0,
    wheel_payload: bytes | None = None,
    extra_record_rows: tuple[tuple[str, str, str], ...] = (),
    record_overrides: dict[str, tuple[str, str]] | None = None,
    record_self: tuple[str, str] = ("", ""),
    record_padding: int = 0,
    omit_members: frozenset[str] = frozenset(),
) -> Path:
    distribution = "arxiv_digest"
    dist_info = dist_info_name or f"{distribution}-{version}.dist-info"
    members = {
        "arxiv_digest/__init__.py": f'__version__ = "{version}"\n'.encode(),
        f"{dist_info}/METADATA": (
            metadata_payload
            if metadata_payload is not None
            else default_metadata(version) + b"x" * metadata_padding
        ),
        f"{dist_info}/WHEEL": (
            wheel_payload
            if wheel_payload is not None
            else default_wheel_metadata()
        ),
    }
    members = {
        name: payload
        for name, payload in members.items()
        if name not in omit_members
    }
    record_buffer = io.StringIO(newline="")
    writer = csv.writer(record_buffer, lineterminator="\n")
    for name, payload in members.items():
        digest, size = (
            (record_digest(payload), str(len(payload)))
            if record_overrides is None or name not in record_overrides
            else record_overrides[name]
        )
        writer.writerow((name, digest, size))
    for row in extra_record_rows:
        writer.writerow(row)
    record_name = f"{dist_info}/RECORD"
    writer.writerow((record_name, *record_self))
    if record_name not in omit_members:
        members[record_name] = (
            record_buffer.getvalue().encode("utf-8") + b"x" * record_padding
        )

    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(regular_zip_info(name), payload)
    return path
