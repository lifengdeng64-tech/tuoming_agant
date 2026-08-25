from __future__ import annotations

import pandas as pd

from tuoming_agent.dashboard.charts import bar_figure, line_figure
from tuoming_agent.dashboard.models import DashboardSelection


def test_dashboard_acceptance_queries_full_artifact_and_bounds_visual_data(
    services, workspace
) -> None:
    frame = pd.DataFrame(
        {
            "月份": pd.date_range("2025-01-01", periods=240, freq="D"),
            "事业部": [f"事业部-{index % 24:02d}" for index in range(240)],
            "营业收入": [float(index + 1) for index in range(240)],
        }
    )
    artifact = services.artifacts.save_result(
        "tenant-a", workspace.id, "经营数据", frame, {}, ()
    )
    selection = DashboardSelection(
        artifact_id=artifact.id,
        measures=("营业收入",),
        aggregation="sum",
        date_column="月份",
        category_column="事业部",
    )

    kpis = services.dashboard.kpis(
        "tenant-a",
        workspace.id,
        selection.artifact_id,
        selection.measures,
        selection.aggregation,
    )
    trend = services.dashboard.grouped(
        "tenant-a",
        workspace.id,
        artifact.id,
        selection.date_column,
        selection.measures[0],
        selection.aggregation,
        sort_dimension=True,
    )
    comparison = services.dashboard.grouped(
        "tenant-a",
        workspace.id,
        artifact.id,
        selection.category_column,
        selection.measures[0],
        selection.aggregation,
    )

    assert kpis[0].value == frame["营业收入"].sum()
    assert len(trend) == 200
    assert len(comparison) == 24
    assert len(line_figure(trend, "月份", ("营业收入",), title="趋势").data) == 1
    assert len(bar_figure(comparison, "事业部", "营业收入", title="对比").data) == 1
