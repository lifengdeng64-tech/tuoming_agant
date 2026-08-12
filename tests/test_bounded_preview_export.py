from __future__ import annotations

import pandas as pd
import pyarrow.parquet as pq
import pytest

from tuoming_agent.exporting import (
    EXCEL_MAX_ESTIMATED_BYTES,
    EXCEL_MAX_ROWS,
    ExportLimitError,
    validate_excel_export,
)
from tuoming_agent.security.masking import ColumnPolicy
from tuoming_agent.storage.errors import AuthorizationError

REAL_PARQUET_FILE = pq.ParquetFile


class TrackingParquetFile:
    """Proxy a real ParquetFile while recording the actual batch read bounds."""

    requested_batch_sizes: list[int] = []
    yielded_batch_sizes: list[int] = []

    def __init__(self, *args, **kwargs):
        self._real = REAL_PARQUET_FILE(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def iter_batches(self, *args, **kwargs):
        batch_size = kwargs.get("batch_size", args[0] if args else 65_536)
        self.requested_batch_sizes.append(batch_size)
        for batch in self._real.iter_batches(*args, **kwargs):
            self.yielded_batch_sizes.append(batch.num_rows)
            yield batch


def _save_masked_result(services, workspace, rows: int = 10):
    source = pd.DataFrame(
        {
            "customer": [f"customer-{index % 3}" for index in range(rows)],
            "amount": range(rows),
        }
    )
    masked, lineage = services.masking.mask_dataframe(
        "tenant-a", source, {"customer": ColumnPolicy("CUSTOMER")}
    )
    artifact = services.artifacts.save_result(
        "tenant-a", workspace.id, "result", masked, lineage, ()
    )
    return artifact, source, masked


def test_preview_reads_only_the_requested_rows_and_restores_only_that_chunk(
    services, workspace, monkeypatch
):
    artifact, source, _masked = _save_masked_result(services, workspace, rows=5_000)
    TrackingParquetFile.requested_batch_sizes = []
    TrackingParquetFile.yielded_batch_sizes = []
    monkeypatch.setattr("tuoming_agent.workspace.service.pq.ParquetFile", TrackingParquetFile)
    monkeypatch.setattr(
        services.artifacts.artifact_store,
        "read_dataframe",
        lambda _path: pytest.fail("bounded preview must not read the full dataframe"),
    )

    preview = services.artifacts.preview("tenant-a", artifact.id, limit=7, restored=True)

    pd.testing.assert_frame_equal(preview, source.head(7))
    assert TrackingParquetFile.requested_batch_sizes == [7]
    assert TrackingParquetFile.yielded_batch_sizes == [7]


@pytest.mark.parametrize("limit", [0, 1001])
def test_preview_rejects_limits_outside_one_to_one_thousand(services, workspace, limit):
    artifact, _source, _masked = _save_masked_result(services, workspace)

    with pytest.raises(ValueError, match="between 1 and 1000"):
        services.artifacts.preview("tenant-a", artifact.id, limit=limit)


def test_masked_parquet_export_reuses_the_authorized_file(services, workspace):
    artifact, _source, _masked = _save_masked_result(services, workspace)

    exported = services.artifacts.export("tenant-a", artifact.id, "parquet")

    assert exported.path == artifact.path
    assert exported.file_name == f"masked-{artifact.id[:8]}.parquet"
    assert exported.mime == "application/vnd.apache.parquet"
    assert not exported.temporary


@pytest.mark.parametrize("restored", [False, True])
def test_csv_export_is_file_backed_and_reads_bounded_real_record_batches(
    services, workspace, monkeypatch, restored
):
    artifact, source, masked = _save_masked_result(services, workspace, rows=25_005)
    TrackingParquetFile.requested_batch_sizes = []
    TrackingParquetFile.yielded_batch_sizes = []
    monkeypatch.setattr("tuoming_agent.exporting.pq.ParquetFile", TrackingParquetFile)

    exported = services.artifacts.export(
        "tenant-a", artifact.id, "csv", restored=restored
    )

    assert exported.path.is_file()
    assert exported.path.parent.name == "exports"
    assert exported.temporary
    assert TrackingParquetFile.requested_batch_sizes == [10_000]
    assert TrackingParquetFile.yielded_batch_sizes == [10_000, 10_000, 5_005]
    expected = source if restored else masked
    pd.testing.assert_frame_equal(pd.read_csv(exported.path), expected, check_dtype=False)
    exported.cleanup()
    assert not exported.path.exists()


def test_restored_csv_uses_batch_resolution_instead_of_scalar_lookups(
    services, workspace, monkeypatch
):
    artifact, source, _masked = _save_masked_result(services, workspace, rows=20_001)
    batch_sizes: list[int] = []
    real_resolve_many = services.vault.resolve_many

    def tracking_resolve_many(tenant_id, tokens):
        batch_sizes.append(len(tokens))
        return real_resolve_many(tenant_id, tokens)

    monkeypatch.setattr(services.vault, "resolve_many", tracking_resolve_many)
    monkeypatch.setattr(
        services.vault,
        "resolve",
        lambda *_args: pytest.fail("restored chunks must not resolve tokens one by one"),
    )

    exported = services.artifacts.export("tenant-a", artifact.id, "csv", restored=True)

    pd.testing.assert_frame_equal(pd.read_csv(exported.path), source, check_dtype=False)
    assert batch_sizes == [3, 3, 1]
    exported.cleanup()


def test_export_authorization_is_checked_before_exposing_a_path(services, workspace):
    artifact, _source, _masked = _save_masked_result(services, workspace)
    services.repository.ensure_tenant("tenant-b")

    with pytest.raises(AuthorizationError):
        services.artifacts.export("tenant-b", artifact.id, "parquet")


@pytest.mark.parametrize(
    ("rows", "estimated_bytes"),
    [
        (EXCEL_MAX_ROWS + 1, 1),
        (1, EXCEL_MAX_ESTIMATED_BYTES + 1),
    ],
)
def test_excel_limits_give_actionable_csv_guidance(rows, estimated_bytes):
    with pytest.raises(ExportLimitError, match=r"CSV.*100,000.*50 MiB"):
        validate_excel_export(rows, estimated_bytes)


def test_small_excel_export_is_file_backed(services, workspace):
    artifact, source, _masked = _save_masked_result(services, workspace)

    exported = services.artifacts.export(
        "tenant-a", artifact.id, "xlsx", restored=True
    )

    assert exported.path.is_file()
    assert exported.file_name == f"restored-{artifact.id[:8]}.xlsx"
    pd.testing.assert_frame_equal(pd.read_excel(exported.path), source)
    exported.cleanup()
