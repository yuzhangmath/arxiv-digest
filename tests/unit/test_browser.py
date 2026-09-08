from __future__ import annotations

import subprocess
from typing import Any

import pytest

import arxiv_digest.browser as browser


URL = "http://127.0.0.1:43123/#token=" + "A" * 43 + "&view=setup"


@pytest.fixture(autouse=True)
def isolated_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BROWSER", "WSL_DISTRO_NAME", "WSL_INTEROP", "DISPLAY", "WAYLAND_DISPLAY"
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(browser.sys, "platform", "linux")
    monkeypatch.setattr(browser.platform, "release", lambda: "6.6.0-generic")
    monkeypatch.setattr(browser.shutil, "which", lambda _name: None)

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unexpected desktop launch or clipboard access")

    monkeypatch.setattr(browser.subprocess, "run", unexpected)
    monkeypatch.setattr(browser.webbrowser, "open", unexpected)


def available(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setattr(
        browser.shutil, "which",
        lambda name: f"/desktop-tools/{name}" if name in names else None,
    )


def record_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(arguments: list[str], **options: Any) -> subprocess.CompletedProcess:
        calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(browser.subprocess, "run", run)
    return calls


@pytest.mark.parametrize(
    ("system", "kernel", "environment", "expected"),
    [
        ("linux", "6.6.0-generic", {}, False),
        ("linux", "4.4.0-Microsoft", {}, True),
        ("linux", "6.6.87.2-microsoft-standard-WSL2", {}, True),
        ("linux", "6.6.0-WSL2", {}, True),
        ("linux", "custom", {"WSL_DISTRO_NAME": "SyntheticLinux"}, True),
        ("linux", "custom", {"WSL_INTEROP": "/run/WSL/123_interop"}, True),
        ("darwin", "Microsoft", {"WSL_DISTRO_NAME": "SyntheticLinux"}, False),
        ("win32", "WSL2", {"WSL_INTEROP": "/run/WSL/123_interop"}, False),
    ],
)
def test_wsl_detection_requires_linux_and_a_wsl_signal(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    kernel: str,
    environment: dict[str, str],
    expected: bool,
) -> None:
    monkeypatch.setattr(browser.sys, "platform", system)
    monkeypatch.setattr(browser.platform, "release", lambda: kernel)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert browser._is_wsl() is expected


@pytest.mark.parametrize("system", ["linux", "darwin", "win32"])
@pytest.mark.parametrize("opened", [True, False])
def test_ordinary_platforms_use_the_standard_browser(
    monkeypatch: pytest.MonkeyPatch, system: str, opened: bool,
) -> None:
    monkeypatch.setattr(browser.sys, "platform", system)
    calls: list[str] = []
    monkeypatch.setattr(
        browser.webbrowser, "open", lambda url: calls.append(url) or opened,
    )

    assert browser.open_browser(URL) is opened
    assert calls == [URL]


@pytest.mark.parametrize(
    "error", [OSError("unavailable"), browser.webbrowser.Error("unavailable")]
)
def test_standard_browser_errors_are_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    def fail(_url: str) -> bool:
        raise error

    monkeypatch.setattr(browser.webbrowser, "open", fail)

    assert browser.open_browser(URL) is False


def test_wsl_honors_an_explicit_browser_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "SyntheticLinux")
    monkeypatch.setenv("BROWSER", "configured-browser %s")
    available(monkeypatch, "wslview", "powershell.exe")
    opened: list[str] = []
    monkeypatch.setattr(
        browser.webbrowser, "open", lambda url: opened.append(url) or True,
    )

    assert browser.open_browser(URL) is True
    assert opened == [URL]


def test_wsl_uses_native_helper_when_browser_override_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "SyntheticLinux")
    monkeypatch.setenv("BROWSER", "configured-browser %s")
    available(monkeypatch, "wslview")
    monkeypatch.setattr(browser.webbrowser, "open", lambda _url: False)
    calls = record_runs(monkeypatch)

    assert browser.open_browser(URL) is True
    assert calls[0][0] == ["/desktop-tools/wslview", URL]


def test_wslview_opens_the_complete_url_without_a_linux_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/123_interop")
    available(monkeypatch, "wslview", "powershell.exe")
    calls = record_runs(monkeypatch)

    assert browser.open_browser(URL) is True
    assert len(calls) == 1
    arguments, options = calls[0]
    assert arguments == ["/desktop-tools/wslview", URL]
    assert options["check"] is True
    assert options["timeout"] == 5
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    assert options.get("shell", False) is False


