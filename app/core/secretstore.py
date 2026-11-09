"""密钥存储：Windows DPAPI 加密，密文与当前用户账户绑定。

关键性质：把 secrets.dat 拷到另一台电脑解不开。
所以用户分享软件目录、甚至整个 AppData 文件夹，都不会泄露 API Key。
非 Windows 平台退化为受限权限文件，并在 UI 上明示。
"""
from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _protect(data: bytes) -> bytes:
    if not IS_WINDOWS:
        return b"PLAIN:" + data
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(f"CryptProtectData 失败：{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _unprotect(data: bytes) -> bytes:
    if data.startswith(b"PLAIN:"):
        return data[6:]
    if not IS_WINDOWS:
        raise OSError("该密钥文件由 Windows 加密，无法在当前系统解密")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("解密失败：密钥文件可能属于其他用户或其他电脑")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


class SecretStore:
    """按 provider 存取 API Key，文件内每个值独立加密。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[str, str] = {}
        self._loaded = False

    def _load(self) -> dict[str, str]:
        if self._loaded:
            return self._cache
        self._loaded = True
        if not self.path.is_file():
            return self._cache
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("密钥文件损坏，已忽略：%s", exc)
            return self._cache
        for provider, encoded in (raw.get("keys") or {}).items():
            try:
                self._cache[provider] = _unprotect(base64.b64decode(encoded)).decode("utf-8")
            except (OSError, ValueError):
                log.warning("无法解密 %s 的密钥（可能来自其他账户）", provider)
        return self._cache

    def _save(self) -> None:
        payload: dict[str, Any] = {"version": 1, "keys": {}}
        for provider, key in self._cache.items():
            payload["keys"][provider] = base64.b64encode(_protect(key.encode("utf-8"))).decode("ascii")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.path)
        if not IS_WINDOWS:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def get(self, provider: str) -> str | None:
        return self._load().get(provider) or None

    def set(self, provider: str, key: str) -> None:
        key = (key or "").strip()
        if not key:
            self.delete(provider)
            return
        self._load()
        self._cache[provider] = key
        self._save()

    def delete(self, provider: str) -> None:
        self._load()
        if self._cache.pop(provider, None) is not None:
            self._save()

    def providers(self) -> list[str]:
        return sorted(k for k, v in self._load().items() if v)

    def masked(self, provider: str) -> str:
        key = self.get(provider)
        if not key:
            return ""
        if len(key) <= 8:
            return "****"
        return f"{key[:3]}****{key[-4:]}"
