from __future__ import annotations

from typing import Any

import pandas as pd

from tuoming_agent.analysis.expression import evaluate_expression
from tuoming_agent.analysis.models import (
    AnalysisPlan,
    CastOperation,
    DeduplicateOperation,
    DeriveOperation,
    DropnaOperation,
    FillnaOperation,
    FilterOperation,
    GroupByOperation,
    HeadOperation,
    MergeOperation,
    RenameOperation,
    SelectOperation,
    SortOperation,
    TailOperation,
)
from tuoming_agent.models import ArtifactRecord, ColumnLineage
from tuoming_agent.workspace.service import ArtifactService


class AnalysisExecutionError(ValueError):
    """Raised for a valid plan that cannot be applied to the selected artifacts."""


class AnalysisExecutor:
    def __init__(self, artifact_service: ArtifactService):
        self.artifact_service = artifact_service

    def execute(self, tenant_id: str, workspace_id: str, plan: AnalysisPlan) -> ArtifactRecord:
        source, dataframe = self.artifact_service.load(tenant_id, plan.input_artifact_id)
        self._assert_workspace(source, workspace_id)
        lineage = dict(source.lineage)
        parent_ids = [source.id]

        try:
            for operation in plan.operations:
                dataframe, lineage, new_parent = self._apply(
                    tenant_id, workspace_id, dataframe, lineage, operation
                )
                if new_parent and new_parent not in parent_ids:
                    parent_ids.append(new_parent)
                dataframe = dataframe.reset_index(drop=True)
        except AnalysisExecutionError:
            raise
        except Exception as exc:
            raise AnalysisExecutionError(
                "The approved analysis plan could not be executed on this schema."
            ) from exc

        return self.artifact_service.save_result(
            tenant_id,
            workspace_id,
            plan.result_name,
            dataframe,
            lineage,
            tuple(parent_ids),
        )

    def _apply(
        self,
        tenant_id: str,
        workspace_id: str,
        dataframe: pd.DataFrame,
        lineage: dict[str, ColumnLineage],
        operation: Any,
    ) -> tuple[pd.DataFrame, dict[str, ColumnLineage], str | None]:
        if isinstance(operation, SelectOperation):
            self._require_columns(dataframe, operation.columns)
            return (
                dataframe.loc[:, operation.columns].copy(),
                {column: lineage[column] for column in operation.columns if column in lineage},
                None,
            )
        if isinstance(operation, FilterOperation):
            return self._filter(dataframe, operation), lineage, None
        if isinstance(operation, SortOperation):
            self._require_columns(dataframe, operation.columns)
            return (
                dataframe.sort_values(operation.columns, ascending=operation.ascending),
                lineage,
                None,
            )
        if isinstance(operation, RenameOperation):
            self._require_columns(dataframe, list(operation.mapping))
            renamed_lineage = {
                operation.mapping.get(column, column): value for column, value in lineage.items()
            }
            return dataframe.rename(columns=operation.mapping), renamed_lineage, None
        if isinstance(operation, CastOperation):
            return self._cast(dataframe, operation), lineage, None
        if isinstance(operation, FillnaOperation):
            self._require_columns(dataframe, list(operation.values))
            return dataframe.fillna(operation.values), lineage, None
        if isinstance(operation, DropnaOperation):
            if operation.subset:
                self._require_columns(dataframe, operation.subset)
            return dataframe.dropna(subset=operation.subset, how=operation.how), lineage, None
        if isinstance(operation, DeduplicateOperation):
            if operation.subset:
                self._require_columns(dataframe, operation.subset)
            return (
                dataframe.drop_duplicates(subset=operation.subset, keep=operation.keep),
                lineage,
                None,
            )
        if isinstance(operation, MergeOperation):
            return self._merge(tenant_id, workspace_id, dataframe, lineage, operation)
        if isinstance(operation, GroupByOperation):
            return self._groupby(dataframe, lineage, operation)
        if isinstance(operation, DeriveOperation):
            derived = dataframe.copy()
            derived[operation.column] = evaluate_expression(derived, operation.expression)
            new_lineage = dict(lineage)
            new_lineage.pop(operation.column, None)
            return derived, new_lineage, None
        if isinstance(operation, HeadOperation):
            return dataframe.head(operation.rows), lineage, None
        if isinstance(operation, TailOperation):
            return dataframe.tail(operation.rows), lineage, None
        raise AnalysisExecutionError("Operation is not in the execution allowlist.")

    def _filter(self, dataframe: pd.DataFrame, operation: FilterOperation) -> pd.DataFrame:
        self._require_columns(dataframe, [operation.column])
        series = dataframe[operation.column]
        operator_name = operation.operator
        if operator_name == "eq":
            mask = series == operation.value
        elif operator_name == "ne":
            mask = series != operation.value
        elif operator_name == "gt":
            mask = series > operation.value
        elif operator_name == "gte":
            mask = series >= operation.value
        elif operator_name == "lt":
            mask = series < operation.value
        elif operator_name == "lte":
            mask = series <= operation.value
        elif operator_name == "contains":
            mask = series.astype("string").str.contains(str(operation.value), regex=False, na=False)
        elif operator_name == "in":
            if not isinstance(operation.value, list):
                raise AnalysisExecutionError("The 'in' filter requires a list value.")
            mask = series.isin(operation.value)
        elif operator_name == "notnull":
            mask = series.notna()
        elif operator_name == "isnull":
            mask = series.isna()
        else:
            raise AnalysisExecutionError("Filter operator is not allowed.")
        return dataframe.loc[mask].copy()

    def _cast(self, dataframe: pd.DataFrame, operation: CastOperation) -> pd.DataFrame:
        self._require_columns(dataframe, list(operation.mapping))
        result = dataframe.copy()
        for column, target in operation.mapping.items():
            if target == "datetime":
                result[column] = pd.to_datetime(result[column], errors="raise")
            else:
                result[column] = result[column].astype(target)
        return result

    def _merge(
        self,
        tenant_id: str,
        workspace_id: str,
        left: pd.DataFrame,
        left_lineage: dict[str, ColumnLineage],
        operation: MergeOperation,
    ) -> tuple[pd.DataFrame, dict[str, ColumnLineage], str]:
        right_artifact, right = self.artifact_service.load(tenant_id, operation.right_artifact_id)
        self._assert_workspace(right_artifact, workspace_id)
        self._require_columns(left, operation.left_on)
        self._require_columns(right, operation.right_on)
        if len(operation.left_on) != len(operation.right_on):
            raise AnalysisExecutionError("Merge key counts must match.")
        result = left.merge(
            right,
            how=operation.how,
            left_on=operation.left_on,
            right_on=operation.right_on,
            suffixes=operation.suffixes,
        )
        output_lineage = self._merge_lineage(
            left,
            left_lineage,
            right,
            right_artifact.lineage,
            operation,
            result,
        )
        return result, output_lineage, right_artifact.id

    @staticmethod
    def _merge_lineage(
        left: pd.DataFrame,
        left_lineage: dict[str, ColumnLineage],
        right: pd.DataFrame,
        right_lineage: dict[str, ColumnLineage],
        operation: MergeOperation,
        result: pd.DataFrame,
    ) -> dict[str, ColumnLineage]:
        output: dict[str, ColumnLineage] = {}
        overlap = (set(left.columns) & set(right.columns)) - set(operation.left_on)
        for column, value in left_lineage.items():
            output_name = f"{column}{operation.suffixes[0]}" if column in overlap else column
            if output_name in result.columns:
                output[output_name] = value
        for column, value in right_lineage.items():
            output_name = f"{column}{operation.suffixes[1]}" if column in overlap else column
            if output_name in result.columns and output_name not in output:
                output[output_name] = value
        return output

    def _groupby(
        self,
        dataframe: pd.DataFrame,
        lineage: dict[str, ColumnLineage],
        operation: GroupByOperation,
    ) -> tuple[pd.DataFrame, dict[str, ColumnLineage], str | None]:
        columns = operation.by + [aggregation.column for aggregation in operation.aggregations]
        self._require_columns(dataframe, columns)
        named_aggregations = {
            aggregation.output: pd.NamedAgg(column=aggregation.column, aggfunc=aggregation.function)
            for aggregation in operation.aggregations
        }
        grouped = (
            dataframe.groupby(operation.by, dropna=False).agg(**named_aggregations).reset_index()
        )
        grouped_lineage = {column: lineage[column] for column in operation.by if column in lineage}
        return grouped, grouped_lineage, None

    @staticmethod
    def _require_columns(dataframe: pd.DataFrame, columns: list[str]) -> None:
        missing = [column for column in columns if column not in dataframe.columns]
        if missing:
            raise AnalysisExecutionError(f"Columns do not exist: {', '.join(missing)}")

    @staticmethod
    def _assert_workspace(artifact: ArtifactRecord, workspace_id: str) -> None:
        if artifact.workspace_id != workspace_id:
            raise AnalysisExecutionError("Artifact belongs to another workspace.")
