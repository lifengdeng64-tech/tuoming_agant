from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from tuoming_agent.security.crypto import STREAM_MAGIC, decrypt_stream_frames, encrypt_stream_frames
from tuoming_agent.settings import (
    MASTER_KEY_CREDENTIAL,
    MODEL_API_KEY_CREDENTIAL,
    LocalSettingsManager,
)

BACKUP_MAGIC = b"TUOMING-BACKUP\x01"
_HEADER_LENGTH = struct.Struct(">I")
_MAX_HEADER_BYTES = 64 * 1024
_MAX_BACKUP_BYTES = 20 * 1024 * 1024 * 1024
_BACKUP_AAD = b"TuomingAgent.backup.v1"


class BackupError(RuntimeError):
    """Raised when a backup cannot be safely created or restored."""


class BackupManager:
    def __init__(self, app_dir: Path, data_dir: Path, settings: LocalSettingsManager):
        self.app_dir = Path(app_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.settings = settings

    def create_backup(self, destination: Path, password: str) -> Path:
        key, salt = _derive_password_key(password)
        destination = Path(destination).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "format": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
            "kdf": {"name": "scrypt", "n": 2**15, "r": 8, "p": 1},
        }
        header_bytes = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="tuoming-backup-") as temporary:
            temporary_dir = Path(temporary)
            archive_path = temporary_dir / "payload.zip"
            self._create_archive(archive_path, key, temporary_dir)
            temporary_output = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                with archive_path.open("rb") as source, temporary_output.open("wb") as target:
                    target.write(BACKUP_MAGIC)
                    target.write(_HEADER_LENGTH.pack(len(header_bytes)))
                    target.write(header_bytes)
                    encrypt_stream_frames(
                        key,
                        source,
                        target,
                        _BACKUP_AAD + header_bytes,
                        1024 * 1024,
                    )
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary_output, destination)
            except Exception:
                temporary_output.unlink(missing_ok=True)
                raise
        return destination

    def stage_restore(self, source: Path, password: str) -> Path:
        source = Path(source).expanduser().resolve()
        if not source.is_file() or source.stat().st_size > _MAX_BACKUP_BYTES:
            raise BackupError("备份文件不存在或超过安全体积上限。")
        staging_root = self.app_dir / "restore-staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        stage_dir = staging_root / uuid.uuid4().hex
        stage_dir.mkdir()
        archive_path = stage_dir / "payload.zip"
        try:
            with source.open("rb") as encrypted:
                header_bytes, key = _read_header(encrypted, password)
                if encrypted.read(len(STREAM_MAGIC)) != STREAM_MAGIC:
                    raise BackupError("备份加密数据头已损坏。")
                with archive_path.open("wb") as decrypted:
                    for chunk in decrypt_stream_frames(
                        key, encrypted, _BACKUP_AAD + header_bytes
                    ):
                        decrypted.write(chunk)
            self._verify_and_extract(archive_path, stage_dir / "restored", key)
            archive_path.unlink(missing_ok=True)
            pending = {
                "format": 1,
                "stage_dir": str(stage_dir),
                "created_at": datetime.now(UTC).isoformat(),
            }
            _atomic_json(self.app_dir / "restore.pending.json", pending)
            return stage_dir
        except Exception as exc:
            shutil.rmtree(stage_dir, ignore_errors=True)
            if isinstance(exc, BackupError):
                raise
            raise BackupError("备份密码错误、文件损坏或格式不受支持。") from exc

    def _create_archive(self, archive_path: Path, key: bytes, temporary_dir: Path) -> None:
        files: list[tuple[Path, str]] = []
        settings_path = self.app_dir / "settings.json"
        if settings_path.is_file():
            files.append((settings_path, "settings.json"))
        if self.data_dir.exists():
            database_path = self.data_dir / "tuoming.sqlite3"
            if database_path.is_file():
                snapshot = temporary_dir / "tuoming.sqlite3"
                with sqlite3.connect(database_path) as source, sqlite3.connect(snapshot) as target:
                    source.backup(target)
                files.append((snapshot, "data/tuoming.sqlite3"))
            for path in self.data_dir.rglob("*"):
                if not path.is_file() or path.name in {
                    "tuoming.sqlite3",
                    "tuoming.sqlite3-wal",
                    "tuoming.sqlite3-shm",
                }:
                    continue
                files.append((path, f"data/{path.relative_to(self.data_dir).as_posix()}"))
        secret_payload = {
            "master_key": _secret_text(self.settings, MASTER_KEY_CREDENTIAL),
            "api_key": _secret_text(self.settings, MODEL_API_KEY_CREDENTIAL),
        }
        nonce = os.urandom(12)
        encrypted_secrets = nonce + AESGCM(key).encrypt(
            nonce,
            json.dumps(secret_payload, separators=(",", ":")).encode("utf-8"),
            b"TuomingAgent.backup.secrets.v1",
        )
        manifest = {
            archive_name: {"sha256": _file_hash(path), "size": path.stat().st_size}
            for path, archive_name in files
        }
        manifest["secrets.enc"] = {
            "sha256": hashlib.sha256(encrypted_secrets).hexdigest(),
            "size": len(encrypted_secrets),
        }
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, archive_name in files:
                archive.write(path, archive_name)
            archive.writestr("secrets.enc", encrypted_secrets)
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, separators=(",", ":"), sort_keys=True),
            )

    def _verify_and_extract(self, archive_path: Path, target: Path, key: bytes) -> None:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) > 100_000:
                raise BackupError("备份文件项过多。")
            for name in names:
                _validate_archive_path(name)
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (KeyError, json.JSONDecodeError) as exc:
                raise BackupError("备份清单缺失或损坏。") from exc
            if set(manifest) != set(names) - {"manifest.json"}:
                raise BackupError("备份清单与文件内容不一致。")
            total_size = 0
            target.mkdir(parents=True)
            for name, metadata in manifest.items():
                content = archive.read(name)
                total_size += len(content)
                if total_size > _MAX_BACKUP_BYTES:
                    raise BackupError("备份解压后超过安全体积上限。")
                if len(content) != int(metadata["size"]):
                    raise BackupError("备份文件大小校验失败。")
                if hashlib.sha256(content).hexdigest() != metadata["sha256"]:
                    raise BackupError("备份文件完整性校验失败。")
                output = target / PurePosixPath(name)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
        encrypted_secrets = (target / "secrets.enc").read_bytes()
        if len(encrypted_secrets) < 29:
            raise BackupError("备份凭据数据已损坏。")
        try:
            plaintext = AESGCM(key).decrypt(
                encrypted_secrets[:12],
                encrypted_secrets[12:],
                b"TuomingAgent.backup.secrets.v1",
            )
            secrets_payload = json.loads(plaintext)
        except Exception as exc:
            raise BackupError("备份凭据校验失败。") from exc
        _atomic_json(target / "secrets.restore.json", secrets_payload)
        (target / "secrets.enc").unlink(missing_ok=True)


