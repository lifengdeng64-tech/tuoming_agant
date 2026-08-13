from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from tuoming_agent.analysis.duckdb_runtime import DuckDBRuntime
from tuoming_agent.analysis.executor import (
    AnalysisExecutor,
    AnalysisResourceError,
)
from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.quality import AnalysisQualityValidator
from tuoming_agent.models import ColumnLineage


def _artifact(services, workspace_id, name, dataframe, lineage=None):
    return services.artifacts.save_result(
        "tenant-a", workspace_id, name, dataframe, lineage or {}, ()
    )


@pytest.fixture
def source(services, workspace):
    return _artifact(
        services,
        workspace.id,
        "source",
        pd.DataFrame(
            {
                "group": ["a", "a", "b", "b"],
                "value": [1.0, None, 3.0, 4.0],
                "text": ["alpha", "beta", None, "alpha"],
                "flag": [True, False, True, False],
            }
        ),
        {"group": ColumnLineage("store"), "text": ColumnLineage("label")},
    )


@pytest.mark.parametrize(
    "operations",
    [
        [{"action": "select", "columns": ["text", "value"]}],
        [{"action": "filter", "column": "value", "operator": "gt", "value": 2}],
        [{"action": "sort", "columns": ["value"], "ascending": False}],
        [{"action": "rename", "mapping": {"value": "amount"}}],
        [{"action": "cast", "mapping": {"value": "string"}}],
        [{"action": "fillna", "values": {"value": 9.0, "text": "missing"}}],
        [{"action": "dropna", "subset": ["value", "text"], "how": "any"}],
        [{"action": "deduplicate", "subset": ["group"], "keep": "last"}],
        [
            {
                "action": "groupby",
                "by": ["group"],
                "aggregations": [{"column": "value", "function": "sum", "output": "total"}],
            }
        ],
        [{"action": "derive", "column": "double", "expression": "col('value') * 2"}],
        [{"action": "head", "rows": 2}],
        [{"action": "tail", "rows": 2}],
    ],
    ids=[
        "select",
        "filter",
        "sort",
        "rename",
        "cast",
        "fillna",
        "dropna",
        "deduplicate",
        "groupby",
        "derive",
        "head",
        "tail",
    ],
)
def test_disk_prepare_matches_legacy_pandas_executor(
    operations, services, workspace, source
):
    executor = AnalysisExecutor(services.artifacts)
    plan = AnalysisPlan(input_artifact_id=source.id, operations=operations)
    expected_artifact = executor.execute("tenant-a", workspace.id, plan)
    _, expected = services.artifacts.load("tenant-a", expected_artifact.id)

    candidate = executor.prepare("tenant-a", workspace.id, plan)
    try:
        actual = pd.read_parquet(candidate.path)
        pd.testing.assert_frame_equal(actual, expected, check_dtype=False)
        assert candidate.row_count == len(expected)
        assert candidate.schema["columns"] == [
            {"name": str(column), "dtype": str(actual[column].dtype)}
            for column in actual.columns
        ]
        assert candidate.parent_ids == (source.id,)
    finally:
        candidate.cleanup()


def test_disk_prepare_matches_pandas_merge_and_parent_lineage(services, workspace, source):
    right = _artifact(
        services,
        workspace.id,
        "lookup",
        pd.DataFrame({"key": ["a", "b"], "text": ["A-name", "B-name"]}),
        {"text": ColumnLineage("lookup-label")},
    )
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        operations=[
            {
                "action": "merge",
                "right_artifact_id": right.id,
                "left_on": ["group"],
                "right_on": ["key"],
                "suffixes": ["_left", "_right"],
            }
        ],
    )
    executor = AnalysisExecutor(services.artifacts)
    expected_artifact = executor.execute("tenant-a", workspace.id, plan)
    _, expected = services.artifacts.load("tenant-a", expected_artifact.id)

    candidate = executor.prepare("tenant-a", workspace.id, plan)
    try:
        pd.testing.assert_frame_equal(
            pd.read_parquet(candidate.path), expected, check_dtype=False
        )
        assert candidate.parent_ids == (source.id, right.id)
        assert candidate.lineage == {
            "group": ColumnLineage("store"),
            "text_left": ColumnLineage("label"),
            "text_right": ColumnLineage("lookup-label"),
        }
    finally:
        candidate.cleanup()


def test_prepare_is_disk_backed_and_candidate_owns_cleanup(
    services, workspace, source, monkeypatch
):
    def reject_dataframe_load(*_args, **_kwargs):
        raise AssertionError("disk execution must not load a source DataFrame")

    monkeypatch.setattr(services.artifacts, "load", reject_dataframe_load)
    candidate = AnalysisExecutor(services.artifacts).prepare(
        "tenant-a",
        workspace.id,
        AnalysisPlan(input_artifact_id=source.id, operations=[{"action": "head", "rows": 2}]),
    )

    path = Path(candidate.path)
    assert path.is_file()
    assert path.suffix == ".parquet"
    assert path.is_relative_to(services.artifacts.artifact_store.root)
    assert candidate.row_count == 2
    assert candidate.dataframe is None

    candidate.cleanup()
    assert not path.exists()
    candidate.cleanup()


