from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from arxiv_digest.profile import PdfDestination, Profile


class DestinationKind(StrEnum):
    DOWNLOADS = "downloads"
    DOCUMENTS = "documents"
    CUSTOM = "custom"


class PickerStatus(StrEnum):
    SELECTED = "selected"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class OpenStatus(StrEnum):
    OPENED = "opened"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FolderChoice:
    kind: DestinationKind
    path: Path
    display_name: str


@dataclass(frozen=True, slots=True)
class FolderPickerResult:
    status: PickerStatus
    choice: FolderChoice | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class OpenFolderResult:
    status: OpenStatus
    message: str | None = None


class FolderValidationError(OSError):
    pass


class FolderService:
    def __init__(
        self,
        *,
        platform: str | None = None,
        home: Path | None = None,
        executable_lookup: Callable[[str], str | None] = shutil.which,
        process_runner: Callable[..., subprocess.CompletedProcess[str]] = (
            subprocess.run
        ),
    ) -> None:
        self.platform = platform or sys.platform
        self.home = Path.home() if home is None else Path(home)
        self.executable_lookup = executable_lookup
        self.process_runner = process_runner

    def standard_choices(self) -> tuple[FolderChoice, FolderChoice]:
        return (
            FolderChoice(
                DestinationKind.DOWNLOADS,
                self.home / "Downloads" / "Arxiv Digest",
                "Downloads / Arxiv Digest",
            ),
            FolderChoice(
                DestinationKind.DOCUMENTS,
                self.home / "Documents" / "Arxiv Digest",
                "Documents / Arxiv Digest",
            ),
        )

    def pick_custom(self) -> FolderPickerResult:
        if self.platform.startswith("linux"):
            executable = self.executable_lookup("zenity")
            if executable is not None:
                arguments = [
                    executable,
                    "--file-selection",
                    "--directory",
                    "--title",
                    "Choose PDF destination",
                ]
            else:
                executable = self.executable_lookup("kdialog")
                if executable is None:
                    return FolderPickerResult(
                        PickerStatus.UNAVAILABLE,
                        message=(
                            "No native folder picker is available; Downloads "
                            "and Documents remain available."
                        ),
                    )
                arguments = [
                    executable,
                    "--getexistingdirectory",
                    str(self.home),
                    "--title",
                    "Choose PDF destination",
                ]
        elif self.platform == "darwin":
            executable = self.executable_lookup("osascript")
            if executable is None:
                return FolderPickerResult(
                    PickerStatus.UNAVAILABLE,
                    message="The native folder picker is unavailable.",
                )
            arguments = [
                executable,
                "-e",
                (
                    'try\nPOSIX path of (choose folder with prompt "Choose PDF '
                    'destination")\non error number -128\nreturn ""\nend try'
                ),
            ]
        else:
            return FolderPickerResult(
                PickerStatus.UNAVAILABLE,
                message="The native folder picker is unavailable.",
            )
        try:
            completed = self.process_runner(
                arguments,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except OSError:
            return FolderPickerResult(
                PickerStatus.UNAVAILABLE,
                message="The native folder picker is unavailable.",
            )
        output = completed.stdout.rstrip("\r\n")
        if self.platform.startswith("linux") and completed.returncode == 1:
            return FolderPickerResult(PickerStatus.CANCELLED)
        if completed.returncode != 0:
            return FolderPickerResult(
                PickerStatus.FAILED,
                message="The native folder picker failed.",
            )
        if not output:
            return FolderPickerResult(PickerStatus.CANCELLED)
        path = Path(output)
        if not path.is_absolute():
            return FolderPickerResult(
                PickerStatus.FAILED,
                message="The native folder picker returned an invalid path.",
            )
        return FolderPickerResult(
            PickerStatus.SELECTED,
            FolderChoice(DestinationKind.CUSTOM, path, path.name),
        )

    def validate(self, choice: FolderChoice) -> PdfDestination:
        if not choice.path.is_absolute():
            raise FolderValidationError(
                "PDF destination must be an absolute path"
            )
        try:
            choice.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not choice.path.is_dir():
                raise OSError("PDF destination is not a directory")
            destination = choice.path.resolve()
        except OSError as error:
            raise FolderValidationError(
                "PDF destination could not be validated"
            ) from error
        descriptor: int | None = None
        probe: Path | None = None
        renamed_probe: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=".arxiv-digest-probe-",
                suffix=".tmp",
                dir=destination,
            )
            probe = Path(name)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                os.fchmod(handle.fileno(), 0o600)
                handle.write(b"arxiv-digest-folder-probe\n")
                handle.flush()
                os.fsync(handle.fileno())
            renamed_probe = probe.with_name(f"{probe.name}.renamed")
            os.replace(probe, renamed_probe)
            probe = None
            renamed_probe.unlink()
            renamed_probe = None
        except OSError as error:
            raise FolderValidationError(
                "PDF destination could not be validated"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if probe is not None:
                probe.unlink(missing_ok=True)
            if renamed_probe is not None:
                renamed_probe.unlink(missing_ok=True)
        return PdfDestination(choice.kind.value, destination)

    def open_active(self, profile: Profile) -> OpenFolderResult:
        destination = profile.pdf_destination.path
        if not destination.is_absolute() or not destination.is_dir():
            return OpenFolderResult(
                OpenStatus.FAILED,
                "The active PDF destination is unavailable.",
            )
        executable_name = (
            "xdg-open" if self.platform.startswith("linux") else "open"
        )
        executable = self.executable_lookup(executable_name)
        if executable is None:
            return OpenFolderResult(
                OpenStatus.UNAVAILABLE,
                "The native folder opener is unavailable.",
            )
        try:
            completed = self.process_runner(
                [executable, str(destination)],
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except OSError:
            return OpenFolderResult(
                OpenStatus.UNAVAILABLE,
                "The native folder opener is unavailable.",
            )
        if completed.returncode != 0:
            return OpenFolderResult(
                OpenStatus.FAILED,
                "The active PDF destination could not be opened.",
            )
        return OpenFolderResult(OpenStatus.OPENED)
