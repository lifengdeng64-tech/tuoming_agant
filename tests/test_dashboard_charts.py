from __future__ import annotations

import pandas as pd
import pytest

from tuoming_agent.dashboard.charts import (
    ChartDataError,
    area_figure,
    bar_figure,
    line_figure,
    pie_figure,
    scatter_figure,
)
from tuoming_agent.ui.theme import PLOTLY_CONFIG


def test_line_figure_uses_minimal_local_theme_and_unified_hover() -> None:
    frame = pd.DataFrame(
        {
            "month": pd.to_datetime(["2026-01-01", "2026-02-01"]),
            "revenue": [10.0, 12.0],
        }
    )

    figure = line_figure(frame, "month", ("revenue",), title="营收趋势")

    assert figure.layout.paper_bgcolor == "rgba(0,0,0,0)"
    assert figure.layout.plot_bgcolor == "rgba(0,0,0,0)"
    assert figure.layout.font.color == "#1D1D1F"
    assert figure.layout.hovermode == "x unified"
    assert figure.data[0].line.color == "#0071E3"
    assert list(figure.data[0].y) == [10.0, 12.0]


def test_bar_figure_is_horizontal_and_keeps_bounded_category_order() -> None:
    frame = pd.DataFrame(
        {"division": ["华东", "华南"], "revenue": [120.0, 100.0]}
    )

    figure = bar_figure(frame, "division", "revenue", title="事业部收入")

    assert figure.data[0].orientation == "h"
    assert list(figure.data[0].y) == ["华东", "华南"]
    assert list(figure.data[0].x) == [120.0, 100.0]
    assert figure.layout.showlegend is False


def test_area_figure_uses_filled_series() -> None:
    frame = pd.DataFrame({"月份": ["一月", "二月"], "营收": [10.0, 12.0]})

    figure = area_figure(frame, "月份", ("营收",), title="营收走势")

    assert figure.data[0].type == "scatter"
    assert figure.data[0].fill == "tozeroy"
    assert figure.layout.hovermode == "x unified"


def test_pie_figure_builds_donut_for_part_to_whole_request() -> None:
    frame = pd.DataFrame({"事业部": ["华东", "华南"], "营收": [60.0, 40.0]})

    figure = pie_figure(frame, "事业部", "营收", title="营收占比")

    assert figure.data[0].type == "pie"
    assert figure.data[0].hole == pytest.approx(0.58)
    assert list(figure.data[0].labels) == ["华东", "华南"]


def test_scatter_figure_uses_two_requested_measures() -> None:
    frame = pd.DataFrame(
        {"营收": [100.0, 120.0], "间夜量": [20.0, 30.0], "事业部": ["华东", "华南"]}
    )

    figure = scatter_figure(
        frame,
        "营收",
        "间夜量",
        title="营收与间夜量关系",
        label="事业部",
    )

    assert figure.data[0].type == "scatter"
    assert figure.data[0].mode == "markers"
    assert list(figure.data[0].x) == [100.0, 120.0]
    assert list(figure.data[0].y) == [20.0, 30.0]


@pytest.mark.parametrize("builder", ["line", "bar"])
def test_chart_builders_reject_more_than_200_points(builder: str) -> None:
    frame = pd.DataFrame(
        {"dimension": list(range(201)), "measure": list(range(201))}
    )

    with pytest.raises(ChartDataError, match="200"):
        if builder == "line":
            line_figure(frame, "dimension", ("measure",), title="Too many")
        else:
            bar_figure(frame, "dimension", "measure", title="Too many")


def test_line_figure_rejects_more_than_four_series() -> None:
    frame = pd.DataFrame({"month": ["Jan"], **{f"m{i}": [i] for i in range(5)}})

    with pytest.raises(ChartDataError, match="4"):
        line_figure(frame, "month", tuple(f"m{i}" for i in range(5)), title="Too many")


def test_plotly_config_disables_vendor_branding_and_keeps_png_export() -> None:
    assert PLOTLY_CONFIG["displaylogo"] is False
    assert PLOTLY_CONFIG["responsive"] is True
    assert PLOTLY_CONFIG["toImageButtonOptions"]["format"] == "png"
