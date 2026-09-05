from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelectOperation(StrictModel):
    action: Literal["select"]
    columns: list[str] = Field(min_length=1)


class FilterOperation(StrictModel):
    action: Literal["filter"]
    column: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in", "notnull", "isnull"]
    value: Any | None = None


class SortOperation(StrictModel):
    action: Literal["sort"]
    columns: list[str] = Field(min_length=1)
    ascending: bool | list[bool] = True


class RenameOperation(StrictModel):
    action: Literal["rename"]
    mapping: dict[str, str] = Field(min_length=1)


class CastOperation(StrictModel):
    action: Literal["cast"]
    mapping: dict[str, Literal["string", "int64", "float64", "boolean", "datetime"]] = Field(
        min_length=1
    )


class FillnaOperation(StrictModel):
    action: Literal["fillna"]
    values: dict[str, Any] = Field(min_length=1)


class DropnaOperation(StrictModel):
    action: Literal["dropna"]
    subset: list[str] | None = None
    how: Literal["any", "all"] = "any"


class DeduplicateOperation(StrictModel):
    action: Literal["deduplicate"]
    subset: list[str] | None = None
    keep: Literal["first", "last", False] = "first"


class MergeOperation(StrictModel):
    action: Literal["merge"]
    right_artifact_id: str
    how: Literal["inner", "left", "right", "outer"] = "inner"
    left_on: list[str] = Field(min_length=1)
    right_on: list[str] = Field(min_length=1)
    suffixes: tuple[str, str] = ("_左表", "_右表")


class Aggregation(StrictModel):
    column: str
    function: Literal["sum", "mean", "median", "min", "max", "count", "nunique", "first", "last"]
    output: str


class GroupByOperation(StrictModel):
    action: Literal["groupby"]
    by: list[str] = Field(min_length=1)
    aggregations: list[Aggregation] = Field(min_length=1)


class DeriveOperation(StrictModel):
    action: Literal["derive"]
    column: str
    expression: str = Field(min_length=1, max_length=1000)


class HeadOperation(StrictModel):
    action: Literal["head"]
    rows: int = Field(default=10, ge=1, le=100_000)


class TailOperation(StrictModel):
    action: Literal["tail"]
    rows: int = Field(default=10, ge=1, le=100_000)


Operation = Annotated[
    SelectOperation
    | FilterOperation
    | SortOperation
    | RenameOperation
    | CastOperation
    | FillnaOperation
    | DropnaOperation
    | DeduplicateOperation
    | MergeOperation
    | GroupByOperation
    | DeriveOperation
    | HeadOperation
    | TailOperation,
    Field(discriminator="action"),
]


DashboardChartType = Literal["line", "area", "bar", "pie", "scatter"]


class DashboardChartIntent(StrictModel):
    """A declarative chart request; data access and rendering remain local."""

    chart_type: DashboardChartType
    title: str | None = Field(default=None, max_length=80)
    dimension: str | None = None
    measures: list[str] = Field(min_length=1, max_length=4)
    aggregation: Literal["sum", "mean", "min", "max", "count"] = "sum"

    @model_validator(mode="after")
    def require_chart_shape(self) -> DashboardChartIntent:
        if self.chart_type in {"line", "area", "bar", "pie"} and not self.dimension:
            raise ValueError(f"{self.chart_type} charts require a dimension.")
        if self.chart_type == "pie" and len(self.measures) != 1:
            raise ValueError("Pie charts require exactly one measure.")
        if self.chart_type == "scatter" and len(self.measures) != 2:
            raise ValueError("Scatter charts require exactly two measures.")
        return self


class DashboardIntent(StrictModel):
    """A bounded BI view that is rendered locally after an approved plan completes."""

    measures: list[str] = Field(min_length=1, max_length=4)
    aggregation: Literal["sum", "mean", "min", "max", "count"] = "sum"
    date_column: str | None = None
    category_column: str | None = None
    charts: list[DashboardChartIntent] = Field(default_factory=list, max_length=4)


class AnalysisPlan(StrictModel):
    input_artifact_id: str
    operations: list[Operation] = Field(default_factory=list, max_length=50)
    result_name: str = Field(default="分析结果", min_length=1, max_length=120)
    safe_summary: str = Field(default="处理完成", max_length=1000)
    dashboard: DashboardIntent | None = None

    @model_validator(mode="after")
    def require_local_action(self) -> AnalysisPlan:
        if not self.operations and self.dashboard is None:
            raise ValueError("A plan must contain operations or a dashboard intent.")
        return self


