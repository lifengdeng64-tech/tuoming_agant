from __future__ import annotations

import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest

from tuoming_agent.config import AppConfig
from tuoming_agent.workspace.service import create_services

MIB = 1024 * 1024


def _generate_csv(path: Path, target_bytes: int) -> int:
    row = b"north,widget,2026-08-12,123.45," + (b"x" * 900) + b"\n"
    with path.open("wb") as stream:
        stream.write(b"region,product,event_date,amount,description\n")
        while stream.tell() + len(row) <= target_bytes:
            stream.write(row)
    return path.stat().st_size


@contextmanager
def _peak_rss_sampler():
    try:
        import psutil
    except ImportError:
        yield lambda: None
        return
    process = psutil.Process()
    peak = [process.memory_info().rss]
    stopped = threading.Event()

    def sample() -> None:
        while not stopped.wait(0.02):
            peak[0] = max(peak[0], process.memory_info().rss)

    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        yield lambda: peak[0]
    finally:
        stopped.set()
        thread.join()
        peak[0] = max(peak[0], process.memory_info().rss)


def _run_pipeline(tmp_path: Path, target_bytes: int) -> dict[str, float | int | None]:
    source = tmp_path / "generated-benchmark.csv"
    actual_bytes = _generate_csv(source, target_bytes)
    config = AppConfig(
        master_key=b"benchmark-master-key-material-32!",
        key_version=1,
        data_dir=tmp_path / "data",
        default_tenant="benchmark",
    )
    services = create_services(config)
    workspace = services.repository.create_workspace("benchmark", "性能验收")
    with source.open("rb") as stream, _peak_rss_sampler() as peak_rss:
        started = time.perf_counter()
        result = services.ingestion.ingest(
            "benchmark",
            workspace.id,
            source.name,
            stream,
            {"generated-benchmark": {}},
        )
        import_seconds = time.perf_counter() - started
        artifact = result.artifacts[0]
        connection = duckdb.connect(":memory:")
        timings: dict[str, float] = {}
        try:
            queries = {
                "filter_seconds": "SELECT count(*) FROM read_parquet(?) WHERE amount > 100",
                "groupby_seconds": (
                    "SELECT region, sum(amount) FROM read_parquet(?) GROUP BY region"
                ),
                "sort_seconds": (
                    "SELECT amount FROM read_parquet(?) ORDER BY amount DESC LIMIT 1000"
                ),
            }
            for label, query in queries.items():
                query_started = time.perf_counter()
                connection.execute(query, [str(artifact.path)]).fetchall()
                timings[label] = time.perf_counter() - query_started
        finally:
            connection.close()
        measured_peak = peak_rss()
    return {
        "input_bytes": actual_bytes,
        "rows": artifact.row_count,
        "import_seconds": import_seconds,
        "peak_rss_bytes": measured_peak,
        **timings,
    }


def test_small_pipeline_performance_smoke(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tuoming_agent.maintenance.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(
            total=16 * 1024**3, used=8 * 1024**3, free=8 * 1024**3
        ),
    )
    metrics = _run_pipeline(tmp_path, MIB)
    print(f"small pipeline benchmark: {metrics}")

    assert metrics["input_bytes"] >= MIB - 100
    assert metrics["rows"] > 1_000
    assert metrics["import_seconds"] < 60


@pytest.mark.performance
@pytest.mark.skipif(
    os.getenv("TUOMING_RUN_LARGE_BENCHMARK") != "1",
    reason="设置 TUOMING_RUN_LARGE_BENCHMARK=1 执行近 200MiB 性能验收",
)
def test_near_200mib_pipeline_imports_within_ten_minutes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tuoming_agent.maintenance.shutil.disk_usage",
        lambda _path: shutil._ntuple_diskusage(
            total=32 * 1024**3, used=16 * 1024**3, free=16 * 1024**3
        ),
    )
    metrics = _run_pipeline(tmp_path, 199 * MIB)
    print(f"near-200MiB pipeline benchmark: {metrics}")

    assert metrics["input_bytes"] >= 198 * MIB
    assert metrics["import_seconds"] < 10 * 60
