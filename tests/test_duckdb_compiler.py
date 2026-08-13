from __future__ import annotations

import base64
import math
import os
import pickle
import shutil
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import MethodType

import duckdb
import pandas as pd
import pytest

import tuoming_agent.analysis.duckdb_compiler as compiler_module
from tuoming_agent.analysis.duckdb_compiler import (
    AuthorizedSource,
)
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
    runtime = DuckDBRuntime(config)
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a", workspace_id, AnalysisPlan(**plan)
    )
    with runtime.connection(compiled.sources) as connection:
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
        ("ne", 3.0, [1.0, None, 4.0]),
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


def test_null_merge_keys_match_and_all_null_aggregations_follow_pandas(services, config, workspace):
    left = _artifact(
        services,
        workspace.id,
        "nullable-left",
        pd.DataFrame({"key": [None, "a"], "value": [None, None]}),
    )
    right = _artifact(
        services,
        workspace.id,
        "nullable-right",
        pd.DataFrame({"key": [None, "a"], "label": ["null-key", "a-key"]}),
    )
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": left.id,
            "operations": [
                {
                    "action": "merge",
                    "right_artifact_id": right.id,
                    "left_on": ["key"],
                    "right_on": ["key"],
                },
                {
                    "action": "groupby",
                    "by": ["label"],
                    "aggregations": [
                        {"column": "value", "function": "sum", "output": "sum"},
                        {"column": "value", "function": "mean", "output": "mean"},
                        {"column": "value", "function": "median", "output": "median"},
                        {"column": "value", "function": "min", "output": "min"},
                        {"column": "value", "function": "max", "output": "max"},
                        {"column": "value", "function": "count", "output": "count"},
                        {"column": "value", "function": "nunique", "output": "nunique"},
                        {"column": "value", "function": "first", "output": "first"},
                        {"column": "value", "function": "last", "output": "last"},
                    ],
                },
            ],
        },
    )
    assert actual == [
        ("a-key", 0.0, None, None, None, None, 0, 0, None, None),
        ("null-key", 0.0, None, None, None, None, 0, 0, None, None),
    ]


def test_numeric_cast_floor_division_and_modulo_follow_pandas(services, config, workspace):
    artifact = _artifact(
        services,
        workspace.id,
        "numeric-edge-cases",
        pd.DataFrame({"fraction": [1.9, -1.9], "numerator": [-3, 3]}),
    )
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": artifact.id,
            "operations": [
                {"action": "cast", "mapping": {"fraction": "int64"}},
                {
                    "action": "derive",
                    "column": "quotient",
                    "expression": "col('numerator') // 2",
                },
                {
                    "action": "derive",
                    "column": "remainder",
                    "expression": "col('numerator') % 2",
                },
                {"action": "select", "columns": ["fraction", "quotient", "remainder"]},
            ],
        },
    )
    assert actual == [(1, -2, 1), (-1, 1, 1)]


def test_large_int64_floor_division_and_modulo_keep_exact_precision(
    services, config, workspace
):
    artifact = _artifact(
        services,
        workspace.id,
        "large-int64",
        pd.DataFrame({"value": [9_223_372_036_854_775_000, -9_223_372_036_854_775_000]}),
    )
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": artifact.id,
            "operations": [
                {
                    "action": "derive",
                    "column": "quotient",
                    "expression": "col('value') // 3",
                },
                {
                    "action": "derive",
                    "column": "remainder",
                    "expression": "col('value') % 3",
                },
                {"action": "select", "columns": ["quotient", "remainder"]},
            ],
        },
    )
    assert actual == [
        (3_074_457_345_618_258_333, 1),
        (-3_074_457_345_618_258_334, 2),
    ]


