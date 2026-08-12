from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd


class UnsupportedFileError(ValueError):
    """Raised for unsupported or unreadable uploads."""


@dataclass(frozen=True)
class ParsedTable:
    logical_name: str
    sheet_name: str | None
    dataframe: pd.DataFrame


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
