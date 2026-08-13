from __future__ import annotations

import hashlib
import os
import shutil
import struct
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event, local

import pandas as pd
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from tuoming_agent.ingestion.limits import validate_upload_size
from tuoming_agent.ingestion.parser import iter_file_chunks, preview_file
from tuoming_agent.ingestion.service import UnsafeIngestionError
from tuoming_agent.maintenance import (
    DiskHeadroomError,
    MaintenanceError,
    cleanup_stale_files,
    ensure_disk_headroom,
)
from tuoming_agent.security.crypto import STREAM_MAGIC, derive_key
from tuoming_agent.security.masking import ColumnPolicy
from tuoming_agent.storage.files import ArtifactStore, SecureFileStore
from tuoming_agent.ui import app as ui_app

MIB = 1024 * 1024
GIB = 1024 * MIB


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


def test_ui_accepted_upload_never_calls_getvalue(monkeypatch) -> None:
    class AcceptedUpload(BytesIO):
        name = "accepted.csv"
        size = len(b"value\n1\n")

        def getvalue(self) -> bytes:
            raise AssertionError("accepted uploads must stay stream based")

    class EmptyContainer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    uploaded = AcceptedUpload(b"value\n1\n")
    monkeypatch.setattr(ui_app.st, "file_uploader", lambda *args, **kwargs: [uploaded])
    monkeypatch.setattr(ui_app.st, "expander", lambda *args, **kwargs: EmptyContainer())
    monkeypatch.setattr(ui_app.st, "data_editor", lambda frame, **kwargs: frame)
    monkeypatch.setattr(ui_app.st, "dataframe", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_app.st, "markdown", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_app.st, "caption", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_app.st, "divider", lambda: None)
    monkeypatch.setattr(
        ui_app.st, "columns", lambda *args, **kwargs: [EmptyContainer(), EmptyContainer()]
    )
    monkeypatch.setattr(ui_app.st, "button", lambda *args, **kwargs: False)
    monkeypatch.setattr(ui_app, "_section_heading", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_app, "_empty_state", lambda *args, **kwargs: None)

    ui_app._render_data_view(object(), "tenant", "workspace", [], [])


def test_secure_file_store_streams_encryption_and_reads_legacy_files(tmp_path: Path) -> None:
    store = SecureFileStore(tmp_path, b"test-master-key-material-32-bytes!")
    content = (b"streaming-content-" * 1000) + b"end"

    path, content_hash, byte_size, created = store.write_stream(
        "tenant-a", "workspace-a", BoundedReadStream(content), chunk_size=257
    )

    assert content_hash == hashlib.sha256(content).hexdigest()
    assert byte_size == len(content)
    assert created is True
    assert store.read("tenant-a", "workspace-a", content_hash, path) == content

    legacy_hash = hashlib.sha256(b"legacy").hexdigest()
    legacy_path = tmp_path / "legacy.enc"
    key = derive_key(store.master_key, "upload", "tenant-a")
    aad = store._aad("tenant-a", "workspace-a", legacy_hash)
    nonce = os.urandom(12)
    legacy_path.write_bytes(nonce + AESGCM(key).encrypt(nonce, b"legacy", aad))
    assert store.read("tenant-a", "workspace-a", legacy_hash, legacy_path) == b"legacy"


def test_secure_stream_attempts_use_unique_owned_final_paths(tmp_path: Path) -> None:
    store = SecureFileStore(tmp_path, b"test-master-key-material-32-bytes!")
    content = b"same content"

    first = store.write_stream("tenant-a", "workspace-a", BytesIO(content))
    second = store.write_stream("tenant-a", "workspace-a", BytesIO(content))

    assert first[0] != second[0]
    assert first[0].exists() and second[0].exists()
    assert first[3] is True and second[3] is True


