from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from tuoming_agent.analysis.duckdb_compiler import DuckDBCompiler
from tuoming_agent.analysis.duckdb_runtime import DuckDBRuntime
from tuoming_agent.analysis.errors import SecurityPolicyViolation
from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.config import AppConfig, ConfigurationError
from tuoming_agent.models import ColumnLineage
from tuoming_agent.storage.errors import AuthorizationError

MIB = 1024 * 1024


def _artifact(services, workspace_id, name, dataframe, lineage=None):
    return services.artifacts.save_result(
        "tenant-a",
        workspace_id,
        name,
        dataframe,
        lineage or {},
        (),
    )


def _compile_and_fetch(services, config, workspace_id, plan):
    compiled = DuckDBCompiler(services.repository).compile(
        "tenant-a", workspace_id, AnalysisPlan(**plan)
    )
    with DuckDBRuntime(config).connection(compiled.sources) as connection:
        return compiled, connection.execute(compiled.sql, compiled.parameters).fetchall()


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
    ("operations", "expected"),
    [
        (
            [{"action": "select", "columns": ["text", "value"]}],
            [("alpha", 1.0), ("beta", None), (None, 3.0), ("alpha", 4.0)],
        ),
        (
            [{"action": "filter", "column": "value", "operator": "gt", "value": 2}],
            [("b", 3.0, None, True), ("b", 4.0, "alpha", False)],
        ),
        (
            [{"action": "sort", "columns": ["value"], "ascending": False}],
            [
                ("b", 4.0, "alpha", False),
                ("b", 3.0, None, True),
                ("a", 1.0, "alpha", True),
                ("a", None, "beta", False),
            ],
        ),
        (
            [
                {"action": "rename", "mapping": {"value": "amount"}},
                {"action": "select", "columns": ["amount"]},
            ],
            [(1.0,), (None,), (3.0,), (4.0,)],
        ),
        (
            [
                {"action": "cast", "mapping": {"value": "string"}},
                {"action": "select", "columns": ["value"]},
            ],
            [("1.0",), (None,), ("3.0",), ("4.0",)],
        ),
        (
            [
                {"action": "fillna", "values": {"value": 9.0, "text": "missing"}},
                {"action": "select", "columns": ["value", "text"]},
            ],
            [(1.0, "alpha"), (9.0, "beta"), (3.0, "missing"), (4.0, "alpha")],
        ),
        (
            [
                {"action": "dropna", "subset": ["value", "text"], "how": "any"},
                {"action": "select", "columns": ["value", "text"]},
            ],
            [(1.0, "alpha"), (4.0, "alpha")],
        ),
        (
            [
                {"action": "deduplicate", "subset": ["group"], "keep": "last"},
                {"action": "select", "columns": ["group", "value"]},
            ],
            [("a", None), ("b", 4.0)],
        ),
        (
            [
                {
                    "action": "groupby",
                    "by": ["group"],
                    "aggregations": [{"column": "value", "function": "sum", "output": "total"}],
                }
            ],
            [("a", 1.0), ("b", 7.0)],
        ),
        (
            [
                {"action": "derive", "column": "double", "expression": "col('value') * 2"},
                {"action": "select", "columns": ["double"]},
            ],
            [(2.0,), (None,), (6.0,), (8.0,)],
        ),
        (
            [{"action": "head", "rows": 2}, {"action": "select", "columns": ["group", "value"]}],
            [("a", 1.0), ("a", None)],
        ),
        (
            [{"action": "tail", "rows": 2}, {"action": "select", "columns": ["group", "value"]}],
            [("b", 3.0), ("b", 4.0)],
        ),
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
def test_compiled_operations_execute_against_registered_parquet(
    operations, expected, services, config, workspace, source
):
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {"input_artifact_id": source.id, "operations": operations},
    )
    assert actual == expected


