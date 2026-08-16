from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class UnsafeUploadError(ValueError):
    """Raised when an upload violates the local file isolation policy."""


@dataclass(frozen=True)
class UploadScanPolicy:
    max_archive_entries: int = 10_000
    max_uncompressed_bytes: int = 512 * 1024 * 1024
    max_entry_bytes: int = 128 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_csv_line_bytes: int = 8 * 1024 * 1024


BLOCKED_EXCEL_PREFIXES = (
    "xl/embeddings/",
    "xl/externallinks/",
    "xl/activex/",
    "xl/vbaproject.bin",
    "xl/vbadata.xml",
    "xl/connections.xml",
    "xl/querytables/",
    "customui/",
)


def scan_upload(
    filename: str,
    source: BinaryIO,
    policy: UploadScanPolicy | None = None,
) -> None:
    """Validate an upload without logging or returning original cell content."""
    policy = policy or UploadScanPolicy()
    suffix = Path(filename).suffix.lower()
    source.seek(0)
    try:
        if suffix == ".csv":
            _scan_csv(source, policy)
        elif suffix in {".xlsx", ".xlsm"}:
            _scan_excel(source, policy)
        else:
            raise UnsafeUploadError("仅支持 CSV、XLSX 和 XLSM 文件。")
    finally:
        source.seek(0)


def _scan_csv(source: BinaryIO, policy: UploadScanPolicy) -> None:
    current_line = 0
    while chunk := source.read(1024 * 1024):
        if b"\x00" in chunk:
            raise UnsafeUploadError("CSV 包含二进制空字节，已阻止读取。")
        for byte in chunk:
            if byte in {10, 13}:
                current_line = 0
            else:
                current_line += 1
                if current_line > policy.max_csv_line_bytes:
                    raise UnsafeUploadError("CSV 存在异常超长行，已阻止读取。")


def _scan_excel(source: BinaryIO, policy: UploadScanPolicy) -> None:
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > policy.max_archive_entries:
                raise UnsafeUploadError("Excel 压缩包文件项过多，疑似压缩炸弹。")
            total_size = 0
            seen: set[str] = set()
            for entry in entries:
                normalized = _safe_archive_name(entry.filename)
                folded = normalized.casefold()
                if folded in seen:
                    raise UnsafeUploadError("Excel 包含重复内部路径，已阻止读取。")
                seen.add(folded)
                if entry.flag_bits & 0x1:
                    raise UnsafeUploadError("不支持加密的 Excel 压缩内容。")
                if any(folded.startswith(prefix) for prefix in BLOCKED_EXCEL_PREFIXES):
                    raise UnsafeUploadError("Excel 包含宏、外部连接、嵌入对象或 ActiveX，已隔离。")
                if entry.file_size > policy.max_entry_bytes:
                    raise UnsafeUploadError("Excel 内部单个文件过大，疑似压缩炸弹。")
                total_size += entry.file_size
                if total_size > policy.max_uncompressed_bytes:
                    raise UnsafeUploadError("Excel 解压后体积超过安全上限。")
                compressed = max(entry.compress_size, 1)
                if entry.file_size / compressed > policy.max_compression_ratio:
                    raise UnsafeUploadError("Excel 压缩比异常，疑似压缩炸弹。")
    except zipfile.BadZipFile as exc:
        raise UnsafeUploadError("Excel 文件结构无效。") from exc


def _safe_archive_name(name: str) -> str:
    normalized = posixpath.normpath(name.replace("\\", "/"))
    path = PurePosixPath(normalized)
    if (
        normalized.startswith("/")
        or normalized == ".."
        or normalized.startswith("../")
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise UnsafeUploadError("Excel 包含不安全的内部路径。")
    return normalized