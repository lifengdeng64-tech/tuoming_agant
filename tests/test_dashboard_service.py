from __future__ import annotations

import math

import pandas as pd
import pytest

from tuoming_agent.dashboard.service import DashboardQueryError
from tuoming_agent.models import ArtifactRecord, utc_now

TENANT = "tenant-a"


def _store_artifact(services, workspace, frame: pd.DataFrame, artifact_id: str) -> ArtifactRecord:
    path = services.artifacts.artifact_store.write_dataframe(
        TENANT, workspace.id, artifact_id, frame
    )
    artifact = ArtifactRecord(
        id=artifact_id,
        tenant_id=TENANT,
        workspace_id=workspace.id,
        kind="analysis_result",
        name="经营数据",
        path=path,
        row_count=len(frame),
        schema={
            "columns": [
                {"name": str(column), "dtype": str(frame[column].dtype)}
                for column in frame.columns
            ]
        },
        created_at=utc_now(),
    )
    services.repository.create_artifact(artifact)
    return artifact


def test_kpis_stream_all_batches_without_using_full_dataframe_load(
    services, workspace, monkeypatch
) -> None:
    frame = pd.DataFrame(
        {
            "revenue": [1.0] * 70_000 + [2.0, float("nan"), float("inf")],
            "division": ["east"] * 70_003,
        }
    )
    artifact = _store_artifact(services, workspace, frame, "artifact-kpi")
    monkeypatch.setattr(
        services.artifacts,
        "load",
        lambda *_args, **_kwargs: pytest.fail("dashboard must not load the full artifact"),
    )

    values = services.dashboard.kpis(
        TENANT, workspace.id, artifact.id, ("revenue",), "sum"
    )

    assert values[0].column == "revenue"
    assert values[0].value == 70_002.0


@pytest.mark.parametrize(
    ("aggregation", "expected"),
    [("mean", 2.0), ("min", 1.0), ("max", 3.0), ("count", 3)],
)
def test_kpis_apply_supported_aggregations(services, workspace, aggregation, expected) -> None:
    artifact = _store_artifact(
        services,
        workspace,
        pd.DataFrame({"value": [1.0, 2.0, 3.0, math.nan, math.inf]}),
        f"artifact-{aggregation}",
    )

    values = services.dashboard.kpis(
        TENANT, workspace.id, artifact.id, ("value",), aggregation
    )

    assert values[0].value == expected


def test_dashboard_queries_reject_unknown_columns(services, workspace) -> None:
    artifact = _store_artifact(
        services,
        workspace,
        pd.DataFrame({"division": ["east"], "revenue": [10.0]}),
        "artifact-columns",
    )

    with pytest.raises(DashboardQueryError, match="column"):
        services.dashboard.grouped(
            TENANT, workspace.id, artifact.id, "forged", "revenue", "sum"
        )


def test_dashboard_queries_reject_cross_workspace_artifacts(services, workspace) -> None:
    artifact = _store_artifact(
        services,
        workspace,
        pd.DataFrame({"division": ["east"], "revenue": [10.0]}),
        "artifact-workspace",
    )
    other = services.repository.create_workspace(TENANT, "其他工作区")

    with pytest.raises(DashboardQueryError, match="workspace"):
        services.dashboard.kpis(TENANT, other.id, artifact.id, ("revenue",), "sum")


def test_grouped_results_are_sorted_and_bounded_to_200_points(services, workspace) -> None:
    frame = pd.DataFrame(
        {
            "division": [f"division-{index:03d}" for index in range(250)],
            "revenue": list(range(250)),
        }
    )
    artifact = _store_artifact(services, workspace, frame, "artifact-grouped")

    result = services.dashboard.grouped(
        TENANT, workspace.id, artifact.id, "division", "revenue", "sum"
    )

    assert len(result) == 200
    assert list(result.columns) == ["division", "revenue"]
    assert result.iloc[0].to_dict() == {"division": "division-249", "revenue": 249.0}


def test_detail_is_masked_and_bounded_to_100_rows(services, workspace) -> None:
    artifact = _store_artifact(
        services,
        workspace,
        pd.DataFrame({"value": list(range(150))}),
        "artifact-detail",
    )

    detail = services.dashboard.detail(TENANT, workspace.id, artifact.id, limit=999)

    assert len(detail) == 100
