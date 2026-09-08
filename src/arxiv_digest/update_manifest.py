"""Closed update-manifest codecs and wheel inspection."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import struct
import zipfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from pathlib import Path
from typing import Literal

from packaging.requirements import InvalidRequirement, Requirement

from arxiv_digest.update_contract import (
    APPLICATION_DATA_GENERATION,
    CENTRAL_DIRECTORY_METADATA_BYTE_LIMIT,
    MANIFEST_BYTE_LIMIT,
    MEMBER_COMPRESSION_RATIO_LIMIT,
    METADATA_MEMBER_BYTE_LIMIT,
    PRODUCT,
    SCHEMA_VERSION,
    SUPPORTED_PLATFORMS,
    UPDATER_PROTOCOL,
    WHEEL_BYTE_LIMIT,
    canonical_version,
)


_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_STRUCT = struct.Struct("<4s4H2LH")
_LOCAL_FILE_HEADER_SIGNATURE = b"PK\x03\x04"
_LOCAL_FILE_HEADER_SIZE = 30
_CENTRAL_FILE_HEADER_SIGNATURE = b"PK\x01\x02"
_CENTRAL_FILE_HEADER_SIZE = 46
_MANIFEST_KEYS = frozenset(
    {
        "application_data_generation",
        "automatic_update",
        "automatic_update_from",
        "channel",
        "platforms",
        "product",
        "python",
        "runtime_requirements_sha256",
        "schema_version",
        "updater_protocol",
        "version",
        "wheel",
    }
)
_PYTHON_POLICY_KEYS = frozenset({"maximum_exclusive", "minimum"})
_WHEEL_IDENTITY_KEYS = frozenset({"name", "sha256", "size"})
_AUTOMATIC_UPDATE_FROM_KEYS = frozenset(
    {"excluded", "maximum_exclusive", "minimum"}
)
_PYTHON_MINOR_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


class ManifestError(ValueError):
    """An update manifest or wheel violates the closed release contract."""


@dataclass(frozen=True, slots=True)
class PythonPolicy:
    minimum: str
    maximum_exclusive: str | None


@dataclass(frozen=True, slots=True)
class WheelIdentity:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AutomaticUpdateFrom:
    minimum: str
    maximum_exclusive: str
    excluded: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UpdateManifest:
    schema_version: int
    product: str
    version: str
    channel: Literal["prerelease", "stable"]
    updater_protocol: int
    application_data_generation: int
    automatic_update: bool
    automatic_update_from: AutomaticUpdateFrom | None
    platforms: tuple[str, ...]
    python: PythonPolicy
    runtime_requirements_sha256: str
    wheel: WheelIdentity


@dataclass(frozen=True, slots=True)
class WheelInspection:
    distribution: str
    version: str
    python: PythonPolicy
    runtime_requirements_sha256: str
    wheel: WheelIdentity


def runtime_requirements_sha256(values: Iterable[str]) -> str:
    """Hash schema-1 Core Metadata ``Requires-Dist`` values."""

    normalized: list[bytes] = []
    for raw in values:
        if type(raw) is not str:
            raise ManifestError("runtime requirement is invalid")
        value = raw.strip(" \t")
        if value == "" or "\n" in value or "\r" in value or "\0" in value:
            raise ManifestError("runtime requirement is invalid")
        try:
            Requirement(value)
        except InvalidRequirement as error:
            raise ManifestError("runtime requirement is invalid") from error
        normalized.append(value.encode("utf-8") + b"\n")
    return hashlib.sha256(b"".join(sorted(normalized))).hexdigest()


def _record_hash(payload: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(payload).digest()
    ).rstrip(b"=").decode("ascii")


def _is_canonical_record_hash(value: str) -> bool:
    if re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", value) is None:
        return False
    encoded = value.removeprefix("sha256=").encode("ascii")
    decoded = base64.urlsafe_b64decode(encoded + b"=")
    return (
        len(decoded) == hashlib.sha256().digest_size
        and base64.urlsafe_b64encode(decoded).rstrip(b"=") == encoded
    )


def _preflight_central_directory(wheel_file: object, wheel_size: int) -> None:
    tail_size = min(wheel_size, 65_535 + _EOCD_STRUCT.size)
    wheel_file.seek(wheel_size - tail_size)  # type: ignore[attr-defined]
    tail = wheel_file.read(tail_size)  # type: ignore[attr-defined]
    position = tail.rfind(_EOCD_SIGNATURE)
    if position < 0 or position + _EOCD_STRUCT.size > len(tail):
        raise ManifestError("wheel ZIP structure is invalid")
    end_record = _EOCD_STRUCT.unpack_from(tail, position)
    central_size = end_record[5]
    comment_size = end_record[7]
    if (
        end_record[3] == 0xFFFF
        or end_record[4] == 0xFFFF
        or central_size == 0xFFFFFFFF
        or end_record[6] == 0xFFFFFFFF
    ):
        raise ManifestError("wheel ZIP64 is unsupported")
    if end_record[1] != 0 or end_record[2] != 0 or end_record[3] != end_record[4]:
        raise ManifestError("wheel ZIP multidisk is unsupported")
    end_offset = wheel_size - tail_size + position
    if end_record[6] + central_size != end_offset:
        raise ManifestError("wheel ZIP structure is invalid")
    if position + _EOCD_STRUCT.size + comment_size != len(tail):
        raise ManifestError("wheel ZIP structure is invalid")
    if central_size > CENTRAL_DIRECTORY_METADATA_BYTE_LIMIT:
        raise ManifestError("wheel central directory exceeds byte limit")
    wheel_file.seek(end_record[6])  # type: ignore[attr-defined]
    central_directory = wheel_file.read(central_size)  # type: ignore[attr-defined]
    if len(central_directory) != central_size:
        raise ManifestError("wheel ZIP structure is invalid")
    cursor = 0
    entry_count = 0
    while cursor < central_size:
        if (
            cursor + _CENTRAL_FILE_HEADER_SIZE > central_size
            or central_directory[cursor : cursor + 4]
            != _CENTRAL_FILE_HEADER_SIGNATURE
        ):
            raise ManifestError("wheel ZIP structure is invalid")
        compressed_size = struct.unpack_from("<I", central_directory, cursor + 20)[0]
        uncompressed_size = struct.unpack_from(
            "<I", central_directory, cursor + 24
        )[0]
        name_size, extra_size, member_comment_size = struct.unpack_from(
            "<3H", central_directory, cursor + 28
        )
        disk_start = struct.unpack_from("<H", central_directory, cursor + 34)[0]
        local_offset = struct.unpack_from("<I", central_directory, cursor + 42)[0]
        entry_end = (
            cursor
            + _CENTRAL_FILE_HEADER_SIZE
            + name_size
            + extra_size
            + member_comment_size
        )
        if entry_end > central_size:
            raise ManifestError("wheel ZIP structure is invalid")
        extra_start = cursor + _CENTRAL_FILE_HEADER_SIZE + name_size
        extra_end = extra_start + extra_size
        extra_cursor = extra_start
        has_zip64_extra = False
        while extra_cursor < extra_end:
            if extra_cursor + 4 > extra_end:
                raise ManifestError("wheel ZIP structure is invalid")
            extra_id, payload_size = struct.unpack_from(
                "<2H", central_directory, extra_cursor
            )
            extra_cursor += 4
            if extra_cursor + payload_size > extra_end:
                raise ManifestError("wheel ZIP structure is invalid")
            has_zip64_extra = has_zip64_extra or extra_id == 0x0001
            extra_cursor += payload_size
        if (
            compressed_size == 0xFFFFFFFF
            or uncompressed_size == 0xFFFFFFFF
            or local_offset == 0xFFFFFFFF
            or disk_start == 0xFFFF
            or has_zip64_extra
        ):
            raise ManifestError("wheel ZIP64 is unsupported")
        if disk_start != 0:
            raise ManifestError("wheel ZIP multidisk is unsupported")
        cursor = entry_end
        entry_count += 1
    if cursor != central_size or entry_count != end_record[4]:
        raise ManifestError("wheel ZIP structure is invalid")


@contextmanager
def _open_wheel_archive(wheel_file: object) -> Iterator[zipfile.ZipFile]:
    try:
        with zipfile.ZipFile(wheel_file, "r") as archive:
            yield archive
    except ManifestError:
        raise
    except (
        csv.Error,
        KeyError,
        MessageError,
        NotImplementedError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
        zipfile.BadZipFile,
    ) as error:
        raise ManifestError("wheel ZIP structure is invalid") from error


def inspect_update_wheel(
    path: Path,
    *,
    expected_version: str | None = None,
) -> WheelInspection:
    """Inspect the bounded metadata of a universal update wheel."""

    wheel_path = Path(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(wheel_path, flags)
    except OSError as error:
        raise ManifestError("wheel must be a regular file") from error
    try:
        return inspect_update_wheel_descriptor(
            descriptor, filename=wheel_path.name, expected_version=expected_version,
        )
    finally:
        os.close(descriptor)


def inspect_update_wheel_descriptor(
    descriptor: int,
    *,
    filename: str,
    expected_version: str | None = None,
) -> WheelInspection:
    """Inspect the caller's open wheel identity without reopening or closing it."""

    if type(descriptor) is not int or descriptor < 0:
        raise ManifestError("wheel descriptor is invalid")
    if type(filename) is not str or Path(filename).name != filename:
        raise ManifestError("wheel filename is invalid")
    wheel_path = Path(filename)
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(descriptor, "rb", closefd=False) as wheel_file:
        file_stat = os.fstat(wheel_file.fileno())
        if not stat.S_ISREG(file_stat.st_mode):
            raise ManifestError("wheel must be a regular file")
        if file_stat.st_size > WHEEL_BYTE_LIMIT:
            raise ManifestError("wheel exceeds byte limit")
        wheel_digest = hashlib.sha256()
        remaining = file_stat.st_size
        while remaining:
            chunk = wheel_file.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ManifestError("wheel changed while being inspected")
            wheel_digest.update(chunk)
            remaining -= len(chunk)
        if wheel_file.read(1):
            raise ManifestError("wheel changed while being inspected")
        _preflight_central_directory(wheel_file, file_stat.st_size)
        wheel_file.seek(0)
        with _open_wheel_archive(wheel_file) as archive:
            infos = archive.infolist()
            if any(not stat.S_ISREG(info.external_attr >> 16) for info in infos):
                raise ManifestError("wheel member type is invalid")
            if any(
                info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                for info in infos
            ):
                raise ManifestError("wheel compression is unsupported")
            for info in infos:
                if (
                    info.header_offset < 0
                    or info.header_offset + _LOCAL_FILE_HEADER_SIZE > file_stat.st_size
                ):
                    raise ManifestError("wheel ZIP structure is invalid")
                wheel_file.seek(info.header_offset)
                local_header = wheel_file.read(_LOCAL_FILE_HEADER_SIZE)
                if (
                    len(local_header) != _LOCAL_FILE_HEADER_SIZE
                    or local_header[:4] != _LOCAL_FILE_HEADER_SIGNATURE
                ):
                    raise ManifestError("wheel ZIP structure is invalid")
                local_flags, local_compression = struct.unpack_from(
                    "<2H", local_header, 6
                )
                local_crc, local_compressed_size, local_file_size = struct.unpack_from(
                    "<3I", local_header, 14
                )
                local_name_size, local_extra_size = struct.unpack_from(
                    "<2H", local_header, 26
                )
                local_metadata_end = (
                    info.header_offset
                    + _LOCAL_FILE_HEADER_SIZE
                    + local_name_size
                    + local_extra_size
                )
                if (
                    local_metadata_end + info.compress_size > file_stat.st_size
                    or local_flags & 0x1
                ):
                    if local_flags & 0x1:
                        raise ManifestError("wheel member is encrypted")
                    raise ManifestError("wheel ZIP structure is invalid")
                wheel_file.seek(info.header_offset + _LOCAL_FILE_HEADER_SIZE)
                local_name = wheel_file.read(local_name_size)
                local_extra = wheel_file.read(local_extra_size)
                filename_encoding = "utf-8" if local_flags & 0x800 else "cp437"
                if (
                    local_flags != info.flag_bits
                    or local_compression != info.compress_type
                    or local_name != info.orig_filename.encode(filename_encoding)
                    or len(local_extra) != local_extra_size
                ):
                    raise ManifestError("wheel ZIP structure is invalid")
                if not local_flags & 0x8 and (
                    local_crc != info.CRC
                    or local_compressed_size != info.compress_size
                    or local_file_size != info.file_size
                ):
                    raise ManifestError("wheel ZIP structure is invalid")
                local_extra_cursor = 0
                while local_extra_cursor < len(local_extra):
                    if local_extra_cursor + 4 > len(local_extra):
                        raise ManifestError("wheel ZIP structure is invalid")
                    extra_id, extra_payload_size = struct.unpack_from(
                        "<2H", local_extra, local_extra_cursor
                    )
                    local_extra_cursor += 4
                    if local_extra_cursor + extra_payload_size > len(local_extra):
                        raise ManifestError("wheel ZIP structure is invalid")
                    if extra_id == 0x0001:
                        raise ManifestError("wheel ZIP64 is unsupported")
                    local_extra_cursor += extra_payload_size
            if any(info.flag_bits & 0x1 for info in infos):
                raise ManifestError("wheel member is encrypted")
            if any(
                info.file_size
                > max(1, info.compress_size) * MEMBER_COMPRESSION_RATIO_LIMIT
                for info in infos
            ):
                raise ManifestError("wheel member compression is excessive")
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ManifestError("wheel has duplicate members")
            if any(
                name.startswith("/")
                or "\\" in name
                or "" in name.split("/")
                or "." in name.split("/")
                or ".." in name.split("/")
                for name in names
            ):
                raise ManifestError("wheel member path is unsafe")
            dist_info_roots = {
                name.split("/", 1)[0]
                for name in names
                if name.split("/", 1)[0].endswith(".dist-info")
            }
            if len(dist_info_roots) != 1:
                raise ManifestError("wheel dist-info tree is invalid")
            dist_info_root = next(iter(dist_info_roots))
            metadata_name = f"{dist_info_root}/METADATA"
            wheel_metadata_name = f"{dist_info_root}/WHEEL"
            record_name = f"{dist_info_root}/RECORD"
            if not {metadata_name, wheel_metadata_name, record_name}.issubset(names):
                raise ManifestError("wheel metadata members are incomplete")
            if archive.getinfo(metadata_name).file_size > METADATA_MEMBER_BYTE_LIMIT:
                raise ManifestError("wheel metadata member is too large")
            metadata_payload = archive.read(metadata_name)
            metadata = BytesParser(policy=policy.default).parsebytes(metadata_payload)
            if metadata.defects:
                raise ManifestError("core metadata is malformed")
            if len(metadata.get_all("Name", [])) != 1 or len(
                metadata.get_all("Version", [])
            ) != 1 or len(metadata.get_all("Requires-Python", [])) != 1 or len(
                metadata.get_all("Metadata-Version", [])
            ) != 1:
                raise ManifestError("core metadata is ambiguous")
            if (
                archive.getinfo(wheel_metadata_name).file_size
                > METADATA_MEMBER_BYTE_LIMIT
            ):
                raise ManifestError("wheel metadata member is too large")
            wheel_metadata_payload = archive.read(wheel_metadata_name)
            wheel_metadata = BytesParser(policy=policy.default).parsebytes(
                wheel_metadata_payload
            )
            if wheel_metadata.defects:
                raise ManifestError("wheel metadata is malformed")
            if [
                str(value) for value in wheel_metadata.get_all("Wheel-Version", [])
            ] != ["1.0"]:
                raise ManifestError("wheel version is unsupported")
            if [str(tag) for tag in wheel_metadata.get_all("Tag", [])] != [
                "py3-none-any"
            ]:
                raise ManifestError("wheel tag is unsupported")
            if [
                str(value)
                for value in wheel_metadata.get_all("Root-Is-Purelib", [])
            ] != ["true"]:
                raise ManifestError("wheel is not purelib")
            if archive.getinfo(record_name).file_size > METADATA_MEMBER_BYTE_LIMIT:
                raise ManifestError("wheel metadata member is too large")
            record_rows = list(
                csv.reader(
                    io.StringIO(
                        archive.read(record_name).decode("utf-8"),
                        newline="",
                    ),
                    strict=True,
                )
            )
            if any(len(row) != 3 for row in record_rows):
                raise ManifestError("wheel RECORD is incomplete")
            record_paths = [row[0] for row in record_rows]
            if len(record_paths) != len(set(record_paths)):
                raise ManifestError("wheel RECORD has duplicate paths")
            if len(record_rows) != len(names) or set(record_paths) != set(names):
                raise ManifestError("wheel RECORD is incomplete")
            record_values = {row[0]: (row[1], row[2]) for row in record_rows}
            if record_values[record_name] != ("", ""):
                raise ManifestError("wheel RECORD self entry is invalid")
            if any(
                not _is_canonical_record_hash(digest)
                for name, (digest, _size) in record_values.items()
                if name != record_name
            ):
                raise ManifestError("wheel RECORD entry is invalid")
            member_sizes = {info.filename: info.file_size for info in infos}
            if any(
                size != str(member_sizes[name])
                for name, (_digest, size) in record_values.items()
                if name != record_name
            ):
                raise ManifestError("wheel RECORD size does not match")
            if record_values[metadata_name][0] != _record_hash(metadata_payload):
                raise ManifestError("wheel RECORD hash does not match")
            if record_values[metadata_name][1] != str(len(metadata_payload)):
                raise ManifestError("wheel RECORD size does not match")
            if record_values[wheel_metadata_name][0] != _record_hash(
                wheel_metadata_payload
            ):
                raise ManifestError("wheel RECORD hash does not match")
            if record_values[wheel_metadata_name][1] != str(
                len(wheel_metadata_payload)
            ):
                raise ManifestError("wheel RECORD size does not match")
        final_stat = os.fstat(wheel_file.fileno())
        if (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_mode,
            final_stat.st_size,
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        ) != (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_mode,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            file_stat.st_ctime_ns,
        ):
            raise ManifestError("wheel changed while being inspected")
    distribution = str(metadata["Name"])
    if distribution != PRODUCT:
        raise ManifestError("wheel distribution does not match")
    python_policy = python_policy_from_requires_python(str(metadata["Requires-Python"]))
    version = str(metadata["Version"])
    try:
        canonical_version(version)
    except ValueError as error:
        raise ManifestError("wheel version is not canonical") from error
    if expected_version is not None and version != expected_version:
        raise ManifestError("wheel version does not match")
    if dist_info_root != f"arxiv_digest-{version}.dist-info":
        raise ManifestError("wheel dist-info name does not match")
    if wheel_path.name != f"arxiv_digest-{version}-py3-none-any.whl":
        raise ManifestError("wheel filename is invalid")
    return WheelInspection(
        distribution=distribution,
        version=version,
        python=python_policy,
        runtime_requirements_sha256=runtime_requirements_sha256(
            [str(value) for value in metadata.get_all("Requires-Dist", [])]
        ),
        wheel=WheelIdentity(
            wheel_path.name,
            file_stat.st_size,
            wheel_digest.hexdigest(),
        ),
    )