def test_disk_quality_uses_parquet_metadata_and_bounded_arrow_aggregates(
    services, workspace
):
    source = _artifact(
        services,
        workspace.id,
        "quality-source",
        pd.DataFrame({"value": [1.0, float("inf")], "note": [None, "ok"]}),
    )
    candidate = AnalysisExecutor(services.artifacts).prepare(
        "tenant-a",
        workspace.id,
        AnalysisPlan(
            input_artifact_id=source.id,
            operations=[{"action": "head", "rows": 2}],
        ),
    )
    try:
        report = AnalysisQualityValidator().validate(candidate)
        assert {issue.code for issue in report.failures} == {"infinite_values"}
        assert {issue.code for issue in report.warnings} == {"null_values"}
    finally:
        candidate.cleanup()


def test_disk_quality_warns_on_row_amplification(services, workspace):
    left = _artifact(
        services, workspace.id, "left", pd.DataFrame({"key": ["a", "b"]})
    )
    right = _artifact(
        services,
        workspace.id,
        "right",
        pd.DataFrame({"key": ["a", "a", "b", "b"]}),
    )
    candidate = AnalysisExecutor(services.artifacts).prepare(
        "tenant-a",
        workspace.id,
        AnalysisPlan(
            input_artifact_id=left.id,
            operations=[
                {
                    "action": "merge",
                    "right_artifact_id": right.id,
                    "left_on": ["key"],
                    "right_on": ["key"],
                }
            ],
        ),
    )
    try:
        report = AnalysisQualityValidator(max_row_amplification=1.5).validate(candidate)
        assert {issue.code for issue in report.warnings} == {"row_amplification"}
    finally:
        candidate.cleanup()


def test_validated_candidate_is_moved_to_uuid_artifact_path(services, workspace, source):
    dangerous_name = "../../outside/result"
    candidate = AnalysisExecutor(services.artifacts).prepare(
        "tenant-a",
        workspace.id,
        AnalysisPlan(
            input_artifact_id=source.id,
            result_name=dangerous_name,
            operations=[{"action": "head", "rows": 1}],
        ),
    )
    temporary = candidate.path

    artifact = services.artifacts.publish_candidate(
        "tenant-a", workspace.id, candidate
    )

    assert temporary is not None and not temporary.exists()
    assert artifact.path.is_file()
    assert artifact.path.parent == (
        services.artifacts.artifact_store.root / "artifacts" / "tenant-a" / workspace.id
    )
    assert artifact.path.name == f"{artifact.id}.parquet"
    assert dangerous_name not in str(artifact.path)
    assert artifact.row_count == 1
    assert services.repository.get_artifact("tenant-a", artifact.id) == artifact


def test_candidate_publication_removes_moved_file_when_registration_fails(
    services, workspace, source
):
    candidate = AnalysisExecutor(services.artifacts).prepare(
        "tenant-a",
        workspace.id,
        AnalysisPlan(input_artifact_id=source.id, operations=[{"action": "head", "rows": 1}]),
    )
    artifacts_root = services.artifacts.artifact_store.root / "artifacts"
    paths_before = set(artifacts_root.rglob("*.parquet"))

    with sqlite3.connect(services.repository.database_path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_analysis_audit
            BEFORE INSERT ON audit_events
            WHEN NEW.event_type = 'analysis_artifact_created'
            BEGIN
                SELECT RAISE(ABORT, 'audit unavailable');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        services.artifacts.publish_candidate("tenant-a", workspace.id, candidate)

    assert set(artifacts_root.rglob("*.parquet")) == paths_before
    assert {artifact.id for artifact in services.repository.list_artifacts(
        "tenant-a", workspace.id
    )} == {source.id}
    assert not list(
        (services.artifacts.artifact_store.root / "analysis-candidates").rglob("*.parquet")
    )


def test_duckdb_out_of_memory_becomes_actionable_resource_error(
    services, workspace, source, monkeypatch
):
    @contextmanager
    def exhaust_memory(_runtime, _sources):
        raise duckdb.OutOfMemoryException("failed to allocate under memory limit")
        yield

    monkeypatch.setattr(DuckDBRuntime, "connection", exhaust_memory)

    with pytest.raises(AnalysisResourceError, match="reduce input size or simplify"):
        AnalysisExecutor(services.artifacts).prepare(
            "tenant-a",
            workspace.id,
            AnalysisPlan(
                input_artifact_id=source.id,
                operations=[{"action": "head", "rows": 1}],
            ),
        )
