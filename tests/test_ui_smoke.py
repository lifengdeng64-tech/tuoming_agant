from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.config import AppConfig
from tuoming_agent.storage.sqlite import DeletionImpact, TableDeletionImpact
from tuoming_agent.ui import app as ui_app
from tuoming_agent.workspace.data_sources import DataSourceDeletionError
from tuoming_agent.workspace.service import create_services


class _EmptyContainer:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_upload_button_uses_confirm_add_label(monkeypatch):
    class AcceptedUpload(BytesIO):
        name = "accepted.csv"
        size = len(b"value\n1\n")

    labels: list[str] = []
    monkeypatch.setattr(
        ui_app.st,
        "file_uploader",
        lambda *_args, **_kwargs: [AcceptedUpload(b"value\n1\n")],
    )
    monkeypatch.setattr(ui_app.st, "expander", lambda *_args, **_kwargs: _EmptyContainer())
    monkeypatch.setattr(ui_app.st, "data_editor", lambda frame, **_kwargs: frame)
    monkeypatch.setattr(ui_app.st, "dataframe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "markdown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "divider", lambda: None)
    monkeypatch.setattr(
        ui_app.st,
        "columns",
        lambda *_args, **_kwargs: [_EmptyContainer(), _EmptyContainer()],
    )

    def button(label, **_kwargs):
        labels.append(label)
        return False

    monkeypatch.setattr(ui_app.st, "button", button)
    monkeypatch.setattr(ui_app, "_section_heading", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app, "_empty_state", lambda *_args, **_kwargs: None)

    ui_app._render_data_view(object(), "tenant", "workspace", [], [])

    assert "确认添加" in labels
    assert not any(label.startswith("确认并追加") for label in labels)


def test_table_deletion_requires_acknowledgement_for_related_analysis(monkeypatch):
    impact = TableDeletionImpact(
        dataset_version_id="version-a",
        dataset_id="dataset-a",
        file_id="file-a",
        logical_name="book::page",
        version=1,
        row_count=643,
        artifact_ids=("source-a", "result-a"),
        analysis_run_ids=("run-a",),
        message_ids=("request-a", "response-a"),
        paths=(Path("source.parquet"), Path("result.parquet")),
    )

    class DataSources:
        deleted = False

        def inspect_table(self, *_args):
            return impact

        def delete_table(self, *_args):
            self.deleted = True

    class Services:
        data_sources = DataSources()

    warnings: list[str] = []
    disabled: list[bool] = []
    state = {"pending-table-delete-workspace": "version-a"}
    monkeypatch.setattr(ui_app.st, "session_state", state)
    monkeypatch.setattr(ui_app.st, "warning", warnings.append)
    monkeypatch.setattr(ui_app.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "checkbox", lambda *_args, **_kwargs: False)

    def button(label, **kwargs):
        if label == "确认删除工作表":
            disabled.append(kwargs["disabled"])
        return False

    monkeypatch.setattr(ui_app.st, "button", button)

    ui_app._render_dataset_version_deletion(
        Services(),
        "tenant",
        "workspace",
        {
            "dataset_version_id": "version-a",
            "logical_name": "book::page",
            "version": 1,
            "row_count": 643,
        },
    )

    assert disabled == [True]
    assert Services.data_sources.deleted is False
    assert any(
        "book::page" in warning
        and "643" in warning
        and "关联对话 2 条" in warning
        for warning in warnings
    )


def test_file_deletion_shows_message_count_and_requires_acknowledgement(monkeypatch):
    impact = DeletionImpact(
        file_id="file-a",
        original_name="source.csv",
        sha256="abc123",
        dataset_version_ids=("version-a",),
        dataset_ids=("dataset-a",),
        artifact_ids=("source-a",),
        analysis_run_ids=(),
        message_ids=("request-a",),
        paths=(Path("source.enc"), Path("source.parquet")),
    )

    class DataSources:
        deleted = False

        def inspect(self, *_args):
            return impact

        def delete(self, *_args):
            self.deleted = True

    class Services:
        data_sources = DataSources()

    warnings: list[str] = []
    disabled: list[bool] = []
    state = {"pending-delete-workspace": "file-a"}
    monkeypatch.setattr(ui_app.st, "session_state", state)
    monkeypatch.setattr(ui_app.st, "warning", warnings.append)
    monkeypatch.setattr(ui_app.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "checkbox", lambda *_args, **_kwargs: False)

    def button(label, **kwargs):
        if label == "确认删除":
            disabled.append(kwargs["disabled"])
        return False

    monkeypatch.setattr(ui_app.st, "button", button)

    ui_app._render_file_deletion(
        Services(),
        "tenant",
        "workspace",
        {"id": "file-a", "original_name": "source.csv"},
    )

    assert disabled == [True]
    assert Services.data_sources.deleted is False
    assert any("关联对话 1 条" in warning for warning in warnings)


def test_confirmed_table_deletion_calls_service_and_refreshes(monkeypatch):
    impact = TableDeletionImpact(
        dataset_version_id="version-a",
        dataset_id="dataset-a",
        file_id="file-a",
        logical_name="book::page",
        version=1,
        row_count=643,
        artifact_ids=("source-a",),
        analysis_run_ids=(),
        paths=(Path("source.parquet"),),
    )
    deleted: list[tuple[str, str, str]] = []

    class DataSources:
        def inspect_table(self, *_args):
            return impact

        def delete_table(self, *args):
            deleted.append(args)
            return impact

    class Services:
        data_sources = DataSources()

    state = {"pending-table-delete-workspace": "version-a"}
    monkeypatch.setattr(ui_app.st, "session_state", state)
    monkeypatch.setattr(ui_app.st, "warning", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "checkbox", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        ui_app.st,
        "button",
        lambda label, **_kwargs: label == "确认删除工作表",
    )
    monkeypatch.setattr(ui_app.st, "rerun", lambda: None)
    monkeypatch.setattr(ui_app, "_set_flash", lambda *_args, **_kwargs: None)

    ui_app._render_dataset_version_deletion(
        Services(),
        "tenant",
        "workspace",
        {
            "dataset_version_id": "version-a",
            "logical_name": "book::page",
            "version": 1,
            "row_count": 643,
        },
    )

    assert deleted == [("tenant", "workspace", "version-a")]
    assert "pending-table-delete-workspace" not in state


def test_table_deletion_shows_domain_failure_without_crashing(monkeypatch):
    impact = TableDeletionImpact(
        dataset_version_id="version-a",
        dataset_id="dataset-a",
        file_id="file-a",
        logical_name="book::page",
        version=1,
        row_count=10,
        artifact_ids=("source-a",),
        analysis_run_ids=(),
        paths=(Path("source.parquet"),),
    )

    class DataSources:
        def inspect_table(self, *_args):
            return impact

        def delete_table(self, *_args):
            raise DataSourceDeletionError("dependencies changed")

    class Services:
        data_sources = DataSources()

    warnings: list[str] = []
    state = {"pending-table-delete-workspace": "version-a"}
    monkeypatch.setattr(ui_app.st, "session_state", state)
    monkeypatch.setattr(ui_app.st, "warning", warnings.append)
    monkeypatch.setattr(ui_app.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        ui_app.st,
        "button",
        lambda label, **_kwargs: label == "确认删除工作表",
    )

    ui_app._render_dataset_version_deletion(
        Services(),
        "tenant",
        "workspace",
        {"dataset_version_id": "version-a"},
    )

    assert warnings[-1] == "删除失败，原数据已保留：dependencies changed"
    assert state["pending-table-delete-workspace"] == "version-a"


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