def python_policy_from_requires_python(requires_python: str) -> PythonPolicy:
    """Parse the one Python-policy grammar shared by wheels and live metadata."""

    if type(requires_python) is not str:
        raise ManifestError("wheel Python policy is unsupported")
    python_version = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    lower_first = re.fullmatch(
        rf">=(?P<minimum>{python_version})(?:,<(?P<maximum>{python_version}))?",
        requires_python,
    )
    upper_first = re.fullmatch(
        rf"<(?P<maximum>{python_version}),>=(?P<minimum>{python_version})",
        requires_python,
    )
    match = lower_first or upper_first
    if match is None:
        raise ManifestError("wheel Python policy is unsupported")
    minimum_python = match.group("minimum")
    maximum_python = match.group("maximum")
    minimum_python_parts = _python_minor(minimum_python)
    maximum_python_parts = (
        None if maximum_python is None else _python_minor(maximum_python)
    )
    if minimum_python_parts is None or (
        maximum_python is not None
        and (
            maximum_python_parts is None
            or maximum_python_parts <= minimum_python_parts
        )
    ):
        raise ManifestError("wheel Python policy is unsupported")
    return PythonPolicy(minimum_python, maximum_python)


def _manifest_value(manifest: UpdateManifest) -> dict[str, object]:
    source = manifest.automatic_update_from
    return {
        "application_data_generation": manifest.application_data_generation,
        "automatic_update": manifest.automatic_update,
        "automatic_update_from": (
            None
            if source is None
            else {
                "excluded": list(source.excluded),
                "maximum_exclusive": source.maximum_exclusive,
                "minimum": source.minimum,
            }
        ),
        "channel": manifest.channel,
        "platforms": list(manifest.platforms),
        "product": manifest.product,
        "python": {
            "maximum_exclusive": manifest.python.maximum_exclusive,
            "minimum": manifest.python.minimum,
        },
        "runtime_requirements_sha256": manifest.runtime_requirements_sha256,
        "schema_version": manifest.schema_version,
        "updater_protocol": manifest.updater_protocol,
        "version": manifest.version,
        "wheel": {
            "name": manifest.wheel.name,
            "sha256": manifest.wheel.sha256,
            "size": manifest.wheel.size,
        },
    }


