from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

from tuoming_agent.ui.theme import ACCENT, PLOTLY_LAYOUT, SERIES_COLORS

_MAX_POINTS = 200
_MAX_SERIES = 4


class ChartDataError(ValueError):
    """Raised when bounded dashboard data cannot be rendered safely."""


def line_figure(
    frame: pd.DataFrame,
    x: str,
    measures: tuple[str, ...],
    *,
    title: str,
) -> go.Figure:
    _validate_frame(frame, (x, *measures))
    if not 1 <= len(measures) <= _MAX_SERIES:
        raise ChartDataError("A line chart must contain between 1 and 4 series.")

    figure = go.Figure()
    for index, measure in enumerate(measures):
        figure.add_trace(
            go.Scatter(
                x=frame[x],
                y=frame[measure],
                name=measure,
                mode="lines+markers",
                line={"color": SERIES_COLORS[index], "width": 3},
                marker={"size": 6},
                connectgaps=False,
            )
        )
    figure.update_layout(
        **PLOTLY_LAYOUT,
        title={"text": title, "x": 0, "xanchor": "left"},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08, "x": 1, "xanchor": "right"},
    )
    return figure


def bar_figure(
    frame: pd.DataFrame,
    category: str,
    measure: str,
    *,
    title: str,
) -> go.Figure:
    _validate_frame(frame, (category, measure))
    figure = go.Figure(
        go.Bar(
            x=frame[measure],
            y=frame[category],
            orientation="h",
            marker={"color": ACCENT, "cornerradius": 6},
            hovertemplate=f"%{{y}}<br>{measure}: %{{x:,.2f}}<extra></extra>",
        )
    )
    figure.update_layout(
        **PLOTLY_LAYOUT,
        title={"text": title, "x": 0, "xanchor": "left"},
        showlegend=False,
    )
    return figure


def _validate_frame(frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
    if len(frame) > _MAX_POINTS:
        raise ChartDataError("Dashboard charts are limited to 200 points.")
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ChartDataError("Dashboard chart columns are missing from the bounded result.")
