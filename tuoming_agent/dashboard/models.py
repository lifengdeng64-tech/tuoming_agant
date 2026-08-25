from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


_NUMERIC_MARKERS = ("int", "float", "double", "decimal")
_DATE_MARKERS = ("date", "time", "timestamp")


def infer_dashboard_defaults(artifact: ArtifactRecord) -> DashboardDefaults:
    numeric: list[str] = []
    dates: list[str] = []
    categories: list[str] = []

    for column in artifact.schema.get("columns", []):
        name = str(column.get("name", "")).strip()
        if not name:
            continue
        dtype = str(column.get("dtype", "")).casefold()
        if any(marker in dtype for marker in _DATE_MARKERS):
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
