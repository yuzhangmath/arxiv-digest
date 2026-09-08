"""Detached standard-library process guard for one fixed offline pipx install."""
from __future__ import annotations

import os
import fcntl
import hashlib
import json
import selectors
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path

if __package__:
    from . import protocol, recovery
else:
    import protocol
    import recovery


class GuardError(RuntimeError):
    pass


def require_empty_safe_trash(path):
    """Pinned pipx clears trash even with skipped maintenance; never repair it."""
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return
    if (not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid()
            or before.st_mode & 0o022):
        raise GuardError("pipx trash is unsafe")
    fd = recovery._open_directory(path)
    try:
        with os.scandir(fd) as entries:
            populated = next(entries, None) is not None
        if populated or recovery._stable(path.lstat()) != recovery._stable(before):
            raise GuardError("pipx trash is populated or changed")
    finally:
        os.close(fd)


def _installation_interpreter(plan):
    """Retain pipx's original selector without changing the verified executable.

    Pinned pipx recreates venv configuration even for ``install --force``.
    Replacing an admitted interpreter alias with its resolved path can therefore
    change nonapplication files. The old inventory authenticates the selector;
    its current resolution must still identify the exact retained interpreter.
    """
    old = plan["old_token"]["core"]
    entries = [entry for entry in old["inventory"]["entries"]
               if entry["path"] == "pipx_metadata.json" and entry["kind"] == "file"]
    if len(entries) != 1:
        raise GuardError("old installation lacks authenticated pipx metadata")
    payload, identity = recovery.read_owned_bytes(Path(plan["paths"]["environment"]) / "pipx_metadata.json")
    if entries[0] != {"kind": "file", "path": "pipx_metadata.json", "mode": identity["mode"],
                      "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}:
        raise GuardError("original pipx metadata changed")
    try:
        metadata = json.loads(payload)
        wire = metadata["source_interpreter"]
        if type(wire) is not dict or set(wire) != {"__type__", "__Path__"} or wire["__type__"] != "Path":
            raise GuardError("original interpreter selector is invalid")
        selector = Path(protocol.validate_path(wire["__Path__"]))
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise GuardError("original interpreter selector is invalid") from error

    parent_before = selector.parent.lstat()
    recovery._check(parent_before, kind="directory", allow_root=True)
    parent = os.open(selector.parent, recovery._flags(True))
    try:
        if recovery._stable(os.fstat(parent)) != recovery._stable(parent_before):
            raise GuardError("interpreter selector parent changed")
        before = os.stat(selector.name, dir_fd=parent, follow_symlinks=False)
        is_link = stat.S_ISLNK(before.st_mode)
        recovery._check(before, kind="symlink" if is_link else "file", allow_root=True)
        target = os.readlink(selector.name, dir_fd=parent) if is_link else None
        resolved = selector.resolve(strict=True)
        if (str(resolved) != plan["paths"]["base_interpreter"]
                or recovery.capture_executable_identity(resolved) != old["interpreter"]):
            raise GuardError("original interpreter selector changed executable")
        if (selector.resolve(strict=True) != resolved
                or recovery._stable(os.stat(selector.name, dir_fd=parent, follow_symlinks=False)) != recovery._stable(before)
                or (is_link and os.readlink(selector.name, dir_fd=parent) != target)
                or recovery._stable(selector.parent.lstat()) != recovery._stable(parent_before)
                or recovery._stable(os.fstat(parent)) != recovery._stable(parent_before)):
            raise GuardError("original interpreter selector changed during validation")
    finally:
        os.close(parent)
    return str(selector)


def install_argv(plan):
    return (plan["paths"]["pipx"], "install", "--force", "--app", "arxiv-digest",
            "--python", _installation_interpreter(plan), "--fetch-python=never",
            "--skip-maintenance", "--backend=pip", "--pip-args=--no-deps --no-index",
            plan["target_wheel"]["path"])


def install_environment(plan):
    paths = plan["paths"]
    private = Path(plan["target_wheel"]["path"]).parent / "pipx-run"
    protocol.ensure_private_directory_strict(private)
    names = ("home", "config", "data", "cache", "state", "logs", "tmp", "uv")
    for name in names:
        protocol.ensure_private_directory_strict(private / name)
    return {
        "HOME": str(private / "home"), "XDG_CONFIG_HOME": str(private / "config"),
        "XDG_DATA_HOME": str(private / "data"), "XDG_CACHE_HOME": str(private / "cache"),
        "XDG_STATE_HOME": str(private / "state"), "TMPDIR": str(private / "tmp"),
        "TMP": str(private / "tmp"), "TEMP": str(private / "tmp"),
        "PATH": os.pathsep.join(dict.fromkeys((str(Path(paths["base_interpreter"]).parent), str(Path(paths["pipx"]).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"))),
        "PIPX_HOME": paths["pipx_home"], "PIPX_BIN_DIR": paths["pipx_bin_dir"],
        "PIPX_SHARED_LIBS": paths["pipx_shared_libs"], "PIPX_MAN_DIR": paths["pipx_man_dir"],
        "PIPX_COMPLETION_DIR": paths["pipx_completion_dir"], "PIPX_LOG_DIR": str(private / "logs"),
        "PIPX_DEFAULT_BACKEND": "pip", "PIPX_FETCH_PYTHON": "never", "PIPX_USE_EMOJI": "0",
        "PIPX_DISABLE_SHARED_LIBS_AUTO_UPGRADE": "1", "PIPX_MAX_LOGS": "10",
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_NO_INPUT": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", "UV_NO_INDEX": "1", "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never", "UV_CACHE_DIR": str(private / "uv"), "LANG": "C.UTF-8",
    }


def process_group_dead(pgid):
    if type(pgid) is not int or pgid <= 0:
        raise GuardError("invalid process group")
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def verify_exclusive_reference(descriptor, path, identity):
    if recovery._identity(os.fstat(descriptor)) != identity:
        raise GuardError("coordination descriptor identity differs")
    probe = protocol.open_private_lock_file(Path(path))
    try:
        if recovery._identity(os.fstat(probe)) != identity:
            raise GuardError("coordination path identity differs")
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        raise GuardError("coordination ownership is not exclusive")
    finally:
        os.close(probe)


def _open_log(path):
    parent = recovery._open_directory(path.parent, private=True)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=parent)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > protocol.PRIVATE_LOG_BYTE_LIMIT
                or recovery._stable(os.stat(path.name, dir_fd=parent, follow_symlinks=False)) != recovery._stable(info)):
            raise GuardError("unsafe fixed diagnostic log")
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
        os.fsync(parent)
        result, descriptor = descriptor, None
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def terminate_and_reap(process, *, grace=protocol.TERM_GRACE_SECONDS):
    """Return only after the child is reaped and its entire group is absent."""
    pgid = process.pid
    if not process_group_dead(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        process.poll()
        if process_group_dead(pgid):
            process.wait()
            return True
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=max(1.0, grace))
    deadline = time.monotonic() + max(1.0, grace)
    while time.monotonic() < deadline:
        if process_group_dead(pgid):
            return True
        time.sleep(0.05)
    return False


def _message(attempt, nonce, kind, **fields):
    return {"schema_version": 1, "attempt_id": attempt, "nonce": nonce, "kind": kind, **fields}


def _assert_message(message, attempt, nonce, kind):
    if message["attempt_id"] != attempt or message["nonce"] != nonce or message["kind"] != kind:
        raise GuardError("guard channel authentication failed")


def installer_child(options, plan):
    """A dormant process group gets durable journal identity before execve."""
    channel = protocol.ControlChannel(socket.socket(fileno=options.control_fd))
    message = channel.receive(time.monotonic() + protocol.READY_DEADLINE_SECONDS)
    _assert_message(message, options.attempt_id, options.nonce, "COMMIT")
    channel.close()
    if recovery.capture_executable_identity(plan["paths"]["pipx"]) != plan["pipx_identity"]:
        raise GuardError("pipx executable changed before execution")
    provenance = protocol.ProtectedProvenanceStore(plan["paths"]["recovery_root"]).read_snapshot()
    reference = None if provenance is None else {"identity": provenance.identity, "sha256": provenance.sha256}
    recovery.verify_installation_token(plan["old_token"], provenance=reference)
    require_empty_safe_trash(Path(plan["paths"]["pipx_home"]) / ".trash")
    # close_fds at spawn admitted only this socket; now no coordination/control
    # descriptor survives into pipx or its descendants.
    argv = install_argv(plan)
    os.execve(argv[0], argv, install_environment(plan))


def run_guard(options, plan_snapshot):
    plan = plan_snapshot.record
    channel = protocol.ControlChannel(socket.socket(fileno=options.control_fd))
    boot = channel.receive(time.monotonic() + protocol.READY_DEADLINE_SECONDS)
    _assert_message(boot, options.attempt_id, options.nonce, "GUARD_BOOT")
    if boot["plan_sha256"] != plan_snapshot.sha256:
        raise GuardError("guard plan identity differs")
    transition_fd = boot["transition_fd"]
    metadata = os.fstat(transition_fd)
    if recovery._identity(metadata) != plan["lock_identities"]["transition"]:
        raise GuardError("guard transition descriptor differs")
    verify_exclusive_reference(transition_fd, Path(plan["paths"]["recovery_root"]) / "update-transition.lock", plan["lock_identities"]["transition"])
    os.set_inheritable(transition_fd, False)
    require_empty_safe_trash(Path(plan["paths"]["pipx_home"]) / ".trash")
    log_path = Path(plan["paths"]["diagnostic_log"])
    log_fd = _open_log(log_path)
    parent_sock, child_sock = socket.socketpair()
    argv = (plan["paths"]["base_interpreter"], "-I", "-B",
            str(Path(plan["paths"]["recovery_root"]) / "runtime/recovery.py"), "--installer-child",
            "--recovery-root", plan["paths"]["recovery_root"], "--attempt-id", options.attempt_id,
            "--nonce", options.nonce, "--control-fd", str(child_sock.fileno()))
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, cwd=plan["paths"]["recovery_root"],
                               env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
                               close_fds=True, pass_fds=(child_sock.fileno(),), start_new_session=True)
    child_sock.close()
    child_channel = protocol.ControlChannel(parent_sock)
    timed_out = False
    try:
        store = protocol.JournalStore(plan["paths"]["recovery_root"])
        current = store.read_snapshot()
        if current.record["state"] != "committed" or current.record["attempt_id"] != options.attempt_id:
            raise GuardError("guard lacks installation authorization")
        installer = {"guard_pid": os.getpid(), "process_group_id": process.pid}
        authorization = protocol.InstallerAuthorization(current.sha256, options.attempt_id, installer)
        current = store.transition(current, {**current.record, "state": "installing", "installer": installer}, authorization=authorization)
        child_channel.send(_message(options.attempt_id, options.nonce, "COMMIT"), time.monotonic() + protocol.READY_DEADLINE_SECONDS)
        child_channel.close()
        channel.send(_message(options.attempt_id, options.nonce, "GUARD_STARTED", pid=process.pid, pgid=process.pid), time.monotonic() + protocol.READY_DEADLINE_SECONDS)
        written = 0
        deadline = time.monotonic() + protocol.PIPX_COMMAND_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(channel.socket, selectors.EVENT_READ, "control")
            selector.register(process.stdout, selectors.EVENT_READ, "output")
            while process.poll() is None or any(key.data == "output" for key in selector.get_map().values()):
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                for key, _ in selector.select(min(0.1, deadline - time.monotonic())):
                    if key.data == "control":
                        try:
                            cancel = channel.receive(min(deadline, time.monotonic() + 1))
                            _assert_message(cancel, options.attempt_id, options.nonce, "GUARD_CANCEL")
                        except (EOFError, OSError, TimeoutError):
                            pass
                        timed_out = True
                        break
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        selector.unregister(process.stdout)
                    elif written < protocol.PRIVATE_LOG_BYTE_LIMIT:
                        chunk = chunk[:protocol.PRIVATE_LOG_BYTE_LIMIT - written]
                        view = memoryview(chunk)
                        while view:
                            count = os.write(log_fd, view)
                            if count <= 0:
                                raise GuardError("short private log write")
                            written += count
                            view = view[count:]
                if timed_out:
                    break
        dead = terminate_and_reap(process) if timed_out or not process_group_dead(process.pid) else True
        process.wait()
        os.fsync(log_fd)
        recovery.fsync_directory(log_path.parent)
        log_bytes, log_identity = recovery.read_owned_bytes(log_path, limit=protocol.PRIVATE_LOG_BYTE_LIMIT)
        if log_identity != recovery._file_identity(os.fstat(log_fd)):
            raise GuardError("guard diagnostic log identity changed")
        import hashlib
        log_reference = {"identity": log_identity, "sha256": hashlib.sha256(log_bytes).hexdigest()}
        if not dead:
            # Keep the borrowed transition reference while any descendant is
            # still unproven. A manual launch cannot mutate underneath it.
            while not process_group_dead(process.pid):
                time.sleep(1)
        channel.send(_message(options.attempt_id, options.nonce, "GUARD_RESULT",
                    returncode=process.returncode, process_group_id=process.pid,
                    processes_dead=True, timed_out=timed_out, log=log_reference), time.monotonic() + 5)
        return 0
    finally:
        child_channel.close()
        if process.poll() is None or not process_group_dead(process.pid):
            if not terminate_and_reap(process):
                while not process_group_dead(process.pid):
                    time.sleep(1)
        if log_fd is not None:
            os.close(log_fd)
        channel.close()
        os.close(transition_fd)  # borrowed descriptor: never LOCK_UN
