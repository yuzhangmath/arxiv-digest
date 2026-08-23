from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest


_TEST_ROOT = Path(
    tempfile.mkdtemp(prefix="arxiv-digest-tests-")
).resolve()
os.environ["ARXIV_DIGEST_TESTING"] = "1"
os.environ["ARXIV_DIGEST_TEST_ROOT"] = str(_TEST_ROOT)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@pytest.fixture(autouse=True)
def deny_non_loopback_network(monkeypatch: pytest.MonkeyPatch) -> None:
    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex

    def normalized_host(host: object) -> str:
        return str(host).casefold()

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object):
        normalized = normalized_host(host)
        if normalized not in _LOOPBACK_HOSTS:
            raise RuntimeError(f"external DNS disabled in tests: {normalized}")
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_gethostbyname(host: object) -> str:
        normalized = normalized_host(host)
        if normalized not in _LOOPBACK_HOSTS:
            raise RuntimeError(f"external DNS disabled in tests: {normalized}")
        return original_gethostbyname(host)

    def guarded_gethostbyname_ex(host: object):
        normalized = normalized_host(host)
        if normalized not in _LOOPBACK_HOSTS:
            raise RuntimeError(f"external DNS disabled in tests: {normalized}")
        return original_gethostbyname_ex(host)

    def guarded_connect(sock: socket.socket, address: object) -> object:
        if not isinstance(address, tuple):
            return original_connect(sock, address)
        host = normalized_host(address[0])
        if host not in _LOOPBACK_HOSTS:
            raise RuntimeError(f"external network disabled in tests: {host}")
        return original_connect(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", guarded_gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", guarded_gethostbyname_ex)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