def test_integer_cast_accepts_strings_and_rejects_nulls_like_pandas(
    services, config, workspace
):
    strings = _artifact(
        services,
        workspace.id,
        "integer-strings",
        pd.DataFrame({"value": ["1", "-2"]}),
    )
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": strings.id,
            "operations": [
                {"action": "cast", "mapping": {"value": "int64"}},
                {"action": "select", "columns": ["value"]},
            ],
        },
    )
    assert actual == [(1,), (-2,)]

    nullable = _artifact(
        services,
        workspace.id,
        "nullable-integer",
        pd.DataFrame({"value": [1.0, None]}),
    )
    with pytest.raises(duckdb.Error):
        _compile_and_fetch(
            services,
            config,
            workspace.id,
            {
                "input_artifact_id": nullable.id,
                "operations": [{"action": "cast", "mapping": {"value": "int64"}}],
            },
        )


@pytest.mark.parametrize(
    "value",
    ["1", "+1", "-2", " 3", "4 ", "\t-005\n", "000", "1_000"],
)
def test_integer_text_cast_acceptance_matches_pandas(
    value, services, config, workspace
):
    expected = int(pd.Series([value]).astype("int64").iloc[0])
    artifact = _artifact(
        services,
        workspace.id,
        f"accepted-integer-{value!r}",
        pd.DataFrame({"value": [value]}),
    )
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": artifact.id,
            "operations": [{"action": "cast", "mapping": {"value": "int64"}}],
        },
    )
    assert actual == [(expected,)]


@pytest.mark.parametrize("value", ["1.5", "1e2", "+ 1", "", "  ", "--1"])
def test_integer_text_cast_rejection_matches_pandas(
    value, services, config, workspace
):
    with pytest.raises(ValueError):
        pd.Series([value]).astype("int64")
    artifact = _artifact(
        services,
        workspace.id,
        f"rejected-integer-{value!r}",
        pd.DataFrame({"value": [value]}),
    )
    with pytest.raises(duckdb.Error):
        _compile_and_fetch(
            services,
            config,
            workspace.id,
            {
                "input_artifact_id": artifact.id,
                "operations": [{"action": "cast", "mapping": {"value": "int64"}}],
            },
        )


def test_boolean_to_integer_cast_remains_pandas_compatible(
    services, config, workspace
):
    values = pd.Series([True, False])
    expected = [(int(value),) for value in values.astype("int64")]
    artifact = _artifact(
        services,
        workspace.id,
        "boolean-integers",
        pd.DataFrame({"value": values}),
    )
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": artifact.id,
            "operations": [{"action": "cast", "mapping": {"value": "int64"}}],
        },
    )
    assert actual == expected


def test_integer_division_by_zero_matches_current_pandas_evaluator(
    services, config, workspace
):
    artifact = _artifact(
        services,
        workspace.id,
        "division-by-zero",
        pd.DataFrame({"value": [3, -3]}),
    )
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": artifact.id,
            "operations": [
                {
                    "action": "derive",
                    "column": "quotient",
                    "expression": "col('value') // 0",
                },
                {
                    "action": "derive",
                    "column": "remainder",
                    "expression": "col('value') % 0",
                },
                {"action": "select", "columns": ["quotient", "remainder"]},
            ],
        },
    )
    assert math.isinf(actual[0][0]) and actual[0][0] > 0
    assert math.isinf(actual[1][0]) and actual[1][0] < 0
    assert math.isnan(actual[0][1]) and math.isnan(actual[1][1])


def test_negative_divisors_and_ne_none_follow_pandas(services, config, workspace):
    artifact = _artifact(
        services,
        workspace.id,
        "negative-divisor",
        pd.DataFrame({"value": [-3.0, None, 3.0]}),
    )
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {
            "input_artifact_id": artifact.id,
            "operations": [
                {"action": "filter", "column": "value", "operator": "ne", "value": None},
                {
                    "action": "derive",
                    "column": "quotient",
                    "expression": "col('value') // -2",
                },
                {
                    "action": "derive",
                    "column": "remainder",
                    "expression": "col('value') % -2",
                },
                {"action": "select", "columns": ["value", "quotient", "remainder"]},
            ],
        },
    )
    assert actual == [(-3.0, 1.0, -1.0), (None, None, None), (3.0, -2.0, -1.0)]


