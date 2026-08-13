from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
import pyarrow.parquet as pq

from tuoming_agent.models import ArtifactRecord
from tuoming_agent.security.masking import MaskingService

EXPORT_BATCH_ROWS = 10_000
EXCEL_MAX_ROWS = 100_000
EXCEL_MAX_ESTIMATED_BYTES = 50 * 1024 * 1024


class ExportLimitError(ValueError):
    """Raised when a result is too large for the deliberately bounded Excel path."""


@dataclass(frozen=True)
class ExportedFile:
    path: Path
    file_name: str
    mime: str
    temporary: bool

    def cleanup(self) -> None:
        if self.temporary:
            self.path.unlink(missing_ok=True)


def validate_excel_export(row_count: int, estimated_bytes: int) -> None:
    """Enforce the Excel safety envelope: 100,000 rows and 50 MiB estimated data."""
    if row_count > EXCEL_MAX_ROWS or estimated_bytes > EXCEL_MAX_ESTIMATED_BYTES:
        raise ExportLimitError(
            "Choose CSV for this result. Excel export is limited to 100,000 rows "
            "and a 50 MiB estimated uncompressed dataset."
        )


def estimate_parquet_bytes(parquet: pq.ParquetFile) -> int:
    """Estimate in-memory data bytes from exact Parquet row-group metadata."""
    return sum(
        parquet.metadata.row_group(index).total_byte_size
        for index in range(parquet.metadata.num_row_groups)
    )


def prepare_export(
    artifact: ArtifactRecord,
    masking: MaskingService,
    tenant_id: str,
    export_root: Path,
    format: Literal["csv", "parquet", "xlsx"],
    *,
    restored: bool = False,
) -> ExportedFile:
    prefix = "restored" if restored else "masked"
    if format == "parquet":
        if restored:
            raise ValueError("Restored Parquet export is not supported; choose restored CSV.")
        return ExportedFile(
            artifact.path,
            f"{prefix}-{artifact.id[:8]}.parquet",
            "application/vnd.apache.parquet",
            False,
        )

    export_root.mkdir(parents=True, exist_ok=True)
    suffix = f".{format}"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{prefix}-{artifact.id[:8]}-", suffix=suffix, dir=export_root
    )
    os.close(descriptor)
    target = Path(temporary_name)
    try:
        parquet = pq.ParquetFile(artifact.path)
        if format == "xlsx":
            validate_excel_export(artifact.row_count, estimate_parquet_bytes(parquet))
            _write_excel(parquet, target, masking, tenant_id, artifact, restored)
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif format == "csv":
            _write_csv(parquet, target, masking, tenant_id, artifact, restored)
            mime = "text/csv"
        else:
            raise ValueError("Export format must be csv, parquet, or xlsx.")
        return ExportedFile(target, f"{prefix}-{artifact.id[:8]}{suffix}", mime, True)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _iter_frames(
    parquet: pq.ParquetFile,
    masking: MaskingService,
    tenant_id: str,
    artifact: ArtifactRecord,
    restored: bool,
):
    for batch in parquet.iter_batches(batch_size=EXPORT_BATCH_ROWS):
        frame = batch.to_pandas()
        yield masking.unmask_dataframe(tenant_id, frame, artifact.lineage) if restored else frame


def _write_csv(
    parquet: pq.ParquetFile,
    target: Path,
    masking: MaskingService,
    tenant_id: str,
    artifact: ArtifactRecord,
    restored: bool,
) -> None:
    first = True
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        for frame in _iter_frames(parquet, masking, tenant_id, artifact, restored):
            frame.to_csv(stream, index=False, header=first)
            first = False
        if first:
            parquet.schema_arrow.empty_table().to_pandas().to_csv(stream, index=False)


def _write_excel(
    parquet: pq.ParquetFile,
    target: Path,
    masking: MaskingService,
    tenant_id: str,
    artifact: ArtifactRecord,
    restored: bool,
) -> None:
    next_row = 0
    first = True
    with pd.ExcelWriter(target, engine="xlsxwriter") as writer:
        for frame in _iter_frames(parquet, masking, tenant_id, artifact, restored):
            frame.to_excel(
                writer,
                index=False,
                sheet_name="Result",
                startrow=next_row,
                header=first,
            )
            next_row += len(frame) + (1 if first else 0)
            first = False
        if first:
            parquet.schema_arrow.empty_table().to_pandas().to_excel(
                writer, index=False, sheet_name="Result"
            )