def test_secure_stream_rejects_tampering_and_truncation(tmp_path: Path) -> None:
    store = SecureFileStore(tmp_path, b"test-master-key-material-32-bytes!")
    path, content_hash, _, _ = store.write_stream(
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


def test_secure_stream_rejects_oversized_frame_before_payload_read(tmp_path: Path) -> None:
    class HeaderOnlyStream(BytesIO):
        def read(self, size: int = -1) -> bytes:
            if size > 1024:
                raise AssertionError("malicious length must not drive a huge read")
            return super().read(size)

    payload = STREAM_MAGIC + struct.pack(">BQI", 0, 0, 0xFFFFFFFF)
    path = tmp_path / "malicious.enc"
    path.write_bytes(payload)
    original_open = Path.open

    def bounded_open(selected, *args, **kwargs):
        if selected == path:
            return HeaderOnlyStream(payload)
        return original_open(selected, *args, **kwargs)

    store = SecureFileStore(tmp_path, b"test-master-key-material-32-bytes!")
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(Path, "open", bounded_open)
        with pytest.raises(ValueError, match="frame length"):
            store.read("tenant-a", "workspace-a", "0" * 64, path)

def test_csv_parser_yields_bounded_row_chunks_without_unbounded_reads() -> None:
    content = pd.DataFrame({"name": ["same", "other", "same", "last"], "value": range(4)})
    source = BoundedReadStream(content.to_csv(index=False).encode())

    chunks = list(iter_file_chunks("records.csv", source, chunk_rows=2))

    assert [len(chunk.dataframe) for chunk in chunks] == [2, 2]
    assert [chunk.logical_name for chunk in chunks] == ["records", "records"]
    assert pd.concat([chunk.dataframe for chunk in chunks], ignore_index=True).equals(content)


def test_csv_encoding_retries_after_long_ascii_prefix_before_gbk_text() -> None:
    prefix = "value\n" + ("ascii\n" * 12_000)
    payload = prefix.encode("ascii") + "中文\n".encode("gbk")

    chunks = list(iter_file_chunks("gbk.csv", BoundedReadStream(payload), chunk_rows=20_000))

    assert chunks[0].dataframe.iloc[-1, 0] == "中文"


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


def test_excel_parser_makes_blank_and_duplicate_headers_unique() -> None:
    source = BytesIO()
    with pd.ExcelWriter(source, engine="xlsxwriter") as writer:
        worksheet = writer.book.add_worksheet("Headers")
        writer.sheets["Headers"] = worksheet
        worksheet.write_row(0, 0, [None, "value", "value"])
        worksheet.write_row(1, 0, ["ordinary", "safe", "safe"])
        worksheet.write_row(2, 0, ["ordinary", "safe", "late@example.com"])

    chunks = list(iter_file_chunks("book.xlsx", BytesIO(source.getvalue()), chunk_rows=10))

    assert chunks[0].dataframe.columns.tolist() == ["Unnamed: 0", "value", "value.1"]
    assert chunks[0].dataframe.loc[1, "value.1"] == "late@example.com"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        (("a", "a", "a.1"), ["a", "a.2", "a.1"]),
        ((None, "Unnamed: 0"), ["Unnamed: 0.1", "Unnamed: 0"]),
    ],
)
def test_excel_preview_and_ingestion_share_collision_safe_headers(headers, expected) -> None:
    source = BytesIO()
    with pd.ExcelWriter(source, engine="xlsxwriter") as writer:
        worksheet = writer.book.add_worksheet("Headers")
        writer.sheets["Headers"] = worksheet
        worksheet.write_row(0, 0, list(headers))
        worksheet.write_row(1, 0, ["safe"] * len(headers))
    content = source.getvalue()

    preview = preview_file("book.xlsx", BytesIO(content))[0]
    ingested = list(iter_file_chunks("book.xlsx", BytesIO(content)))[0]

    assert preview.dataframe.columns.tolist() == expected
    assert ingested.dataframe.columns.tolist() == expected