@pytest.mark.parametrize("producer", ["derive", "groupby", "merge"])
def test_schema_producers_cannot_collide_with_private_row_order(
    producer, services, config, workspace
):
    artifact = _artifact(
        services,
        workspace.id,
        "private-name-left",
        pd.DataFrame({"key": ["a", "b"], "value": [10, 20]}),
    )
    if producer == "derive":
        operations = [
            {
                "action": "derive",
                "column": "__tuoming_row_order",
                "expression": "col('value') / 10",
            },
            {"action": "sort", "columns": ["value"], "ascending": False},
        ]
    elif producer == "groupby":
        operations = [
            {
                "action": "groupby",
                "by": ["key"],
                "aggregations": [
                    {
                        "column": "value",
                        "function": "sum",
                        "output": "__tuoming_row_order",
                    }
                ],
            },
            {"action": "sort", "columns": ["__tuoming_row_order"], "ascending": False},
        ]
    else:
        right = _artifact(
            services,
            workspace.id,
            "private-name-right",
            pd.DataFrame({"key": ["a", "b"], "__tuoming_row_order": [1, 2]}),
        )
        operations = [
            {
                "action": "merge",
                "right_artifact_id": right.id,
                "left_on": ["key"],
                "right_on": ["key"],
            },
            {"action": "sort", "columns": ["__tuoming_row_order"], "ascending": False},
        ]
    operations.extend(
        [
            {"action": "head", "rows": 1},
            {"action": "select", "columns": ["key"]},
        ]
    )
    _compiled, actual = _compile_and_fetch(
        services,
        config,
        workspace.id,
        {"input_artifact_id": artifact.id, "operations": operations},
    )
    assert actual == [("b",)]


def test_missing_column_is_rejected_before_sql_execution(services, config, workspace, source):
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        operations=[{"action": "select", "columns": ["read_csv_auto('/secret')"]}],
    )
    with pytest.raises(ValueError, match="Columns do not exist"):
        DuckDBRuntime(config).compiler(services.repository).compile(
            "tenant-a", workspace.id, plan
        )


def test_compiler_rejects_cross_workspace_and_cross_tenant_sources(
    services, config, workspace, source
):
    other_workspace = services.repository.create_workspace("tenant-a", "other")
    other_workspace_artifact = _artifact(
        services, other_workspace.id, "other", pd.DataFrame({"id": [1]})
    )
    cross_workspace = AnalysisPlan(
        input_artifact_id=other_workspace_artifact.id,
        operations=[{"action": "head", "rows": 1}],
    )
    with pytest.raises(SecurityPolicyViolation, match="another workspace"):
        DuckDBRuntime(config).compiler(services.repository).compile(
            "tenant-a", workspace.id, cross_workspace
        )

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
        DuckDBRuntime(config).compiler(services.repository).compile(
            "tenant-a", workspace.id, cross_tenant
        )


def test_merge_rejects_secondary_artifact_larger_than_50_mib(
    services, config, workspace, source
):
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
        DuckDBRuntime(config).compiler(services.repository).compile(
            "tenant-a", workspace.id, plan
        )


def test_runtime_registers_only_trusted_sources_then_locks_external_access(
    services, config, workspace, source, tmp_path
):
    runtime = DuckDBRuntime(config)
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(
            input_artifact_id=source.id,
            operations=[{"action": "select", "columns": ["group"]}],
        ),
    )
    untrusted_csv = tmp_path / "untrusted.csv"
    untrusted_csv.write_text("secret\nvalue\n", encoding="utf-8")
    with runtime.connection(compiled.sources) as connection:
        settings = connection.execute(
            """SELECT current_setting('memory_limit'), current_setting('threads'),
                      current_setting('max_temp_directory_size'),
                      current_setting('temp_directory'),
                      current_setting('enable_external_access'),
                      current_setting('lock_configuration')"""
        ).fetchone()
        assert settings[0] == "2.0 GiB"
        assert settings[1] == 4
        assert settings[2] == "4.0 GiB"
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


