from __future__ import annotations

import hashlib
import os
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tuoming_agent.ingestion.limits import validate_upload_size
from tuoming_agent.ingestion.parser import iter_file_chunks, preview_file
from tuoming_agent.ingestion.service import UnsafeIngestionError
from tuoming_agent.security.crypto import derive_key
from tuoming_agent.storage.files import ArtifactStore, SecureFileStore
from tuoming_agent.ui import app as ui_app

MIB = 1024 * 1024


class BoundedReadStream(BytesIO):
    """A real seekable stream that rejects attempts to materialize all remaining bytes."""

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("stream consumers must use bounded reads")
        return super().read(size)

    def read1(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("stream consumers must use bounded reads")
        return super().read(size)


@pytest.mark.parametrize(
    ("filename", "limit"),
    [("report.csv", 200 * MIB), ("report.xlsx", 100 * MIB), ("report.xlsm", 100 * MIB)],
)
def test_upload_size_accepts_exact_file_limit(filename: str, limit: int) -> None:
    validate_upload_size(filename, limit)


@pytest.mark.parametrize(
    ("filename", "limit"),
    [("report.csv", 200 * MIB), ("report.xlsx", 100 * MIB), ("report.xlsm", 100 * MIB)],
)
def test_upload_size_rejects_files_larger_than_limit(filename: str, limit: int) -> None:
    with pytest.raises(ValueError):
        validate_upload_size(filename, limit + 1)


def test_ingestion_rejects_an_oversized_upload_before_parsing_or_writing(
    services, workspace
) -> None:
    class OversizedCsv(bytes):
        def __len__(self) -> int:
            return 200 * MIB + 1

    content = OversizedCsv(b"value\n1\n")

    with pytest.raises(ValueError, match="CSV uploads must be 200 MiB or smaller"):
        services.ingestion.ingest(
            "tenant-a", workspace.id, "too-large.csv", content, policies={}
        )

    assert services.repository.list_files("tenant-a", workspace.id) == []
    assert services.repository.list_artifacts("tenant-a", workspace.id) == []


def test_csv_preview_reads_at_most_default_sample_rows() -> None:
    content = pd.DataFrame({"value": range(501)}).to_csv(index=False).encode("utf-8")

    tables = preview_file("report.csv", BytesIO(content))

    assert len(tables) == 1
    assert tables[0].logical_name == "report"
    assert tables[0].dataframe["value"].tolist() == list(range(500))


def test_excel_preview_reads_at_most_default_sample_rows_per_sheet() -> None:
    content = BytesIO()
    with pd.ExcelWriter(content, engine="openpyxl") as writer:
        pd.DataFrame({"value": range(501)}).to_excel(writer, sheet_name="First", index=False)
        pd.DataFrame({"value": range(501, 1002)}).to_excel(writer, sheet_name="Second", index=False)

    tables = preview_file("report.xlsx", BytesIO(content.getvalue()))

    assert [len(table.dataframe) for table in tables] == [500, 500]
    assert tables[1].dataframe["value"].tolist() == list(range(501, 1001))


def test_ui_rejects_an_oversized_upload_before_reading_its_content(monkeypatch) -> None:
    class OversizedUpload:
        name = "too-large.csv"
        size = 200 * MIB + 1

        def getvalue(self) -> bytes:
            raise AssertionError("the UI must reject an oversized file before getvalue()")

    class EmptyContainer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    errors: list[str] = []
    monkeypatch.setattr(ui_app.st, "file_uploader", lambda *args, **kwargs: [OversizedUpload()])
    monkeypatch.setattr(ui_app.st, "error", errors.append)
    monkeypatch.setattr(ui_app.st, "divider", lambda: None)
    monkeypatch.setattr(
        ui_app.st,
        "columns",
        lambda *args, **kwargs: [EmptyContainer(), EmptyContainer()],
    )
    monkeypatch.setattr(ui_app.st, "button", lambda *args, **kwargs: False)
    monkeypatch.setattr(ui_app, "_section_heading", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_app, "_empty_state", lambda *args, **kwargs: None)

    ui_app._render_data_view(object(), "tenant", "workspace", [], [])

    assert errors == ["too-large.csv: CSV uploads must be 200 MiB or smaller."]


def test_secure_file_store_streams_encryption_and_reads_legacy_files(tmp_path: Path) -> None:
    store = SecureFileStore(tmp_path, b"test-master-key-material-32-bytes!")
    content = (b"streaming-content-" * 1000) + b"end"

    path, content_hash, byte_size = store.write_stream(
        "tenant-a", "workspace-a", BoundedReadStream(content), chunk_size=257
    )

    assert content_hash == hashlib.sha256(content).hexdigest()
    assert byte_size == len(content)
    assert store.read("tenant-a", "workspace-a", content_hash, path) == content

    legacy_hash = hashlib.sha256(b"legacy").hexdigest()
    legacy_path = tmp_path / "legacy.enc"
    key = derive_key(store.master_key, "upload", "tenant-a")
    aad = store._aad("tenant-a", "workspace-a", legacy_hash)
    nonce = os.urandom(12)
    legacy_path.write_bytes(nonce + AESGCM(key).encrypt(nonce, b"legacy", aad))
    assert store.read("tenant-a", "workspace-a", legacy_hash, legacy_path) == b"legacy"


def test_secure_stream_rejects_tampering_and_truncation(tmp_path: Path) -> None:
    store = SecureFileStore(tmp_path, b"test-master-key-material-32-bytes!")
    path, content_hash, _ = store.write_stream(
        "tenant-a", "workspace-a", BytesIO(b"authenticated payload"), chunk_size=7
    )
    original = path.read_bytes()

    tampered = bytearray(original)
    tampered[-20] ^= 1
    path.write_bytes(tampered)
    with pytest.raises((InvalidTag, ValueError)):
        store.read("tenant-a", "workspace-a", content_hash, path)

    path.write_bytes(original[:-1])
    with pytest.raises((InvalidTag, ValueError)):
        store.read("tenant-a", "workspace-a", content_hash, path)


def test_csv_parser_yields_bounded_row_chunks_without_unbounded_reads() -> None:
    content = pd.DataFrame({"name": ["same", "other", "same", "last"], "value": range(4)})
    source = BoundedReadStream(content.to_csv(index=False).encode())

    chunks = list(iter_file_chunks("records.csv", source, chunk_rows=2))

    assert [len(chunk.dataframe) for chunk in chunks] == [2, 2]
    assert [chunk.logical_name for chunk in chunks] == ["records", "records"]
    assert pd.concat([chunk.dataframe for chunk in chunks], ignore_index=True).equals(content)


def test_excel_parser_uses_cached_formula_values_and_row_chunks() -> None:
    source = BytesIO()
    with pd.ExcelWriter(source, engine="xlsxwriter") as writer:
        worksheet = writer.book.add_worksheet("Cached")
        writer.sheets["Cached"] = worksheet
        worksheet.write_row(0, 0, ["name", "calculated"])
        worksheet.write_row(1, 0, ["first"])
        worksheet.write_formula(1, 1, "=1+2", None, 3)
        worksheet.write_row(2, 0, ["second", 4])
        worksheet.write_row(3, 0, ["third", 5])

    chunks = list(iter_file_chunks("book.xlsx", BytesIO(source.getvalue()), chunk_rows=2))

    assert [len(chunk.dataframe) for chunk in chunks] == [2, 1]
    assert chunks[0].sheet_name == "Cached"
    assert chunks[0].dataframe["calculated"].tolist() == [3, 4]


def test_artifact_store_writes_multiple_chunks_with_one_schema(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    chunks = [
        pd.DataFrame({"token": ["A", "B"], "amount": [1, 2]}),
        pd.DataFrame({"token": ["A"], "amount": [3]}),
    ]

    path = store.write_chunks("tenant-a", "workspace-a", "artifact-a", iter(chunks))

    assert store.read_dataframe(path).to_dict("records") == [
        {"token": "A", "amount": 1},
        {"token": "B", "amount": 2},
        {"token": "A", "amount": 3},
    ]
    assert list(path.parent.glob("*.tmp.parquet")) == []


def test_ingestion_validates_sensitive_values_discovered_in_a_later_batch(
    services, workspace
) -> None:
    content = pd.DataFrame(
        {"value": (["ordinary"] * 50_000) + ["late@example.com"]}
    ).to_csv(index=False).encode()

    with pytest.raises(UnsafeIngestionError, match="value"):
        services.ingestion.ingest(
            "tenant-a", workspace.id, "late.csv", content, {"late": {}}
        )

    assert services.repository.list_files("tenant-a", workspace.id) == []
    assert services.repository.list_artifacts("tenant-a", workspace.id) == []
    assert list((services.ingestion.artifact_store.root / "artifacts").rglob("*.parquet")) == []


def test_ingestion_does_not_publish_metadata_when_chunked_parquet_write_fails(
    monkeypatch, services, workspace
) -> None:
    def fail_write(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(services.ingestion.artifact_store, "write_chunks", fail_write)
    content = pd.DataFrame({"value": [1, 2]}).to_csv(index=False).encode()

    with pytest.raises(OSError, match="simulated disk failure"):
        services.ingestion.ingest(
            "tenant-a", workspace.id, "failure.csv", content, {"failure": {}}
        )

    assert services.repository.list_files("tenant-a", workspace.id) == []
    assert services.repository.list_artifacts("tenant-a", workspace.id) == []
