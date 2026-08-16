from __future__ import annotations

import base64
import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.naming import GeneratedNameValidationError
from tuoming_agent.config import AppConfig
from tuoming_agent.security.credentials import WindowsDpapiSecretStore
from tuoming_agent.security.dlp import SensitiveContentError
from tuoming_agent.settings import MASTER_KEY_CREDENTIAL
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


def test_generated_name_error_is_shown_in_chinese(monkeypatch, tmp_path: Path):
    message = "模型未能生成合规的中文字段名称，请重试。"

    class Conversations:
        sanitizer = object()

        @staticmethod
        def add_user_message(*_args):
            return SimpleNamespace(id="message-a", safe_content="汇总营收")

        @staticmethod
        def build_safe_context(*_args, **_kwargs):
            return {}

    class Repository:
        @staticmethod
        def list_messages(*_args, **_kwargs):
            return []

        @staticmethod
        def add_audit_event(*_args, **_kwargs):
            raise AssertionError("中文命名错误不应进入通用异常处理")

    class Workflow:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def latest_for_conversation(*_args):
            return None

        @staticmethod
        def start(*_args, **_kwargs):
            raise GeneratedNameValidationError(message)

    services = SimpleNamespace(
        artifacts=object(),
        conversations=Conversations(),
        repository=Repository(),
    )
    artifact = SimpleNamespace(
        id="artifact-a",
        schema={"columns": [{"name": "revenue"}]},
        row_count=2,
        lineage=(),
    )
    config = AppConfig(
        master_key=b"k" * 32,
        key_version=1,
        data_dir=tmp_path,
        default_tenant="tenant-a",
        analyst_api_key="test-key",
    )
    errors: list[str] = []

    monkeypatch.setattr(ui_app, "SafeAnalysisPlanner", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(ui_app, "AnalysisWorkflowService", Workflow)
    monkeypatch.setattr(ui_app, "_section_heading", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app, "_empty_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "selectbox", lambda *_args, **_kwargs: "artifact-a")
    monkeypatch.setattr(ui_app.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "divider", lambda: None)
    monkeypatch.setattr(ui_app.st, "markdown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "chat_input", lambda *_args, **_kwargs: "汇总营收")
    monkeypatch.setattr(ui_app.st, "chat_message", lambda *_args, **_kwargs: _EmptyContainer())
    monkeypatch.setattr(ui_app.st, "spinner", lambda *_args, **_kwargs: _EmptyContainer())
    monkeypatch.setattr(ui_app.st, "error", errors.append)

    ui_app._render_analysis_view(
        services,
        config,
        "tenant-a",
        "workspace-a",
        "conversation-a",
        [artifact],
    )

    assert errors == [message]


def test_revision_generated_name_error_is_shown_without_rerun(monkeypatch):
    message = "模型未能生成合规的中文字段名称，请重试。"
    plan = AnalysisPlan(
        input_artifact_id="artifact-a",
        result_name="营收分析",
        operations=[{"action": "head", "rows": 5}],
    )
    current_plan = SimpleNamespace(
        version=1,
        plan=plan,
        reason="initial",
        decision="pending",
    )
    snapshot = SimpleNamespace(
        run={
            "id": "run-a",
            "status": "awaiting_confirmation",
            "repair_count": 0,
            "max_repairs": 3,
            "source_artifact_id": "artifact-a",
        },
        plan_versions=(current_plan,),
        attempts=(),
        current_plan=current_plan,
    )

    class Sanitizer:
        @staticmethod
        def sanitize(_tenant_id, text):
            return text

    class Conversations:
        sanitizer = Sanitizer()

        @staticmethod
        def build_safe_context(*_args, **_kwargs):
            return {}

    class Masking:
        @staticmethod
        def restore_display_value(_tenant_id, value):
            return value

    class Workflow:
        @staticmethod
        def revise(*_args, **_kwargs):
            raise GeneratedNameValidationError(message)

    services = SimpleNamespace(conversations=Conversations(), masking=Masking())
    errors: list[str] = []
    action_column = SimpleNamespace(button=lambda *_args, **_kwargs: False)

    monkeypatch.setattr(ui_app.st, "container", lambda *_args, **_kwargs: _EmptyContainer())
    monkeypatch.setattr(ui_app.st, "subheader", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "markdown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "columns", lambda *_args, **_kwargs: (action_column,) * 2)
    monkeypatch.setattr(ui_app.st, "form", lambda *_args, **_kwargs: _EmptyContainer())
    monkeypatch.setattr(ui_app.st, "text_area", lambda *_args, **_kwargs: "请修改字段")
    monkeypatch.setattr(ui_app.st, "form_submit_button", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ui_app.st, "spinner", lambda *_args, **_kwargs: _EmptyContainer())
    monkeypatch.setattr(ui_app.st, "error", errors.append)
    monkeypatch.setattr(
        ui_app.st,
        "rerun",
        lambda: (_ for _ in ()).throw(AssertionError("命名错误后不应重新运行")),
    )
    monkeypatch.setattr(
        ui_app,
        "_set_flash",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("命名错误不应显示成功通知")
        ),
    )

    ui_app._render_workflow_card(
        services,
        Workflow(),
        snapshot,
        "tenant-a",
        "workspace-a",
        "conversation-a",
    )

    assert errors == [message]


def test_revision_sensitive_feedback_is_shown_without_rerun(monkeypatch):
    message = "修改意见包含敏感明文，已在本地阻止。"
    plan = AnalysisPlan(
        input_artifact_id="artifact-a",
        result_name="营收分析",
        operations=[{"action": "head", "rows": 5}],
    )
    current_plan = SimpleNamespace(
        version=1,
        plan=plan,
        reason="initial",
        decision="pending",
    )
    snapshot = SimpleNamespace(
        run={
            "id": "run-a",
            "status": "awaiting_confirmation",
            "repair_count": 0,
            "max_repairs": 3,
            "source_artifact_id": "artifact-a",
        },
        plan_versions=(current_plan,),
        attempts=(),
        current_plan=current_plan,
    )

    class Sanitizer:
        @staticmethod
        def sanitize(*_args, **_kwargs):
            raise SensitiveContentError(message)

    class Conversations:
        sanitizer = Sanitizer()

        @staticmethod
        def build_safe_context(*_args, **_kwargs):
            raise AssertionError("敏感反馈被阻止后不应构建模型上下文")

    class Masking:
        @staticmethod
        def restore_display_value(_tenant_id, value):
            return value

    class Workflow:
        @staticmethod
        def revise(*_args, **_kwargs):
            raise AssertionError("敏感反馈被阻止后不应修改计划")

    services = SimpleNamespace(conversations=Conversations(), masking=Masking())
    errors: list[str] = []
    action_column = SimpleNamespace(button=lambda *_args, **_kwargs: False)

    monkeypatch.setattr(ui_app.st, "container", lambda *_args, **_kwargs: _EmptyContainer())
    monkeypatch.setattr(ui_app.st, "subheader", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "markdown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ui_app.st, "columns", lambda *_args, **_kwargs: (action_column,) * 2)
    monkeypatch.setattr(ui_app.st, "form", lambda *_args, **_kwargs: _EmptyContainer())
    monkeypatch.setattr(ui_app.st, "text_area", lambda *_args, **_kwargs: "ABC    Store")
    monkeypatch.setattr(ui_app.st, "form_submit_button", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ui_app.st, "spinner", lambda *_args, **_kwargs: _EmptyContainer())
    monkeypatch.setattr(ui_app.st, "error", errors.append)
    monkeypatch.setattr(
        ui_app.st,
        "rerun",
        lambda: (_ for _ in ()).throw(AssertionError("敏感反馈后不应重新运行")),
    )
    monkeypatch.setattr(
        ui_app,
        "_set_flash",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("敏感反馈不应显示成功通知")
        ),
    )

    ui_app._render_workflow_card(
        services,
        Workflow(),
        snapshot,
        "tenant-a",
        "workspace-a",
        "conversation-a",
    )

    assert errors == [message]


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



@pytest.mark.skipif(os.name != "nt", reason="desktop onboarding uses Windows DPAPI")
def test_first_run_initializes_local_security_and_shows_model_setup(
    monkeypatch, tmp_path: Path
) -> None:
    app_dir = tmp_path / "desktop-profile"
    monkeypatch.delenv("MASKING_MASTER_KEY", raising=False)
    monkeypatch.delenv("ANALYST_API_KEY", raising=False)
    monkeypatch.setenv("TUOMING_APP_DIR", str(app_dir))
    monkeypatch.setenv("TUOMING_DATA_DIR", str(app_dir / "data"))
    monkeypatch.setenv("TUOMING_DESKTOP", "1")

    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.run(timeout=30)

    assert not app.exception
    assert "模型服务商" in [item.label for item in app.selectbox]
    assert "API Key" in [item.label for item in app.text_input]
    assert "测试连接" in [item.label for item in app.button]
    assert "保存并进入工作台" in [item.label for item in app.button]
    secret_store = WindowsDpapiSecretStore(app_dir / "credentials")
    assert len(secret_store.get(MASTER_KEY_CREDENTIAL) or b"") == 32
    settings_text = (app_dir / "settings.json").read_text(encoding="utf-8")
    assert "masking-master-key" not in settings_text

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
    token = services.vault.tokenize("ui-workflow-tenant", "brand", "华住")
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        result_name="销售预览",
        operations=[
            {"action": "filter", "column": "品牌名称", "operator": "eq", "value": token}
        ],
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
    assert any("'华住'" in item.value for item in app.markdown)

    stored_plan = services.repository.list_analysis_plan_versions(
        "ui-workflow-tenant", run["id"]
    )[0]
    assert stored_plan["plan"]["operations"][0]["value"] == token
