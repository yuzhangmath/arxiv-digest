from __future__ import annotations

import io
from email.message import Message

import pytest


class _Response(io.BytesIO):
    def __init__(
        self,
        payload: bytes,
        *,
        url: str,
        status: int,
        location: str | None = None,
    ) -> None:
        super().__init__(payload)
        self.status = status
        self._url = url
        self.headers = Message()
        if location is not None:
            self.headers["Location"] = location

    def geturl(self) -> str:
        return self._url


def test_release_asset_transport_follows_each_validated_hop_manually() -> None:
    from arxiv_digest.update_http import open_release_asset

    initial = (
        "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
        "v0.3.1/UPDATE_MANIFEST.json"
    )
    redirected = (
        "https://release-assets.githubusercontent.com/github-production-"
        "release-asset/123/manifest?sp=r&sig=abc"
    )
    first = _Response(b"", url=initial, status=302, location=redirected)
    final = _Response(b"manifest", url=redirected, status=200)
    responses = iter((first, final))
    opened: list[tuple[str, float]] = []

    def open_url(request: object, *, timeout: float) -> _Response:
        opened.append((request.full_url, timeout))  # type: ignore[attr-defined]
        return next(responses)

    response = open_release_asset(
        initial,
        open_url=open_url,
        deadline_at=12.0,
        monotonic=lambda: 10.0,
    )

    assert response is final
    assert opened == [(initial, 2.0), (redirected, 2.0)]
    assert first.closed is True
    assert final.closed is False
    final.close()


def test_release_asset_transport_rejects_a_hidden_automatic_redirect() -> None:
    from arxiv_digest.update_http import (
        ReleaseAssetTransportError,
        open_release_asset,
    )

    initial = (
        "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
        "v0.3.1/UPDATE_MANIFEST.json"
    )
    hidden = _Response(
        b"manifest",
        url=(
            "https://release-assets.githubusercontent.com/asset/manifest"
            "?sig=hidden"
        ),
        status=200,
    )

    try:
        open_release_asset(
            initial,
            open_url=lambda request, *, timeout: hidden,
        )
    except ReleaseAssetTransportError as error:
        assert "hidden redirect" in str(error)
    else:
        raise AssertionError("hidden redirect was accepted")

    assert hidden.closed is True


def test_release_asset_transport_stops_after_five_redirects() -> None:
    from arxiv_digest.update_http import (
        ReleaseAssetTransportError,
        open_release_asset,
    )

    initial = (
        "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
        "v0.3.1/UPDATE_MANIFEST.json"
    )
    locations = [
        f"https://release-assets.githubusercontent.com/asset/{number}?sig=x"
        for number in range(1, 7)
    ]
    responses = iter(
        _Response(
            b"",
            url=(initial if index == 0 else locations[index - 1]),
            status=302,
            location=locations[index],
        )
        for index in range(6)
    )
    opened: list[str] = []

    def open_url(request: object, *, timeout: float) -> _Response:
        del timeout
        opened.append(request.full_url)  # type: ignore[attr-defined]
        return next(responses)

    with pytest.raises(ReleaseAssetTransportError, match="redirect limit"):
        open_release_asset(initial, open_url=open_url)

    assert opened == [initial, *locations[:5]]


def test_release_asset_transport_requires_a_literal_lowercase_host() -> None:
    from arxiv_digest.update_http import (
        ReleaseAssetTransportError,
        open_release_asset,
    )

    opened: list[object] = []
    with pytest.raises(ReleaseAssetTransportError, match="invalid"):
        open_release_asset(
            "https://GITHUB.COM/yuzhangmath/arxiv-digest/releases/download/"
            "v0.3.1/UPDATE_MANIFEST.json",
            open_url=lambda request, *, timeout: opened.append(request),
        )

    assert opened == []


def test_release_asset_transport_requires_exact_unstripped_initial_url() -> None:
    from arxiv_digest.update_http import (
        ReleaseAssetTransportError,
        open_release_asset,
    )

    opened: list[object] = []
    with pytest.raises(ReleaseAssetTransportError, match="invalid"):
        open_release_asset(
            "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
            "v0.3.1/UPDATE_MANIFEST.json\n",
            open_url=lambda request, *, timeout: opened.append(request),
        )

    assert opened == []


def test_release_asset_transport_rejects_control_characters_in_redirect() -> None:
    from arxiv_digest.update_http import (
        ReleaseAssetTransportError,
        open_release_asset,
    )

    initial = (
        "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
        "v0.3.1/UPDATE_MANIFEST.json"
    )
    first = _Response(
        b"",
        url=initial,
        status=302,
        location=(
            "https://release-assets.githubusercontent.com/asset/manifest"
            "?sig=x\n"
        ),
    )
    opened: list[object] = []

    with pytest.raises(ReleaseAssetTransportError, match="invalid"):
        open_release_asset(
            initial,
            open_url=lambda request, *, timeout: (
                opened.append(request) or first
            ),
        )

    assert len(opened) == 1
    assert first.closed is True


def test_release_asset_transport_closes_a_response_when_inspection_raises() -> None:
    from arxiv_digest.update_http import open_release_asset

    initial = (
        "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
        "v0.3.1/UPDATE_MANIFEST.json"
    )

    class ExplodingResponse:
        def __init__(self, accessor: str) -> None:
            self.accessor = accessor
            self.closed = False
            self.headers = Message()

        @property
        def status(self) -> int:
            if self.accessor == "status":
                raise RuntimeError("status failed")
            return 200

        def geturl(self) -> str:
            if self.accessor == "geturl":
                raise RuntimeError("geturl failed")
            return initial

        def close(self) -> None:
            self.closed = True

    for accessor in ("geturl", "status"):
        response = ExplodingResponse(accessor)
        with pytest.raises(RuntimeError, match=f"{accessor} failed"):
            open_release_asset(
                initial,
                open_url=lambda request, *, timeout: response,
            )
        assert response.closed is True


def test_release_asset_transport_rejects_a_response_after_the_deadline() -> None:
    from arxiv_digest.update_http import open_release_asset

    initial = (
        "https://github.com/yuzhangmath/arxiv-digest/releases/download/"
        "v0.3.1/UPDATE_MANIFEST.json"
    )
    response = _Response(b"manifest", url=initial, status=200)
    now = [0.0]

    def open_url(request: object, *, timeout: float) -> _Response:
        del request, timeout
        now[0] = 2.0
        return response

    with pytest.raises(TimeoutError, match="deadline"):
        open_release_asset(
            initial,
            open_url=open_url,
            deadline_at=1.0,
            monotonic=lambda: now[0],
        )

    assert response.closed is True