def test_runtime_rejects_dataclass_replacement_of_trusted_source(
    services, config, workspace, source
):
    compiled = DuckDBRuntime(config).compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(
            input_artifact_id=source.id,
            operations=[{"action": "select", "columns": ["group"]}],
        ),
    )
    with pytest.raises(TypeError):
        replace(compiled.sources[0], relation_name="untrusted")


def test_authorized_source_capability_cannot_be_constructed_copied_or_pickled(
    services, config, workspace, source
):
    runtime = DuckDBRuntime(config)
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(input_artifact_id=source.id, operations=[{"action": "head", "rows": 1}]),
    )
    trusted = compiled.sources[0]
    with pytest.raises(TypeError):
        AuthorizedSource(source.id, "source_0", source.path)
    forged = object.__new__(AuthorizedSource)
    with (
        pytest.raises(ValueError, match="authorized by this runtime"),
        DuckDBRuntime(
            config=AppConfig(
                master_key=b"test-master-key-material-32-bytes!",
                key_version=1,
                data_dir=source.path.parent,
                default_tenant="tenant-a",
            )
        ).connection((forged,)),
    ):
        pass
    with pytest.raises(TypeError):
        replace(trusted, path=source.path)
    with pytest.raises(TypeError):
        pickle.dumps(trusted)


def test_compiler_module_exposes_no_source_mint_or_metadata_resolver():
    assert not hasattr(compiler_module, "_mint_source")
    assert not hasattr(compiler_module, "authorized_source_metadata")


def test_runtime_owns_compiler_capabilities_and_consumes_them_once(
    services, config, workspace, source
):
    runtime = DuckDBRuntime(config)
    assert callable(getattr(runtime, "compiler", None))
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(input_artifact_id=source.id, operations=[{"action": "head", "rows": 1}]),
    )
    with runtime.connection(compiled.sources) as connection:
        assert connection.execute(compiled.sql, compiled.parameters).fetchall()
    with (
        pytest.raises(ValueError, match="authorized by this runtime"),
        runtime.connection(compiled.sources),
    ):
        pass


def test_runtime_public_api_does_not_publish_authority_closures(config):
    runtime = DuckDBRuntime(config)
    assert not hasattr(runtime, "__dict__")
    assert isinstance(runtime.compiler, MethodType)
    assert runtime.compiler.__func__.__closure__ is None
    assert isinstance(runtime.connection, MethodType)
    assert runtime.connection.__wrapped__.__closure__ is None
    assert not any(
        hasattr(runtime, name)
        for name in ("authorize", "consume", "mint", "resolve", "source_authorizer")
    )


def test_source_capability_cannot_cross_runtime_instances(services, config, workspace, source):
    issuer = DuckDBRuntime(config)
    assert callable(getattr(issuer, "compiler", None))
    compiled = issuer.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(input_artifact_id=source.id, operations=[{"action": "head", "rows": 1}]),
    )
    with (
        pytest.raises(ValueError, match="authorized by this runtime"),
        DuckDBRuntime(config).connection(compiled.sources),
    ):
        pass


def test_known_token_clone_and_mutated_legitimate_capability_are_rejected(
    services, config, workspace, source
):
    runtime = DuckDBRuntime(config)
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(input_artifact_id=source.id, operations=[{"action": "head", "rows": 1}]),
    )
    trusted = compiled.sources[0]
    clone = object.__new__(AuthorizedSource)
    object.__setattr__(clone, "_relation_name", trusted.relation_name)
    object.__setattr__(clone, "_token", object.__getattribute__(trusted, "_token"))
    with (
        pytest.raises(ValueError, match="authorized by this runtime"),
        runtime.connection((clone,)),
    ):
        pass

    object.__setattr__(trusted, "_relation_name", "tampered")
    with (
        pytest.raises(ValueError, match="authorized by this runtime"),
        runtime.connection(compiled.sources),
    ):
        pass


