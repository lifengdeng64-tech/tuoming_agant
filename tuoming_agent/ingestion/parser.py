from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

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


def _read_csv(content: bytes) -> pd.DataFrame:
    errors: list[str] = []
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5"):
        try:
            return pd.read_csv(BytesIO(content), encoding=encoding)
        except UnicodeDecodeError:
            errors.append(encoding)
        except pd.errors.ParserError as exc:
            raise UnsupportedFileError("CSV structure could not be parsed.") from exc
    raise UnsupportedFileError(f"CSV encoding is unsupported; attempted: {', '.join(errors)}")


def _read_excel(stem: str, content: bytes) -> list[ParsedTable]:
    try:
        workbook = pd.ExcelFile(BytesIO(content), engine="openpyxl")
        return [
            ParsedTable(
                logical_name=f"{stem}::{sheet_name}",
                sheet_name=sheet_name,
                dataframe=workbook.parse(sheet_name),
            )
            for sheet_name in workbook.sheet_names
        ]
    except Exception as exc:
        raise UnsupportedFileError("Excel workbook could not be parsed.") from exc
