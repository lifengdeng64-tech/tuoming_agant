from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pandas as pd
import pyarrow as pa
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
        writer: pq.ParquetWriter | None = None
        try:
            for dataframe in chunks:
                table = pa.Table.from_pandas(dataframe, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(temporary, table.schema)
                elif table.schema != writer.schema:
                    table = table.cast(writer.schema)
                writer.write_table(table)
            if writer is None:
                raise ValueError("At least one dataframe chunk is required.")
            writer.close()
            writer = None
            os.replace(temporary, target)
            return target
        except Exception:
            if writer is not None:
                try:
                    writer.close()
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def read_dataframe(path: str | Path) -> pd.DataFrame:
        return pd.read_parquet(path)
