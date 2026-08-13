from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_STALE_AGE = timedelta(hours=24)
INPUT_HEADROOM_MULTIPLIER = 3
_REPARSE_POINT = 0x400
_HEX_32 = r"[0-9a-fA-F]{32}"
_HEX_64 = r"[0-9a-fA-F]{64}"
_OWNED_FILES = {
    "uploads": (
        re.compile(rf"\.{_HEX_32}\.tmp"),
        re.compile(rf"{_HEX_64}\.{_HEX_32}\.enc"),
    ),
    "artifacts": (re.compile(rf".+\.{_HEX_32}\.tmp\.parquet"),),
    "analysis-candidates": (
        re.compile(r".+\.parquet"),
        re.compile(rf".+\.{_HEX_32}\.tmp\.parquet"),
    ),
    "exports": (re.compile(r"(?:masked|restored)-[0-9a-fA-F]{8}-.+\.(?:csv|xlsx)"),),
    "duckdb-temp": (re.compile(r"\d+-[0-9a-fA-F]+\.parquet"),),
}


class DiskHeadroomError(RuntimeError):
    """Raised before ingestion when the configured data volume lacks safe headroom."""


class MaintenanceError(RuntimeError):
    """Raised when cleanup cannot prove which task-owned files are safe to remove."""


def required_disk_headroom(input_size: int, temp_reserve_bytes: int) -> int:
    if input_size < 0 or temp_reserve_bytes < 0:
        raise ValueError("Disk headroom inputs must be non-negative.")
    return INPUT_HEADROOM_MULTIPLIER * input_size + temp_reserve_bytes


def ensure_disk_headroom(data_dir: str | Path, input_size: int, temp_reserve_bytes: int) -> None:
    """Check conservative free-space headroom; this does not reserve space atomically."""
    required = required_disk_headroom(input_size, temp_reserve_bytes)
    available = shutil.disk_usage(Path(data_dir)).free
    if available < required:
        raise DiskHeadroomError(
            "磁盘可用空间不足：本次导入至少需要 "
            f"{_format_bytes(required)}，当前仅有 {_format_bytes(available)}。"
            "请清理空间或调小待上传文件后重试；此检查不会原子预留磁盘空间。"
        )


def cleanup_stale_files(
    data_dir: str | Path,
    database_path: str | Path,
    *,
    older_than: timedelta = DEFAULT_STALE_AGE,
    now: datetime | None = None,
) -> list[Path]:
    """Remove old task-owned orphans without following links or leaving ``data_dir``."""
    if older_than.total_seconds() < 0:
        raise ValueError("Cleanup age must be non-negative.")
    root = Path(os.path.abspath(data_dir))
    if _is_reparse_point(root):
        raise MaintenanceError("数据目录是符号链接或重解析点，已停止清理以避免越界。")
    references = _load_referenced_paths(Path(database_path))
    cutoff = (now or datetime.now(UTC)).timestamp() - older_than.total_seconds()
    removed: list[Path] = []

    for owned_root_name, patterns in _OWNED_FILES.items():
        owned_root = root / owned_root_name
        if not owned_root.exists() or _is_reparse_point(owned_root):
            continue
        for candidate in _iter_regular_files(owned_root):
            if not any(pattern.fullmatch(candidate.name) for pattern in patterns):
                continue
            if _path_key(candidate) in references:
                continue
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if _stat_is_reparse(metadata) or metadata.st_mtime >= cutoff:
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            removed.append(candidate)

        if owned_root_name == "duckdb-temp":
            _remove_empty_snapshot_directories(owned_root)
    return removed


def _load_referenced_paths(database_path: Path) -> set[str]:
    if not database_path.exists():
        raise MaintenanceError("SQLite 元数据不存在，已停止清理且未删除任何文件。")
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            paths = [row[0] for row in connection.execute("SELECT encrypted_path FROM files")]
            paths.extend(row[0] for row in connection.execute("SELECT path FROM artifacts"))
    except (OSError, sqlite3.Error) as exc:
        raise MaintenanceError("无法安全读取 SQLite 元数据，已停止清理且未删除任何文件。") from exc
    return {_path_key(Path(path)) for path in paths}


def _iter_regular_files(root: Path):
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if _stat_is_reparse(metadata):
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                yield Path(entry.path)


def _remove_empty_snapshot_directories(root: Path) -> None:
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return
    for entry in entries:
        if not entry.name.startswith("sources-"):
            continue
        try:
            metadata = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if _stat_is_reparse(metadata) or not entry.is_dir(follow_symlinks=False):
            continue
        with suppress(FileNotFoundError, OSError):
            Path(entry.path).rmdir()


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_reparse_point(path: Path) -> bool:
    try:
        return _stat_is_reparse(path.lstat())
    except FileNotFoundError:
        return False


def _stat_is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _format_bytes(value: int) -> str:
    gib = value / 1024**3
    return f"{gib:.2f} GiB" if gib >= 1 else f"{value / 1024**2:.2f} MiB"
