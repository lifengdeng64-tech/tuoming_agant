from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from tuoming_agent.analysis.duckdb_compiler import (
    AuthorizedSource,
    DuckDBCompiler,
)
from tuoming_agent.analysis.errors import SecurityPolicyViolation
from tuoming_agent.config import AppConfig, ConfigurationError, validate_duckdb_settings
from tuoming_agent.storage.base import Repository


class _RepositorySourceAuthorizer:
    """Narrow compiler callback bound to one runtime and trusted repository."""

    __slots__ = ("__repository", "__runtime")

    def __init__(self, runtime: DuckDBRuntime, repository: Repository):
        self.__runtime = runtime
        self.__repository = repository

    def __call__(
        self,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
        relation_name: str,
        role: str,
        size_limit: int,
    ) -> AuthorizedSource:
        return self.__runtime._DuckDBRuntime__authorize(  # noqa: SLF001
            self.__repository,
            tenant_id,
            workspace_id,
            artifact_id,
            relation_name,
            role,
            size_limit,
        )


class DuckDBRuntime:
    """Create an isolated, resource-bounded connection for one compiled task.

    The security boundary is untrusted model/user data. Arbitrary Python executing in this
    process is trusted: it already has repository and master-key access. Slots and name-mangled
    state reduce accidental authority exposure; they are not a cryptographic reflection barrier.
    """

    __slots__ = ("config", "__registry", "__registry_lock")

    def __init__(self, config: AppConfig):
        self.config = config
        self.__registry: dict[bytes, tuple[AuthorizedSource, tuple[Any, ...]]] = {}
        self.__registry_lock = threading.Lock()

    def compiler(self, repository: Repository) -> DuckDBCompiler:
        """Return a compiler whose source authorizer is bound to this runtime/repository."""
        return DuckDBCompiler(repository, _RepositorySourceAuthorizer(self, repository))

    def __authorize(
        self,
        repository: Repository,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
        relation_name: str,
        role: str,
        size_limit: int,
    ) -> AuthorizedSource:
        artifact = repository.get_artifact(tenant_id, artifact_id)
        if artifact.workspace_id != workspace_id:
            raise SecurityPolicyViolation("Artifact belongs to another workspace.")
        path = artifact.path.resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".parquet":
            raise SecurityPolicyViolation("Artifact is not an authorized local Parquet source.")
        stat = path.stat()
        if stat.st_size > size_limit:
            raise ValueError(f"The {role} artifact is over its authorized size limit.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        token = secrets.token_bytes(32)
        with self.__registry_lock:
            while token in self.__registry:
                token = secrets.token_bytes(32)
            source = object.__new__(AuthorizedSource)
            object.__setattr__(source, "_relation_name", relation_name)
            object.__setattr__(source, "_token", token)
            metadata = (
                relation_name,
                path,
                role,
                size_limit,
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                digest,
            )
            self.__registry[token] = (source, metadata)
        return source

    def __consume(self, sources: tuple[AuthorizedSource, ...]) -> tuple[tuple[Any, ...], ...]:
        resolved: list[tuple[bytes, tuple[Any, ...]]] = []
        seen: set[bytes] = set()
        with self.__registry_lock:
            for source in sources:
                token: object = None
                try:
                    token = object.__getattribute__(source, "_token")
                    entry = self.__registry.get(token)
                except (AttributeError, TypeError):
                    entry = None
                if (
                    not isinstance(token, bytes)
                    or token in seen
                    or entry is None
                    or entry[0] is not source
                    or entry[1][0] != source.relation_name
                ):
                    raise ValueError("DuckDB sources must be authorized by this runtime.")
                seen.add(token)
                resolved.append((token, entry[1]))
            for token, _metadata in resolved:
                self.__registry.pop(token)
        return tuple(metadata for _token, metadata in resolved)

    @contextmanager
    def connection(
        self, sources: tuple[AuthorizedSource, ...]
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        if not sources:
            raise ValueError("DuckDB sources must be authorized by this runtime.")
        metadata = self.__consume(sources)
        validate_duckdb_settings(self.config)
        temporary = self.config.data_dir / "duckdb-temp"
        temporary.mkdir(parents=True, exist_ok=True)
        data_directory = Path(self.config.data_dir).resolve()
        resolved_temporary = temporary.resolve()
        if not resolved_temporary.is_relative_to(data_directory):
            raise ConfigurationError("DuckDB temp directory must remain under TUOMING_DATA_DIR.")
        snapshot_directory = Path(
            tempfile.mkdtemp(prefix="sources-", dir=resolved_temporary)
        )
        connection: duckdb.DuckDBPyConnection | None = None
        opened: list[tuple[BinaryIO, pa.RecordBatchReader]] = []
        try:
            snapshots = [
                self._snapshot_source(source, snapshot_directory, index)
                for index, source in enumerate(metadata)
            ]
            connection = duckdb.connect(
                database=":memory:",
                config={
                    "memory_limit": self.config.duckdb_memory_limit,
                    "threads": self.config.duckdb_threads,
                    "max_temp_directory_size": self.config.duckdb_max_temp_directory_size,
                    "temp_directory": str(resolved_temporary),
                },
            )
            for relation_name, snapshot in snapshots:
                stream = snapshot.open("rb")
                try:
                    parquet = pq.ParquetFile(stream)
                    reader = pa.RecordBatchReader.from_batches(
                        parquet.schema_arrow, parquet.iter_batches()
                    )
                    connection.register(relation_name, reader)
                except Exception:
                    stream.close()
                    raise
                opened.append((stream, reader))
            connection.execute("SET enable_external_access = false")
            connection.execute("SET lock_configuration = true")
            yield connection
        finally:
            if connection is not None:
                connection.close()
            for stream, reader in opened:
                reader.close()
                stream.close()
            if snapshot_directory.exists():
                for snapshot in snapshot_directory.iterdir():
                    snapshot.unlink(missing_ok=True)
                snapshot_directory.rmdir()

    @staticmethod
    def _snapshot_source(
        source: tuple[Any, ...], snapshot_directory: Path, index: int
    ) -> tuple[str, Path]:
        (
            relation_name,
            path,
            _role,
            size_limit,
            device,
            inode,
            size,
            modified_ns,
            expected_digest,
        ) = source
        snapshot = snapshot_directory / f"{index}-{secrets.token_hex(16)}.parquet"
        digest = hashlib.sha256()
        copied = 0
        with path.open("rb") as stream, snapshot.open("xb") as output:
            before = os.fstat(stream.fileno())
            if (
                before.st_dev != device
                or before.st_ino != inode
                or before.st_size != size
                or before.st_mtime_ns != modified_ns
            ):
                raise SecurityPolicyViolation("Artifact changed after compilation.")
            while chunk := stream.read(1024 * 1024):
                copied += len(chunk)
                if copied > size_limit:
                    raise SecurityPolicyViolation("Artifact changed after compilation.")
                digest.update(chunk)
                output.write(chunk)
            after = os.fstat(stream.fileno())
        if (
            copied != size
            or after.st_dev != device
            or after.st_ino != inode
            or after.st_size != size
            or after.st_mtime_ns != modified_ns
            or digest.hexdigest() != expected_digest
        ):
            raise SecurityPolicyViolation("Artifact changed after compilation.")
        return relation_name, snapshot