def _python_minor(value: object) -> tuple[int, int] | None:
    if type(value) is not str or _PYTHON_MINOR_PATTERN.fullmatch(value) is None:
        return None
    major, minor = value.split(".")
    try:
        return int(major), int(minor)
    except ValueError:
        return None


def _validate_manifest(manifest: UpdateManifest) -> None:
    if (
        type(manifest) is not UpdateManifest
        or type(manifest.schema_version) is not int
        or manifest.schema_version != SCHEMA_VERSION
        or type(manifest.product) is not str
        or manifest.product != PRODUCT
        or type(manifest.version) is not str
        or type(manifest.channel) is not str
        or manifest.channel not in {"prerelease", "stable"}
        or type(manifest.updater_protocol) is not int
        or manifest.updater_protocol != UPDATER_PROTOCOL
        or type(manifest.application_data_generation) is not int
        or manifest.application_data_generation != APPLICATION_DATA_GENERATION
        or type(manifest.automatic_update) is not bool
        or type(manifest.platforms) is not tuple
        or not manifest.platforms
        or any(type(platform) is not str for platform in manifest.platforms)
        or manifest.platforms != tuple(sorted(manifest.platforms))
        or len(manifest.platforms) != len(set(manifest.platforms))
        or not set(manifest.platforms).issubset(SUPPORTED_PLATFORMS)
        or type(manifest.runtime_requirements_sha256) is not str
        or re.fullmatch(
            r"[0-9a-f]{64}", manifest.runtime_requirements_sha256
        ) is None
    ):
        raise ManifestError("manifest schema is invalid")
    try:
        canonical_version(manifest.version)
    except ValueError as error:
        raise ManifestError("manifest schema is invalid") from error
    if type(manifest.python) is not PythonPolicy:
        raise ManifestError("manifest schema is invalid")
    minimum_python = _python_minor(manifest.python.minimum)
    maximum_python = (
        None
        if manifest.python.maximum_exclusive is None
        else _python_minor(manifest.python.maximum_exclusive)
    )
    if (
        minimum_python is None
        or (
            manifest.python.maximum_exclusive is not None
            and (maximum_python is None or maximum_python <= minimum_python)
        )
    ):
        raise ManifestError("manifest schema is invalid")
    if (
        type(manifest.wheel) is not WheelIdentity
        or type(manifest.wheel.name) is not str
        or manifest.wheel.name
        != f"arxiv_digest-{manifest.version}-py3-none-any.whl"
        or type(manifest.wheel.size) is not int
        or not 1 <= manifest.wheel.size <= WHEEL_BYTE_LIMIT
        or type(manifest.wheel.sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", manifest.wheel.sha256) is None
    ):
        raise ManifestError("manifest schema is invalid")
    if manifest.automatic_update != (manifest.automatic_update_from is not None):
        raise ManifestError("manifest schema is invalid")
    source = manifest.automatic_update_from
    if source is None:
        return
    if (
        type(source) is not AutomaticUpdateFrom
        or type(source.minimum) is not str
        or type(source.maximum_exclusive) is not str
        or type(source.excluded) is not tuple
        or any(type(version) is not str for version in source.excluded)
        or len(source.excluded) != len(set(source.excluded))
    ):
        raise ManifestError("manifest schema is invalid")
    try:
        minimum = canonical_version(source.minimum)
        maximum = canonical_version(source.maximum_exclusive)
        target = canonical_version(manifest.version)
        exclusions = tuple(canonical_version(version) for version in source.excluded)
    except ValueError as error:
        raise ManifestError("manifest schema is invalid") from error
    if (
        not minimum < maximum <= target
        or exclusions != tuple(sorted(exclusions))
        or any(not minimum <= excluded < maximum for excluded in exclusions)
    ):
        raise ManifestError("manifest schema is invalid")


def serialize_update_manifest(manifest: UpdateManifest) -> bytes:
    """Return the canonical schema-1 JSON representation."""

    _validate_manifest(manifest)
    payload = (
        json.dumps(_manifest_value(manifest), separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    if len(payload) > MANIFEST_BYTE_LIMIT:
        raise ManifestError("manifest exceeds byte limit")
    return payload


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, member in pairs:
        if key in value:
            raise ManifestError("manifest has duplicate keys")
        value[key] = member
    return value


def _reject_json_constant(_value: str) -> object:
    raise ManifestError("manifest JSON is invalid")


def parse_update_manifest(payload: bytes) -> UpdateManifest:
    """Decode a schema-1 manifest."""

    if type(payload) is not bytes:
        raise ManifestError("manifest payload must be bytes")
    if len(payload) > MANIFEST_BYTE_LIMIT:
        raise ManifestError("manifest exceeds byte limit")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except ManifestError:
        raise
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise ManifestError("manifest JSON is invalid") from error
    if type(value) is not dict or value.keys() != _MANIFEST_KEYS:
        raise ManifestError("manifest schema is invalid")
    if type(value["platforms"]) is not list:
        raise ManifestError("manifest schema is invalid")
    source_value = value["automatic_update_from"]
    if source_value is not None and (
        type(source_value) is not dict
        or source_value.keys() != _AUTOMATIC_UPDATE_FROM_KEYS
        or type(source_value["excluded"]) is not list
    ):
        raise ManifestError("manifest schema is invalid")
    source = (
        None
        if source_value is None
        else AutomaticUpdateFrom(
            source_value["minimum"],
            source_value["maximum_exclusive"],
            tuple(source_value["excluded"]),
        )
    )
    python_value = value["python"]
    wheel_value = value["wheel"]
    if (
        type(python_value) is not dict
        or python_value.keys() != _PYTHON_POLICY_KEYS
        or type(wheel_value) is not dict
        or wheel_value.keys() != _WHEEL_IDENTITY_KEYS
    ):
        raise ManifestError("manifest schema is invalid")
    manifest = UpdateManifest(
        schema_version=value["schema_version"],
        product=value["product"],
        version=value["version"],
        channel=value["channel"],
        updater_protocol=value["updater_protocol"],
        application_data_generation=value["application_data_generation"],
        automatic_update=value["automatic_update"],
        automatic_update_from=source,
        platforms=tuple(value["platforms"]),
        python=PythonPolicy(
            python_value["minimum"], python_value["maximum_exclusive"]
        ),
        runtime_requirements_sha256=value["runtime_requirements_sha256"],
        wheel=WheelIdentity(
            wheel_value["name"], wheel_value["size"], wheel_value["sha256"]
        ),
    )
    _validate_manifest(manifest)
    if serialize_update_manifest(manifest) != payload:
        raise ManifestError("manifest encoding is not canonical")
    return manifest


def build_update_manifest(
    path: Path,
    *,
    version: str,
    channel: Literal["prerelease", "stable"],
    automatic_update: bool,
    automatic_update_from: AutomaticUpdateFrom | None,
) -> UpdateManifest:
    """Build a schema-1 manifest for an already-built wheel."""

    inspection = inspect_update_wheel(path, expected_version=version)
    manifest = UpdateManifest(
        schema_version=SCHEMA_VERSION,
        product=PRODUCT,
        version=version,
        channel=channel,
        updater_protocol=UPDATER_PROTOCOL,
        application_data_generation=APPLICATION_DATA_GENERATION,
        automatic_update=automatic_update,
        automatic_update_from=automatic_update_from,
        platforms=SUPPORTED_PLATFORMS,
        python=inspection.python,
        runtime_requirements_sha256=inspection.runtime_requirements_sha256,
        wheel=inspection.wheel,
    )
    _validate_manifest(manifest)
    return manifest
