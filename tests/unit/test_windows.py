from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import arxiv_digest


@pytest.fixture
def windows(monkeypatch):
    # pywin32 exports OPEN_REPARSE_POINT from win32file, not win32con.
    # Keep these namespaces strict so missing exports fail on every platform.
    win32con = SimpleNamespace(
        READ_CONTROL=0x20000, WRITE_DAC=0x40000, WRITE_OWNER=0x80000,
        GENERIC_READ=0x80000000, GENERIC_WRITE=0x40000000,
        FILE_SHARE_READ=1, FILE_SHARE_WRITE=2, FILE_SHARE_DELETE=4,
        OPEN_EXISTING=3, OPEN_ALWAYS=4, FILE_FLAG_BACKUP_SEMANTICS=0x02000000,
    )
    win32file = SimpleNamespace(
        FILE_FLAG_OPEN_REPARSE_POINT=0x00200000,
        CreateFile=Mock(), ReOpenFile=Mock(),
    )
    modules = {
        "msvcrt": SimpleNamespace(get_osfhandle=Mock(return_value=42)),
        "ntsecuritycon": SimpleNamespace(FILE_READ_ATTRIBUTES=0x80),
        "pywintypes": SimpleNamespace(error=type("WindowsError", (Exception,), {})),
        "win32api": SimpleNamespace(),
        "win32con": win32con,
        "win32file": win32file,
        "win32security": SimpleNamespace(),
        "winerror": SimpleNamespace(),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(arxiv_digest.__file__).with_name("_windows.py")
    spec = importlib.util.spec_from_file_location("windows_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("directory", [False, True])
def test_private_open_uses_no_follow_flag_without_allowing_deletion(windows, directory):
    path = Path("private-entry")

    handle = windows._open(path, directory=directory)

    assert handle is windows.win32file.CreateFile.return_value
    args = windows.win32file.CreateFile.call_args.args
    assert args[0] == str(path)
    assert args[2] == 3  # Share reads/writes, but prevent path replacement.
    assert args[4] == 3  # OPEN_EXISTING
    assert args[5] == 0x02200000  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS


def test_private_permissions_reopen_the_handle_without_following_reparse_points(
    windows, monkeypatch,
):
    repair = Mock()
    monkeypatch.setattr(windows, "_repair_private_acl", repair)

    windows.set_private_file_permissions(7)

    windows.msvcrt.get_osfhandle.assert_called_once_with(7)
    windows.win32file.ReOpenFile.assert_called_once_with(42, 0x60000, 7, 0x00200000)
    handle = windows.win32file.ReOpenFile.return_value
    repair.assert_called_once_with(handle, directory=False)
    handle.Close.assert_called_once_with()


@pytest.mark.parametrize("directory", [False, True])
def test_default_owner_repair_reopens_without_following_reparse_points(
    windows, monkeypatch, directory,
):
    handle = object()
    monkeypatch.setattr(windows, "_information", Mock())
    monkeypatch.setattr(
        windows, "_security",
        Mock(return_value=SimpleNamespace(GetSecurityDescriptorOwner=lambda: "default-owner")),
    )
    monkeypatch.setattr(windows, "_user_sid", lambda: "current-user")
    set_acl = Mock()
    monkeypatch.setattr(windows, "_set_acl", set_acl)

    windows._repair_private_acl(handle, directory=directory)

    flags = 0x02200000 if directory else 0x00200000
    windows.win32file.ReOpenFile.assert_called_once_with(handle, 0xE0000, 7, flags)
    ownership_handle = windows.win32file.ReOpenFile.return_value
    set_acl.assert_called_once_with(ownership_handle, directory=directory, owner="current-user")
    ownership_handle.Close.assert_called_once_with()
