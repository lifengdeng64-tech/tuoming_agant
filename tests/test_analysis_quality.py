from __future__ import annotations

import pandas as pd

from tuoming_agent.analysis.executor import AnalysisCandidate
from tuoming_agent.analysis.quality import AnalysisQualityValidator
from tuoming_agent.models import ColumnLineage


def _candidate(frame: pd.DataFrame, lineage=None) -> AnalysisCandidate:
    return AnalysisCandidate(
        name="result",
        dataframe=frame,
        lineage=lineage or {},
        parent_ids=("source",),
    )


def test_quality_validator_accepts_roundtrippable_result():
    report = AnalysisQualityValidator().validate(
        _candidate(
            pd.DataFrame({"store": ["A", "B"], "sales": [1.0, 2.0]}),
            {"store": ColumnLineage("store")},
        )
    )
    assert report.passed is True
    assert report.failures == ()


def test_quality_validator_rejects_duplicate_columns_invalid_lineage_and_infinity():
    frame = pd.DataFrame([[1.0, float("inf")]], columns=["value", "value"])
    report = AnalysisQualityValidator().validate(
        _candidate(frame, {"missing": ColumnLineage("store")})
    )
    assert report.passed is False
    assert {item.code for item in report.failures} == {
        "duplicate_columns",
        "invalid_lineage",
        "infinite_values",
    }


def test_quality_validator_warns_for_empty_and_null_results():
    report = AnalysisQualityValidator().validate(
        _candidate(pd.DataFrame({"value": pd.Series([None], dtype="object")}))
    )
    assert report.passed is True
    assert {item.code for item in report.warnings} == {"null_values"}

    empty = AnalysisQualityValidator().validate(_candidate(pd.DataFrame({"value": []})))
    assert {item.code for item in empty.warnings} == {"empty_result"}

