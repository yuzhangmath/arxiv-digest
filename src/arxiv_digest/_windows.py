"""Native Windows file privacy and locking, imported only on Windows.

Private ACLs allow the current user, SYSTEM, and the local Administrators group.
They are protected from inherited grants; directories pass their private grants
to children, including SQLite journals. Reparse points and multiply linked files
are rejected. Open coordination handles deny deletion while they are held.
"""

from __future__ import annotations

import ctypes
import errno
import msvcrt
import os
from functools import wraps
from pathlib import Path

import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32file
import win32security
import winerror


def _os_errors(function):
    """Expose ordinary OSError subclasses to the platform-independent callers."""
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except pywintypes.error as exc:
            raise ctypes.WinError(exc.winerror) from exc
    return invoke


def _user_sid():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _default_owner_sid():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenOwner)
    finally:
        token.Close()


def _private_sids():
    return (
        _user_sid(),
        win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None),
        win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None),
    )


def _private_acl(*, directory: bool):
    acl = win32security.ACL()
    flags = (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE) if directory else 0
    for sid in _private_sids():
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid,
        )
    return acl


def _security_attributes(*, directory: bool):
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(_user_sid(), False)
    descriptor.SetSecurityDescriptorDacl(True, _private_acl(directory=directory), False)
    descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED,
    )
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def _information(handle, *, directory: bool):
    information = win32file.GetFileInformationByHandle(handle)
    attributes = information[0]
    if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PermissionError("private path must not be a reparse point")
    if bool(attributes & win32con.FILE_ATTRIBUTE_DIRECTORY) != directory:
        raise PermissionError("private path has an unexpected file type")
    if not directory and (
        win32file.GetFileType(handle) != win32file.FILE_TYPE_DISK or information[7] != 1
    ):
        raise PermissionError("private file must be a regular file with one link")
    return information