def test_excel_preview_policy_name_masks_same_sensitive_column_during_ingestion(
    services, workspace
) -> None:
    source = BytesIO()
    with pd.ExcelWriter(source, engine="xlsxwriter") as writer:
        worksheet = writer.book.add_worksheet("Headers")
        writer.sheets["Headers"] = worksheet
        worksheet.write_row(0, 0, ["a", "a", "a.1"])
        worksheet.write_row(1, 0, ["safe", "safe", "late@example.com"])
    content = source.getvalue()
    logical_name = "book::Headers"
    preview = preview_file("book.xlsx", BytesIO(content))[0]
    sensitive_column = preview.dataframe.columns[2]

    result = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "book.xlsx",
        content,
        {logical_name: {sensitive_column: ColumnPolicy("email")}},
    )

    masked = services.ingestion.artifact_store.read_dataframe(result.artifacts[0].path)
    assert sensitive_column == "a.1"
    assert masked.loc[0, sensitive_column].startswith("EMAIL_V1_")


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


def test_ingestion_scans_past_first_200_values_within_each_chunk(services, workspace) -> None:
    content = pd.DataFrame(
        {"value": (["ordinary"] * 201) + ["late@example.com"]}
    ).to_csv(index=False).encode()

    with pytest.raises(UnsafeIngestionError, match="value"):
        services.ingestion.ingest(
            "tenant-a", workspace.id, "within.csv", content, {"within": {}}
        )

    assert services.repository.list_files("tenant-a", workspace.id) == []


def test_atomic_publication_rolls_back_all_metadata_on_mid_transaction_failure(
    services, workspace
) -> None:
    content = pd.DataFrame({"value": [1]}).to_csv(index=False).encode()
    original = services.repository._publish_artifact

    def fail_after_insert(connection, artifact):
        original(connection, artifact)
        raise RuntimeError("mid-publication failure")

    services.repository._publish_artifact = fail_after_insert
    try:
        with pytest.raises(RuntimeError, match="mid-publication failure"):
            services.ingestion.ingest(
                "tenant-a", workspace.id, "atomic.csv", content, {"atomic": {}}
            )
    finally:
        services.repository._publish_artifact = original

    assert services.repository.list_files("tenant-a", workspace.id) == []
    assert services.repository.list_artifacts("tenant-a", workspace.id) == []
    assert services.repository.list_datasets("tenant-a", workspace.id) == []
    assert list((services.ingestion.artifact_store.root / "artifacts").rglob("*.parquet")) == []

    retry = services.ingestion.ingest(
        "tenant-a", workspace.id, "atomic.csv", content, {"atomic": {}}
    )
    assert retry.duplicate is False
    assert len(services.repository.list_files("tenant-a", workspace.id)) == 1


def test_source_mutation_removes_new_encrypted_orphan(monkeypatch, services, workspace) -> None:
    orphan = services.ingestion.secure_file_store.root / "mutated.enc"

    def mismatched_stream_write(*args, **kwargs):
        orphan.write_bytes(b"orphan")
        return orphan, "f" * 64, 999, True

    monkeypatch.setattr(
        services.ingestion.secure_file_store, "write_stream", mismatched_stream_write
    )
    content = pd.DataFrame({"value": [1]}).to_csv(index=False).encode()

    with pytest.raises(ValueError, match="changed"):
        services.ingestion.ingest(
            "tenant-a", workspace.id, "mutated.csv", content, {"mutated": {}}
        )

    assert not orphan.exists()


def test_mutation_to_preexisting_original_never_deletes_winner(
    monkeypatch, services, workspace
) -> None:
    winner_content = pd.DataFrame({"value": [1]}).to_csv(index=False).encode()
    winner = services.ingestion.ingest(
        "tenant-a", workspace.id, "winner.csv", winner_content, {"winner": {}}
    )
    file_record = services.repository.find_file_by_hash(
        "tenant-a", workspace.id, winner.content_hash
    )
    winner_path = Path(file_record["encrypted_path"])
    original_write_stream = services.ingestion.secure_file_store.write_stream

    def changed_to_existing(*args, **kwargs):
        path, content_hash, byte_size, _created = original_write_stream(*args, **kwargs)
        return winner_path, winner.content_hash, byte_size, False

    monkeypatch.setattr(
        services.ingestion.secure_file_store, "write_stream", changed_to_existing
    )
    different = pd.DataFrame({"value": [2]}).to_csv(index=False).encode()

    with pytest.raises(ValueError, match="changed"):
        services.ingestion.ingest(
            "tenant-a", workspace.id, "changed.csv", different, {"changed": {}}
        )

    assert services.ingestion.secure_file_store.read(
        "tenant-a", workspace.id, winner.content_hash, winner_path
    ) == winner_content


def test_duplicate_race_cleans_loser_files_and_returns_existing_result(
    monkeypatch, services, workspace
) -> None:
    content = pd.DataFrame({"value": [1]}).to_csv(index=False).encode()
    first = services.ingestion.ingest(
        "tenant-a", workspace.id, "race.csv", content, {"race": {}}
    )
    file_record = services.repository.find_file_by_hash(
        "tenant-a", workspace.id, first.content_hash
    )
    monkeypatch.setattr(services.repository, "find_file_by_hash", lambda *args: None)

    duplicate = services.ingestion.ingest(
        "tenant-a", workspace.id, "race.csv", content, {"race": {}}
    )

    assert duplicate.duplicate is True
    assert duplicate.file_id == first.file_id
    assert duplicate.artifacts == first.artifacts
    assert len(list((services.ingestion.artifact_store.root / "artifacts").rglob("*.parquet"))) == 1
    assert services.ingestion.secure_file_store.read(
        "tenant-a", workspace.id, first.content_hash, file_record["encrypted_path"]
    ) == content


def test_concurrent_duplicate_ingestions_publish_one_complete_version(
    monkeypatch, services, workspace
) -> None:
    content = pd.DataFrame({"value": [1, 2]}).to_csv(index=False).encode()
    barrier = Barrier(2)
    original_find = services.repository.find_file_by_hash

    def synchronized_find(*args):
        result = original_find(*args)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(services.repository, "find_file_by_hash", synchronized_find)

    def ingest_once():
        return services.ingestion.ingest(
            "tenant-a", workspace.id, "concurrent.csv", content, {"concurrent": {}}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ingest_once(), range(2)))

    assert sorted(result.duplicate for result in results) == [False, True]
    assert results[0].file_id == results[1].file_id
    assert len(services.repository.list_files("tenant-a", workspace.id)) == 1
    assert len(services.repository.list_artifacts("tenant-a", workspace.id)) == 1
    assert services.repository.list_datasets("tenant-a", workspace.id)[0]["version"] == 1
    assert len(list((services.ingestion.artifact_store.root / "artifacts").rglob("*.parquet"))) == 1


def test_failing_attempt_cleanup_cannot_delete_interleaved_winner_original(
    monkeypatch, services, workspace
) -> None:
    content = pd.DataFrame({"value": [1, 2]}).to_csv(index=False).encode()
    state = local()
    initial_lookup = Barrier(2)
    loser_written = Event()
    winner_written = Event()
    loser_done = Event()
    original_find = services.repository.find_file_by_hash
    original_write = services.ingestion.secure_file_store.write_stream
    original_publish = services.repository.publish_ingestion

    def synchronized_find(*args):
        if not getattr(state, "initial_lookup_complete", False):
            state.initial_lookup_complete = True
            result = original_find(*args)
            initial_lookup.wait(timeout=10)
            return result
        return original_find(*args)

    def ordered_write(*args, **kwargs):
        if state.role == "loser":
            result = original_write(*args, **kwargs)
            loser_written.set()
            assert winner_written.wait(timeout=10)
            return result
        assert loser_written.wait(timeout=10)
        result = original_write(*args, **kwargs)
        winner_written.set()
        return result

    def ordered_publish(*args, **kwargs):
        if state.role == "loser":
            raise RuntimeError("forced creator publication failure")
        assert loser_done.wait(timeout=10)
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(services.repository, "find_file_by_hash", synchronized_find)
    monkeypatch.setattr(services.ingestion.secure_file_store, "write_stream", ordered_write)
    monkeypatch.setattr(services.repository, "publish_ingestion", ordered_publish)

    def ingest_as(role):
        state.role = role
        try:
            return services.ingestion.ingest(
                "tenant-a", workspace.id, "interleaved.csv", content, {"interleaved": {}}
            )
        finally:
            if role == "loser":
                loser_done.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        loser_future = pool.submit(ingest_as, "loser")
        winner_future = pool.submit(ingest_as, "winner")
        with pytest.raises(RuntimeError, match="forced creator publication failure"):
            loser_future.result(timeout=20)
        winner = winner_future.result(timeout=20)

    winner_file = original_find(
        "tenant-a", workspace.id, winner.content_hash
    )
    winner_path = Path(winner_file["encrypted_path"])
    assert winner_path.exists()
    assert services.ingestion.secure_file_store.read(
        "tenant-a", workspace.id, winner.content_hash, winner_path
    ) == content
    assert len(list(winner_path.parent.glob("*.enc"))) == 1


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


def test_disk_headroom_requires_three_inputs_plus_temp_reserve(monkeypatch, tmp_path) -> None:
    input_size = 7 * MIB
    required = 3 * input_size + 4 * GIB
    monkeypatch.setattr(
        "tuoming_agent.maintenance.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(total=required * 2, used=required, free=required),
    )

    ensure_disk_headroom(tmp_path, input_size, 4 * GIB)

    monkeypatch.setattr(
        "tuoming_agent.maintenance.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(
            total=required * 2, used=required + 1, free=required - 1
        ),
    )
    with pytest.raises(DiskHeadroomError, match="磁盘可用空间不足"):
        ensure_disk_headroom(tmp_path, input_size, 4 * GIB)


def test_ingestion_rejects_low_disk_before_any_ingestion_write(
    monkeypatch, services, workspace
) -> None:
    content = b"value\n1\n"
    required = 3 * len(content) + 4 * GIB
    monkeypatch.setattr(
        "tuoming_agent.maintenance.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(total=required, used=1, free=required - 1),
    )
    monkeypatch.setattr(
        services.repository,
        "find_file_by_hash",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("disk check must happen before repository lookup")
        ),
    )
    with pytest.raises(DiskHeadroomError, match="清理空间或调小待上传文件"):
        services.ingestion.ingest(
            "tenant-a", workspace.id, "low-space.csv", content, {"low-space": {}}
        )

    assert services.repository.list_files("tenant-a", workspace.id) == []
    assert services.repository.list_artifacts("tenant-a", workspace.id) == []
    assert not (services.ingestion.secure_file_store.root / "uploads").exists()
    assert not (services.ingestion.artifact_store.root / "artifacts").exists()


