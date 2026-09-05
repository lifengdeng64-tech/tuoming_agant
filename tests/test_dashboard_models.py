from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from tuoming_agent.analysis.models import DashboardChartIntent
from tuoming_agent.dashboard.models import (
    DashboardSelection,
    infer_dashboard_defaults,
    resolve_dashboard_chart_specs,
)
from tuoming_agent.models import ArtifactRecord


def _artifact(columns: list[tuple[str, str]]) -> ArtifactRecord:
    return ArtifactRecord(
        id="artifact-1",
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        kind="analysis_result",
        name="经营分析",
        path=Path("artifact.parquet"),
        row_count=10,
        schema={
            "columns": [{"name": name, "dtype": dtype} for name, dtype in columns]
        },
    )


def test_infer_dashboard_defaults_preserves_schema_order_by_column_role() -> None:
    defaults = infer_dashboard_defaults(
        _artifact(
            [
                ("月份", "datetime64[ns]"),
                ("事业部", "object"),
                ("营业收入", "float64"),
                ("间夜量", "int64"),
                ("是否开业", "bool"),
            ]
        )
    )

    assert defaults.date_columns == ("月份",)
    assert defaults.category_columns == ("事业部", "是否开业")
    assert defaults.numeric_columns == ("营业收入", "间夜量")
    assert defaults.date_column == "月份"
    assert defaults.category_column == "事业部"
    assert defaults.measure_columns == ("营业收入", "间夜量")


def test_infer_dashboard_defaults_limits_default_measures_to_four() -> None:
    defaults = infer_dashboard_defaults(
        _artifact([(f"m{index}", "double") for index in range(6)])
    )

    assert defaults.measure_columns == ("m0", "m1", "m2", "m3")


def test_infer_dashboard_defaults_handles_schema_without_numeric_columns() -> None:
    defaults = infer_dashboard_defaults(_artifact([("hotel", "string")]))

    assert defaults.numeric_columns == ()
    assert defaults.measure_columns == ()
    assert defaults.category_column == "hotel"


def test_dashboard_selection_rejects_more_than_four_measures() -> None:
    with pytest.raises(ValidationError):
        DashboardSelection(
            artifact_id="artifact-1",
            measures=("a", "b", "c", "d", "e"),
        )


def test_dashboard_selection_rejects_unknown_aggregation() -> None:
    with pytest.raises(ValidationError):
        DashboardSelection(
            artifact_id="artifact-1",
            measures=("revenue",),
            aggregation="median",
        )


def test_chart_resolver_preserves_requested_chart_types_and_order() -> None:
    artifact = _artifact(
        [("月份", "datetime64[ns]"), ("事业部", "object"), ("营收", "float64")]
    )
    requested = (
        DashboardChartIntent(
            chart_type="pie",
            title="事业部营收占比",
            dimension="事业部",
            measures=["营收"],
        ),
        DashboardChartIntent(
            chart_type="area",
            title="月度营收走势",
            dimension="月份",
            measures=["营收"],
        ),
    )

    specs = resolve_dashboard_chart_specs(
        artifact,
        ("营收",),
        "sum",
        "月份",
        "事业部",
        requested,
    )

    assert [spec.chart_type for spec in specs] == ["pie", "area"]
    assert [spec.title for spec in specs] == ["事业部营收占比", "月度营收走势"]


def test_chart_resolver_drops_unknown_model_columns_without_duplicate_fallbacks() -> None:
    artifact = _artifact(
        [("事业部", "object"), ("营收", "float64"), ("预算", "float64")]
    )
    requested = (
        DashboardChartIntent(
            chart_type="bar",
            dimension="不存在字段",
            measures=["营收"],
        ),
    )

    specs = resolve_dashboard_chart_specs(
        artifact,
        ("营收", "预算"),
        "sum",
        None,
        "事业部",
        requested,
    )

    assert [spec.chart_type for spec in specs] == ["bar", "scatter"]
    assert specs[0].dimension == "事业部"
    assert specs[1].measures == ("营收", "预算")
