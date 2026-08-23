from __future__ import annotations

import tempfile
import stat
from pathlib import Path
from subprocess import CompletedProcess

import pytest

import arxiv_digest.folders as folders_module
from arxiv_digest.folders import (
    DestinationKind,
    FolderChoice,
    FolderService,
    OpenStatus,
    PickerStatus,
)
from arxiv_digest.profile import (
    PdfDestination,
    Profile,
    ProfileRepository,
)


def test_standard_choices_are_friendly_and_need_no_typed_paths(
    tmp_path: Path,
) -> None:
    service = FolderService(platform="darwin", home=tmp_path)

    assert service.standard_choices() == (
        FolderChoice(
            DestinationKind.DOWNLOADS,
            tmp_path / "Downloads" / "Arxiv Digest",
            "Downloads / Arxiv Digest",
        ),
        FolderChoice(
            DestinationKind.DOCUMENTS,
            tmp_path / "Documents" / "Arxiv Digest",
            "Documents / Arxiv Digest",
        ),
    )
    assert not (tmp_path / "Downloads").exists()
    assert not (tmp_path / "Documents").exists()


def test_macos_custom_choice_uses_the_native_picker_argument_array(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(
        arguments: list[str],
        **options: object,
    ) -> CompletedProcess[str]:
        calls.append((arguments, options))
        return CompletedProcess(
            arguments,
            0,
            stdout=f"{tmp_path / 'Chosen; $Folder'}\n",
            stderr="",
        )

    service = FolderService(
        platform="darwin",
        home=tmp_path,
        executable_lookup=lambda name: f"/usr/bin/{name}",
        process_runner=run,
    )

    result = service.pick_custom()

    assert result.status is PickerStatus.SELECTED
    assert result.choice == FolderChoice(
        DestinationKind.CUSTOM,
        tmp_path / "Chosen; $Folder",
        "Chosen; $Folder",
    )
    assert calls == [
        (
            [
                "/usr/bin/osascript",
                "-e",
                (
                    'try\nPOSIX path of (choose folder with prompt "Choose PDF '
                    'destination")\non error number -128\nreturn ""\nend try'
                ),
            ],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "shell": False,
            },
        )
    ]


