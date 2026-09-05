from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tuoming_agent.analysis.models import DashboardChartIntent, DashboardChartType
from tuoming_agent.models import ArtifactRecord

AggregationName = Literal["sum", "mean", "min", "max", "count"]


class DashboardSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    measures: tuple[str, ...] = Field(min_length=1, max_length=4)
    aggregation: AggregationName = "sum"
    date_column: str | None = None
    category_column: str | None = None


@dataclass(frozen=True)
class DashboardDefaults:
    numeric_columns: tuple[str, ...]
    date_columns: tuple[str, ...]
    category_columns: tuple[str, ...]
    measure_columns: tuple[str, ...]
    date_column: str | None
    category_column: str | None


@dataclass(frozen=True)
class DashboardChartSpec:
    chart_type: DashboardChartType
    title: str
    dimension: str | None
    measures: tuple[str, ...]
    aggregation: AggregationName


_NUMERIC_MARKERS = ("int", "uint", "float", "double", "decimal")
_DATE_MARKERS = ("date", "time", "timestamp")
_DATE_NAME_MARKERS = (
    "日期",
    "时间",
    "月份",
    "年月",
    "年度",
    "date",
    "time",
    "month",
    "year",
)


def infer_dashboard_defaults(artifact: ArtifactRecord) -> DashboardDefaults:
    numeric: list[str] = []
    dates: list[str] = []
    categories: list[str] = []

    for column in artifact.schema.get("columns", []):
        name = str(column.get("name", "")).strip()
        if not name:
            continue
        dtype = str(column.get("dtype", "")).casefold()
        if any(marker in dtype for marker in _DATE_MARKERS) or (
            not any(marker in dtype for marker in _NUMERIC_MARKERS)
            and any(marker in name.casefold() for marker in _DATE_NAME_MARKERS)
        ):
            dates.append(name)
        elif any(marker in dtype for marker in _NUMERIC_MARKERS):
            numeric.append(name)
        else:
            categories.append(name)

    return DashboardDefaults(
        numeric_columns=tuple(numeric),
        date_columns=tuple(dates),
        category_columns=tuple(categories),
        measure_columns=tuple(numeric[:4]),
        date_column=dates[0] if dates else None,
        category_column=categories[0] if categories else None,
    )


def resolve_dashboard_chart_specs(
    artifact: ArtifactRecord,
    measures: tuple[str, ...],
    aggregation: AggregationName,
    date_column: str | None,
    category_column: str | None,
    planned: Sequence[DashboardChartIntent] = (),
) -> tuple[DashboardChartSpec, ...]:
    """Resolve model intent against the local schema, then add safe useful defaults."""
    defaults = infer_dashboard_defaults(artifact)
    available = {
        str(column.get("name", "")) for column in artifact.schema.get("columns", [])
    }
    numeric = set(defaults.numeric_columns)
    selected = tuple(column for column in measures if column in numeric)[:4]
    valid_planned: list[DashboardChartSpec] = []
    seen: set[tuple[object, ...]] = set()

    for chart in planned:
        chart_measures = tuple(chart.measures)
        if not chart_measures or any(
            column not in numeric or column not in selected for column in chart_measures
        ):
            continue
        if chart.dimension is not None and chart.dimension not in available:
            continue
        if chart.dimension is not None and chart.dimension in chart_measures:
            continue
        key = (chart.chart_type, chart.dimension, chart_measures, chart.aggregation)
        if key in seen:
            continue
        seen.add(key)
        valid_planned.append(
            DashboardChartSpec(
                chart_type=chart.chart_type,
                title=chart.title or _default_chart_title(
                    chart.chart_type, chart.dimension, chart_measures
                ),
                dimension=chart.dimension,
                measures=chart_measures,
                aggregation=chart.aggregation,
            )
        )
        if len(valid_planned) == 4:
            break

    if valid_planned:
        return tuple(valid_planned)

    fallback: list[DashboardChartSpec] = []
    if date_column in available and selected:
        fallback.append(
            DashboardChartSpec(
                chart_type="line",
                title=f"{date_column}趋势",
                dimension=date_column,
                measures=selected,
                aggregation=aggregation,
            )
        )
    if category_column in available and selected:
        fallback.append(
            DashboardChartSpec(
                chart_type="bar",
                title=f"{category_column} · {selected[0]}对比",
                dimension=category_column,
                measures=(selected[0],),
                aggregation=aggregation,
            )
        )
    if len(selected) >= 2:
        fallback.append(
            DashboardChartSpec(
                chart_type="scatter",
                title=f"{selected[0]}与{selected[1]}关系",
                dimension=category_column if category_column in available else None,
                measures=selected[:2],
                aggregation=aggregation,
            )
        )
    return tuple(fallback[:4])


def _default_chart_title(
    chart_type: DashboardChartType,
    dimension: str | None,
    measures: tuple[str, ...],
) -> str:
    measure_text = "、".join(measures)
    if chart_type == "line":
        return f"{measure_text}趋势"
    if chart_type == "area":
        return f"{measure_text}变化"
    if chart_type == "bar":
        return f"{dimension} · {measure_text}对比"
    if chart_type == "pie":
        return f"{dimension} · {measure_text}占比"
    return f"{measures[0]}与{measures[1]}关系"