def test_merge_executes_equality_join_and_tracks_authorized_source_lineage(
    services, config, workspace, source
):
    right = _artifact(
        services,
        workspace.id,
        "lookup",
        pd.DataFrame({"key": ["a", "b"], "text": ["A-name", "B-name"]}),
        {"text": ColumnLineage("lookup-label")},
    )
    compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": source.id,
            "operations": [
                {
                    "action": "merge",
                    "right_artifact_id": right.id,
                    "left_on": ["group"],
                    "right_on": ["key"],
                    "how": "inner",
                    "suffixes": ["_left", "_right"],
                },
                {"action": "select", "columns": ["group", "text_left", "text_right"]},
            ],
        },
    )
    assert actual == [
        ("a", "alpha", "A-name"),
        ("a", "beta", "A-name"),
        ("b", None, "B-name"),
        ("b", "alpha", "B-name"),
    ]
    assert len(compiled.sources) == 2
    assert compiled.lineage == {
        "group": ColumnLineage("store"),
        "text_left": ColumnLineage("label"),
        "text_right": ColumnLineage("lookup-label"),
    }


@pytest.mark.parametrize(
    ("operator", "value", "expected_values"),
    [
        ("eq", 3.0, [3.0]),
        ("ne", 3.0, [1.0, 4.0]),
        ("gte", 3.0, [3.0, 4.0]),
        ("lt", 3.0, [1.0]),
        ("lte", 3.0, [1.0, 3.0]),
        ("contains", "ph", [1.0, 4.0]),
        ("in", [1.0, 4.0], [1.0, 4.0]),
        ("notnull", None, [1.0, 3.0, 4.0]),
        ("isnull", None, [None]),
    ],
)
def test_filter_operator_allowlist_binds_values(
    operator, value, expected_values, services, config, workspace, source
):
    column = "text" if operator == "contains" else "value"
    compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": source.id,
            "operations": [
                {"action": "filter", "column": column, "operator": operator, "value": value},
                {"action": "select", "columns": ["value"]},
            ],
        },
    )
    assert [row[0] for row in actual] == expected_values
    if operator not in {"notnull", "isnull"}:
        assert "?" in compiled.sql


def test_hostile_identifier_is_quoted_and_hostile_values_never_enter_sql(
    services, config, workspace
):
    hostile_column = 'total"; DROP TABLE source_0; --'
    hostile_value = "x'); INSTALL httpfs; --"
    artifact = _artifact(
        services,
        workspace.id,
        "hostile",
        pd.DataFrame({hostile_column: [hostile_value, "safe"]}),
    )
    compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": artifact.id,
            "operations": [
                {
                    "action": "filter",
                    "column": hostile_column,
                    "operator": "in",
                    "value": [hostile_value],
                },
                {"action": "select", "columns": [hostile_column]},
            ],
        },
    )
    assert actual == [(hostile_value,)]
    assert '"total""; DROP TABLE source_0; --"' in compiled.sql
    assert hostile_value not in compiled.sql
    assert compiled.parameters == (hostile_value,)


def test_operation_output_cannot_collide_with_compiler_internal_identifier(
    services, config, workspace, source
):
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": source.id,
            "operations": [
                {"action": "rename", "mapping": {"group": "__tuoming_row_order"}},
                {"action": "sort", "columns": ["value"], "ascending": False},
                {"action": "select", "columns": ["__tuoming_row_order"]},
            ],
        },
    )
    assert actual == [("b",), ("b",), ("a",), ("a",)]


def test_nested_projection_parameters_match_sql_placeholder_order(
    services, config, workspace, source
):
    compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": source.id,
            "operations": [
                {"action": "filter", "column": "group", "operator": "eq", "value": "a"},
                {"action": "fillna", "values": {"value": 9.0}},
                {
                    "action": "derive",
                    "column": "adjusted",
                    "expression": "col('value') + 1",
                },
                {"action": "select", "columns": ["adjusted"]},
            ],
        },
    )
    assert compiled.parameters == (1, 9.0, "a")
    assert actual == [(2.0,), (10.0,)]


def test_missing_column_is_rejected_before_sql_execution(services, workspace, source):
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        operations=[{"action": "select", "columns": ["read_csv_auto('/secret')"]}],
    )
    with pytest.raises(ValueError, match="Columns do not exist"):
        DuckDBCompiler(services.repository).compile("tenant-a", workspace.id, plan)


