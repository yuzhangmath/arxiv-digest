"""Exact command-line surface for arXiv Digest."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol



class CliApplication(Protocol):
    def open_dashboard(
        self,
        intent: str,
        *,
        copy_url: bool = False,
    ) -> int: ...

    def doctor(self) -> int: ...

    def export_backup(self, destination: Path) -> int: ...

    def import_backup(self, source: Path) -> int: ...

    def install_launcher(self) -> int: ...



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
) -> int:
    parser = _parser()
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if arguments.copy_url and arguments.command not in {
        None, "init", "library", "config"
    }:
        parser.error("--copy-url is only available when opening the dashboard")
    if application_factory is None:
        from arxiv_digest.application import create_application

        application_factory = create_application
    application = application_factory()
    if arguments.command in {None, "init", "library", "config"}:
        return application.open_dashboard(
            arguments.command or "default", copy_url=arguments.copy_url
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
