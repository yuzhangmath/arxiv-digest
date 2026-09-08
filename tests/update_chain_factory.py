"""External-boundary fixtures for full production update/relaunch chains.

Application wheels retain the production coordinator, helper, detector, storage
and internal commands. Release HTTP transport and desktop browser boundaries are
synthetic; bounded exception observers retain failures without changing decisions.
One explicitly broken target import exercises rollback.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

from arxiv_digest.update_contract import release_urls
from arxiv_digest.update_manifest import AutomaticUpdateFrom, build_update_manifest, serialize_update_manifest
from arxiv_digest.update_runtime import protocol


SOURCE = Path(__file__).resolve().parents[1] / "src/arxiv_digest"
API_URL = "https://api.github.com/repos/yuzhangmath/arxiv-digest/releases?per_page=100"
BOUNDARY_FUNCTIONS = {
    "update_internal.py": ("_authenticate", "_validate_package", "run_self_check", "run_relaunch"),
    "update_runtime/helper.py": ("validate_prepared_inputs", "run_installer", "internal_operation", "healthy_relaunch",
        "restore_and_relaunch", "_wait_parent_exit", "_acquire", "_recover_or_terminal"),
    "update_runtime/guard.py": ("installer_child", "run_guard"),
}
ERROR_CLASSES = frozenset({"EOFError", "TimeoutError", "OSError", "FileNotFoundError", "PermissionError", "ValueError",
    "RuntimeError", "TypeError", "KeyError", "AssertionError", "HelperError", "GuardError", "SnapshotError",
    "ProtocolError", "StoreError", "LockTimeoutError", "InstanceSecurityError", "ProcessDeathUnproven",
    "ModuleNotFoundError", "ImportError", "AttributeError", "SystemExit", "KeyboardInterrupt", "OtherError"})
FRAME_FUNCTIONS = {
    Path(name).name: frozenset(node.name for node in ast.walk(ast.parse((SOURCE / name).read_text()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))) | {"<module>"}
    for name in (*BOUNDARY_FUNCTIONS, "update_runtime/recovery.py", "update_runtime/protocol.py",
        "web/lifecycle.py", "backup.py", "application.py", "atomic.py", "update_locks.py", "maintenance.py", "storage/database.py")
}
INVENTORY_LABELS = frozenset({"venv_configuration", "activation", "python_link", "shared_libraries", "dependency", "other"})
INVENTORY_FIELDS = frozenset({"kind", "mode", "size", "sha256", "target", "presence"})
CONFIGURATION_FIELDS = frozenset({"home", "include-system-site-packages", "version", "executable", "command", "prompt", "other"})


def _fixture_inventory_changes(plan, recovery):
    """Expose only fixed member categories and changed field names."""
    old = plan["old_token"]["core"]
    current = recovery.scan_environment(plan["paths"]["environment"], allowed_external_symlinks=recovery._external_links(old))
    def retained(inventory):
        result = {}
        app_roots = {"arxiv_digest", "arxiv_digest-" + plan["old_version"] + ".dist-info",
                     "arxiv_digest-" + plan["target_version"] + ".dist-info"}
        for item in inventory["entries"]:
            parts = item["path"].split("/")
            if item["path"] in {"pipx_metadata.json", "bin/arxiv-digest"}:
                continue
            if "site-packages" in parts:
                index = parts.index("site-packages")
                if len(parts) > index + 1 and parts[index + 1] in app_roots:
                    continue
            result[item["path"]] = item
        return result
    before, after = retained(old["inventory"]), retained(current)
    changes = []
    for name in sorted(before.keys() | after.keys()):
        left, right = before.get(name), after.get(name)
        if left == right:
            continue
        if name == "pyvenv.cfg":
            label = "venv_configuration"
        elif name in {"bin/activate", "bin/activate.csh", "bin/activate.fish", "bin/Activate.ps1"}:
            label = "activation"
        elif name == "lib64" or name.startswith("bin/python"):
            label = "python_link"
        elif name.endswith("/pipx_shared.pth"):
            label = "shared_libraries"
        elif "site-packages" in name.split("/"):
            label = "dependency"
        else:
            label = "other"
        fields = ["presence"] if left is None or right is None else sorted(
            key for key in left.keys() | right.keys() if left.get(key) != right.get(key))
        change = {"member": label, "fields": fields}
        if label == "venv_configuration" and left is not None and right is not None:
            configs = []
            for root in (plan["paths"]["snapshot"], plan["paths"]["environment"]):
                payload, _ = recovery.read_owned_bytes(Path(root) / "pyvenv.cfg", limit=65536)
                configs.append(dict(line.partition("=")[::2] for line in payload.decode().splitlines() if "=" in line))
            change["configuration_fields"] = sorted({
                key.strip() if key.strip() in CONFIGURATION_FIELDS else "other"
                for key in configs[0].keys() | configs[1].keys() if configs[0].get(key) != configs[1].get(key)
            })
        if change not in changes:
            changes.append(change)
        if len(changes) == 8:
            break
    return changes


def _exception_observers(root: Path, module: str) -> str:
    """Install observers only in synthetic releases, preserving returns/raises."""
    import inspect
    support = "\nCONFIGURATION_FIELDS = " + repr(sorted(CONFIGURATION_FIELDS)) + "\n" + inspect.getsource(_fixture_inventory_changes)
    return support + '''

# Test-only structural exception observations; no exception messages or values.
def _install_fixture_exception_observers():
    import fcntl, functools, json, os, stat
    from pathlib import Path
    destination = Path(ROOT_LITERAL, "update-boundary-errors.jsonl")
    allowed_frames = FRAME_LITERAL
    allowed_errors = ERROR_LITERAL
    def write(event):
        payload = (json.dumps(event, separators=(",", ":")) + "\\n").encode()
        if len(payload) > 4096:
            return
        descriptor = os.open(destination, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                return
            if info.st_size + len(payload) > 65536:
                return
            if os.read(descriptor, 65537).count(b"\\n") >= 64:
                return
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    def record(boundary, error, args):
        frames = []
        traceback = error.__traceback__
        while traceback is not None:
            code = traceback.tb_frame.f_code
            module = Path(code.co_filename).name
            if module in allowed_frames and code.co_name in allowed_frames[module]:
                frames.append({"module": module, "function": code.co_name, "line": traceback.tb_lineno})
                frames = frames[-8:]
            traceback = traceback.tb_next
        event = {"boundary": boundary, "error": type(error).__name__ if type(error).__name__ in allowed_errors else "OtherError",
                 "frames": frames}
        if boundary == "validate_target_installation":
            try:
                event["inventory_changes"] = _fixture_inventory_changes(args[0], recovery)
            except Exception:
                pass
        write(event)
    def observe(function, boundary):
        @functools.wraps(function)
        def observed(*args, **kwargs):
            if boundary == "_recover_or_terminal":
                try:
                    current = args[1].read_snapshot()
                    state = "absent" if current is None else current.record["state"]
                    if state in STATE_LITERAL:
                        write({"boundary": boundary, "journal_state": state})
                except Exception:
                    pass
            try:
                result = function(*args, **kwargs)
            except BaseException as error:
                try:
                    record(boundary, error, args)
                except Exception:
                    pass
                raise
            if boundary == "run_installer":
                try:
                    if type(result["returncode"]) is int and -255 <= result["returncode"] <= 255 and type(result["timed_out"]) is bool:
                        write({"boundary": boundary, "returncode": result["returncode"], "timed_out": result["timed_out"]})
                except Exception:
                    pass
            return result
        return observed
    for name in BOUNDARY_LITERAL:
        globals()[name] = observe(globals()[name], name)
    if MODULE_LITERAL == "update_runtime/helper.py":
        recovery.validate_target_installation = observe(recovery.validate_target_installation, "validate_target_installation")
_install_fixture_exception_observers()
del _install_fixture_exception_observers
'''.replace("ROOT_LITERAL", repr(str(root))).replace("FRAME_LITERAL", repr({key: sorted(value) for key, value in FRAME_FUNCTIONS.items()})).replace(
        "ERROR_LITERAL", repr(sorted(ERROR_CLASSES))).replace("BOUNDARY_LITERAL", repr(BOUNDARY_FUNCTIONS[module])).replace(
        "STATE_LITERAL", repr(sorted(protocol.STATES | {"absent"}))).replace("MODULE_LITERAL", repr(module))


def boundary_error_diagnostics(root: Path):
    """Validate the complete bounded record before exposing structural labels."""
    try:
        with (root / "update-boundary-errors.jsonl").open("rb") as stream:
            payload = stream.read(65537)
        if len(payload) > 65536:
            return "over_limit"
        records = [json.loads(line) for line in payload.splitlines()]
        if len(records) > 64:
            return "over_limit"
        boundaries = {name for names in BOUNDARY_FUNCTIONS.values() for name in names} | {"validate_target_installation"}
        for record in records:
            if type(record) is dict and record.get("boundary") == "run_installer" and "returncode" in record:
                if (set(record) != {"boundary", "returncode", "timed_out"}
                        or type(record["returncode"]) is not int or not -255 <= record["returncode"] <= 255
                        or type(record["timed_out"]) is not bool):
                    return "unreadable"
                continue
            if type(record) is dict and record.get("boundary") == "_recover_or_terminal" and "journal_state" in record:
                if set(record) != {"boundary", "journal_state"} or record["journal_state"] not in protocol.STATES | {"absent"}:
                    return "unreadable"
                continue
            if type(record) is dict and "inventory_changes" in record:
                if (record.get("boundary") != "validate_target_installation" or type(record["inventory_changes"]) is not list
                        or len(record["inventory_changes"]) > 8):
                    return "unreadable"
                for change in record["inventory_changes"]:
                    if (type(change) is not dict or set(change) not in ({"member", "fields"}, {"member", "fields", "configuration_fields"})
                            or change["member"] not in INVENTORY_LABELS or type(change["fields"]) is not list
                            or not set(change["fields"]) <= INVENTORY_FIELDS):
                        return "unreadable"
                    if "configuration_fields" in change and (change["member"] != "venv_configuration"
                            or type(change["configuration_fields"]) is not list
                            or not set(change["configuration_fields"]) <= CONFIGURATION_FIELDS):
                        return "unreadable"
            if (type(record) is not dict or set(record) - {"inventory_changes"} != {"boundary", "error", "frames"}
                    or record["boundary"] not in boundaries or record["error"] not in ERROR_CLASSES
                    or type(record["frames"]) is not list or len(record["frames"]) > 8):
                return "unreadable"
            for frame in record["frames"]:
                if (type(frame) is not dict or set(frame) != {"module", "function", "line"}
                        or frame["module"] not in FRAME_FUNCTIONS or frame["function"] not in FRAME_FUNCTIONS[frame["module"]]
                        or type(frame["line"]) is not int or not 1 <= frame["line"] <= 100000):
                    return "unreadable"
        return records
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError, KeyError):
        return "unreadable"


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
    for module in BOUNDARY_FUNCTIONS:
        result["src/arxiv_digest/" + module] = (SOURCE / module).read_bytes() + _exception_observers(root, module).encode()
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