def apply_pending_restore(app_dir: Path, settings: LocalSettingsManager) -> Path | None:
    app_dir = Path(app_dir).resolve()
    pending_path = app_dir / "restore.pending.json"
    if not pending_path.is_file():
        return None
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        stage_dir = Path(pending["stage_dir"]).resolve()
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        raise BackupError("待恢复任务已损坏。") from exc
    staging_root = (app_dir / "restore-staging").resolve()
    if staging_root not in stage_dir.parents:
        raise BackupError("待恢复目录不在受控范围内。")
    restored = stage_dir / "restored"
    secrets_payload = json.loads((restored / "secrets.restore.json").read_text(encoding="utf-8"))
    recovery = app_dir / "recovery" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    recovery.mkdir(parents=True)
    data_dir = app_dir / "data"
    try:
        if data_dir.exists():
            shutil.move(str(data_dir), str(recovery / "data"))
        settings_path = app_dir / "settings.json"
        if settings_path.exists():
            shutil.copy2(settings_path, recovery / "settings.json")
        restored_data = restored / "data"
        if restored_data.exists():
            shutil.move(str(restored_data), str(data_dir))
        restored_settings = restored / "settings.json"
        if restored_settings.exists():
            shutil.copy2(restored_settings, settings_path)
        _restore_secret(settings, MASTER_KEY_CREDENTIAL, secrets_payload.get("master_key"))
        _restore_secret(settings, MODEL_API_KEY_CREDENTIAL, secrets_payload.get("api_key"))
        pending_path.unlink(missing_ok=True)
        shutil.rmtree(stage_dir, ignore_errors=True)
        return recovery
    except Exception as exc:
        raise BackupError("恢复未完成，旧数据保留在 recovery 目录中。") from exc


def _derive_password_key(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    if len(password) < 10:
        raise BackupError("备份密码至少需要 10 个字符。")
    salt = salt or os.urandom(16)
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))
    return key, salt


def _read_header(source, password: str) -> tuple[bytes, bytes]:
    if source.read(len(BACKUP_MAGIC)) != BACKUP_MAGIC:
        raise BackupError("不是 Tuoming 加密备份。")
    length_bytes = source.read(_HEADER_LENGTH.size)
    if len(length_bytes) != _HEADER_LENGTH.size:
        raise BackupError("备份头已损坏。")
    header_length = _HEADER_LENGTH.unpack(length_bytes)[0]
    if not 1 <= header_length <= _MAX_HEADER_BYTES:
        raise BackupError("备份头长度无效。")
    header_bytes = source.read(header_length)
    try:
        header = json.loads(header_bytes)
        if header.get("format") != 1 or header.get("kdf", {}).get("name") != "scrypt":
            raise BackupError("备份版本不受支持。")
        salt = base64.urlsafe_b64decode(header["salt"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupError("备份头格式无效。") from exc
    key, _ = _derive_password_key(password, salt)
    return header_bytes, key


def _validate_archive_path(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise BackupError("备份包含不安全路径。")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _secret_text(settings: LocalSettingsManager, name: str) -> str | None:
    value = settings.secret_store.get(name)
    return base64.urlsafe_b64encode(value).decode("ascii") if value else None


def _restore_secret(settings: LocalSettingsManager, name: str, value: str | None) -> None:
    if value:
        settings.secret_store.set(name, base64.urlsafe_b64decode(value))
    else:
        settings.secret_store.delete(name)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise