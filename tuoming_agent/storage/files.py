from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from tuoming_agent.security.crypto import (
    STREAM_MAGIC,
    decrypt_bytes,
    decrypt_stream_frames,
    derive_key,
    encrypt_stream_frames,
)


class SecureFileStore:
    """Stores original uploads encrypted with tenant-derived AES-GCM keys."""

    def __init__(self, root: str | Path, master_key: bytes):
        self.root = Path(root)
        self.master_key = master_key

    def write(self, tenant_id: str, workspace_id: str, content_hash: str, content: bytes) -> Path:
        path, actual_hash, _, _ = self.write_stream(
            tenant_id, workspace_id, BytesIO(content)
        )
        if actual_hash != content_hash:
            raise ValueError("content_hash does not match content.")
        return path

    def write_stream(
        self,
        tenant_id: str,
        workspace_id: str,
        source: BinaryIO,
        chunk_size: int = 1024 * 1024,
    ) -> tuple[Path, str, int, bool]:
        target_dir = self.root / "uploads" / tenant_id / workspace_id
        target_dir.mkdir(parents=True, exist_ok=True)
        key = derive_key(self.master_key, "upload", tenant_id)
        aad = self._stream_aad(tenant_id, workspace_id)
        temporary = target_dir / f".{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("wb") as encrypted:
                content_hash, byte_size = encrypt_stream_frames(
                    key, source, encrypted, aad, chunk_size
                )
            target = target_dir / f"{content_hash}.{uuid.uuid4().hex}.enc"
            os.replace(temporary, target)
            return target, content_hash, byte_size, True
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def read(self, tenant_id: str, workspace_id: str, content_hash: str, path: str | Path) -> bytes:
        key = derive_key(self.master_key, "upload", tenant_id)
        with Path(path).open("rb") as encrypted:
            prefix = encrypted.read(len(STREAM_MAGIC))
            if prefix == STREAM_MAGIC:
                plaintext = b"".join(
                    decrypt_stream_frames(
                        key, encrypted, self._stream_aad(tenant_id, workspace_id)
                    )
                )
                import hashlib

                if hashlib.sha256(plaintext).hexdigest() != content_hash:
                    raise ValueError("Encrypted upload does not match its content hash.")
                return plaintext
            encrypted.seek(0)
            payload = encrypted.read()
            aad = self._aad(tenant_id, workspace_id, content_hash)
            return decrypt_bytes(key, payload[:12], payload[12:], aad)

    @staticmethod
    def _aad(tenant_id: str, workspace_id: str, content_hash: str) -> bytes:
        return f"{tenant_id}|{workspace_id}|{content_hash}".encode()

    @staticmethod
    def _stream_aad(tenant_id: str, workspace_id: str) -> bytes:
        return f"tuoming-upload-stream-v1|{tenant_id}|{workspace_id}".encode()


