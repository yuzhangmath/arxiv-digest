"""Exact command-line surface for arXiv Digest."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol


class CliApplication(Protocol):
    def open_dashboard(self, intent: str) -> int: ...

    def doctor(self) -> int: ...

    def export_backup(self, destination: Path) -> int: ...

    def import_backup(self, source: Path) -> int: ...

    def install_launcher(self) -> int: ...


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arxiv-digest", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("init", allow_abbrev=False)
    commands.add_parser("library", allow_abbrev=False)
    commands.add_parser("config", allow_abbrev=False)
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
    arguments = _parser().parse_args(argv)
    if application_factory is None:
        from arxiv_digest.application import create_application

        application_factory = create_application
    application = application_factory()
    if arguments.command is None:
        return application.open_dashboard("default")
    if arguments.command in {"init", "library", "config"}:
        return application.open_dashboard(arguments.command)
    if arguments.command == "doctor":
        return application.doctor()
    if arguments.command == "export":
        return application.export_backup(arguments.file)
    if arguments.command == "import":
        return application.import_backup(arguments.file)
    if arguments.command == "install-launcher":
        return application.install_launcher()
    raise AssertionError("argparse accepted an unsupported command")
