from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from arxiv_digest.arxiv_access import ArxivCooldown, ArxivRateLimited
from arxiv_digest.curl_transport import CurlResponse, CurlTransportError, CurlUnavailable
from arxiv_digest.rate_limit import ArxivHttpClient, ArxivRequestCancelled, Interface


URL = "https://arxiv.org/catchup/cs.CL/2026-08-16?abs=True&page=1"
OAI_URL = "https://oaipmh.arxiv.org/oai?verb=GetRecord&identifier=oai%3AarXiv.org%3A2608.00001&metadataPrefix=arXivRaw"
OAI_LIST_URL = "https://oaipmh.arxiv.org/oai?verb=ListRecords&metadataPrefix=arXivRaw&set=cs%3ACL&from=2026-08-01"
OAI_NEXT_URL = "https://oaipmh.arxiv.org/oai?verb=ListRecords&resumptionToken=fixture%2Bpage%3D2"


def response(status=200, body=b"<html>complete fixture</html>", **headers):
    return CurlResponse(
        status=status,
        body=body,
        headers={"Content-Type": "text/html", **headers},
    )


def client_with_fallback(replies, *, status=406, body=b"", cooldown=None):
    elapsed = [0.0]
    calls = []

    class RefusingOpener:
        def open(self, request, timeout=None):
            calls.append(("python", request.full_url, elapsed[0], timeout))
            headers = Message()
            headers["Content-Type"] = "text/html"
            raise HTTPError(request.full_url, status, "private detail", headers, BytesIO(body))

    class Fallback:
        def get(self, url, **options):
            calls.append(("curl", url, elapsed[0], options))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    client = ArxivHttpClient(
        user_agent="fixture", contact_url="https://example.invalid/contact",
        opener=RefusingOpener(),
        monotonic=lambda: elapsed[0],
        sleeper=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
        cooldown=cooldown,
        curl_transport=Fallback(),
    )
    return client, elapsed, calls


@pytest.mark.parametrize("interface,url,media,delay", [
    (Interface.CATCHUP, URL, "text/html", 15),
    (Interface.OAI, OAI_URL, "application/xml", 3),
    (Interface.OAI, OAI_LIST_URL, "application/xml", 3),
    (Interface.OAI, OAI_NEXT_URL, "application/xml", 3),
])
def test_406_uses_one_paced_fallback_response_directly(interface, url, media, delay):
    client, _, calls = client_with_fallback([response(body=b"<fixture/>", **{"Content-Type": media})])
    result = client.get(url, interface=interface, accept=media, max_bytes=128)
    assert result.body == b"<fixture/>"
    assert result.final_url == url
    assert result.headers["content-type"] == media
    assert [(kind, target, at) for kind, target, at, _ in calls] == [
        ("python", url, 0), ("curl", url, delay),
    ]
    assert calls[-1][3]["max_bytes"] == 128
    assert calls[-1][3]["headers"]["User-agent"] == "fixture (+https://example.invalid/contact)"


@pytest.mark.parametrize("interface,url", [
    (Interface.ATOM, "https://rss.arxiv.org/atom/cs.CL"),
    (Interface.PDF, "https://arxiv.org/pdf/2608.00001"),
    (Interface.OAI, "https://oaipmh.arxiv.org/oai?verb=Identify"),
    (Interface.OAI, "https://oaipmh.arxiv.org/oai?verb=ListSets"),
    (Interface.OAI, "https://oaipmh.arxiv.org/oai?verb=ListRecords&verb=GetRecord"),
])
def test_other_requests_keep_the_original_406(interface, url):
    client, _, calls = client_with_fallback([])
    with pytest.raises(HTTPError) as caught:
        client.get(url, interface=interface, accept="text/html")
    assert caught.value.code == 406
    assert len(calls) == 1


@pytest.mark.parametrize("status,body", [(429, b""), (406, b"Rate exceeded")])
@pytest.mark.parametrize("interface,url", [(Interface.CATCHUP, URL), (Interface.OAI, OAI_LIST_URL)])
def test_rate_limits_never_launch_fallback(status, body, interface, url):
    client, _, calls = client_with_fallback([], status=status, body=body)
    with pytest.raises(ArxivRateLimited):
        client.get(url, interface=interface, accept="text/html")
    assert len(calls) == 1


@pytest.mark.parametrize("reply", [response(429), response(406, b"Rate exceeded")])
@pytest.mark.parametrize("interface,url", [(Interface.CATCHUP, URL), (Interface.OAI, OAI_LIST_URL)])
def test_fallback_rate_limit_enters_the_shared_cooldown(reply, interface, url):
    cooldown = ArxivCooldown(wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc))
    client, _, calls = client_with_fallback([reply], cooldown=cooldown)
    with pytest.raises(ArxivRateLimited) as caught:
        client.get(url, interface=interface, accept="text/html")
    assert caught.value.attempted
    with pytest.raises(ArxivRateLimited):
        client.get(url, interface=interface, accept="text/html")
    assert len(calls) == 2