class ArtifactStore:
    """Stores only masked dataframes, using unique artifact paths for concurrency safety."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def write_dataframe(
        self,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
        dataframe: pd.DataFrame,
    ) -> Path:
        target_dir = self.root / "artifacts" / tenant_id / workspace_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{artifact_id}.parquet"
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp.parquet")
        dataframe.to_parquet(temporary, index=False)
        os.replace(temporary, target)
        return target

    def write_chunks(
        self,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
        chunks: Iterable[pd.DataFrame],
    ) -> Path:
        target_dir = self.root / "artifacts" / tenant_id / workspace_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{artifact_id}.parquet"
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp.parquet")
        temporary_paths = {temporary}
        writer: pq.ParquetWriter | None = None
        writer_schema: pa.Schema | None = None
        exact_float_integers: dict[str, bool] = {}
        try:
            for dataframe in chunks:
                table = pa.Table.from_pandas(
                    dataframe, preserve_index=False
                ).replace_schema_metadata(None)
                for field in table.schema:
                    if pa.types.is_integer(field.type):
                        exact_float_integers[field.name] = exact_float_integers.get(
                            field.name, True
                        ) and self._integer_column_fits_float(table[field.name])
                if writer is None:
                    writer_schema = table.schema
                    writer = pq.ParquetWriter(temporary, writer_schema)
                elif table.schema != writer_schema:
                    widened = self._widen_schema(
                        writer_schema,
                        table.schema,
                        {
                            name
                            for name, is_exact in exact_float_integers.items()
                            if is_exact
                        },
                    )
                    if widened != writer_schema:
                        writer.close()
                        writer = None
                        previous = temporary
                        temporary = target.with_suffix(
                            f".{uuid.uuid4().hex}.tmp.parquet"
                        )
                        temporary_paths.add(temporary)
                        writer = pq.ParquetWriter(temporary, widened)
                        parquet = pq.ParquetFile(previous)
                        try:
                            for batch in parquet.iter_batches():
                                writer.write_table(
                                    pa.Table.from_batches([batch]).cast(
                                        widened
                                    )
                                )
                        finally:
                            parquet.close()
                        previous.unlink(missing_ok=True)
                        temporary_paths.discard(previous)
                        writer_schema = widened
                    table = table.cast(writer_schema)
                writer.write_table(table)
            if writer is None:
                raise ValueError("At least one dataframe chunk is required.")
            writer.close()
            writer = None
            os.replace(temporary, target)
            temporary_paths.discard(temporary)
            return target
        except Exception:
            if writer is not None:
                writer.close()
            for pending_path in temporary_paths:
                pending_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _widen_schema(
        current: pa.Schema,
        incoming: pa.Schema,
        exact_float_integer_columns: set[str] | None = None,
    ) -> pa.Schema:
        if current.names != incoming.names:
            raise ValueError("All dataframe chunks must have the same columns.")
        exact_float_integer_columns = exact_float_integer_columns or set()
        fields: list[pa.Field] = []
        for current_field, incoming_field in zip(current, incoming, strict=True):
            current_type = current_field.type
            incoming_type = incoming_field.type
            if current_type == incoming_type:
                widened_type = current_type
            elif pa.types.is_null(current_type):
                widened_type = incoming_type
            elif pa.types.is_null(incoming_type):
                widened_type = current_type
            elif pa.types.is_integer(current_type) and pa.types.is_integer(
                incoming_type
            ):
                widened_type = ArtifactStore._widen_integer_types(
                    current_type, incoming_type
                )
            elif (
                pa.types.is_integer(current_type)
                or pa.types.is_floating(current_type)
            ) and (
                pa.types.is_integer(incoming_type)
                or pa.types.is_floating(incoming_type)
            ):
                has_integer = pa.types.is_integer(
                    current_type
                ) or pa.types.is_integer(incoming_type)
                widened_type = (
                    pa.float64()
                    if not has_integer
                    or current_field.name in exact_float_integer_columns
                    else pa.string()
                )
            else:
                widened_type = pa.string()
            fields.append(pa.field(current_field.name, widened_type, nullable=True))
        return pa.schema(fields)

    @staticmethod
    def _integer_column_fits_float(column: pa.ChunkedArray) -> bool:
        bounds = pc.min_max(column).as_py()
        minimum = bounds["min"]
        maximum = bounds["max"]
        if minimum is None or maximum is None:
            return True
        return minimum >= -(2**53) and maximum <= 2**53

    @staticmethod
    def _widen_integer_types(current: pa.DataType, incoming: pa.DataType) -> pa.DataType:
        if pa.types.is_signed_integer(current) == pa.types.is_signed_integer(incoming):
            bit_width = max(current.bit_width, incoming.bit_width)
            integer_types = (
                {8: pa.int8(), 16: pa.int16(), 32: pa.int32(), 64: pa.int64()}
                if pa.types.is_signed_integer(current)
                else {
                    8: pa.uint8(),
                    16: pa.uint16(),
                    32: pa.uint32(),
                    64: pa.uint64(),
                }
            )
            return integer_types[bit_width]

        signed = current if pa.types.is_signed_integer(current) else incoming
        unsigned = current if pa.types.is_unsigned_integer(current) else incoming
        required_bits = max(signed.bit_width, unsigned.bit_width + 1)
        for bit_width, arrow_type in (
            (8, pa.int8()),
            (16, pa.int16()),
            (32, pa.int32()),
            (64, pa.int64()),
        ):
            if bit_width >= required_bits:
                return arrow_type
        return pa.string()

    def write_record_batches(
        self,
        tenant_id: str,
        workspace_id: str,
        batches: pa.RecordBatchReader,
    ) -> Path:
        target_dir = self.root / "analysis-candidates" / tenant_id / workspace_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{uuid.uuid4()}.parquet"
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp.parquet")
        writer: pq.ParquetWriter | None = None
        try:
            writer = pq.ParquetWriter(temporary, batches.schema)
            for batch in batches:
                writer.write_batch(batch)
            writer.close()
            writer = None
            os.replace(temporary, target)
            return target
        except Exception:
            if writer is not None:
                writer.close()
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def inspect_parquet(path: str | Path) -> tuple[dict[str, object], int]:
        parquet = pq.ParquetFile(path)
        empty = parquet.schema_arrow.empty_table().to_pandas()
        schema = {
            "columns": [
                {"name": str(column), "dtype": str(empty[column].dtype)}
                for column in empty.columns
            ]
        }
        return schema, parquet.metadata.num_rows

    def publish_candidate(
        self,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
        candidate_path: str | Path,
    ) -> Path:
        source = Path(candidate_path)
        candidate_dir = (
            self.root / "analysis-candidates" / tenant_id / workspace_id
        ).resolve()
        if (
            not source.is_file()
            or source.suffix.lower() != ".parquet"
            or source.resolve().parent != candidate_dir
        ):
            raise ValueError("Analysis candidate is not owned by this workspace.")
        target_dir = self.root / "artifacts" / tenant_id / workspace_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{artifact_id}.parquet"
        os.replace(source, target)
        return target

    @staticmethod
    def read_dataframe(path: str | Path) -> pd.DataFrame:
        return pd.read_parquet(path)
