"""Windows: keep ``.private`` to the user, as mode 0700 does elsewhere.

Windows ignores the mode bits, so ``.private`` gets a protected access list
instead: full control for the current user and for SYSTEM, inherited by
everything created below it, and nothing inherited from the folder above. A
data root on another drive, where Windows gives every signed-in user access,
is then private too. Standard library only (ctypes).

The owner can always change an access list, so ``.private`` must also be owned
by the user (or by SYSTEM or Administrators). Otherwise another local user who
made the folder first could open it again later. Private files below it are
checked the same way on the open handle, as mode 0o077 is checked elsewhere.
"""

from __future__ import annotations

import os
import re

# Accounts that may hold access to .private: the user (added at run time),
# SYSTEM and Administrators. Administrators can take any file on Windows, as
# root can on macOS and Linux.
# OWNER RIGHTS (OW) gives rights to whoever owns the object, and the owner is
# checked separately. Python 3.12.4 and later put it on folders made with
# os.mkdir(mode=0o700) on Windows, together with SYSTEM and Administrators.
_ALLOWED_ALIASES = frozenset(
    {"SY", "BA", "OW", "S-1-5-18", "S-1-5-32-544", "S-1-3-4"}
)
_ACE = re.compile(r"\(([^()]*)\)")
_OWNER = re.compile(r"O:([^:()]+?)(?=[GDS]:|$)")

# Shown when .private belongs to someone else; says what the user can do.
OWNER_MESSAGE = (
    "Mappen .private eies av en annen bruker på denne maskinen. Flytt den bort, "
    "eller velg en datarot du eier selv med --root eller FAMILIEKILDER_ROOT, og prøv igjen."
)

_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_FILE_OBJECT = 1
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1


def private_sddl(user_sid: str) -> str:
    return f"D:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)"


def _allowed(sid: str, user_sid: str, canonical) -> bool:
    if sid in _ALLOWED_ALIASES or sid == user_sid:
        return True
    full = canonical(sid)
    return full == user_sid or full in _ALLOWED_ALIASES


def sddl_owner_ok(sddl: str, user_sid: str, canonical=lambda sid: sid) -> bool:
    """The owner is the user, SYSTEM or Administrators."""

    found = _OWNER.search(sddl) if isinstance(sddl, str) else None
    return bool(found) and _allowed(found.group(1), user_sid, canonical)


def sddl_is_private(
    sddl: str, user_sid: str, canonical=lambda sid: sid, *, protected: bool = True
) -> bool:
    """An allowed owner and a DACL whose entries all belong to the user, SYSTEM or Administrators.

    `protected` also requires that nothing is inherited from the folder above,
    as for ``.private`` itself. `canonical` turns an SDDL alias into a full
    SID; Windows writes the built-in Administrator account as LA, for example.
    """

    if not isinstance(sddl, str) or "D:" not in sddl:
        return False
    if not sddl_owner_ok(sddl, user_sid, canonical):
        return False
    dacl = sddl[sddl.index("D:") + 2 :]
    dacl = dacl.split("S:", 1)[0]
    flags = dacl.split("(", 1)[0]
    if protected and "P" not in flags:
        return False
    entries = _ACE.findall(dacl)
    if not entries:
        return False
    for entry in entries:
        fields = entry.split(";")
        if len(fields) < 6:
            return False
        kind, sid = fields[0], fields[5]
        # Deny entries (D, OD, XD) only take access away.
        if kind in {"D", "OD", "XD"}:
            continue
        if not _allowed(sid, user_sid, canonical):
            return False
    return True


def _api():
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.GetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    ]
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetSecurityInfo.restype = wintypes.DWORD
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, advapi, kernel


def current_user_sid() -> str:
    ctypes, wintypes, advapi, kernel = _api()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation")
        # TOKEN_USER starts with a SID_AND_ATTRIBUTES whose first field is the SID pointer.
        sid_pointer = ctypes.c_void_p.from_buffer(buffer).value
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW")
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def restrict(path: os.PathLike[str] | str) -> None:
    """Give ``path`` the protected, user-only access list."""

    set_dacl(path, private_sddl(current_user_sid()))


def set_dacl(path: os.PathLike[str] | str, sddl: str) -> None:
    """Replace the access list of ``path`` with the protected DACL in ``sddl``."""

    ctypes, wintypes, advapi, kernel = _api()
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise OSError(ctypes.get_last_error(), "ConvertStringSecurityDescriptor")
    try:
        if not advapi.SetFileSecurityW(
            os.fspath(path),
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            descriptor,
        ):
            raise OSError(ctypes.get_last_error(), "SetFileSecurityW")
    finally:
        kernel.LocalFree(descriptor)


_READ_INFORMATION = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION


def _to_sddl(api, descriptor) -> str:
    ctypes, wintypes, advapi, kernel = api
    text = wintypes.LPWSTR()
    if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        descriptor, _SDDL_REVISION_1, _READ_INFORMATION, ctypes.byref(text), None
    ):
        raise OSError(ctypes.get_last_error(), "ConvertSecurityDescriptorToString")
    try:
        return text.value
    finally:
        kernel.LocalFree(text)


def read_sddl(path: os.PathLike[str] | str) -> str:
    """Owner and DACL of ``path`` as SDDL."""

    api = _api()
    ctypes, wintypes, advapi, kernel = api
    needed = wintypes.DWORD()
    advapi.GetFileSecurityW(os.fspath(path), _READ_INFORMATION, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise OSError(ctypes.get_last_error(), "GetFileSecurityW")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi.GetFileSecurityW(
        os.fspath(path), _READ_INFORMATION, buffer, needed, ctypes.byref(needed)
    ):
        raise OSError(ctypes.get_last_error(), "GetFileSecurityW")
    return _to_sddl(api, buffer)


def read_handle_sddl(file_descriptor: int) -> str:
    """Owner and DACL of an open file, so the file checked is the file read."""

    import msvcrt

    api = _api()
    ctypes, wintypes, advapi, kernel = api
    descriptor = ctypes.c_void_p()
    result = advapi.GetSecurityInfo(
        msvcrt.get_osfhandle(file_descriptor),
        _SE_FILE_OBJECT,
        _READ_INFORMATION,
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise OSError(result, "GetSecurityInfo")
    try:
        return _to_sddl(api, descriptor)
    finally:
        kernel.LocalFree(descriptor)


def canonical_sid(text: str) -> str:
    """The full S-1-… form of a SID or an SDDL alias such as LA or BA."""

    ctypes, wintypes, advapi, kernel = _api()
    sid = ctypes.c_void_p()
    if not advapi.ConvertStringSidToSidW(text, ctypes.byref(sid)):
        return text
    try:
        result = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(result)):
            return text
        try:
            return result.value
        finally:
            kernel.LocalFree(result)
    finally:
        kernel.LocalFree(sid)


def is_private(path: os.PathLike[str] | str) -> bool:
    """``.private``: allowed owner and the protected, user-only access list."""

    return sddl_is_private(read_sddl(path), current_user_sid(), canonical_sid)


def owner_allowed(path: os.PathLike[str] | str) -> bool:
    return sddl_owner_ok(read_sddl(path), current_user_sid(), canonical_sid)


def file_is_private(file_descriptor: int) -> bool:
    """An open private file: allowed owner, and nobody else in its access list."""

    return sddl_is_private(
        read_handle_sddl(file_descriptor), current_user_sid(), canonical_sid, protected=False
    )