def test_stale_cleanup_only_removes_owned_unreferenced_patterns(
    services, workspace, config
) -> None:
    result = services.ingestion.ingest(
        "tenant-a", workspace.id, "kept.csv", b"value\n1\n", {"kept": {}}
    )
    referenced_upload = Path(
        services.repository.find_file_by_hash(
            "tenant-a", workspace.id, result.content_hash
        )["encrypted_path"]
    )
    referenced_artifact = result.artifacts[0].path
    old = datetime(2026, 8, 10, tzinfo=UTC)
    now = datetime(2026, 8, 12, tzinfo=UTC)

    orphan_upload = (
        config.data_dir
        / "uploads"
        / "tenant-a"
        / workspace.id
        / f"{'a' * 64}.{'b' * 32}.enc"
    )
    stale_upload_tmp = orphan_upload.parent / f".{'c' * 32}.tmp"
    recent_upload_tmp = orphan_upload.parent / f".{'d' * 32}.tmp"
    stale_artifact_tmp = (
        config.data_dir
        / "artifacts"
        / "tenant-a"
        / workspace.id
        / f"artifact.{'e' * 32}.tmp.parquet"
    )
    stale_candidate = (
        config.data_dir / "analysis-candidates" / "tenant-a" / workspace.id / "candidate.parquet"
    )
    stale_export = config.data_dir / "exports" / "masked-12345678-orphan.csv"
    unknown = config.data_dir / "exports" / "notes.txt"
    snapshot_dir = config.data_dir / "duckdb-temp" / "sources-abandoned"
    stale_snapshot = snapshot_dir / f"0-{'f' * 32}.parquet"
    paths = (
        orphan_upload,
        stale_upload_tmp,
        recent_upload_tmp,
        stale_artifact_tmp,
        stale_candidate,
        stale_export,
        unknown,
        stale_snapshot,
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"owned")
    for path in (*paths[:2], *paths[3:], referenced_upload, referenced_artifact):
        os.utime(path, (old.timestamp(), old.timestamp()))
    os.utime(recent_upload_tmp, (now.timestamp(), now.timestamp()))

    removed = cleanup_stale_files(config.data_dir, config.database_path, now=now)

    assert set(removed) == {
        orphan_upload,
        stale_upload_tmp,
        stale_artifact_tmp,
        stale_candidate,
        stale_export,
        stale_snapshot,
    }
    assert recent_upload_tmp.exists()
    assert unknown.exists()
    assert referenced_upload.exists()
    assert referenced_artifact.exists()
    assert not snapshot_dir.exists()


def test_stale_cleanup_fails_closed_when_sqlite_references_cannot_be_read(tmp_path) -> None:
    data_dir = tmp_path / "data"
    candidate = data_dir / "analysis-candidates" / "tenant" / "workspace" / "old.parquet"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"candidate")
    old = datetime(2026, 8, 10, tzinfo=UTC).timestamp()
    os.utime(candidate, (old, old))
    database = data_dir / "tuoming.sqlite3"
    database.write_bytes(b"not sqlite")

    with pytest.raises(MaintenanceError, match="元数据"):
        cleanup_stale_files(
            data_dir,
            database,
            now=datetime(2026, 8, 12, tzinfo=UTC),
        )

    assert candidate.exists()


def test_stale_cleanup_fails_closed_when_sqlite_metadata_is_missing(tmp_path) -> None:
    data_dir = tmp_path / "data"
    candidate = data_dir / "analysis-candidates" / "tenant" / "workspace" / "old.parquet"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"candidate")
    old = datetime(2026, 8, 10, tzinfo=UTC).timestamp()
    os.utime(candidate, (old, old))

    with pytest.raises(MaintenanceError, match="元数据"):
        cleanup_stale_files(
            data_dir,
            data_dir / "missing.sqlite3",
            now=datetime(2026, 8, 12, tzinfo=UTC),
        )

    assert candidate.exists()


def test_stale_cleanup_never_follows_directory_symlinks(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "tuoming.sqlite3"
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE files(encrypted_path TEXT); CREATE TABLE artifacts(path TEXT);"
        )
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = outside / "escaped.tmp.parquet"
    escaped.write_bytes(b"must stay")
    old = datetime(2026, 8, 10, tzinfo=UTC).timestamp()
    os.utime(escaped, (old, old))
    link = data_dir / "artifacts"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 权限不允许创建目录符号链接")

    removed = cleanup_stale_files(
        data_dir,
        database,
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert removed == []
    assert escaped.exists()
