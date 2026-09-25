"""Rick's IBKR login for IB Gateway, kept in Windows Credential Manager and nowhere else.

Rick stores it once, himself, with ibkr_login_setup.cmd (scripts/ibkr_login_setup.py): it
asks in a console, the password with hidden input, and calls `store`. Windows keeps it as a
generic credential encrypted with DPAPI under Rick's own Windows account, persisted on this
PC only (not roamed): another Windows user cannot read it, and it never exists as a file.

The Gateway launcher (scripts/ibgateway.pyw) calls `load` at each start and hands the two
values straight to IBC (asxbot.ibkr.ibc). Nothing in this project writes them to a file, a
log, an event, a message or an exception text: `Login.__repr__` masks both, and every
error below names only the credential's target, never its contents.

DATA ONLY in spirit, like the rest of this package: a login lets IB Gateway run; Gateway's
"Read-Only API" setting (and IBC's ReadOnlyApi=yes) still refuses every order.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

TARGET = "asxbot/ibkr-gateway-login"
COMMENT = "IBKR login for IB Gateway, used only by the asx-bot Gateway launcher (IBC)."

_GENERIC = 1  # CRED_TYPE_GENERIC
_PERSIST_LOCAL_MACHINE = 2  # this PC, this Windows user; survives logoff and reboot
_NOT_FOUND = 1168  # ERROR_NOT_FOUND


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class Login:
    user: str
    password: str

    def __repr__(self) -> str:  # a Login printed by accident shows nothing
        return "Login(user=***, password=***)"

    __str__ = __repr__


def _api():
    if sys.platform != "win32":
        raise CredentialError("Windows Credential Manager exists only on Windows")
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredWriteW.argtypes = (ctypes.POINTER(CREDENTIAL), wintypes.DWORD)
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredReadW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL)))  # fmt: skip
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = (ctypes.c_void_p,)
    adv.CredFree.restype = None
    return ctypes, CREDENTIAL, adv


def store(user: str, password: str, target: str = TARGET) -> None:
    """Save (or replace) the login. Raises CredentialError, naming only the target."""
    user, password = (user or "").strip(), password or ""
    if not user or not password:
        raise CredentialError("both the username and the password are needed")
    ctypes, CREDENTIAL, adv = _api()
    blob = password.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIAL()
    cred.Type = _GENERIC
    cred.TargetName = target
    cred.Comment = COMMENT
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _PERSIST_LOCAL_MACHINE
    cred.UserName = user
    ok = adv.CredWriteW(ctypes.byref(cred), 0)
    ctypes.memset(buf, 0, len(blob))
    if not ok:
        raise CredentialError(f"Windows refused to save {target} (error {ctypes.get_last_error()})")


def load(target: str = TARGET) -> Login | None:
    """The stored login, or None if Rick has not stored one (or it cannot be read)."""
    try:
        ctypes, CREDENTIAL, adv = _api()
    except CredentialError:
        return None
    p = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(target, _GENERIC, 0, ctypes.byref(p)):
        return None  # ERROR_NOT_FOUND, or unreadable: either way there is no login to use
    try:
        c = p.contents
        size = int(c.CredentialBlobSize)
        raw = ctypes.string_at(c.CredentialBlob, size) if size else b""
        user = c.UserName or ""
    finally:
        adv.CredFree(p)
    password = raw.decode("utf-16-le", errors="strict") if raw else ""
    if not user or not password:
        return None
    return Login(user, password)


def stored(target: str = TARGET) -> bool:
    return load(target) is not None


def forget(target: str = TARGET) -> bool:
    """Delete the stored login. True if one was deleted."""
    ctypes, _, adv = _api()
    if adv.CredDeleteW(target, _GENERIC, 0):
        return True
    if ctypes.get_last_error() == _NOT_FOUND:
        return False
    raise CredentialError(f"Windows refused to delete {target} (error {ctypes.get_last_error()})")


__all__ = ["TARGET", "CredentialError", "Login", "forget", "load", "store", "stored"]
