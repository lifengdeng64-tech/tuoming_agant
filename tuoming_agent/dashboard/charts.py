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


def area_figure(
    frame: pd.DataFrame,
    x: str,
    measures: tuple[str, ...],
    *,
    title: str,
) -> go.Figure:
    _validate_frame(frame, (x, *measures))
    if not 1 <= len(measures) <= _MAX_SERIES:
        raise ChartDataError("An area chart must contain between 1 and 4 series.")

    figure = go.Figure()
    for index, measure in enumerate(measures):
        figure.add_trace(
            go.Scatter(
                x=frame[x],
                y=frame[measure],
                name=measure,
                mode="lines",
                fill="tozeroy",
                line={"color": SERIES_COLORS[index], "width": 2.5},
                opacity=0.72,
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


def pie_figure(
    frame: pd.DataFrame,
    category: str,
    measure: str,
    *,
    title: str,
) -> go.Figure:
    _validate_frame(frame, (category, measure))
    data = frame.loc[:, [category, measure]].copy()
    data[measure] = pd.to_numeric(data[measure], errors="coerce")
    data = data.dropna(subset=[category, measure]).sort_values(
        measure, ascending=False, kind="stable"
    )
    if len(data) > 9:
        top = data.head(8)
        remainder = pd.DataFrame(
            {category: ["其他"], measure: [data.iloc[8:][measure].sum()]}
        )
        data = pd.concat([top, remainder], ignore_index=True)
    figure = go.Figure(
        go.Pie(
            labels=data[category],
            values=data[measure],
            hole=0.58,
            sort=False,
            marker={"colors": list(SERIES_COLORS)},
            textinfo="percent",
            hovertemplate=(
                f"%{{label}}<br>{measure}: %{{value:,.2f}}<br>"
                "占比: %{percent}<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        **PLOTLY_LAYOUT,
        title={"text": title, "x": 0, "xanchor": "left"},
        legend={"orientation": "h", "y": -0.08, "x": 0, "xanchor": "left"},
    )
    return figure


def scatter_figure(
    frame: pd.DataFrame,
    x: str,
    y: str,
    *,
    title: str,
    label: str | None = None,
) -> go.Figure:
    columns = (x, y, label) if label else (x, y)
    _validate_frame(frame, tuple(column for column in columns if column is not None))
    customdata = frame[label] if label else None
    hover_label = "%{customdata}<br>" if label else ""
    figure = go.Figure(
        go.Scatter(
            x=frame[x],
            y=frame[y],
            customdata=customdata,
            mode="markers",
            marker={
                "color": ACCENT,
                "size": 11,
                "opacity": 0.72,
                "line": {"color": "rgba(255,255,255,.88)", "width": 1},
            },
            hovertemplate=(
                f"{hover_label}{x}: %{{x:,.2f}}<br>{y}: %{{y:,.2f}}<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        **PLOTLY_LAYOUT,
        title={"text": title, "x": 0, "xanchor": "left"},
        showlegend=False,
        hovermode="closest",
        xaxis_title=x,
        yaxis_title=y,
    )
    return figure


def _validate_frame(frame: pd.DataFrame, columns: tuple[str, ...]) -> None:
    if len(frame) > _MAX_POINTS:
        raise ChartDataError("Dashboard charts are limited to 200 points.")
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ChartDataError("Dashboard chart columns are missing from the bounded result.")
