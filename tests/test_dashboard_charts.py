from __future__ import annotations

import pandas as pd
import pytest

from tuoming_agent.dashboard.charts import ChartDataError, bar_figure, line_figure
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
