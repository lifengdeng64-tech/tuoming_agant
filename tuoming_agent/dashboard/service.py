from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd
import pyarrow.parquet as pq

from tuoming_agent.analysis.duckdb_runtime import DuckDBRuntime
from tuoming_agent.analysis.models import (
    Aggregation,
    AnalysisPlan,
    GroupByOperation,
    HeadOperation,
    SortOperation,
)
from tuoming_agent.config import AppConfig
from tuoming_agent.dashboard.models import AggregationName
from tuoming_agent.models import ArtifactRecord
from tuoming_agent.storage.base import Repository

if TYPE_CHECKING:
    from tuoming_agent.workspace.service import ArtifactService

_AGGREGATIONS = {"sum", "mean", "min", "max", "count"}
_KPI_BATCH_SIZE = 65_536
_MAX_CHART_POINTS = 200
_MAX_DETAIL_ROWS = 100


class DashboardQueryError(ValueError):
    """Raised when a dashboard request exceeds its local safety contract."""


@dataclass(frozen=True)
class KPIValue:
    column: str
    aggregation: AggregationName
    value: float | int | None


class DashboardService:
    def __init__(
        self,
        repository: Repository,
        artifact_service: ArtifactService,
        config: AppConfig,
    ) -> None:
        self.repository = repository
        self.artifact_service = artifact_service
        self.config = config

    def kpis(
        self,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
        measures: tuple[str, ...],
        aggregation: AggregationName,
    ) -> tuple[KPIValue, ...]:
        if not 1 <= len(measures) <= 4:
            raise DashboardQueryError("Dashboard measures must contain between 1 and 4 columns.")
        self._validate_aggregation(aggregation)
        artifact = self._artifact(tenant_id, workspace_id, artifact_id)
        self._require_columns(artifact, measures)

        states = {
            column: {"sum": 0.0, "count": 0, "min": None, "max": None}
            for column in measures
        }
        parquet = pq.ParquetFile(artifact.path)
        for batch in parquet.iter_batches(columns=list(measures), batch_size=_KPI_BATCH_SIZE):
            frame = batch.to_pandas()
            for column in measures:
                values = pd.to_numeric(frame[column], errors="coerce")
                values = values[values.map(lambda value: pd.notna(value) and math.isfinite(value))]
                if values.empty:
                    continue
                state = states[column]
                state["sum"] += float(values.sum())
                state["count"] += int(values.count())
                current_min = float(values.min())
                current_max = float(values.max())
                state["min"] = (
                    current_min if state["min"] is None else min(state["min"], current_min)
                )
                state["max"] = (
                    current_max if state["max"] is None else max(state["max"], current_max)
                )

        return tuple(
            KPIValue(column, aggregation, self._aggregate_value(states[column], aggregation))
            for column in measures
        )

    def grouped(
        self,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
        dimension: str,
        measure: str,
        aggregation: AggregationName,
        *,
        sort_dimension: bool = False,
    ) -> pd.DataFrame:
        self._validate_aggregation(aggregation)
        artifact = self._artifact(tenant_id, workspace_id, artifact_id)
        self._require_columns(artifact, (dimension, measure))
        if dimension == measure:
            raise DashboardQueryError("Dashboard dimension and measure must be different columns.")

        plan = AnalysisPlan(
            input_artifact_id=artifact.id,
            operations=[
                GroupByOperation(
                    action="groupby",
                    by=[dimension],
                    aggregations=[
                        Aggregation(column=measure, function=aggregation, output=measure)
                    ],
                ),
                SortOperation(
                    action="sort",
                    columns=[dimension if sort_dimension else measure],
                    ascending=bool(sort_dimension),
                ),
                HeadOperation(action="head", rows=_MAX_CHART_POINTS),
            ],
            result_name="Dashboard query",
            safe_summary="Bounded local dashboard aggregation",
        )
        runtime = DuckDBRuntime(self.config)
        try:
            compiled = runtime.compiler(self.repository).compile(
                tenant_id, workspace_id, plan
            )
            with runtime.connection(compiled.sources) as connection:
                result = connection.execute(compiled.sql, compiled.parameters).fetch_df()
        except DashboardQueryError:
            raise
        except Exception as exc:
            raise DashboardQueryError("The bounded local dashboard query failed.") from exc
        if len(result) > _MAX_CHART_POINTS:
            raise DashboardQueryError("Dashboard chart data exceeded the 200 point limit.")
        return result.loc[:, [dimension, measure]]

    def detail(
        self,
        tenant_id: str,
        workspace_id: str,
        artifact_id: str,
        *,
        limit: int = _MAX_DETAIL_ROWS,
    ) -> pd.DataFrame:
        artifact = self._artifact(tenant_id, workspace_id, artifact_id)
        bounded_limit = max(1, min(int(limit), _MAX_DETAIL_ROWS))
        return self.artifact_service.preview(
            tenant_id, artifact.id, limit=bounded_limit, restored=False
        )

    def _artifact(
        self, tenant_id: str, workspace_id: str, artifact_id: str
    ) -> ArtifactRecord:
        artifact = self.repository.get_artifact(tenant_id, artifact_id)
        if artifact.workspace_id != workspace_id:
            raise DashboardQueryError("Artifact belongs to another workspace.")
        if artifact.path.suffix.casefold() != ".parquet":
            raise DashboardQueryError("Dashboard artifacts must be local Parquet files.")
        return artifact

    @staticmethod
    def _require_columns(artifact: ArtifactRecord, columns: tuple[str, ...]) -> None:
        available = {
            str(column.get("name", "")) for column in artifact.schema.get("columns", [])
        }
        if any(column not in available for column in columns):
            raise DashboardQueryError("Dashboard column is not present in the artifact schema.")

    @staticmethod
    def _validate_aggregation(aggregation: str) -> None:
        if aggregation not in _AGGREGATIONS:
            raise DashboardQueryError("Dashboard aggregation is not allowed.")

    @staticmethod
    def _aggregate_value(state: dict[str, Any], aggregation: str) -> float | int | None:
        count = int(state["count"])
        if aggregation == "count":
            return count
        if count == 0:
            return None
        if aggregation == "sum":
            return float(state["sum"])
        if aggregation == "mean":
            return float(state["sum"]) / count
        if aggregation == "min":
            return float(state["min"])
        if aggregation == "max":
            return float(state["max"])
        raise DashboardQueryError("Dashboard aggregation is not allowed.")