def test_linux_prefers_zenity_over_kdialog(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def lookup(name: str) -> str | None:
        return {
            "zenity": "/opt/bin/zenity",
            "kdialog": "/opt/bin/kdialog",
        }.get(name)

    def run(arguments: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(arguments)
        return CompletedProcess(
            arguments,
            0,
            stdout=f"{tmp_path / 'Linux Choice'}\n",
            stderr="",
        )

    service = FolderService(
        platform="linux",
        home=tmp_path,
        executable_lookup=lookup,
        process_runner=run,
    )

    result = service.pick_custom()

    assert result.status is PickerStatus.SELECTED
    assert calls == [
        [
            "/opt/bin/zenity",
            "--file-selection",
            "--directory",
            "--title",
            "Choose PDF destination",
        ]
    ]


def test_linux_falls_back_to_kdialog(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(arguments)
        return CompletedProcess(
            arguments,
            0,
            stdout=f"{tmp_path / 'Kdialog Choice'}\n",
            stderr="",
        )

    service = FolderService(
        platform="linux",
        home=tmp_path,
        executable_lookup=lambda name: (
            "/opt/bin/kdialog" if name == "kdialog" else None
        ),
        process_runner=run,
    )

    result = service.pick_custom()

    assert result.status is PickerStatus.SELECTED
    assert calls == [
        [
            "/opt/bin/kdialog",
            "--getexistingdirectory",
            str(tmp_path),
            "--title",
            "Choose PDF destination",
        ]
    ]


def test_linux_without_a_picker_returns_a_clear_unavailable_result(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(arguments)
        raise AssertionError("runner must not be called")

    service = FolderService(
        platform="linux",
        home=tmp_path,
        executable_lookup=lambda _: None,
        process_runner=run,
    )

    result = service.pick_custom()

    assert result.status is PickerStatus.UNAVAILABLE
    assert result.choice is None
    assert result.message == (
        "No native folder picker is available; Downloads and Documents "
        "remain available."
    )
    assert calls == []


def test_macos_without_osascript_returns_unavailable_without_running(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(arguments)
        raise AssertionError("runner must not be called")

    service = FolderService(
        platform="darwin",
        home=tmp_path,
        executable_lookup=lambda _: None,
        process_runner=run,
    )

    result = service.pick_custom()

    assert result.status is PickerStatus.UNAVAILABLE
    assert result.choice is None
    assert result.message == "The native folder picker is unavailable."
    assert calls == []


def test_picker_disappearing_before_launch_returns_unavailable(
    tmp_path: Path,
) -> None:
    def missing(*_: object, **__: object) -> CompletedProcess[str]:
        raise FileNotFoundError(str(Path("/") / "Users" / "example" / "private" / "tool"))

    service = FolderService(
        platform="darwin",
        home=tmp_path,
        executable_lookup=lambda _: "/usr/bin/osascript",
        process_runner=missing,
    )

    result = service.pick_custom()

    assert result.status is PickerStatus.UNAVAILABLE
    assert result.choice is None
    assert result.message == "The native folder picker is unavailable."
    assert "private" not in result.message


def test_picker_cancellation_leaves_the_prior_profile_untouched(
    tmp_path: Path,
) -> None:
    repository = ProfileRepository(
        tmp_path / "profile.json",
        tmp_path / "profile.lock",
    )
    original = Profile(
        schema_version=1,
        revision=1,
        categories=("cs.SE",),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination(
            "downloads",
            tmp_path / "Downloads" / "Arxiv Digest",
        ),
    )
    repository.save_atomic(original, expected_revision=None)
    service = FolderService(
        platform="linux",
        home=tmp_path,
        executable_lookup=lambda name: (
            "/usr/bin/zenity" if name == "zenity" else None
        ),
        process_runner=lambda arguments, **_: CompletedProcess(
            arguments,
            1,
            stdout="",
            stderr="",
        ),
    )

    result = service.pick_custom()

    assert result.status is PickerStatus.CANCELLED
    assert result.choice is None
    assert repository.load() == original


def test_picker_failure_is_redacted_and_not_reported_as_cancellation(
    tmp_path: Path,
) -> None:
    service = FolderService(
        platform="linux",
        home=tmp_path,
        executable_lookup=lambda name: (
            "/usr/bin/zenity" if name == "zenity" else None
        ),
        process_runner=lambda arguments, **_: CompletedProcess(
            arguments,
            2,
            stdout="",
            stderr=f"{Path('/') / 'Users' / 'example' / 'secret-folder'} failed",
        ),
    )

    result = service.pick_custom()

    assert result.status is PickerStatus.FAILED
    assert result.choice is None
    assert result.message == "The native folder picker failed."
    assert "secret-folder" not in result.message


def test_picker_rejects_relative_output_without_exposing_it(
    tmp_path: Path,
) -> None:
    service = FolderService(
        platform="linux",
        home=tmp_path,
        executable_lookup=lambda name: (
            "/usr/bin/zenity" if name == "zenity" else None
        ),
        process_runner=lambda arguments, **_: CompletedProcess(
            arguments,
            0,
            stdout="../../private-folder\n",
            stderr="",
        ),
    )

    result = service.pick_custom()

    assert result.status is PickerStatus.FAILED
    assert result.choice is None
    assert result.message == "The native folder picker returned an invalid path."
    assert "private-folder" not in result.message


def test_validation_creates_and_probes_the_destination_in_place(
    tmp_path: Path,
) -> None:
    service = FolderService(platform="darwin", home=tmp_path)
    choice = service.standard_choices()[0]

    destination = service.validate(choice)

    assert destination == PdfDestination("downloads", choice.path.resolve())
    assert choice.path.is_dir()
    assert tuple(choice.path.iterdir()) == ()


def test_validation_rejects_a_relative_destination_without_creating_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    relative = Path("relative-private-folder")
    service = FolderService(platform="darwin", home=tmp_path)

    with pytest.raises(OSError, match="absolute"):
        service.validate(
            FolderChoice(DestinationKind.CUSTOM, relative, "Relative")
        )

    assert not (tmp_path / relative).exists()


def test_validation_rejects_a_destination_that_cannot_create_a_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed_directories: list[Path] = []

    def fail_probe(*_: object, dir: Path, **__: object) -> tuple[int, str]:
        observed_directories.append(Path(dir))
        raise PermissionError("synthetic unwritable folder")

    monkeypatch.setattr(tempfile, "mkstemp", fail_probe)
    service = FolderService(platform="darwin", home=tmp_path)
    choice = service.standard_choices()[0]

    with pytest.raises(OSError, match="could not be validated"):
        service.validate(choice)

    assert observed_directories == [choice.path.resolve()]


def test_validation_redacts_a_directory_creation_failure(
    tmp_path: Path,
) -> None:
    occupied = tmp_path / "private-filename"
    occupied.write_text("synthetic", encoding="utf-8")
    service = FolderService(platform="darwin", home=tmp_path)

    with pytest.raises(OSError) as raised:
        service.validate(
            FolderChoice(DestinationKind.CUSTOM, occupied, "Private")
        )

    assert str(raised.value) == "PDF destination could not be validated"
    assert "private-filename" not in str(raised.value)


def test_validation_fsyncs_a_private_probe_then_renames_and_deletes_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fsynced: list[int] = []
    replacements: list[tuple[Path, Path]] = []
    real_fsync = folders_module.os.fsync
    real_replace = folders_module.os.replace

    def fsync(descriptor: int) -> None:
        fsynced.append(descriptor)
        real_fsync(descriptor)

    def replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        assert stat.S_IMODE(source_path.stat().st_mode) == 0o600
        replacements.append((source_path, Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(folders_module.os, "fsync", fsync)
    monkeypatch.setattr(folders_module.os, "replace", replace)
    service = FolderService(platform="darwin", home=tmp_path)
    choice = service.standard_choices()[1]

    service.validate(choice)

    assert len(fsynced) == 1
    assert len(replacements) == 1
    assert replacements[0][0].parent == choice.path
    assert replacements[0][1].parent == choice.path
    assert tuple(choice.path.iterdir()) == ()


def test_macos_open_uses_only_the_active_validated_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "Active; $(Folder)"
    destination.mkdir()
    profile = Profile(
        schema_version=1,
        revision=1,
        categories=("cs.SE",),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("custom", destination),
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(
        arguments: list[str],
        **options: object,
    ) -> CompletedProcess[str]:
        calls.append((arguments, options))
        return CompletedProcess(arguments, 0, stdout="", stderr="")

    service = FolderService(
        platform="darwin",
        home=tmp_path,
        executable_lookup=lambda name: (
            "/usr/bin/open" if name == "open" else None
        ),
        process_runner=run,
    )

    result = service.open_active(profile)

    assert result.status is OpenStatus.OPENED
    assert calls == [
        (
            ["/usr/bin/open", str(destination)],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "shell": False,
            },
        )
    ]


def test_linux_open_uses_xdg_open_with_one_path_argument(tmp_path: Path) -> None:
    destination = tmp_path / "Active Linux Folder"
    destination.mkdir()
    profile = Profile(
        schema_version=1,
        revision=1,
        categories=("cs.SE",),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("documents", destination),
    )
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(arguments)
        return CompletedProcess(arguments, 0, stdout="", stderr="")

    service = FolderService(
        platform="linux",
        home=tmp_path,
        executable_lookup=lambda name: (
            "/usr/bin/xdg-open" if name == "xdg-open" else None
        ),
        process_runner=run,
    )

    result = service.open_active(profile)

    assert result.status is OpenStatus.OPENED
    assert calls == [["/usr/bin/xdg-open", str(destination)]]


def test_open_reports_an_unavailable_native_opener_without_running(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "Active Folder"
    destination.mkdir()
    profile = Profile(
        schema_version=1,
        revision=1,
        categories=("cs.SE",),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("custom", destination),
    )

    def do_not_run(*_: object, **__: object) -> CompletedProcess[str]:
        raise AssertionError("runner must not be called")

    service = FolderService(
        platform="linux",
        home=tmp_path,
        executable_lookup=lambda _: None,
        process_runner=do_not_run,
    )

    result = service.open_active(profile)

    assert result.status is OpenStatus.UNAVAILABLE
    assert result.message == "The native folder opener is unavailable."


def test_open_reports_a_redacted_native_opener_failure(tmp_path: Path) -> None:
    destination = tmp_path / "Active Folder"
    destination.mkdir()
    profile = Profile(
        schema_version=1,
        revision=1,
        categories=("cs.SE",),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("custom", destination),
    )
    service = FolderService(
        platform="darwin",
        home=tmp_path,
        executable_lookup=lambda _: "/usr/bin/open",
        process_runner=lambda arguments, **_: CompletedProcess(
            arguments,
            1,
            stdout="",
            stderr=f"{Path('/') / 'Users' / 'example' / 'private-folder'} failed",
        ),
    )

    result = service.open_active(profile)

    assert result.status is OpenStatus.FAILED
    assert result.message == "The active PDF destination could not be opened."
    assert "private-folder" not in result.message


def test_open_refuses_an_active_destination_that_is_not_a_directory(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "Missing Folder"
    profile = Profile(
        schema_version=1,
        revision=1,
        categories=("cs.SE",),
        keywords=(),
        phrases=(),
        authors=(),
        seed_papers=(),
        pdf_destination=PdfDestination("custom", missing),
    )
    calls: list[list[str]] = []

    def run(arguments: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(arguments)
        return CompletedProcess(arguments, 0, stdout="", stderr="")

    service = FolderService(
        platform="darwin",
        home=tmp_path,
        executable_lookup=lambda _: "/usr/bin/open",
        process_runner=run,
    )

    result = service.open_active(profile)

    assert result.status is OpenStatus.FAILED
    assert result.message == "The active PDF destination is unavailable."
    assert calls == []
