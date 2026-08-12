from __future__ import annotations

import base64
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.config import AppConfig
from tuoming_agent.workspace.service import create_services


def test_streamlit_workspace_renders_without_runtime_errors(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MASKING_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode("ascii"))
    monkeypatch.setenv("TUOMING_DATA_DIR", str(tmp_path / "ui-data"))
    monkeypatch.setenv("TUOMING_DEFAULT_TENANT", "ui-test-tenant")
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.run(timeout=30)
    assert not app.exception
    assert app.title[0].value == "默认工作区"
    assert [metric.label for metric in app.metric] == [
        "数据集",
        "上传文件",
        "数据制品",
        "近期消息",
    ]


def test_results_page_uses_bounded_preview_without_eager_full_load(monkeypatch, tmp_path: Path):
    encoded_key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    data_dir = tmp_path / "bounded-results-ui"
    monkeypatch.setenv("MASKING_MASTER_KEY", encoded_key)
    monkeypatch.setenv("TUOMING_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TUOMING_DEFAULT_TENANT", "ui-results-tenant")
    config = AppConfig(
        master_key=b"k" * 32,
        key_version=1,
        data_dir=data_dir,
        default_tenant="ui-results-tenant",
    )
    services = create_services(config)
    workspace = services.repository.create_workspace("ui-results-tenant", "result-workspace")
    services.artifacts.save_result(
        "ui-results-tenant",
        workspace.id,
        "bounded-result",
        pd.DataFrame({"value": range(1_001)}),
        {},
        (),
    )
    monkeypatch.setattr(
        "tuoming_agent.workspace.service.ArtifactService.load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("result page must not eagerly load the full artifact")
        ),
    )

    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.query_params["view"] = "结果"
    app.run(timeout=30)

    assert not app.exception
    assert len(app.dataframe) >= 1


def test_streamlit_restores_pending_plan_and_requires_confirmation(monkeypatch, tmp_path: Path):
    encoded_key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    data_dir = tmp_path / "workflow-ui-data"
    monkeypatch.setenv("MASKING_MASTER_KEY", encoded_key)
    monkeypatch.setenv("TUOMING_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TUOMING_DEFAULT_TENANT", "ui-workflow-tenant")
    monkeypatch.setenv("ANALYST_API_KEY", "test-key")

    config = AppConfig(
        master_key=b"k" * 32,
        key_version=1,
        data_dir=data_dir,
        default_tenant="ui-workflow-tenant",
        analyst_api_key="test-key",
    )
    services = create_services(config)
    workspace = services.repository.create_workspace("ui-workflow-tenant", "闭环测试")
    source = services.artifacts.save_result(
        "ui-workflow-tenant",
        workspace.id,
        "source",
        pd.DataFrame({"sales": [10, 20]}),
        {},
        (),
    )
    conversation = services.repository.create_conversation(
        "ui-workflow-tenant", workspace.id
    )
    run = services.repository.create_analysis_run(
        "ui-workflow-tenant",
        workspace.id,
        conversation["id"],
        source.id,
        "summarize",
        {},
        3,
    )
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        result_name="销售预览",
        operations=[{"action": "head", "rows": 1}],
    )
    services.repository.create_analysis_plan_version(
        "ui-workflow-tenant", run["id"], plan.model_dump(mode="json"), "initial"
    )
    services.repository.update_analysis_run(
        "ui-workflow-tenant",
        run["id"],
        expected_status="planning",
        status="awaiting_confirmation",
    )

    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.query_params["view"] = "分析"
    app.run(timeout=30)

    assert not app.exception
    assert "确认并执行" in [button.label for button in app.button]
    assert "拒绝计划" in [button.label for button in app.button]
    assert any("等待确认" in item.value for item in app.subheader)
    assert any("结果名称：销售预览" in item.value for item in app.markdown)
