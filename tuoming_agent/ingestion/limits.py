from __future__ import annotations

from pathlib import Path

MIB = 1024 * 1024
_SIZE_LIMITS = {
    ".csv": 200 * MIB,
    ".xlsx": 100 * MIB,
    ".xlsm": 100 * MIB,
}


def validate_upload_size(filename: str, size: int) -> None:
    """Raise when an upload exceeds its format-specific local processing limit."""
    suffix = Path(filename).suffix.lower()
    try:
        limit = _SIZE_LIMITS[suffix]
    except KeyError as exc:
        raise ValueError("Only CSV, XLSX and XLSM files are supported.") from exc
    if size > limit:
        raise ValueError(
            f"{suffix[1:].upper()} uploads must be {limit // MIB} MiB or smaller."
        )
