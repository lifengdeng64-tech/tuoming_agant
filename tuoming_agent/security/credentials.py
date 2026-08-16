from __future__ import annotations

import ctypes
import hashlib
import os
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Protocol


class CredentialStoreError(RuntimeError):
    """Raised when an operating-system protected secret cannot be read or written."""


class SecretStore(Protocol):
    def get(self, name: str) -> bytes | None: ...

    def set(self, name: str, value: bytes) -> None: ...

    def delete(self, name: str) -> None: ...


class MemorySecretStore:
    """Small injectable store used by tests; production uses Windows DPAPI."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, name: str) -> bytes | None:
        return self.values.get(name)

    def set(self, name: str, value: bytes) -> None:
        self.values[name] = bytes(value)

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


if os.name == "nt":

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]


class WindowsDpapiSecretStore:
    """Current-user-scoped encrypted files backed by Windows DPAPI."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x01
    _ENTROPY = b"TuomingAgent.DPAPI.v1"

    def __init__(self, root: Path):
        if os.name != "nt":
            raise CredentialStoreError("Windows DPAPI is only available on Windows.")
        self.root = Path(root)

    def get(self, name: str) -> bytes | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return self._unprotect(path.read_bytes())
        except OSError as exc:
            raise CredentialStoreError(
                "\u65e0\u6cd5\u8bfb\u53d6\u672c\u673a\u5b89\u5168\u51ed\u636e\uff0c"
                "\u8bf7\u68c0\u67e5 Windows \u7528\u6237\u6743\u9650\u3002"
            ) from exc

    def set(self, name: str, value: bytes) -> None:
        if not value:
            raise ValueError("A stored secret cannot be empty.")
        self.root.mkdir(parents=True, exist_ok=True)
        encrypted = self._protect(bytes(value))
        file_descriptor, temporary_name = tempfile.mkstemp(prefix=".secret-", dir=self.root)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path(name))
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def _path(self, name: str) -> Path:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.credential"

    @classmethod
    def _protect(cls, plaintext: bytes) -> bytes:
        return cls._crypt(plaintext, protect=True)

    @classmethod
    def _unprotect(cls, ciphertext: bytes) -> bytes:
        return cls._crypt(ciphertext, protect=False)

    @classmethod
    def _crypt(cls, payload: bytes, *, protect: bool) -> bytes:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_pointer = ctypes.POINTER(_DataBlob)
        crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.c_void_p,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL

        payload_blob, payload_buffer = cls._blob(payload)
        entropy_blob, entropy_buffer = cls._blob(cls._ENTROPY)
        output_blob = _DataBlob()
        if protect:
            success = crypt32.CryptProtectData(
                ctypes.byref(payload_blob),
                "Tuoming Agent local credential",
                ctypes.byref(entropy_blob),
                None,
                None,
                cls._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        else:
            success = crypt32.CryptUnprotectData(
                ctypes.byref(payload_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                cls._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        # Keep input buffers alive until the Windows call has completed.
        del payload_buffer, entropy_buffer
        if not success:
            error = ctypes.get_last_error()
            raise OSError(error, f"Windows DPAPI operation failed (error code {error}).")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))

    @staticmethod
    def _blob(payload: bytes):
        buffer = ctypes.create_string_buffer(payload, len(payload))
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        return _DataBlob(len(payload), pointer), buffer


def get_default_secret_store(app_dir: Path) -> SecretStore:
    """Return the production store without silently falling back to plaintext."""

    if os.name != "nt":
        raise CredentialStoreError(
            "Desktop-managed credentials currently require Windows DPAPI. "
            "Use environment variables for source development on other platforms."
        )
    return WindowsDpapiSecretStore(Path(app_dir) / "credentials")
