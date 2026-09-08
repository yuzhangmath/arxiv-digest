"""Exact command-line surface for arXiv Digest."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class CliApplication(Protocol):
    def open_dashboard(
        self,
        intent: str,
        *,
        instance_resolved: Callable[[], None],
        copy_url: bool = False,
    ) -> int: ...

    def doctor(self) -> int: ...

    def export_backup(self, destination: Path) -> int: ...

    def import_backup(self, source: Path) -> int: ...

    def install_launcher(self) -> int: ...


class PreflightDisposition(StrEnum):
    """Startup result after validating the authoritative protected journal."""

    ALLOW = "allow"
    RECOVER = "recover"
    BLOCK = "block"


class _PreflightPaths(Protocol):
    update_transition_lock_path: Path
    update_journal_path: Path
    recovery_wrapper_path: Path

    def ensure_update_coordination(self) -> None: ...


def _default_journal_classifier(path: Path) -> PreflightDisposition:
    from arxiv_digest.update_runtime.protocol import classify_journal

    result = classify_journal(path.parent)
    if result == "allow":
        return PreflightDisposition.ALLOW
    if result == "recover":
        from arxiv_digest.update_internal import recovery_runtime_is_verified
        if recovery_runtime_is_verified(path.parent):
            return PreflightDisposition.RECOVER
    return PreflightDisposition.BLOCK


def _default_internal_dispatch(argv: Sequence[str]) -> int | None:
    from arxiv_digest.update_internal import dispatch_internal
    return dispatch_internal(argv)


def _exec_recovery(wrapper: Path) -> int:
    import subprocess
    from arxiv_digest.update_internal import recovery_runtime_is_verified
    if not recovery_runtime_is_verified(wrapper.parent):
        return 3
    return subprocess.run((str(wrapper),), stdin=subprocess.DEVNULL, close_fds=True, check=False).returncode


def _report_doctor_recovery(
    disposition: PreflightDisposition, output: Callable[[str], None],
) -> int:
    from arxiv_digest import __version__

    state = "pending" if disposition is PreflightDisposition.RECOVER else "blocked"
    output(
        f"arXiv Digest {__version__}\n"
        f"Update recovery: {state}\n"
        "No recovery was attempted. Follow the update recovery instructions "
        "in the troubleshooting guide."
    )
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arxiv-digest", allow_abbrev=False)
    copy_help = "copy the dashboard URL instead of opening a browser"
    parser.add_argument("--copy-url", action="store_true", help=copy_help)
    commands = parser.add_subparsers(dest="command")
    for command in ("init", "library", "config"):
        dashboard = commands.add_parser(command, allow_abbrev=False)
        dashboard.add_argument(
            "--copy-url",
            action="store_true",
            default=argparse.SUPPRESS,
            help=copy_help,
        )
    commands.add_parser("doctor", allow_abbrev=False)
    export = commands.add_parser("export", allow_abbrev=False)
    export.add_argument("file", type=Path)
    restore = commands.add_parser("import", allow_abbrev=False)
    restore.add_argument("file", type=Path)
    commands.add_parser("install-launcher", allow_abbrev=False)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    application_factory: Callable[[], CliApplication] | None = None,
    paths_factory: Callable[[], _PreflightPaths] | None = None,
    journal_classifier: Callable[[Path], PreflightDisposition] = (
        _default_journal_classifier
    ),
    transition_acquire: Callable[[Path], Any] | None = None,
    recovery_executor: Callable[[Path], int] = _exec_recovery,
    internal_dispatch: Callable[[Sequence[str]], int | None] = (
        _default_internal_dispatch
    ),
    output: Callable[[str], None] = print,
) -> int:
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    internal_result = internal_dispatch(raw_argv)
    if internal_result is not None:
        return internal_result
    parser = _parser()
    arguments = parser.parse_args(raw_argv)
    if arguments.copy_url and arguments.command not in {
        None, "init", "library", "config"
    }:
        parser.error("--copy-url is only available when opening the dashboard")

    if paths_factory is None:
        from arxiv_digest.paths import resolve_paths

        paths_factory = resolve_paths
    paths = paths_factory()
    if transition_acquire is None:
        from arxiv_digest.update_contract import LOCK_WAIT_TIMEOUT_SECONDS
        from arxiv_digest.update_locks import acquire_shared

        transition_acquire = lambda path: acquire_shared(
            path,
            timeout=LOCK_WAIT_TIMEOUT_SECONDS,
        )
    try:
        paths.ensure_update_coordination()
        transition = transition_acquire(paths.update_transition_lock_path)
    except TimeoutError:
        output("An arXiv Digest update is in progress. Try again shortly.")
        return 3
    except OSError:
        if arguments.command == "doctor":
            return _report_doctor_recovery(PreflightDisposition.BLOCK, output)
        raise

    def release_transition() -> None:
        nonlocal transition
        if transition is None:
            return
        held, transition = transition, None
        held.release()

    try:
        disposition = journal_classifier(paths.update_journal_path)
        if arguments.command == "doctor" and disposition is not PreflightDisposition.ALLOW:
            # Diagnostics must not dispatch recovery or open potentially
            # interrupted application data. Keep every value here allowlisted.
            return _report_doctor_recovery(disposition, output)
        if disposition is PreflightDisposition.RECOVER:
            release_transition()
            result = recovery_executor(paths.recovery_wrapper_path)
            if result == 0:
                if journal_classifier(paths.update_journal_path) is not PreflightDisposition.ALLOW:
                    output("arXiv Digest recovery did not establish safe ordinary startup.")
                    return 3
                # Recovery obtains exclusive ownership only after the ordinary
                # shared reference closes. A successful fixed entry is followed
                # by a fresh ordinary preflight and normal instance resolution.
                return main(raw_argv, application_factory=application_factory,
                    paths_factory=paths_factory, journal_classifier=journal_classifier,
                    transition_acquire=transition_acquire, recovery_executor=recovery_executor,
                    internal_dispatch=internal_dispatch, output=output)
            return result
        if disposition is not PreflightDisposition.ALLOW:
            output(
                "arXiv Digest found update recovery state that cannot be "
                "continued safely. Run the fixed recovery command: "
                f"{paths.recovery_wrapper_path}"
            )
            return 3
        if application_factory is None:
            from arxiv_digest.application import create_application

            application_factory = lambda: create_application(paths=paths)
        application = application_factory()
        if arguments.command is None:
            return application.open_dashboard(
                "default",
                instance_resolved=release_transition,
                copy_url=arguments.copy_url,
            )
        if arguments.command in {"init", "library", "config"}:
            return application.open_dashboard(
                arguments.command,
                instance_resolved=release_transition,
                copy_url=arguments.copy_url,
            )
        if arguments.command == "doctor":
            return application.doctor()
        if arguments.command == "export":
            return application.export_backup(arguments.file)
        if arguments.command == "import":
            return application.import_backup(arguments.file)
        if arguments.command == "install-launcher":
            return application.install_launcher()
        raise AssertionError("argparse accepted an unsupported command")
    finally:
        release_transition()
