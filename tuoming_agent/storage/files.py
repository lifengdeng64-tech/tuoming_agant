from __future__ import annotations

import os
import uuid
from pathlib import Path

import pandas as pd

from tuoming_agent.security.crypto import decrypt_bytes, derive_key, encrypt_bytes


class SecureFileStore:
    """Stores original uploads encrypted with tenant-derived AES-GCM keys."""

    def __init__(self, root: str | Path, master_key: bytes):
        self.root = Path(root)
        self.master_key = master_key

    def write(self, tenant_id: str, workspace_id: str, content_hash: str, content: bytes) -> Path:
        target_dir = self.root / "uploads" / tenant_id / workspace_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{content_hash}.enc"
        if target.exists():
            return target
        key = derive_key(self.master_key, "upload", tenant_id)
        aad = self._aad(tenant_id, workspace_id, content_hash)
        nonce, ciphertext = encrypt_bytes(key, content, aad)
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(nonce + ciphertext)
        os.replace(temporary, target)
        return target

    def read(self, tenant_id: str, workspace_id: str, content_hash: str, path: str | Path) -> bytes:
        payload = Path(path).read_bytes()
        key = derive_key(self.master_key, "upload", tenant_id)
        aad = self._aad(tenant_id, workspace_id, content_hash)
        return decrypt_bytes(key, payload[:12], payload[12:], aad)

    @staticmethod
    def _aad(tenant_id: str, workspace_id: str, content_hash: str) -> bytes:
        return f"{tenant_id}|{workspace_id}|{content_hash}".encode()


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

    @staticmethod
    def read_dataframe(path: str | Path) -> pd.DataFrame:
        return pd.read_parquet(path)

