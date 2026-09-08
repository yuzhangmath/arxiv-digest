"""External-boundary fixtures for full production update/relaunch chains.

Application wheels retain the production coordinator, helper, detector, storage
and internal commands. Only the release HTTP transport, desktop browser boundary
and one explicitly broken target import differ in these synthetic releases.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from arxiv_digest.update_contract import release_urls
from arxiv_digest.update_manifest import AutomaticUpdateFrom, build_update_manifest, serialize_update_manifest


SOURCE = Path(__file__).resolve().parents[1] / "src/arxiv_digest"
API_URL = "https://api.github.com/repos/yuzhangmath/arxiv-digest/releases?per_page=100"


def source_overrides(root: Path, version: str, *, broken_target: bool = False) -> dict[str, bytes]:
    """Inject deterministic I/O at external boundaries, never updater decisions."""
    transport = '''

# Test-only canonical release transport: preserve every production parser.
def _safe_open_url(request, *, timeout):
    import io, json
    from email.message import Message
    from pathlib import Path
    root = Path(ROOT_LITERAL)
    url = request.full_url
    routes = json.loads((root / "release-routes.json").read_text())
    route = routes[url]
    payload = Path(route["path"]).read_bytes()
    with (root / "release-requests.jsonl").open("a") as log:
        log.write(json.dumps(url) + "\\n")
    class Response(io.BytesIO):
        status = 200
        def geturl(self):
            return url
    response = Response(payload)
    response.headers = Message()
    response.headers["Content-Type"] = route["content_type"]
    response.headers["Content-Length"] = str(len(payload))
    return response
'''.replace("ROOT_LITERAL", repr(str(root)))
    browser = '''

# Test-only desktop boundary. The real server has already passed health checks.
def open_browser(url):
    import json, os
    from pathlib import Path
    from arxiv_digest import __version__
    with Path(ROOT_LITERAL, "browser-launches.jsonl").open("a") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "version": __version__, "url": url}) + "\\n")
    return True
'''.replace("ROOT_LITERAL", repr(str(root)))
    # An application import can never turn a fixture into a live arXiv client.
    network_guard = '''
import sys as _test_sys
def _test_network_audit(event, args):
    if event in {"socket.getaddrinfo", "socket.gethostbyname"} and args[0] not in {"127.0.0.1", "::1", "localhost"}:
        raise OSError("non-loopback DNS disabled in update chain")
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise OSError("non-loopback network disabled in update chain")
_test_sys.addaudithook(_test_network_audit)
'''
    result = {
        "src/arxiv_digest/__init__.py": (f'__version__ = "{version}"\n' + network_guard).encode(),
        "src/arxiv_digest/update_http.py": SOURCE.joinpath("update_http.py").read_bytes() + transport.encode(),
        "src/arxiv_digest/browser.py": SOURCE.joinpath("browser.py").read_bytes() + browser.encode(),
    }
    if broken_target:
        # The wheel itself is well-formed. The real authenticated target
        # self-check reaches this import failure after the real pipx install.
        payload = SOURCE.joinpath("application.py").read_text()
        marker = "from __future__ import annotations\n"
        assert marker in payload
        result["src/arxiv_digest/application.py"] = payload.replace(
            marker, marker + '\nraise RuntimeError("synthetic second-target import failure")\n', 1,
        ).encode()
    return result


def publish_releases(root: Path, wheels: dict[str, Path], *, visible: tuple[str, ...]) -> None:
    """Serve exact real-wheel manifests behind canonical GitHub URL fixtures."""
    release_root = root / "release-payloads"
    release_root.mkdir(exist_ok=True)
    routes, records = {}, []
    for version, wheel in wheels.items():
        enabled = version != "0.3.0"
        manifest = build_update_manifest(
            wheel, version=version, channel="prerelease", automatic_update=enabled,
            automatic_update_from=AutomaticUpdateFrom("0.3.0", version, ()) if enabled else None,
        )
        manifest_path = release_root / f"manifest-{version}.json"
        manifest_path.write_bytes(serialize_update_manifest(manifest))
        base = release_urls(version)["wheel_prefix"]
        assets = []
        for name, path, content_type in (
            (wheel.name, wheel, "application/octet-stream"),
            ("UPDATE_MANIFEST.json", manifest_path, "application/json"),
        ):
            payload = path.read_bytes()
            routes[base + name] = {"path": str(path), "content_type": content_type}
            assets.append({"name": name, "size": len(payload), "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                           "browser_download_url": base + name, "state": "uploaded"})
        for name in (f"arxiv_digest-{version}.tar.gz", "SHA256SUMS"):
            assets.append({"name": name, "size": 1, "digest": "sha256:" + "a" * 64,
                           "browser_download_url": base + name, "state": "uploaded"})
        if version in visible:
            records.append({"tag_name": "v" + version, "draft": False, "prerelease": True, "assets": assets})
    listing = release_root / "releases.json"
    listing.write_text(json.dumps(records))
    routes[API_URL] = {"path": str(listing), "content_type": "application/json"}
    (root / "release-routes.json").write_text(json.dumps(routes))
