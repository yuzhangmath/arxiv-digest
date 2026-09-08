"""Open dashboard URLs and copy them using local desktop tools."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import webbrowser


_HELPER_TIMEOUT_SECONDS = 5
_WINDOWS_OPEN_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "Start-Process -FilePath ([Console]::In.ReadToEnd())"
)


def _is_wsl() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    kernel = platform.release().casefold()
    return bool(
        os.environ.get("WSL_DISTRO_NAME")
        or os.environ.get("WSL_INTEROP")
        or "microsoft" in kernel
        or "wsl" in kernel
    )


def _run_helper(command: list[str], *, text_input: str | None = None) -> bool:
    executable = shutil.which(command[0])
    if executable is None:
        return False
    try:
        subprocess.run(
            [executable, *command[1:]],
            input=text_input,
            stdin=subprocess.DEVNULL if text_input is None else None,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=_HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def open_browser(url: str) -> bool:
    """Prefer Windows browser helpers in WSL; retain explicit BROWSER choices."""
    wsl = _is_wsl()
    if not wsl or os.environ.get("BROWSER"):
        try:
            if webbrowser.open(url):
                return True
        except (OSError, webbrowser.Error):
            pass
        if not wsl:
            return False
    if _run_helper(["wslview", url]):
        return True
    # Send the URL as data, preserving its token and &view fragment without
    # passing it through cmd.exe or interpolating it into PowerShell code.
    return _run_helper(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-Command", _WINDOWS_OPEN_SCRIPT,
        ],
        text_input=url,
    )


def copy_url(url: str) -> bool:
    """Copy on explicit request, returning False when no clipboard is available."""
    if _is_wsl():
        commands = [["clip.exe"]]
    elif sys.platform == "darwin":
        commands = [["pbcopy"]]
    elif sys.platform.startswith("linux"):
        commands = []
        if os.environ.get("WAYLAND_DISPLAY"):
            commands.append(["wl-copy"])
        if os.environ.get("DISPLAY"):
            commands.extend([
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ])
    else:
        commands = []
    return any(_run_helper(command, text_input=url) for command in commands)