@pytest.mark.parametrize("interface,url", [(Interface.CATCHUP, URL), (Interface.OAI, OAI_NEXT_URL)])
def test_fallback_406_does_not_repeat(interface, url):
    client, _, calls = client_with_fallback([response(406)])
    with pytest.raises(HTTPError) as caught:
        client.get(url, interface=interface, accept="text/html")
    assert caught.value.code == 406
    assert len(calls) == 2


def test_fallback_redirect_is_validated_and_paced_before_following():
    target = "https://arxiv.org/catchup/cs.CL/2026-08-16?abs=True&page=2"
    client, _, calls = client_with_fallback([
        response(302, **{"Location": "?abs=True&page=2"}), response(),
    ])
    result = client.get(URL, interface=Interface.CATCHUP, accept="text/html")
    assert result.final_url == target
    assert [(c[0], c[2]) for c in calls] == [("python", 0), ("curl", 15), ("curl", 30)]


@pytest.mark.parametrize("target", ["https://example.invalid/private", "http://arxiv.org/catchup", "https://arxiv.org:8443/catchup"])
def test_fallback_redirect_cannot_leave_allowed_https_endpoints(target):
    client, _, calls = client_with_fallback([response(302, **{"Location": target})])
    with pytest.raises(ValueError, match="allowed arXiv HTTPS"):
        client.get(URL, interface=Interface.CATCHUP, accept="text/html")
    assert len(calls) == 2


def test_cancellation_during_fallback_pacing_stops_before_launch():
    client, elapsed, calls = client_with_fallback([])
    with pytest.raises(ArxivRequestCancelled):
        client.get(URL, interface=Interface.CATCHUP, accept="text/html", cancelled=lambda: elapsed[0] >= 1)
    assert len(calls) == 1


@pytest.mark.parametrize("reply,limit", [
    (response(**{"Content-Type": "application/octet-stream"}), 100),
    (response(body=b"x" * 101), 100),
])
def test_fallback_keeps_content_and_size_validation(reply, limit):
    client, _, calls = client_with_fallback([reply])
    with pytest.raises(ValueError):
        client.get(URL, interface=Interface.CATCHUP, accept="text/html", max_bytes=limit)
    assert len(calls) == 2


@pytest.mark.parametrize("error", [CurlUnavailable(), CurlTransportError()])
@pytest.mark.parametrize("interface,url", [(Interface.CATCHUP, URL), (Interface.OAI, OAI_LIST_URL)])
def test_missing_or_failed_curl_preserves_original_http_status(error, interface, url):
    client, _, calls = client_with_fallback([error])
    with pytest.raises(HTTPError) as caught:
        client.get(url, interface=interface, accept="text/html")
    assert caught.value.code == 406
    assert len(calls) == 2


def test_cooldown_activated_during_fallback_wait_prevents_launch():
    cooldown = ArxivCooldown(wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc))
    client, elapsed, calls = client_with_fallback([], cooldown=cooldown)

    def sleep(seconds):
        elapsed[0] += seconds
        cooldown.record(retry_after="60", http_status=429)

    client._sleeper = sleep
    with pytest.raises(ArxivRateLimited) as caught:
        client.get(URL, interface=Interface.CATCHUP, accept="text/html")
    assert caught.value.attempted
    assert len(calls) == 1


def test_fallback_redirect_chain_stops_at_the_existing_retry_deadline():
    from dataclasses import replace
    from arxiv_digest.rate_limit import DEFAULT_POLICIES

    client, _, calls = client_with_fallback([response(302, **{"Location": URL})])
    client._policies = {Interface.CATCHUP: replace(DEFAULT_POLICIES[Interface.CATCHUP], max_total_retry_seconds=20)}
    with pytest.raises(HTTPError) as caught:
        client.get(URL, interface=Interface.CATCHUP, accept="text/html")
    assert caught.value.code == 406
    assert len(calls) == 2
    assert calls[-1][3]["timeout"] == 5


def test_fallback_does_not_start_if_pacing_would_exceed_deadline():
    from dataclasses import replace
    from arxiv_digest.rate_limit import DEFAULT_POLICIES

    client, _, calls = client_with_fallback([])
    client._policies = {Interface.CATCHUP: replace(DEFAULT_POLICIES[Interface.CATCHUP], max_total_retry_seconds=10)}
    with pytest.raises(HTTPError) as caught:
        client.get(URL, interface=Interface.CATCHUP, accept="text/html")
    assert caught.value.code == 406
    assert len(calls) == 1


@pytest.mark.parametrize("reply", [response(429), response(406, b"Rate exceeded")])
def test_received_rate_limit_is_saved_even_if_cancellation_arrives(reply):
    cooldown = ArxivCooldown(wall_clock=lambda: datetime(2026, 8, 22, tzinfo=timezone.utc))
    client, _, _ = client_with_fallback([], cooldown=cooldown)
    canceled = [False]

    def get(url, **options):
        canceled[0] = True
        return reply

    client._curl_transport = SimpleNamespace(get=get)
    with pytest.raises(ArxivRateLimited):
        client.get(URL, interface=Interface.CATCHUP, accept="text/html", cancelled=lambda: canceled[0])
    assert cooldown.active() is not None
