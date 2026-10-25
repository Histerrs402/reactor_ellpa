"""Хранение пароля / ключа портала.

В Windows значение шифруется средствами системы (DPAPI) для текущей учётной записи:
расшифровать его можно только под этим же пользователем на этом же компьютере,
поэтому копия базы на другом компьютере пароль не раскрывает.
На других системах (используется только в тестах) значение лишь кодируется.
"""
from __future__ import annotations

import base64
import sys

DPAPI = "dpapi:"
PLAIN = "b64:"


def protect(plain: str) -> str:
    if not plain:
        return ""
    data = plain.encode("utf-8")
    if sys.platform == "win32":
        try:
            return DPAPI + base64.b64encode(_dpapi(data, encrypt=True)).decode("ascii")
        except Exception:  # noqa: BLE001 — запасной вариант ниже
            pass
    return PLAIN + base64.b64encode(data).decode("ascii")


def reveal(stored: str | None) -> str:
    """Исходное значение; пустая строка, если расшифровать нельзя (например, база
    перенесена на другой компьютер — тогда пароль нужно ввести заново)."""
    if not stored:
        return ""
    try:
        if stored.startswith(DPAPI):
            return _dpapi(base64.b64decode(stored[len(DPAPI):]), encrypt=False).decode("utf-8")
        if stored.startswith(PLAIN):
            return base64.b64decode(stored[len(PLAIN):]).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def is_protected(stored: str | None) -> bool:
    return bool(stored) and stored.startswith(DPAPI)


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    flags = 0x01  # CRYPTPROTECT_UI_FORBIDDEN
    if encrypt:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), ctypes.c_wchar_p("RepairMonitor"), None, None,
                                      None, flags, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, flags,
                                        ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