def _security(handle, *, allow_default_owner: bool = False):
    descriptor = win32security.GetSecurityInfo(
        handle, win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = descriptor.GetSecurityDescriptorOwner()
    if owner != _user_sid() and not (allow_default_owner and owner == _default_owner_sid()):
        raise PermissionError("private path must be owned by the current user")
    return descriptor


def _validate_acl(handle, *, directory: bool) -> None:
    descriptor = _security(handle)
    control, _revision = descriptor.GetSecurityDescriptorControl()
    dacl = descriptor.GetSecurityDescriptorDacl()
    if not control & win32security.SE_DACL_PROTECTED or dacl is None:
        raise PermissionError("private path must have a protected private ACL")
    allowed = _private_sids()
    owner_has_access = False
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or len(ace) != 3:
            raise PermissionError("private path has an unsupported ACL entry")
        (kind, flags), mask, sid = ace
        if sid not in allowed:
            raise PermissionError("private path ACL grants access outside its owner and system administrators")
        if sid == allowed[0] and not flags & win32con.INHERIT_ONLY_ACE:
            if mask & ntsecuritycon.FILE_ALL_ACCESS == ntsecuritycon.FILE_ALL_ACCESS:
                inherited = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
                owner_has_access |= not directory or flags & inherited == inherited
    if not owner_has_access:
        raise PermissionError("private path ACL must grant its owner full access")


def _set_acl(handle, *, directory: bool, owner=None) -> None:
    _information(handle, directory=directory)
    _security(handle, allow_default_owner=owner is not None)
    information = win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION
    if owner is not None:
        information |= win32security.OWNER_SECURITY_INFORMATION
    win32security.SetSecurityInfo(
        handle, win32security.SE_FILE_OBJECT, information,
        owner, None, _private_acl(directory=directory), None,
    )
    _validate_acl(handle, directory=directory)


def _repair_private_acl(handle, *, directory: bool) -> None:
    _information(handle, directory=directory)
    descriptor = _security(handle, allow_default_owner=True)
    if descriptor.GetSecurityDescriptorOwner() == _user_sid():
        _set_acl(handle, directory=directory)
        return
    # Elevated tokens can create Python files/directories owned by the
    # Administrators group. Repair only this process token's default owner;
    # strict validation never changes ownership or accepts this alternative.
    flags = win32con.FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= win32con.FILE_FLAG_BACKUP_SEMANTICS
    ownership_handle = win32file.ReOpenFile(
        handle, win32con.READ_CONTROL | win32con.WRITE_DAC | win32con.WRITE_OWNER,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        flags,
    )
    try:
        _set_acl(ownership_handle, directory=directory, owner=_user_sid())
    finally:
        ownership_handle.Close()


def _identity(information) -> tuple[int, int, int]:
    return information[4], information[8], information[9]


def _open(path: Path, *, directory: bool, create: bool = False, writable: bool = False):
    access = win32con.READ_CONTROL | ntsecuritycon.FILE_READ_ATTRIBUTES
    if not directory:
        access |= win32con.GENERIC_READ
    if writable:
        access |= win32con.WRITE_DAC
        if not directory:
            access |= win32con.GENERIC_WRITE
    return win32file.CreateFile(
        str(path), access, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
        _security_attributes(directory=directory) if create else None,
        win32con.OPEN_ALWAYS if create else win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )


@_os_errors
def ensure_private_directory(path: Path, *, strict: bool) -> None:
    if not path.parent.exists():
        ensure_private_directory(path.parent, strict=strict)
    try:
        win32file.CreateDirectory(str(path), _security_attributes(directory=True))
    except pywintypes.error as exc:
        if exc.winerror != winerror.ERROR_ALREADY_EXISTS:
            raise
    handle = _open(path, directory=True, writable=not strict)
    try:
        information = _information(handle, directory=True)
        if not strict:
            _repair_private_acl(handle, directory=True)
        _validate_acl(handle, directory=True)
        # The first handle excludes deletion; a second no-follow open also
        # detects replacement through a renamed ancestor.
        current = _open(path, directory=True)
        try:
            if _identity(_information(current, directory=True)) != _identity(information):
                raise PermissionError("private directory path identity changed")
        finally:
            current.Close()
    finally:
        handle.Close()


@_os_errors
def open_private_file(path: Path, *, create: bool) -> int:
    handle = _open(path, directory=False, create=create, writable=create)
    try:
        _information(handle, directory=False)
        _validate_acl(handle, directory=False)
        descriptor = msvcrt.open_osfhandle(
            int(handle), (os.O_RDWR if create else os.O_RDONLY) | os.O_BINARY | os.O_NOINHERIT,
        )
        # The CRT descriptor now owns this HANDLE.
        handle.Detach()
    except BaseException:
        handle.Close()
        raise
    try:
        validate_private_path(descriptor, path)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@_os_errors
def validate_private_file(descriptor: int) -> None:
    handle = msvcrt.get_osfhandle(descriptor)
    _information(handle, directory=False)
    _validate_acl(handle, directory=False)


@_os_errors
def validate_private_path(descriptor: int, path: Path) -> None:
    handle = msvcrt.get_osfhandle(descriptor)
    information = _information(handle, directory=False)
    _validate_acl(handle, directory=False)
    current = _open(path, directory=False)
    try:
        if _identity(_information(current, directory=False)) != _identity(information):
            raise PermissionError("private file path identity changed")
        _validate_acl(current, directory=False)
    finally:
        current.Close()
    if os.get_inheritable(descriptor):
        raise PermissionError("private descriptor must not be inheritable")


@_os_errors
def set_private_file_permissions(descriptor: int) -> None:
    # CRT-created handles need not have WRITE_DAC. Reopen the object itself,
    # not its filename, to obtain that access without a substitution race.
    handle = win32file.ReOpenFile(
        msvcrt.get_osfhandle(descriptor), win32con.READ_CONTROL | win32con.WRITE_DAC,
        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
        win32con.FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        _repair_private_acl(handle, directory=False)
    finally:
        handle.Close()


@_os_errors
def lock(descriptor: int, *, blocking: bool) -> None:
    flags = win32con.LOCKFILE_EXCLUSIVE_LOCK
    if not blocking:
        flags |= win32con.LOCKFILE_FAIL_IMMEDIATELY
    try:
        win32file.LockFileEx(msvcrt.get_osfhandle(descriptor), flags, 1, 0, pywintypes.OVERLAPPED())
    except pywintypes.error as exc:
        if exc.winerror == winerror.ERROR_LOCK_VIOLATION:
            raise BlockingIOError(errno.EAGAIN, "private file is already locked") from exc
        raise


@_os_errors
def unlock(descriptor: int) -> None:
    win32file.UnlockFileEx(msvcrt.get_osfhandle(descriptor), 1, 0, pywintypes.OVERLAPPED())


@_os_errors
def replace_file(source: Path, destination: Path) -> None:
    win32file.MoveFileEx(
        str(source), str(destination),
        win32file.MOVEFILE_REPLACE_EXISTING | win32file.MOVEFILE_WRITE_THROUGH,
    )