def test_runtime_rejects_source_replaced_after_compile(services, config, workspace, source):
    runtime = DuckDBRuntime(config)
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(input_artifact_id=source.id, operations=[{"action": "head", "rows": 1}]),
    )
    replacement = source.path.with_name("replacement.parquet")
    pd.DataFrame(
        {"group": ["evil"], "value": [999.0], "text": ["evil"], "flag": [False]}
    ).to_parquet(replacement, index=False)
    os.replace(replacement, source.path)
    with (
        pytest.raises(SecurityPolicyViolation, match="changed after compilation"),
        runtime.connection(compiled.sources),
    ):
        pass


def test_registered_source_keeps_stable_file_identity(services, config, workspace, source):
    runtime = DuckDBRuntime(config)
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(
            input_artifact_id=source.id,
            operations=[{"action": "select", "columns": ["group"]}],
        ),
    )
    replacement = source.path.with_name("replacement-after-registration.parquet")
    pd.DataFrame(
        {"group": ["evil"], "value": [999.0], "text": ["evil"], "flag": [False]}
    ).to_parquet(replacement, index=False)
    with runtime.connection(compiled.sources) as connection:
        with suppress(PermissionError):
            os.replace(replacement, source.path)
        assert connection.execute(compiled.sql, compiled.parameters).fetchall() == [
            ("a",),
            ("a",),
            ("b",),
            ("b",),
        ]


def test_runtime_snapshot_ignores_same_inode_overwrite_after_connection_yields(
    services, config, workspace, source
):
    runtime = DuckDBRuntime(config)
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(
            input_artifact_id=source.id,
            operations=[{"action": "select", "columns": ["group"]}],
        ),
    )
    replacement = source.path.with_name("same-inode-overwrite.parquet")
    pd.DataFrame(
        {"group": ["evil"], "value": [999.0], "text": ["evil"], "flag": [False]}
    ).to_parquet(replacement, index=False)
    with runtime.connection(compiled.sources) as connection:
        with source.path.open("r+b") as destination, replacement.open("rb") as attacker:
            destination.seek(0)
            shutil.copyfileobj(attacker, destination)
            destination.truncate()
        assert connection.execute(compiled.sql, compiled.parameters).fetchall() == [
            ("a",),
            ("a",),
            ("b",),
            ("b",),
        ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duckdb_memory_limit", "3GiB"),
        ("duckdb_threads", 5),
        ("duckdb_max_temp_directory_size", "5GiB"),
    ],
)
def test_runtime_rejects_direct_config_values_above_resource_caps(
    field, value, services, config, workspace, source
):
    unsafe = replace(config, **{field: value})
    runtime = DuckDBRuntime(unsafe)
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(input_artifact_id=source.id, operations=[{"action": "head", "rows": 1}]),
    )
    with (
        pytest.raises(ConfigurationError),
        runtime.connection(compiled.sources),
    ):
        pass


def test_runtime_rejects_resolved_temp_directory_outside_data_dir(
    services, config, workspace, source, tmp_path
):
    class EscapingDataDirectory:
        def __fspath__(self):
            return str(config.data_dir)

        def __truediv__(self, _child):
            return tmp_path / "outside" / "duckdb-temp"

    runtime = DuckDBRuntime(replace(config, data_dir=EscapingDataDirectory()))
    compiled = runtime.compiler(services.repository).compile(
        "tenant-a",
        workspace.id,
        AnalysisPlan(input_artifact_id=source.id, operations=[{"action": "head", "rows": 1}]),
    )
    with (
        pytest.raises(ConfigurationError, match="temp directory"),
        runtime.connection(compiled.sources),
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
