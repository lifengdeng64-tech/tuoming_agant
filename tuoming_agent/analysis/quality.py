from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from tuoming_agent.analysis.executor import AnalysisCandidate


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, value: dict[str, str]) -> QualityIssue:
        return cls(code=value["code"], message=value["message"])


@dataclass(frozen=True)
class QualityReport:
    passed: bool
    failures: tuple[QualityIssue, ...] = ()
    warnings: tuple[QualityIssue, ...] = ()

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "failures": [item.to_dict() for item in self.failures],
            "warnings": [item.to_dict() for item in self.warnings],
        }

    @classmethod
    def from_dict(cls, value: dict) -> QualityReport:
        return cls(
            passed=bool(value["passed"]),
            failures=tuple(QualityIssue.from_dict(item) for item in value.get("failures", [])),
            warnings=tuple(QualityIssue.from_dict(item) for item in value.get("warnings", [])),
        )


class AnalysisQualityValidator:
    def __init__(
        self,
        large_result_rows: int = 1_000_000,
        max_row_amplification: float = 100.0,
    ):
        self.large_result_rows = large_result_rows
        self.max_row_amplification = max_row_amplification

    def validate(self, candidate: AnalysisCandidate) -> QualityReport:
        if candidate.path is not None:
            return self._validate_parquet(candidate)
        if candidate.dataframe is None:
            return QualityReport(
                False,
                failures=(
                    QualityIssue("serialization_failed", "Result data is unavailable."),
                ),
            )
        return self._validate_dataframe(candidate)

    def _validate_dataframe(self, candidate: AnalysisCandidate) -> QualityReport:
        frame = candidate.dataframe
        failures: list[QualityIssue] = []
        warnings: list[QualityIssue] = []
        columns = [str(column) for column in frame.columns]

        if any(not column.strip() for column in columns):
            failures.append(QualityIssue("blank_columns", "输出包含空列名。"))
        if len(columns) != len(set(columns)):
            failures.append(QualityIssue("duplicate_columns", "输出列名不唯一。"))
        invalid_lineage = sorted(set(candidate.lineage) - set(columns))
        if invalid_lineage:
            failures.append(QualityIssue("invalid_lineage", "字段血缘引用了不存在的输出列。"))

        numeric = frame.select_dtypes(include="number")
        if not numeric.empty and np.isinf(numeric.to_numpy(dtype=float, na_value=np.nan)).any():
            failures.append(QualityIssue("infinite_values", "数值结果包含正负无穷。"))

        if len(columns) == len(set(columns)) and all(column.strip() for column in columns):
            try:
                payload = BytesIO()
                frame.to_parquet(payload, index=False)
                payload.seek(0)
                restored = pd.read_parquet(payload)
                if restored.shape != frame.shape or list(restored.columns) != list(frame.columns):
                    raise ValueError("round-trip shape or schema mismatch")
            except Exception:
                failures.append(QualityIssue("serialization_failed", "结果无法可靠序列化并读回。"))

        if frame.empty:
            warnings.append(QualityIssue("empty_result", "结果为空，请确认筛选条件。"))
        elif frame.isna().any(axis=None):
            warnings.append(QualityIssue("null_values", "结果中仍包含空值。"))
        if len(frame) > self.large_result_rows:
            warnings.append(QualityIssue("large_result", "输出数据量较大。"))

        return QualityReport(not failures, tuple(failures), tuple(warnings))

    def _validate_parquet(self, candidate: AnalysisCandidate) -> QualityReport:
        failures: list[QualityIssue] = []
        warnings: list[QualityIssue] = []
        columns = [
            str(item.get("name", ""))
            for item in (candidate.schema or {}).get("columns", [])
            if isinstance(item, dict)
        ]

        if any(not column.strip() for column in columns):
            failures.append(QualityIssue("blank_columns", "输出包含空列名。"))
        if len(columns) != len(set(columns)):
            failures.append(QualityIssue("duplicate_columns", "输出列名不唯一。"))
        invalid_lineage = sorted(set(candidate.lineage) - set(columns))
        if invalid_lineage:
            failures.append(QualityIssue("invalid_lineage", "字段血缘引用了不存在的输出列。"))

        row_count = candidate.row_count or 0
        null_count = 0
        has_infinite = False
        try:
            parquet = pq.ParquetFile(candidate.path)
            if parquet.metadata.num_rows != row_count:
                raise ValueError("row count mismatch")
            if parquet.schema_arrow.names != columns:
                raise ValueError("schema mismatch")
            for batch in parquet.iter_batches(batch_size=65_536):
                null_count += sum(column.null_count for column in batch.columns)
                for index, field in enumerate(batch.schema):
                    if pa.types.is_floating(field.type):
                        finite = pc.is_finite(batch.column(index))
                        if pc.any(pc.invert(pc.fill_null(finite, True))).as_py():
                            has_infinite = True
            if has_infinite:
                failures.append(QualityIssue("infinite_values", "数值结果包含正负无穷。"))
        except Exception:
            failures.append(QualityIssue("serialization_failed", "结果无法可靠序列化并读回。"))

        cell_count = row_count * len(columns)
        null_ratio = null_count / cell_count if cell_count else 0.0
        if row_count == 0:
            warnings.append(QualityIssue("empty_result", "结果为空，请确认筛选条件。"))
        elif null_ratio > 0:
            warnings.append(QualityIssue("null_values", "结果中仍包含空值。"))
        if row_count > self.large_result_rows:
            warnings.append(QualityIssue("large_result", "输出数据量较大。"))
        if (
            candidate.input_row_count
            and row_count / candidate.input_row_count > self.max_row_amplification
        ):
            warnings.append(
                QualityIssue("row_amplification", "输出行数相对输入显著放大。")
            )

        return QualityReport(not failures, tuple(failures), tuple(warnings))