def test_compiler_rejects_cross_workspace_and_cross_tenant_sources(services, workspace, source):
    other_workspace = services.repository.create_workspace("tenant-a", "other")
    other_workspace_artifact = _artifact(
        services, other_workspace.id, "other", pd.DataFrame({"id": [1]})
    )
    cross_workspace = AnalysisPlan(
        input_artifact_id=other_workspace_artifact.id,
        operations=[{"action": "head", "rows": 1}],
    )
    with pytest.raises(SecurityPolicyViolation, match="another workspace"):
        DuckDBCompiler(services.repository).compile("tenant-a", workspace.id, cross_workspace)

    services.repository.ensure_tenant("tenant-b")
    tenant_b_workspace = services.repository.create_workspace("tenant-b", "tenant-b-workspace")
    tenant_b_artifact = services.artifacts.save_result(
        "tenant-b",
        tenant_b_workspace.id,
        "tenant-b-source",
        pd.DataFrame({"id": [1]}),
        {},
        (),
    )
    cross_tenant = AnalysisPlan(
        input_artifact_id=tenant_b_artifact.id,
        operations=[{"action": "head", "rows": 1}],
    )
    with pytest.raises(AuthorizationError, match="another tenant"):
        DuckDBCompiler(services.repository).compile("tenant-a", workspace.id, cross_tenant)


def test_merge_rejects_secondary_artifact_larger_than_50_mib(services, workspace, source):
    right = _artifact(services, workspace.id, "large-right", pd.DataFrame({"id": [1]}))
    with right.path.open("r+b") as stream:
        stream.truncate(50 * MIB + 1)
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        operations=[
            {
                "action": "merge",
                "right_artifact_id": right.id,
                "left_on": ["group"],
                "right_on": ["id"],
            }
        ],
    )
    with pytest.raises(ValueError, match="secondary artifact.*50 MiB"):
        DuckDBCompiler(services.repository).compile("tenant-a", workspace.id, plan)


def test_runtime_registers_only_trusted_sources_then_locks_external_access(
    services, config, workspace, source, tmp_path
):
    compiled = DuckDBCompiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(
            input_artifact_id=source.id,
            operations=[{"action": "select", "columns": ["group"]}],
        ),
    )
    untrusted_csv = tmp_path / "untrusted.csv"
    untrusted_csv.write_text("secret\nvalue\n", encoding="utf-8")
    with DuckDBRuntime(config).connection(compiled.sources) as connection:
        settings = connection.execute(
            """SELECT current_setting('memory_limit'), current_setting('threads'),
                      current_setting('max_temp_directory_size'),
                      current_setting('temp_directory'),
                      current_setting('enable_external_access'),
                      current_setting('lock_configuration')"""
        ).fetchone()
        assert settings[0] == "1.8 GiB"
        assert settings[1] == 4
        assert settings[2] == "3.7 GiB"
        assert Path(settings[3]).is_relative_to(config.data_dir)
        assert settings[4:] == (False, True)
        assert connection.execute(compiled.sql, compiled.parameters).fetchall() == [
            ("a",),
            ("a",),
            ("b",),
            ("b",),
        ]
        with pytest.raises(duckdb.Error):
            connection.execute("SELECT * FROM read_csv_auto(?)", [str(untrusted_csv)]).fetchall()
        with pytest.raises(duckdb.Error):
            connection.execute("INSTALL httpfs")
        with pytest.raises(duckdb.Error):
            connection.execute("LOAD httpfs")
        with pytest.raises(duckdb.Error):
            connection.execute("SET enable_external_access=true")


def test_runtime_rejects_a_compiler_source_whose_trusted_path_was_replaced(
    services, config, workspace, source, tmp_path
):
    compiled = DuckDBCompiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(
            input_artifact_id=source.id,
            operations=[{"action": "select", "columns": ["group"]}],
        ),
    )
    untrusted = tmp_path / "untrusted.parquet"
    pd.DataFrame({"secret": ["value"]}).to_parquet(untrusted)
    tampered = replace(compiled.sources[0], path=untrusted)
    with (
        pytest.raises(ValueError, match="authorized by the compiler"),
        DuckDBRuntime(config).connection((tampered,)),
    ):
        pass


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DUCKDB_MEMORY_LIMIT", "3GiB"),
        ("DUCKDB_THREADS", "5"),
        ("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "5GiB"),
        ("DUCKDB_MEMORY_LIMIT", "2GiB'; LOAD httpfs; --"),
    ],
)
def test_duckdb_environment_limits_are_validated(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv("MASKING_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    monkeypatch.setenv("TUOMING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        AppConfig.from_env()
