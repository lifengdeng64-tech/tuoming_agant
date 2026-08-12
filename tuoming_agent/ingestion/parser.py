from __future__ import annotations

import codecs
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from itertools import islice
from pathlib import Path
from typing import BinaryIO

import openpyxl
import pandas as pd


class UnsupportedFileError(ValueError):
    """Raised for unsupported or unreadable uploads."""


@dataclass(frozen=True)
class ParsedTable:
    logical_name: str
    sheet_name: str | None
    dataframe: pd.DataFrame


def iter_file_chunks(
    filename: str, source: BinaryIO, chunk_rows: int = 50_000
) -> Iterator[ParsedTable]:
    if chunk_rows < 1:
        raise ValueError("chunk_rows must be positive.")
    suffix = Path(filename).suffix.lower()
    stem = Path(filename).stem.strip() or "dataset"
    if suffix == ".csv":
        encoding = _detect_csv_encoding(source)
        source.seek(0)
        try:
            for dataframe in pd.read_csv(source, encoding=encoding, chunksize=chunk_rows):
                yield ParsedTable(stem, None, dataframe)
        except (UnicodeDecodeError, pd.errors.ParserError) as exc:
            raise UnsupportedFileError("CSV structure could not be parsed.") from exc
        return
    if suffix in {".xlsx", ".xlsm"}:
        try:
            source.seek(0)
            workbook = openpyxl.load_workbook(
                source, read_only=True, data_only=True, keep_vba=False
            )
            try:
                for worksheet in workbook.worksheets:
                    rows = worksheet.iter_rows(values_only=True)
                    header = next(rows, None)
                    if header is None:
                        continue
                    columns = _normalize_excel_headers(header)
                    while batch := list(islice(rows, chunk_rows)):
                        yield ParsedTable(
                            f"{stem}::{worksheet.title}",
                            worksheet.title,
                            pd.DataFrame(batch, columns=columns),
                        )
            finally:
                workbook.close()
        except Exception as exc:
            if isinstance(exc, UnsupportedFileError):
                raise
            raise UnsupportedFileError("Excel workbook could not be parsed.") from exc
        return
    raise UnsupportedFileError("Only CSV, XLSX and XLSM files are supported.")


def parse_file(filename: str, content: bytes) -> list[ParsedTable]:
    suffix = Path(filename).suffix.lower()
    stem = Path(filename).stem.strip() or "dataset"
    if suffix == ".csv":
        return [ParsedTable(stem, None, _read_csv(content))]
    if suffix in {".xlsx", ".xlsm"}:
        return _read_excel(stem, content)
    raise UnsupportedFileError("Only CSV, XLSX and XLSM files are supported.")


def preview_file(
    filename: str, source: BinaryIO, sample_rows: int = 500
) -> list[ParsedTable]:
    """Read only the first rows of each uploaded table for policy selection."""
    suffix = Path(filename).suffix.lower()
    stem = Path(filename).stem.strip() or "dataset"
    if suffix == ".csv":
        return [ParsedTable(stem, None, _read_csv(source, sample_rows))]
    if suffix in {".xlsx", ".xlsm"}:
        return _read_excel(stem, source, sample_rows)
    raise UnsupportedFileError("Only CSV, XLSX and XLSM files are supported.")


def _read_csv(content: bytes | BinaryIO, sample_rows: int | None = None) -> pd.DataFrame:
    errors: list[str] = []
    source = BytesIO(content) if isinstance(content, bytes) else content
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5"):
        try:
            source.seek(0)
            return pd.read_csv(source, encoding=encoding, nrows=sample_rows)
        except UnicodeDecodeError:
            errors.append(encoding)
        except pd.errors.ParserError as exc:
            raise UnsupportedFileError("CSV structure could not be parsed.") from exc
    raise UnsupportedFileError(f"CSV encoding is unsupported; attempted: {', '.join(errors)}")


def _detect_csv_encoding(source: BinaryIO, sample_size: int = 64 * 1024) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5"):
        source.seek(0)
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        try:
            while chunk := source.read(sample_size):
                decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
            return encoding
        except UnicodeDecodeError:
            continue
    raise UnsupportedFileError("CSV encoding is unsupported.")


def _normalize_excel_headers(header: tuple[object, ...]) -> list[str]:
    columns: list[str] = []
    counts: dict[str, int] = {}
    for index, value in enumerate(header):
        base = f"Unnamed: {index}" if value is None or str(value) == "" else str(value)
        occurrence = counts.get(base, 0)
        candidate = base if occurrence == 0 else f"{base}.{occurrence}"
        while candidate in counts:
            occurrence += 1
            candidate = f"{base}.{occurrence}"
        counts[base] = occurrence + 1
        counts[candidate] = 1
        columns.append(candidate)
    return columns


def _read_excel(
    stem: str, content: bytes | BinaryIO, sample_rows: int | None = None
) -> list[ParsedTable]:
    try:
        source = BytesIO(content) if isinstance(content, bytes) else content
        source.seek(0)
        workbook = pd.ExcelFile(source, engine="openpyxl")
        return [
            ParsedTable(
                logical_name=f"{stem}::{sheet_name}",
                sheet_name=sheet_name,
                dataframe=workbook.parse(sheet_name, nrows=sample_rows),
            )
            for sheet_name in workbook.sheet_names
        ]
    except Exception as exc:
        raise UnsupportedFileError("Excel workbook could not be parsed.") from exc
