from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import duckdb
import pyarrow.dataset as ds

from tuoming_agent.analysis.duckdb_compiler import AuthorizedSource, is_authorized_source
from tuoming_agent.config import AppConfig


class DuckDBRuntime:
    """Create an isolated, resource-bounded connection for one compiled task."""

    def __init__(self, config: AppConfig):
        self.config = config

    @contextmanager
    def connection(
        self, sources: tuple[AuthorizedSource, ...]
    ) -> Iterator[duckdb.DuckDBPyConnection]:
        if not sources or any(not is_authorized_source(source) for source in sources):
            raise ValueError("DuckDB sources must be authorized by the compiler.")
        temporary = self.config.data_dir / "duckdb-temp"
        temporary.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(
            database=":memory:",
            config={
                "memory_limit": self.config.duckdb_memory_limit,
                "threads": self.config.duckdb_threads,
                "max_temp_directory_size": self.config.duckdb_max_temp_directory_size,
                "temp_directory": str(temporary.resolve()),
            },
        )
        try:
            for source in sources:
                dataset = ds.dataset(source.path, format="parquet")
                connection.register(source.relation_name, dataset)
            connection.execute("SET enable_external_access = false")
            connection.execute("SET lock_configuration = true")
            yield connection
        finally:
            connection.close()
