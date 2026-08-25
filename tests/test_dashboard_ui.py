from __future__ import annotations

import base64
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from tuoming_agent.config import AppConfig
from tuoming_agent.dashboard.service import KPIValue
from tuoming_agent.models import ArtifactRecord
from tuoming_agent.ui.app import VIEW_OPTIONS
from tuoming_agent.ui.dashboard import format_kpi_value, preferred_dashboard_artifact
from tuoming_agent.workspace.service import create_services


def _artifact(artifact_id: str, kind: str, created_at: str) -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact_id,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        kind=kind,
        name=artifact_id,
        path=Path(f"{artifact_id}.parquet"),
        row_count=10,
        schema={"columns": []},
        created_at=created_at,
    )


def test_navigation_includes_dashboard() -> None:
    assert "仪表盘" in VIEW_OPTIONS


def test_dashboard_prefers_newest_analysis_result() -> None:
    artifacts = [
        _artifact("new-dataset", "dataset", "2026-08-25T12:00:00+00:00"),
        _artifact("old-analysis", "analysis_result", "2026-08-24T12:00:00+00:00"),
        _artifact("new-analysis", "analysis_result", "2026-08-25T10:00:00+00:00"),
    ]

    assert preferred_dashboard_artifact(artifacts).id == "new-analysis"


def test_dashboard_falls_back_to_newest_dataset() -> None:
    artifacts = [
        _artifact("old", "dataset", "2026-08-24T12:00:00+00:00"),
        _artifact("new", "dataset", "2026-08-25T12:00:00+00:00"),
    ]

    assert preferred_dashboard_artifact(artifacts).id == "new"


def test_kpi_values_are_compact_and_human_readable() -> None:
    assert format_kpi_value(KPIValue("收入", "sum", 12_345_678.9)) == "1,234.57 万"
    assert format_kpi_value(KPIValue("酒店数", "count", 1234)) == "1,234"
    assert format_kpi_value(KPIValue("完成率", "mean", None)) == "—"


def test_dashboard_page_renders_local_plotly_charts(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "dashboard-ui"
    tenant_id = "dashboard-ui-tenant"
    monkeypatch.setenv(
        "MASKING_MASTER_KEY",
        base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
    )
    monkeypatch.setenv("TUOMING_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TUOMING_DEFAULT_TENANT", tenant_id)
    services = create_services(
        AppConfig(
            master_key=b"k" * 32,
            key_version=1,
            data_dir=data_dir,
            default_tenant=tenant_id,
        )
    )
    workspace = services.repository.create_workspace(tenant_id, "经营看板")
    services.artifacts.save_result(
        tenant_id,
        workspace.id,
        "月度营收",
        pd.DataFrame(
            {
                "月份": pd.to_datetime(["2026-06-01", "2026-07-01", "2026-08-01"]),
                "事业部": ["华东", "华南", "华东"],
                "营业收入": [120_000.0, 135_000.0, 142_000.0],
            }
        ),
        {},
        (),
    )

    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.query_params["view"] = "仪表盘"
    app.run(timeout=30)

    assert not app.exception
    assert len(app.get("plotly_chart")) == 2
    assert any("经营仪表盘" in item.value for item in app.markdown)
    assert len(app.dataframe) == 1
