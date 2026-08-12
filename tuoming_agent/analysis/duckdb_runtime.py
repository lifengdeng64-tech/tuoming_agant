from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from tuoming_agent.analysis.duckdb_compiler import (
    AuthorizedSource,
    authorized_source_metadata,
)
from tuoming_agent.analysis.errors import SecurityPolicyViolation
from tuoming_agent.config import AppConfig, ConfigurationError, validate_duckdb_settings


class DuckDBRuntime:
    """Create an isolated, resource-bounded connection for one compiled task."""

    def __init__(self, config: AppConfig):
        self.config = config

    @contextmanager
    def connection(
        self, sources: tuple[AuthorizedSource, ...]
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        metadata = tuple(authorized_source_metadata(source) for source in sources)
        if not sources or any(source is None for source in metadata):
            raise ValueError("DuckDB sources must be authorized by the compiler.")
        validate_duckdb_settings(self.config)
        temporary = self.config.data_dir / "duckdb-temp"
        temporary.mkdir(parents=True, exist_ok=True)
        data_directory = Path(self.config.data_dir).resolve()
        resolved_temporary = temporary.resolve()
        if not resolved_temporary.is_relative_to(data_directory):
            raise ConfigurationError("DuckDB temp directory must remain under TUOMING_DATA_DIR.")
        connection = duckdb.connect(
            database=":memory:",
            config={
                "memory_limit": self.config.duckdb_memory_limit,
                "threads": self.config.duckdb_threads,
                "max_temp_directory_size": self.config.duckdb_max_temp_directory_size,
                "temp_directory": str(resolved_temporary),
            },
        )
        opened: list[tuple[BinaryIO, pa.RecordBatchReader]] = []
        try:
            for source in metadata:
                assert source is not None
                stream = source.path.open("rb")
                try:
                    self._assert_unchanged(stream, source)
                    parquet = pq.ParquetFile(stream)
                    reader = pa.RecordBatchReader.from_batches(
                        parquet.schema_arrow, parquet.iter_batches()
                    )
                    connection.register(source.relation_name, reader)
                except Exception:
                    stream.close()
                    raise
                opened.append((stream, reader))
            connection.execute("SET enable_external_access = false")
            connection.execute("SET lock_configuration = true")
            yield connection
        finally:
            connection.close()
            for stream, reader in opened:
                reader.close()
                stream.close()

    @staticmethod
    def _assert_unchanged(stream: BinaryIO, source: object) -> None:
        current = os.fstat(stream.fileno())
        expected = source
        if (
            current.st_dev != expected.device
            or current.st_ino != expected.inode
            or current.st_size != expected.size
            or current.st_mtime_ns != expected.modified_ns
            or current.st_size > expected.size_limit
        ):
            raise SecurityPolicyViolation("Artifact changed after compilation.")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected.sha256:
            raise SecurityPolicyViolation("Artifact changed after compilation.")
        stream.seek(0)
