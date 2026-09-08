from __future__ import annotations

import io
import os
from dataclasses import replace
from email.message import Message
from pathlib import Path

import pytest

from tests.unit.test_update_discovery import _eligible_bundle
from tests.update_wheel_factory import write_valid_wheel


class Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str, *, status=200, length=None, media="application/octet-stream"):
        super().__init__(payload)
        self.status = status
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = media
        self.headers["Content-Length"] = str(len(payload)) if length is None else length

    def geturl(self):
        return self.url


@pytest.fixture
def release(tmp_path):
    from arxiv_digest.update_discovery import UpdateDescriptor
    installed, target, installation = _eligible_bundle(tmp_path)
    path = write_valid_wheel(tmp_path / target.wheel_asset.name, version=target.version)
    assert path.stat().st_size == target.wheel_asset.size
    directory = tmp_path / "attempt"
    directory.mkdir(mode=0o700)
    return UpdateDescriptor(installed, target, installation), path.read_bytes(), directory


def test_target_only_download_is_inspected_through_the_created_read_write_descriptor(release, monkeypatch):
    import arxiv_digest.update_download as download
    descriptor, payload, directory = release
    opened = []
    inspected = []
    real_inspect = download.inspect_update_wheel_descriptor

    def inspect(fd, **kwargs):
        assert os.read(fd, 2) == b"PK"
        os.lseek(fd, 0, os.SEEK_SET)
        inspected.append(os.fstat(fd).st_ino)
        return real_inspect(fd, **kwargs)

    monkeypatch.setattr(download, "inspect_update_wheel_descriptor", inspect)

    def opener(request, **_kwargs):
        opened.append(request.full_url)
        return Response(payload, request.full_url)

    result = download.download_target_wheel(descriptor, directory, open_url=opener)
    assert opened == [descriptor.target.wheel_asset.url]
    assert result.path.read_bytes() == payload
    assert result.inspection.wheel == descriptor.target.manifest.wheel
    assert inspected == [result.path.stat().st_ino]
    assert result.path.stat().st_mode & 0o777 == 0o600
    from arxiv_digest.update_manifest import serialize_update_manifest
    manifest = directory / "UPDATE_MANIFEST.json"
    assert manifest.read_bytes() == serialize_update_manifest(descriptor.target.manifest)
    assert manifest.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("options", [
    {"status": 206}, {"status": 404}, {"length": "01"}, {"length": "+1"},
    {"length": "0"}, {"length": "999999999999999999999999"},
    {"media": "text/html"}, {"media": "application/json"},
])
def test_hostile_headers_are_rejected_before_file_creation(release, options):
    from arxiv_digest.update_download import DownloadError, download_target_wheel
    descriptor, payload, directory = release
    with pytest.raises(DownloadError):
        download_target_wheel(descriptor, directory, open_url=lambda request, **kw: Response(payload, request.full_url, **options))
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("mutation", [lambda p: p[:-1], lambda p: p+b"extra", lambda p: b"X"+p[1:]])
def test_short_overlong_and_tampered_transfers_remove_only_the_created_file(release, mutation):
    from arxiv_digest.update_download import DownloadError, download_target_wheel
    descriptor, payload, directory = release
    with pytest.raises(DownloadError):
        download_target_wheel(descriptor, directory, open_url=lambda request, **kw: Response(mutation(payload), request.full_url, length=str(len(payload))))
    assert list(directory.iterdir()) == []


def test_preexisting_path_and_symlink_are_never_overwritten(release):
    from arxiv_digest.update_download import DownloadError, download_target_wheel
    descriptor, payload, directory = release
    target = directory / descriptor.target.wheel_asset.name
    sentinel = directory / "sentinel"
    sentinel.write_bytes(b"preserve")
    target.symlink_to(sentinel)
    with pytest.raises(DownloadError):
        download_target_wheel(descriptor, directory, open_url=lambda request, **kw: Response(payload, request.full_url))
    assert target.is_symlink() and sentinel.read_bytes() == b"preserve"


def test_cleanup_preserves_a_replacement_inode(release, monkeypatch):
    import arxiv_digest.update_download as download
    descriptor, payload, directory = release
    target = directory / descriptor.target.wheel_asset.name

    def inspect(fd, **kwargs):
        assert os.read(fd, 2) == b"PK"
        target.unlink()
        target.write_bytes(b"external replacement")
        raise ValueError("synthetic inspection fault")

    monkeypatch.setattr(download, "inspect_update_wheel_descriptor", inspect)
    with pytest.raises(download.DownloadError):
        download.download_target_wheel(descriptor, directory, open_url=lambda request, **kw: Response(payload, request.full_url))
    assert target.read_bytes() == b"external replacement"


def test_expiry_and_cancellation_are_bounded_and_cleanup(release):
    from arxiv_digest.update_download import DownloadError, download_target_wheel
    descriptor, payload, directory = release
    for kwargs in ({"deadline_at": 0, "monotonic": lambda: 1}, {"cancelled": lambda: True}):
        with pytest.raises(DownloadError):
            download_target_wheel(descriptor, directory, open_url=lambda request, **kw: Response(payload, request.full_url), **kwargs)
        assert list(directory.iterdir()) == []


def test_descriptor_inspection_uses_open_identity_not_a_replaced_path(tmp_path):
    from arxiv_digest.update_manifest import inspect_update_wheel_descriptor
    path = write_valid_wheel(tmp_path / "arxiv_digest-0.3.0-py3-none-any.whl")
    fd = os.open(path, os.O_RDONLY)
    try:
        path.unlink()
        path.write_bytes(b"not a wheel")
        assert inspect_update_wheel_descriptor(fd, filename=path.name, expected_version="0.3.0").version == "0.3.0"
        os.fstat(fd)  # Inspection borrows and does not close its caller's descriptor.
    finally:
        os.close(fd)
