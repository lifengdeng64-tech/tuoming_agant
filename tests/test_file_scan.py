from __future__ import annotations

import io
import zipfile

import pytest

from tuoming_agent.security.file_scan import UnsafeUploadError, UploadScanPolicy, scan_upload


def _archive(entries: dict[str, bytes]) -> io.BytesIO:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    target.seek(0)
    return target


def test_safe_excel_package_is_accepted() -> None:
    source = _archive({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<x/>"})
    scan_upload("safe.xlsx", source)
    assert source.tell() == 0


@pytest.mark.parametrize(
    "entry",
    [
        "xl/embeddings/oleObject1.bin",
        "xl/externalLinks/externalLink1.xml",
        "xl/activeX/a.bin",
        "xl/vbaProject.bin",
        "xl/connections.xml",
        "xl/queryTables/queryTable1.xml",
    ],
)
def test_embedded_or_external_excel_content_is_blocked(entry: str) -> None:
    with pytest.raises(UnsafeUploadError, match="宏、外部连接"):
        scan_upload("unsafe.xlsx", _archive({entry: b"payload"}))


def test_excel_zip_bomb_ratio_is_blocked() -> None:
    policy = UploadScanPolicy(max_compression_ratio=5)
    with pytest.raises(UnsafeUploadError, match="压缩比异常"):
        scan_upload("bomb.xlsx", _archive({"xl/worksheets/sheet1.xml": b"0" * 100_000}), policy)


def test_archive_path_traversal_is_blocked() -> None:
    with pytest.raises(UnsafeUploadError, match="内部路径"):
        scan_upload("unsafe.xlsx", _archive({"../outside.bin": b"x"}))


def test_binary_csv_is_blocked_without_exposing_content() -> None:
    secret = b"private-value\x00tail"
    with pytest.raises(UnsafeUploadError) as captured:
        scan_upload("unsafe.csv", io.BytesIO(secret))
    assert "private-value" not in str(captured.value)