def test_powershell_receives_the_url_as_data_with_fragment_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "SyntheticLinux")
    available(monkeypatch, "powershell.exe")
    calls = record_runs(monkeypatch)

    assert browser.open_browser(URL) is True
    assert len(calls) == 1
    arguments, options = calls[0]
    assert arguments[:4] == [
        "/desktop-tools/powershell.exe", "-NoProfile", "-NonInteractive", "-Command"
    ]
    script = arguments[4]
    assert "Start-Process" in script
    assert "[Console]::In.ReadToEnd()" in script
    assert "$ErrorActionPreference = 'Stop'" in script
    assert URL not in script
    assert options["input"] == URL
    assert options["text"] is True
    assert options["check"] is True
    assert options["timeout"] == 5
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    assert options.get("shell", False) is False


HELPER_ERRORS = (
    OSError("interop is disabled"),
    subprocess.CalledProcessError(1, "desktop-helper"),
    subprocess.TimeoutExpired("desktop-helper", 5),
)


@pytest.mark.parametrize("error", HELPER_ERRORS)
def test_failed_wslview_falls_back_to_powershell(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "SyntheticLinux")
    available(monkeypatch, "wslview", "powershell.exe")
    calls: list[str] = []

    def run(arguments: list[str], **_options: Any) -> subprocess.CompletedProcess:
        calls.append(arguments[0])
        if arguments[0].endswith("/wslview"):
            raise error
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(browser.subprocess, "run", run)

    assert browser.open_browser(URL) is True
    assert calls == ["/desktop-tools/wslview", "/desktop-tools/powershell.exe"]


@pytest.mark.parametrize("error", HELPER_ERRORS)
def test_wsl_helper_failure_does_not_launch_xdg_open(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "SyntheticLinux")
    available(monkeypatch, "powershell.exe")

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(browser.subprocess, "run", fail)

    assert browser.open_browser(URL) is False


def test_wsl_without_windows_helpers_does_not_launch_xdg_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "SyntheticLinux")

    assert browser.open_browser(URL) is False


@pytest.mark.parametrize(
    ("system", "environment", "command"),
    [
        ("linux", {"WSL_DISTRO_NAME": "SyntheticLinux"}, ["clip.exe"]),
        ("darwin", {}, ["pbcopy"]),
        ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, ["wl-copy"]),
        ("linux", {"DISPLAY": ":0"}, ["xclip", "-selection", "clipboard"]),
        ("linux", {"DISPLAY": ":0"}, ["xsel", "--clipboard", "--input"]),
    ],
)
def test_clipboard_uses_platform_tool_and_copies_the_full_url(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    environment: dict[str, str],
    command: list[str],
) -> None:
    monkeypatch.setattr(browser.sys, "platform", system)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    available(monkeypatch, command[0])
    calls = record_runs(monkeypatch)

    assert browser.copy_url(URL) is True
    assert len(calls) == 1
    arguments, options = calls[0]
    assert arguments == [f"/desktop-tools/{command[0]}", *command[1:]]
    assert options["input"] == URL
    assert options["text"] is True
    assert options["check"] is True
    assert options["timeout"] == 5
    assert options["stdout"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.DEVNULL
    assert options.get("shell", False) is False


@pytest.mark.parametrize("error", HELPER_ERRORS)
def test_clipboard_failure_is_reported_without_crashing(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "SyntheticLinux")
    available(monkeypatch, "clip.exe")

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(browser.subprocess, "run", fail)

    assert browser.copy_url(URL) is False


def test_linux_clipboard_falls_back_to_an_available_x11_selection_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    available(monkeypatch, "wl-copy", "xclip", "xsel")
    calls: list[list[str]] = []

    def run(arguments: list[str], **_options: Any) -> subprocess.CompletedProcess:
        calls.append(arguments)
        if not arguments[0].endswith("/xsel"):
            raise subprocess.CalledProcessError(1, arguments)
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(browser.subprocess, "run", run)

    assert browser.copy_url(URL) is True
    assert calls == [
        ["/desktop-tools/wl-copy"],
        ["/desktop-tools/xclip", "-selection", "clipboard"],
        ["/desktop-tools/xsel", "--clipboard", "--input"],
    ]


def test_headless_linux_does_not_attempt_desktop_clipboard_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    available(monkeypatch, "wl-copy", "xclip", "xsel")

    assert browser.copy_url(URL) is False


def test_wsl_clipboard_does_not_fall_back_to_a_linux_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "SyntheticLinux")
    monkeypatch.setenv("DISPLAY", ":0")
    available(monkeypatch, "xclip", "xsel")

    assert browser.copy_url(URL) is False
