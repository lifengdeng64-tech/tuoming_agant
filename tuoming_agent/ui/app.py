from __future__ import annotations

import hashlib
import html
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from tuoming_agent import __version__
from tuoming_agent.analysis.errors import AnalysisServiceError
from tuoming_agent.analysis.naming import GeneratedNameValidationError
from tuoming_agent.analysis.planner import SafeAnalysisPlanner
from tuoming_agent.analysis.presentation import describe_plan
from tuoming_agent.analysis.workflow import AnalysisWorkflowService, WorkflowSnapshot
from tuoming_agent.backup import BackupError, BackupManager
from tuoming_agent.config import AppConfig, ConfigurationError
from tuoming_agent.desktop.updater import UpdateError, UpdateManager
from tuoming_agent.exporting import ExportLimitError
from tuoming_agent.ingestion.limits import validate_upload_size
from tuoming_agent.ingestion.parser import preview_file
from tuoming_agent.ingestion.scanner import detect_sensitive_columns
from tuoming_agent.maintenance import DiskHeadroomError
from tuoming_agent.providers import create_provider
from tuoming_agent.security.dlp import SensitiveContentError
from tuoming_agent.security.masking import ColumnPolicy
from tuoming_agent.settings import (
    PROVIDER_BY_ID,
    PROVIDERS,
    LocalSettingsManager,
    ModelSettings,
    NetworkSettings,
    default_app_dir,
)
from tuoming_agent.storage.errors import AuthorizationError, RecordNotFoundError
from tuoming_agent.ui.styles import APP_STYLES
from tuoming_agent.workspace.data_sources import DataSourceDeletionError
from tuoming_agent.workspace.service import ApplicationServices, create_services

VIEW_OPTIONS = ("概览", "数据", "分析", "结果", "设置")
NORMALIZERS = ("text", "casefold", "phone", "identifier")


def run() -> None:
    st.set_page_config(
        page_title="Tuoming Agent",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(APP_STYLES, unsafe_allow_html=True)
    try:
        config = AppConfig.from_runtime()
    except ConfigurationError as exc:
        _render_configuration_error(str(exc))
        st.stop()

    settings_manager = LocalSettingsManager(config.app_dir or default_app_dir())
    if config.managed_runtime and not config.analyst_api_key:
        _render_first_run(settings_manager, config)
        return

    services = _load_services(config)
    tenant_id = config.default_tenant
    services.repository.ensure_tenant(tenant_id)
    workspace_id = _workspace_sidebar(services, config, tenant_id)
    workspace = services.repository.get_workspace(tenant_id, workspace_id)
    conversation = services.repository.get_or_create_conversation(tenant_id, workspace_id)
    artifacts = services.repository.list_artifacts(tenant_id, workspace_id)
    datasets = services.repository.list_datasets(tenant_id, workspace_id)
    files = services.repository.list_files(tenant_id, workspace_id)
    messages = services.repository.list_messages(tenant_id, conversation["id"], 100)

    _render_flash()
    _render_header(workspace.name, workspace.id, bool(config.analyst_api_key))
    _render_metrics(
        dataset_count=len(datasets),
        file_count=len(files),
        artifact_count=len(artifacts),
        message_count=len(messages),
    )
    selected_view = _view_navigation()

    if selected_view == "概览":
        _render_overview(services, tenant_id, workspace_id, datasets, artifacts)
    elif selected_view == "数据":
        _render_data_view(services, tenant_id, workspace_id, datasets, files)
    elif selected_view == "分析":
        _render_analysis_view(
            services,
            config,
            tenant_id,
            workspace_id,
            conversation["id"],
            artifacts,
        )
    elif selected_view == "结果":
        _render_results_view(services, tenant_id, workspace_id, artifacts)
    else:
        _render_settings(settings_manager, config, services, tenant_id, workspace_id)


@st.cache_resource(show_spinner=False)
def _load_services(config: AppConfig) -> ApplicationServices:
    return create_services(config)


def _render_configuration_error(message: str) -> None:
    st.title("Tuoming Agent 无法启动")
    st.error(message)
    st.caption("程序没有覆盖已有密钥或历史数据。请先备份本地数据，再根据提示恢复。")


def _workspace_sidebar(services: ApplicationServices, config: AppConfig, tenant_id: str) -> str:
    st.sidebar.markdown(
        """
        <div class="brand-lockup">
            <div class="brand-mark">T</div>
            <div><strong>透明数据</strong><span>安全分析工作台</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.caption("工作区")
    workspaces = services.repository.list_workspaces(tenant_id)
    if not workspaces:
        workspaces = [services.repository.create_workspace(tenant_id, "默认工作区")]

    requested = st.query_params.get("workspace")
    workspace_ids = [workspace.id for workspace in workspaces]
    selected_index = workspace_ids.index(requested) if requested in workspace_ids else 0
    labels = {workspace.id: workspace.name for workspace in workspaces}
    selected = st.sidebar.selectbox(
        "当前工作区",
        workspace_ids,
        index=selected_index,
        format_func=lambda value: labels[value],
        label_visibility="collapsed",
    )
    if requested != selected:
        st.query_params["workspace"] = selected

    with st.sidebar.expander("新建工作区"):
        name = st.text_input("名称", key="new_workspace_name", placeholder="例如：8 月经营分析")
        if (
            st.button(
                "新建工作区",
                type="primary",
                icon=":material/add:",
                use_container_width=True,
            )
            and name.strip()
        ):
            workspace = services.repository.create_workspace(tenant_id, name.strip())
            st.query_params["workspace"] = workspace.id
            _set_flash("success", f"工作区“{name.strip()}”已创建。")
            st.rerun()

    st.sidebar.divider()
    provider = PROVIDER_BY_ID.get(config.analyst_provider, PROVIDER_BY_ID["openai_compatible"])
    model_state = provider.label if config.analyst_api_key else "未配置"
    safe_tenant_id = html.escape(tenant_id)
    st.sidebar.markdown(
        f"""
        <div class="sidebar-status">
            <span>安全租户</span><strong>{safe_tenant_id}</strong>
            <span>密钥版本</span><strong>V{config.key_version}</strong>
            <span>分析模型</span><strong>{model_state}</strong>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if (
        config.managed_runtime
        and os.getenv("TUOMING_DESKTOP") == "1"
        and st.sidebar.button(
            "退出 Tuoming Agent",
            icon=":material/power_settings_new:",
            use_container_width=True,
        )
    ):
        if config.app_dir:
            (config.app_dir / "shutdown.request").write_text("quit", encoding="utf-8")
        st.sidebar.info("正在安全关闭本地服务…")
        st.stop()
    st.sidebar.caption("v0.3 · Local-first desktop")
    return selected


def _render_header(workspace_name: str, workspace_id: str, model_ready: bool) -> None:
    title_col, state_col = st.columns([4, 1.3], vertical_alignment="center")
    with title_col:
        st.caption("CURRENT WORKSPACE")
        st.title(workspace_name)
        st.caption(f"ID {workspace_id}")
    with state_col:
        model_label = "分析模型已连接" if model_ready else "仅本地处理"
        st.markdown(
            f"""
            <div class="security-state">
                <span><i></i> 本地安全边界已启用</span>
                <small>{model_label}</small>
            </div>
            """,
            unsafe_allow_html=True,
        )


def _render_metrics(
    dataset_count: int, file_count: int, artifact_count: int, message_count: int
) -> None:
    columns = st.columns(4)
    columns[0].metric("数据集", dataset_count, border=True)
    columns[1].metric("上传文件", file_count, border=True)
    columns[2].metric("数据制品", artifact_count, border=True)
    columns[3].metric("近期消息", message_count, border=True)


def _view_navigation() -> str:
    requested = st.query_params.get("view", "概览")
    default = requested if requested in VIEW_OPTIONS else "概览"
    selected = (
        st.segmented_control(
            "工作区视图",
            VIEW_OPTIONS,
            default=default,
            label_visibility="collapsed",
            width="stretch",
        )
        or "概览"
    )
    if requested != selected:
        st.query_params["view"] = selected
    return selected


def _render_first_run(settings_manager: LocalSettingsManager, config: AppConfig) -> None:
    st.markdown(
        """
        <div class="onboarding-hero">
            <div class="brand-mark onboarding-mark">T</div>
            <div>
                <span class="eyebrow">WELCOME TO TUOMING AGENT</span>
                <h1>连接你的分析模型</h1>
                <p>本地安全空间已经创建。完成一次模型配置后，就可以进入工作台。</p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    trust_columns = st.columns(3)
    trust_columns[0].markdown("**原始数据留在本机**\n\n模型只接收脱敏后的请求与元数据。")
    trust_columns[1].markdown("**密钥由 Windows 保护**\n\nAPI Key 与脱敏主密钥使用 DPAPI 保存。")
    trust_columns[2].markdown("**以后直接进入工作台**\n\n设置保存后，重启无需重复填写。")
    st.divider()
    _render_model_settings_form(settings_manager, config, prefix="onboarding", first_run=True)


def _render_settings(
    settings_manager: LocalSettingsManager,
    config: AppConfig,
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
) -> None:
    model_tab, network_tab, resilience_tab, update_tab = st.tabs(
        ["模型", "企业网络", "备份与恢复", "更新与审计"]
    )
    with model_tab:
        _render_model_settings(settings_manager, config)
    with network_tab:
        _render_network_settings(settings_manager, config)
    with resilience_tab:
        _render_backup_settings(settings_manager, config)
    with update_tab:
        _render_update_settings(settings_manager, config, services, tenant_id, workspace_id)


def _render_network_settings(settings_manager: LocalSettingsManager, config: AppConfig) -> None:
    _section_heading("企业网络", "代理与 TLS")
    st.caption(
        "始终验证 TLS；Tuoming 不提供关闭证书校验的选项。代理凭据请配置在 Windows 系统代理中。"
    )
    saved = settings_manager.load_network_settings()
    use_system_proxy = st.checkbox(
        "使用 Windows/环境系统代理",
        value=saved.use_system_proxy,
        key="network-use-system-proxy",
    )
    proxy_url = st.text_input(
        "显式代理 URL（可选）",
        value=saved.proxy_url,
        placeholder="http://proxy.company.local:8080",
        help="不得包含用户名或密码。",
        key="network-proxy-url",
    )
    ca_bundle_path = st.text_input(
        "企业 CA 文件路径（可选）",
        value=saved.ca_bundle_path,
        placeholder=r"C:\Company\certs\enterprise-ca.pem",
        key="network-ca-path",
    )
    if st.button("保存网络设置", type="primary", key="network-save"):
        try:
            settings_manager.save_network_settings(
                NetworkSettings(use_system_proxy, proxy_url, ca_bundle_path)
            )
            _set_flash("success", "企业网络设置已保存，重启 Tuoming 后对全部 Provider 生效。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def _render_backup_settings(settings_manager: LocalSettingsManager, config: AppConfig) -> None:
    _section_heading("备份与恢复", "可迁移加密备份")
    st.caption(
        "备份包含工作区、脱敏制品和迁移所需凭据；使用独立密码加密。密码不会保存，丢失后无法恢复。"
    )
    backup_password = st.text_input(
        "新备份密码",
        type="password",
        key="backup-password",
        help="至少 10 个字符。",
    )
    if st.button("创建加密备份", key="create-backup"):
        try:
            backup_dir = (config.app_dir or default_app_dir()) / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("TuomingAgent-%Y%m%d-%H%M%S.tmbak")
            path = BackupManager(
                config.app_dir or default_app_dir(), config.data_dir, settings_manager
            ).create_backup(backup_dir / name, backup_password)
            st.session_state["latest-backup"] = str(path)
            st.success(f"备份已创建：{path}")
        except BackupError as exc:
            st.error(str(exc))
    latest_backup = st.session_state.get("latest-backup")
    if latest_backup and Path(latest_backup).is_file():
        backup_path = Path(latest_backup)
        if backup_path.stat().st_size <= 250 * 1024 * 1024:
            st.download_button(
                "下载刚创建的备份",
                data=backup_path.read_bytes(),
                file_name=backup_path.name,
                mime="application/octet-stream",
                key="download-backup",
            )
        else:
            st.info("备份超过 250MiB，请直接从上方本地路径复制，避免浏览器占用过多内存。")

    st.divider()
    restore_upload = st.file_uploader(
        "选择 .tmbak 备份",
        type=["tmbak"],
        key="restore-upload",
    )
    restore_password = st.text_input("备份密码", type="password", key="restore-password")
    if st.button("验证并安排恢复", key="stage-restore", disabled=restore_upload is None):
        try:
            import_dir = (config.app_dir or default_app_dir()) / "restore-imports"
            import_dir.mkdir(parents=True, exist_ok=True)
            source = import_dir / f"{uuid.uuid4().hex}.tmbak"
            source.write_bytes(restore_upload.getvalue())
            try:
                BackupManager(
                    config.app_dir or default_app_dir(), config.data_dir, settings_manager
                ).stage_restore(source, restore_password)
            finally:
                source.unlink(missing_ok=True)
            st.success("备份已验证。请从系统托盘退出并重新启动 Tuoming，恢复将在启动前完成。")
        except BackupError as exc:
            st.error(str(exc))


def _render_update_settings(
    settings_manager: LocalSettingsManager,
    config: AppConfig,
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
) -> None:
    _section_heading("更新与审计", f"当前版本 {__version__}")
    st.caption("仅从官方 GitHub Releases 检查更新；安装前验证 SHA-256，并可绑定发布证书指纹。")
    if st.button("检查更新", key="check-update"):
        manager = UpdateManager(config.app_dir or default_app_dir(), config.network_settings)
        try:
            update = manager.check()
            st.session_state["available-update"] = update
            if update.is_newer:
                st.success(f"发现新版本 {update.version}。")
            else:
                st.info("当前已经是最新版。")
        except UpdateError as exc:
            st.error(str(exc))
        finally:
            manager.close()
    update = st.session_state.get("available-update")
    if update and update.is_newer and st.button("下载并验证更新", key="download-update"):
        manager = UpdateManager(config.app_dir or default_app_dir(), config.network_settings)
        try:
            downloaded = manager.download(update)
            st.session_state["downloaded-update"] = str(downloaded.path)
            st.success(f"更新已验证并下载：{downloaded.path.name}。点击下方按钮开始安装。")
        except UpdateError as exc:
            st.error(str(exc))
        finally:
            manager.close()
    downloaded_path = st.session_state.get("downloaded-update")
    if (
        downloaded_path
        and Path(downloaded_path).is_file()
        and st.button("退出并安装更新", type="primary", key="install-update")
    ):
        manager = UpdateManager(config.app_dir or default_app_dir(), config.network_settings)
        try:
            manager.launch_installer(Path(downloaded_path))
            ((config.app_dir or default_app_dir()) / "shutdown.request").write_text(
                "update", encoding="utf-8"
            )
            st.info("安装器已启动，Tuoming 正在退出。")
        except UpdateError as exc:
            st.error(str(exc))
        finally:
            manager.close()

    rollback_manager = UpdateManager(config.app_dir or default_app_dir(), config.network_settings)
    try:
        rollback_candidates = rollback_manager.rollback_candidates()
    finally:
        rollback_manager.close()
    if rollback_candidates:
        st.divider()
        rollback_by_label = {
            f"版本 {item['version']} · 签名 {item['signature_status']}": item["path"]
            for item in rollback_candidates
        }
        rollback_label = st.selectbox(
            "已验证安装器（可用于回滚）",
            options=list(rollback_by_label),
            key="rollback-installer",
        )
        if st.button("退出并安装所选版本", key="install-rollback"):
            manager = UpdateManager(config.app_dir or default_app_dir(), config.network_settings)
            try:
                manager.launch_installer(Path(rollback_by_label[rollback_label]))
                ((config.app_dir or default_app_dir()) / "shutdown.request").write_text(
                    "rollback", encoding="utf-8"
                )
                st.info("回滚安装器已启动，Tuoming 正在退出。")
            except UpdateError as exc:
                st.error(str(exc))
            finally:
                manager.close()

    st.caption(
        "当前版本按 Windows 用户隔离数据与 DPAPI 凭据；企业身份联合与集中 SIEM 推送需在部署时接入。"
    )
    events = services.repository.list_audit_events(tenant_id, workspace_id, 5000)
    audit_lines = "\n".join(
        json.dumps(
            {
                "event_id": event["id"],
                "event_type": event["event_type"],
                "created_at": event["created_at"],
                "details": event["details"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for event in reversed(events)
    )
    st.download_button(
        "导出当前工作区安全审计 JSONL",
        data=audit_lines.encode("utf-8"),
        file_name=f"tuoming-audit-{workspace_id}.jsonl",
        mime="application/x-ndjson",
        key="download-audit",
        help="审计导出不包含原始行、密钥或解密后的映射值。",
    )


def _render_model_settings(settings_manager: LocalSettingsManager, config: AppConfig) -> None:
    _section_heading("模型设置", "本机安全凭据")
    st.caption(
        "API Key \u4ec5\u4fdd\u5b58\u5728\u5f53\u524d Windows "
        "\u7528\u6237\u7684 DPAPI \u51ed\u636e\u4e2d\uff0c"
        "\u4e0d\u5199\u5165 settings.json\u3001SQLite "
        "\u6216\u9879\u76ee\u6e90\u7801\u3002"
    )
    if not config.managed_runtime:
        st.info("当前由开发环境变量覆盖运行配置；桌面版保存的设置将在移除环境变量后生效。")
    _render_model_settings_form(settings_manager, config, prefix="settings", first_run=False)


def _render_model_settings_form(
    settings_manager: LocalSettingsManager,
    config: AppConfig,
    *,
    prefix: str,
    first_run: bool,
) -> None:
    saved = (
        settings_manager.load_model_settings() if config.managed_runtime else config.model_settings
    )
    provider_ids = [provider.id for provider in PROVIDERS]
    initial_provider = saved.provider if saved.provider in provider_ids else "deepseek"
    provider_id = st.selectbox(
        "模型服务商",
        provider_ids,
        index=provider_ids.index(initial_provider),
        format_func=lambda item: PROVIDER_BY_ID[item].label,
        key=f"{prefix}-provider",
    )
    definition = PROVIDER_BY_ID[provider_id]
    custom_model_label = "自定义模型名称…"
    model_options = [*definition.models, custom_model_label]
    configured_model = saved.model_name if saved.provider == provider_id else ""
    initial_model = (
        configured_model if configured_model in definition.models else custom_model_label
    )
    model_choice = st.selectbox(
        "模型",
        model_options,
        index=model_options.index(initial_model),
        key=f"{prefix}-model-choice-{provider_id}",
    )
    if model_choice == custom_model_label:
        model_name = st.text_input(
            "自定义模型名称",
            value=configured_model if configured_model not in definition.models else "",
            placeholder="例如：my-company-model",
            key=f"{prefix}-custom-model-{provider_id}",
        ).strip()
    else:
        model_name = model_choice

    custom_endpoint = provider_id == "openai_compatible"
    configured_url = saved.base_url if saved.provider == provider_id else definition.base_url
    base_url = st.text_input(
        "Base URL",
        value=configured_url or definition.base_url,
        disabled=not custom_endpoint,
        placeholder="https://api.example.com/v1",
        help="常见服务商使用官方地址；自定义 OpenAI Compatible API 可以编辑。",
        key=f"{prefix}-base-url-{provider_id}",
    ).strip()
    saved_provider_has_key = saved.provider == provider_id and bool(config.analyst_api_key)
    api_key = st.text_input(
        "API Key",
        type="password",
        value="",
        placeholder=(
            "已安全保存，留空则保持不变" if saved_provider_has_key else "请输入自己的 API Key"
        ),
        help="仅发送给所选模型服务商，Tuoming 不会把它上传到自有服务器。",
        key=f"{prefix}-api-key-{provider_id}",
    )
    candidate = ModelSettings(provider=provider_id, base_url=base_url, model_name=model_name)

    result = st.session_state.get(f"{prefix}-connection-result")
    if result:
        renderer = st.success if result["ok"] else st.error
        renderer(("✓ " if result["ok"] else "") + result["message"])

    test_col, save_col = st.columns([1, 1])
    if test_col.button(
        "测试连接",
        icon=":material/cable:",
        use_container_width=True,
        key=f"{prefix}-test",
    ):
        stored_key = settings_manager.get_api_key() if saved_provider_has_key else None
        candidate_key = api_key.strip() or stored_key
        if not candidate_key:
            st.session_state[f"{prefix}-connection-result"] = {
                "ok": False,
                "message": "请先填写 API Key。",
            }
        else:
            try:
                with st.spinner("正在验证模型连接"):
                    provider = create_provider(candidate, candidate_key, config.network_settings)
                    connection = provider.test_connection()
                st.session_state[f"{prefix}-connection-result"] = {
                    "ok": connection.ok,
                    "message": connection.message,
                }
            except Exception as exc:
                st.session_state[f"{prefix}-connection-result"] = {
                    "ok": False,
                    "message": str(exc),
                }
        st.rerun()

    save_label = "保存并进入工作台" if first_run else "保存设置"
    if save_col.button(
        save_label,
        type="primary",
        icon=":material/check:",
        use_container_width=True,
        key=f"{prefix}-save",
    ):
        try:
            existing_key = settings_manager.get_api_key() if saved_provider_has_key else None
            if not api_key.strip() and not existing_key:
                raise ValueError("请填写 API Key 后再保存。")
            settings_manager.save_model_settings(candidate)
            if api_key.strip():
                settings_manager.save_api_key(api_key)
            st.session_state.pop(f"{prefix}-connection-result", None)
            _set_flash("success", "模型设置已安全保存。")
            st.query_params["view"] = "概览"
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))


def _render_overview(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    datasets: list[dict[str, Any]],
    artifacts: list[Any],
) -> None:
    st.subheader("工作区概览")
    catalog_col, activity_col = st.columns([1.2, 1], gap="large")
    with catalog_col:
        _section_heading("数据目录", "最近更新")
        if datasets:
            table = pd.DataFrame(
                [
                    {
                        "数据集": item["logical_name"],
                        "版本": f"V{item['version']}",
                        "更新时间": _format_timestamp(item["version_created_at"]),
                    }
                    for item in datasets[:8]
                ]
            )
            st.dataframe(table, use_container_width=True, hide_index=True, height=302)
        else:
            _empty_state("暂无数据集", "数据")

    with activity_col:
        _section_heading("安全活动", "最近 8 条")
        events = services.repository.list_audit_events(tenant_id, workspace_id, 8)
        if events:
            activity = pd.DataFrame(
                [
                    {
                        "事件": _event_label(event["event_type"]),
                        "详情": _event_detail(event),
                        "时间": _format_timestamp(event["created_at"]),
                    }
                    for event in events
                ]
            )
            st.dataframe(activity, use_container_width=True, hide_index=True, height=302)
        else:
            _empty_state("暂无活动记录", "盾")

    st.divider()
    _section_heading("最近制品", f"共 {len(artifacts)} 个")
    if artifacts:
        artifact_table = pd.DataFrame(
            [
                {
                    "名称": artifact.name,
                    "类型": _artifact_kind(artifact.kind),
                    "行数": artifact.row_count,
                    "字段": len(artifact.schema.get("columns", [])),
                    "脱敏字段": len(artifact.lineage),
                    "生成时间": _format_timestamp(artifact.created_at),
                }
                for artifact in artifacts[:10]
            ]
        )
        st.dataframe(artifact_table, use_container_width=True, hide_index=True)
    else:
        _empty_state("暂无数据制品", "表")


def _render_data_view(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    datasets: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> None:
    _section_heading("追加数据", "CSV · XLSX · XLSM")
    uploaded_files = st.file_uploader(
        "上传业务文件",
        type=["csv", "xlsx", "xlsm"],
        accept_multiple_files=True,
        key=f"uploader-{workspace_id}",
        label_visibility="collapsed",
    )
    prepared: list[
        tuple[
            str,
            Any,
            dict[str, dict[str, ColumnPolicy]],
            dict[str, set[str]],
        ]
    ] = []

    for uploaded in uploaded_files or []:
        try:
            validate_upload_size(uploaded.name, uploaded.size)
            tables = preview_file(uploaded.name, uploaded)
        except ValueError as exc:
            st.error(f"{uploaded.name}: {exc}")
            continue
        file_key = hashlib.sha256(f"{uploaded.name}|{uploaded.size}".encode()).hexdigest()[:12]
        file_policies: dict[str, dict[str, ColumnPolicy]] = {}
        file_retained: dict[str, set[str]] = {}
        with st.expander(
            f"{uploaded.name} · {_format_bytes(uploaded.size)} · {len(tables)} 张表",
            expanded=True,
            icon=":material/draft:",
        ):
            for table_index, table in enumerate(tables):
                if table_index:
                    st.divider()
                detected = detect_sensitive_columns(table.dataframe)
                st.markdown(f"**{table.logical_name}**")
                st.caption(
                    f"{len(table.dataframe):,} 行 · {len(table.dataframe.columns)} 列 · "
                    f"检测到 {len(detected)} 个疑似敏感字段。"
                    "取消勾选表示明确保留原值，提交时将记入审计日志。"
                )
                policy_frame = _policy_frame(table.dataframe, detected)
                edited = st.data_editor(
                    policy_frame,
                    key=f"policy-{workspace_id}-{file_key}-{table.logical_name}",
                    use_container_width=True,
                    hide_index=True,
                    disabled=["字段", "检测结果"],
                    num_rows="fixed",
                    column_config={
                        "脱敏": st.column_config.CheckboxColumn("脱敏", width="small"),
                        "字段": st.column_config.TextColumn("字段", width="medium"),
                        "语义域": st.column_config.TextColumn("语义域", width="medium"),
                        "标准化": st.column_config.SelectboxColumn(
                            "标准化", options=list(NORMALIZERS), width="medium"
                        ),
                        "检测结果": st.column_config.TextColumn("检测结果", width="small"),
                    },
                )
                policies, retained = _column_choices(edited)
                file_policies[table.logical_name] = policies
                file_retained[table.logical_name] = retained
                with st.expander("数据预览"):
                    st.dataframe(
                        table.dataframe.head(8),
                        use_container_width=True,
                        hide_index=True,
                    )
        prepared.append((uploaded.name, uploaded, file_policies, file_retained))

    if prepared and st.button(
        "确认添加",
        type="primary",
        icon=":material/upload_file:",
        use_container_width=False,
    ):
        try:
            results = [
                services.ingestion.ingest(
                    tenant_id,
                    workspace_id,
                    filename,
                    source,
                    policies,
                    retained_columns=retained,
                )
                for filename, source, policies, retained in prepared
            ]
            added = sum(not result.duplicate for result in results)
            duplicates = len(results) - added
            _set_flash("success", f"已追加 {added} 个文件，跳过 {duplicates} 个重复文件。")
            st.rerun()
        except (DiskHeadroomError, ValueError) as exc:
            st.error(str(exc))

    st.divider()
    dataset_col, file_col = st.columns([1.2, 1], gap="large")
    with dataset_col:
        _section_heading("数据集与版本", f"{len(datasets)} 个")
        if datasets:
            dataset_table = pd.DataFrame(
                [
                    {
                        "数据集": item["logical_name"],
                        "版本": f"V{item['version']}",
                        "行数": item["row_count"],
                        "制品 ID": str(item["artifact_id"])[:8],
                        "更新时间": _format_timestamp(item["version_created_at"]),
                    }
                    for item in datasets
                ]
            )
            st.dataframe(dataset_table, use_container_width=True, hide_index=True)
            for dataset in datasets:
                action_col, delete_col = st.columns([4, 1], vertical_alignment="center")
                action_col.caption(
                    f"{dataset['logical_name']} · V{dataset['version']} · "
                    f"{dataset['row_count']:,} 行"
                )
                if delete_col.button(
                    "删除工作表",
                    key=(f"delete-table-{workspace_id}-{dataset['dataset_version_id']}"),
                    use_container_width=True,
                ):
                    st.session_state[f"pending-table-delete-{workspace_id}"] = dataset[
                        "dataset_version_id"
                    ]
                    st.rerun()
                _render_dataset_version_deletion(services, tenant_id, workspace_id, dataset)
        else:
            _empty_state("暂无数据集", "数据")
    with file_col:
        _section_heading("文件记录", f"{len(files)} 个")
        if files:
            file_table = pd.DataFrame(
                [
                    {
                        "文件": item["original_name"],
                        "大小": _format_bytes(item["byte_size"]),
                        "指纹": item["sha256"][:10],
                        "上传时间": _format_timestamp(item["created_at"]),
                    }
                    for item in files
                ]
            )
            st.dataframe(file_table, use_container_width=True, hide_index=True)
            for file_record in files:
                action_col, delete_col = st.columns([4, 1], vertical_alignment="center")
                action_col.caption(f"{file_record['original_name']} · {file_record['sha256'][:10]}")
                if delete_col.button(
                    "删除",
                    key=f"delete-file-{workspace_id}-{file_record['id']}",
                    use_container_width=True,
                ):
                    st.session_state[f"pending-delete-{workspace_id}"] = file_record["id"]
                    st.rerun()
                _render_file_deletion(services, tenant_id, workspace_id, file_record)
        else:
            _empty_state("暂无上传记录", "文件")


def _render_dataset_version_deletion(
    services: Any,
    tenant_id: str,
    workspace_id: str,
    dataset: dict[str, Any],
) -> None:
    state_key = f"pending-table-delete-{workspace_id}"
    dataset_version_id = dataset["dataset_version_id"]
    if st.session_state.get(state_key) != dataset_version_id:
        return
    try:
        impact = services.data_sources.inspect_table(tenant_id, workspace_id, dataset_version_id)
    except (AuthorizationError, RecordNotFoundError, ValueError, OSError) as exc:
        st.warning(f"无法检查工作表删除影响：{exc}")
        return

    has_dependencies = (
        impact.analysis_run_count > 0 or impact.artifact_count > 1 or impact.message_count > 0
    )
    st.warning(
        f"将删除工作表 {impact.logical_name} V{impact.version}（{impact.row_count:,} 行）、"
        f"{impact.artifact_count} 个本地制品和 "
        f"{impact.analysis_run_count} 个关联分析、关联对话 {impact.message_count} 条。"
    )
    acknowledged = True
    if has_dependencies:
        acknowledged = st.checkbox(
            "我了解关联分析和结果也会删除",
            key=f"ack-table-delete-{workspace_id}-{dataset_version_id}",
        )
    st.caption("加密原文件和同一文件的其他工作表会保留；该操作不能撤销。")
    if st.button(
        "确认删除工作表",
        key=f"confirm-table-delete-{workspace_id}-{dataset_version_id}",
        type="primary",
        disabled=not acknowledged,
    ):
        try:
            deleted = services.data_sources.delete_table(
                tenant_id, workspace_id, dataset_version_id
            )
        except (
            AuthorizationError,
            DataSourceDeletionError,
            RecordNotFoundError,
            ValueError,
            OSError,
        ) as exc:
            st.warning(f"删除失败，原数据已保留：{exc}")
            return
        st.session_state.pop(state_key, None)
        _set_flash(
            "success",
            f"已删除工作表 {deleted.logical_name} 及 {deleted.artifact_count - 1} 个关联制品。",
        )
        st.rerun()
    if st.button(
        "取消",
        key=f"cancel-table-delete-{workspace_id}-{dataset_version_id}",
    ):
        st.session_state.pop(state_key, None)
        st.rerun()


def _render_file_deletion(
    services: Any,
    tenant_id: str,
    workspace_id: str,
    file_record: dict[str, Any],
) -> None:
    state_key = f"pending-delete-{workspace_id}"
    if st.session_state.get(state_key) != file_record["id"]:
        return
    try:
        impact = services.data_sources.inspect(tenant_id, workspace_id, file_record["id"])
    except (AuthorizationError, RecordNotFoundError, ValueError, OSError) as exc:
        st.warning(f"无法检查删除影响：{exc}")
        return

    has_dependencies = (
        impact.analysis_run_count > 0 or impact.artifact_count > 1 or impact.message_count > 0
    )
    st.warning(
        f"将删除 {impact.dataset_version_count} 个数据版本、"
        f"{impact.artifact_count} 个本地制品和 "
        f"{impact.analysis_run_count} 个关联分析、关联对话 {impact.message_count} 条。"
    )
    acknowledged = True
    if has_dependencies:
        acknowledged = st.checkbox(
            "我了解关联内容也会删除",
            key=f"ack-delete-{workspace_id}-{file_record['id']}",
        )
    st.caption("该操作会删除本地加密原件和关联制品，且不能撤销。")
    if st.button(
        "确认删除",
        key=f"confirm-delete-{workspace_id}-{file_record['id']}",
        type="primary",
        disabled=not acknowledged,
    ):
        try:
            deleted = services.data_sources.delete(tenant_id, workspace_id, file_record["id"])
        except (AuthorizationError, RecordNotFoundError, ValueError, OSError) as exc:
            st.warning(f"删除失败，原数据已保留：{exc}")
            return
        st.session_state.pop(state_key, None)
        _set_flash(
            "success",
            f"已删除 {deleted.original_name} 及 {deleted.artifact_count} 个关联制品。",
        )
        st.rerun()
    if st.button(
        "取消",
        key=f"cancel-delete-{workspace_id}-{file_record['id']}",
    ):
        st.session_state.pop(state_key, None)
        st.rerun()


def _render_analysis_view(
    services: ApplicationServices,
    config: AppConfig,
    tenant_id: str,
    workspace_id: str,
    conversation_id: str,
    artifacts: list[Any],
) -> None:
    _section_heading("安全分析", "结构化计划 · 本地执行")
    if not artifacts:
        _empty_state("请先添加数据制品", "分析")
        return

    artifact_by_id = {artifact.id: artifact for artifact in artifacts}
    source_id = st.selectbox(
        "主数据源",
        list(artifact_by_id),
        format_func=lambda artifact_id: _artifact_option(artifact_by_id[artifact_id]),
        key=f"analysis-source-{workspace_id}",
    )
    source = artifact_by_id[source_id]
    source_columns = len(source.schema.get("columns", []))
    st.caption(
        f"{source.row_count:,} 行 · {source_columns} 列 · "
        f"{len(source.lineage)} 个脱敏字段 · ID {source.id[:8]}"
    )

    if not config.analyst_api_key:
        st.warning("分析模型未配置。文件处理、脱敏、预览和导出仍可正常使用。")

    st.divider()
    messages = services.repository.list_messages(tenant_id, conversation_id, limit=50)
    if not messages:
        st.markdown('<div class="chat-empty">分析对话将在这里保留</div>', unsafe_allow_html=True)
    for message in messages:
        with st.chat_message(message.role):
            st.markdown(message.safe_content)
            if message.artifact_id:
                result_col, action_col = st.columns([4, 1], vertical_alignment="center")
                result_col.caption(f"结果制品 {message.artifact_id[:8]}")
                if action_col.button(
                    "查看",
                    key=f"open-result-{message.id}",
                    icon=":material/open_in_new:",
                    use_container_width=True,
                ):
                    st.session_state[f"result-selected-{workspace_id}"] = message.artifact_id
                    st.query_params["view"] = "结果"
                    st.rerun()

    workflow = None
    snapshot = None
    if config.analyst_api_key:
        planner = SafeAnalysisPlanner(
            config.analyst_api_key,
            config.analyst_base_url,
            config.analyst_model_name,
            services.conversations.sanitizer,
            provider_name=config.analyst_provider,
            network_settings=config.network_settings,
        )
        workflow = AnalysisWorkflowService(
            services.repository,
            services.artifacts,
            planner,
            config.analysis_max_repair_attempts,
        )
        snapshot = workflow.latest_for_conversation(tenant_id, workspace_id, conversation_id)
        if snapshot:
            _render_workflow_card(
                services, workflow, snapshot, tenant_id, workspace_id, conversation_id
            )

    waiting = bool(snapshot and snapshot.run["status"] == "awaiting_confirmation")
    prompt = st.chat_input(
        "输入清洗、合并或分析要求",
        disabled=not config.analyst_api_key or waiting,
    )
    if not prompt:
        return

    with st.chat_message("user"):
        st.markdown(prompt)
    try:
        request = services.conversations.add_user_message(tenant_id, conversation_id, prompt)
        context = services.conversations.build_safe_context(
            tenant_id,
            workspace_id,
            conversation_id,
            preferred_artifact_id=source_id,
        )
        if workflow is None:
            raise ValueError("分析模型未配置。")
        with st.spinner("正在生成安全分析计划"):
            created = workflow.start(
                tenant_id,
                workspace_id,
                conversation_id,
                source_id,
                request.safe_content,
                context,
                request_message_id=request.id,
            )
        if created.run["status"] == "awaiting_confirmation":
            _set_flash("success", "计划已生成，请预览并确认后再执行。")
        elif created.run["status"] == "security_blocked":
            _set_flash("error", "计划触发安全拒绝，已阻止且不会自动修复。")
        st.rerun()
    except GeneratedNameValidationError as exc:
        st.error(str(exc))
    except AnalysisServiceError as exc:
        services.repository.add_audit_event(
            tenant_id,
            "analysis_failed",
            workspace_id,
            {"source_artifact_id": source_id, "error_code": exc.error_code},
        )
        st.error(exc.public_message)
    except (SensitiveContentError, ValueError) as exc:
        st.error(str(exc))
    except Exception:
        services.repository.add_audit_event(
            tenant_id,
            "analysis_failed",
            workspace_id,
            {"source_artifact_id": source_id},
        )
        st.error("分析服务暂时不可用，请检查模型配置后重试。")


def _render_workflow_card(
    services: ApplicationServices,
    workflow: AnalysisWorkflowService,
    snapshot: WorkflowSnapshot,
    tenant_id: str,
    workspace_id: str,
    conversation_id: str,
) -> None:
    status_labels = {
        "planning": "正在规划",
        "awaiting_confirmation": "等待确认",
        "executing": "正在执行",
        "validating": "正在校验",
        "repairing": "正在生成修复计划",
        "repairable_error": "可修复错误",
        "completed": "已完成",
        "rejected": "已拒绝",
        "security_blocked": "安全阻止",
        "failed": "失败",
    }
    status = snapshot.run["status"]
    with st.container(border=True):
        st.subheader(
            f"分析运行 · {status_labels.get(status, status)}",
            help=f"运行 ID：{snapshot.run['id']}",
        )
        st.caption(
            f"计划版本 {snapshot.current_plan.version if snapshot.plan_versions else '-'} · "
            f"已修复 {snapshot.run['repair_count']}/{snapshot.run['max_repairs']} 次"
        )

        if snapshot.plan_versions:
            for line in describe_plan(
                snapshot.current_plan.plan,
                resolve_value=lambda value: services.masking.restore_display_value(
                    tenant_id, value
                ),
            ):
                st.markdown(f"- {line}")
            if snapshot.current_plan.reason == "repair" and snapshot.run["error_message"]:
                st.warning(f"上次执行未通过：{snapshot.run['error_message']}")

        if len(snapshot.plan_versions) > 1 or snapshot.attempts:
            decision_labels = {
                "pending": "待确认",
                "confirmed": "已确认",
                "rejected": "已拒绝",
                "superseded": "已被新版替代",
            }
            with st.expander("查看计划与执行历史"):
                for version in reversed(snapshot.plan_versions):
                    st.markdown(
                        f"**计划 V{version.version}** · "
                        f"{decision_labels.get(version.decision, version.decision)} · "
                        f"来源：{version.reason}"
                    )
                for attempt in reversed(snapshot.attempts):
                    detail = f" · {attempt.error_message}" if attempt.error_message else ""
                    st.caption(
                        f"执行 #{attempt.attempt_number}（计划 V{attempt.plan_version}）"
                        f"：{attempt.status}{detail}"
                    )

        if status == "awaiting_confirmation":
            confirm_col, reject_col = st.columns(2)
            if confirm_col.button(
                "确认并执行",
                type="primary",
                key=f"confirm-run-{snapshot.run['id']}",
                use_container_width=True,
            ):
                with st.spinner("正在本地执行并进行质量校验"):
                    result = workflow.confirm(tenant_id, snapshot.run["id"])
                if result.run["status"] == "completed":
                    artifact_id = result.run["result_artifact_id"]
                    services.conversations.add_assistant_message(
                        tenant_id,
                        conversation_id,
                        result.current_plan.plan.safe_summary,
                        artifact_id,
                        analysis_run_id=result.run["id"],
                    )
                    st.session_state[f"result-selected-{workspace_id}"] = artifact_id
                    _set_flash("success", f"分析完成，已生成制品 {artifact_id[:8]}。")
                elif result.run["status"] == "awaiting_confirmation":
                    _set_flash("error", "执行未通过，已生成修复计划，请重新确认。")
                elif result.run["status"] == "security_blocked":
                    _set_flash("error", "安全校验拒绝：已停止，未执行自动修复。")
                else:
                    _set_flash("error", result.run["error_message"] or "分析执行失败。")
                st.rerun()
            if reject_col.button(
                "拒绝计划",
                key=f"reject-run-{snapshot.run['id']}",
                use_container_width=True,
            ):
                workflow.reject(tenant_id, snapshot.run["id"])
                _set_flash("success", "计划已拒绝，没有执行任何数据操作。")
                st.rerun()

            with st.form(f"revise-run-{snapshot.run['id']}"):
                feedback = st.text_area(
                    "修改意见", placeholder="例如：只保留最近 30 天，并按门店汇总"
                )
                submitted = st.form_submit_button("根据意见生成新版计划")
            if submitted and feedback.strip():
                try:
                    safe_feedback = services.conversations.sanitizer.sanitize(
                        tenant_id, feedback.strip()
                    )
                    context = services.conversations.build_safe_context(
                        tenant_id,
                        workspace_id,
                        conversation_id,
                        preferred_artifact_id=snapshot.run["source_artifact_id"],
                    )
                    with st.spinner("正在生成新版计划"):
                        workflow.revise(tenant_id, snapshot.run["id"], safe_feedback, context)
                except (
                    GeneratedNameValidationError,
                    AnalysisServiceError,
                    SensitiveContentError,
                    ValueError,
                ) as exc:
                    st.error(str(exc))
                else:
                    _set_flash("success", "新版计划已生成，请再次确认。")
                    st.rerun()

        elif status == "completed":
            st.success(f"质量校验通过，制品 {snapshot.run['result_artifact_id'][:8]} 已保存。")
            latest_attempt = snapshot.attempts[-1] if snapshot.attempts else None
            if latest_attempt and latest_attempt.quality_report:
                for warning in latest_attempt.quality_report.warnings:
                    st.warning(warning.message)
        elif status == "security_blocked":
            st.error(f"安全拒绝（不会自动修复）：{snapshot.run['error_message']}")
        elif status == "failed":
            st.error(snapshot.run["error_message"] or "分析运行失败。")
        elif status == "rejected":
            st.info("计划已由用户拒绝，未执行。")


def _render_results_view(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifacts: list[Any],
) -> None:
    _section_heading("数据制品", f"{len(artifacts)} 个")
    if not artifacts:
        _empty_state("当前工作区暂无数据制品", "表")
        return

    kind_filter = (
        st.segmented_control(
            "制品类型",
            ("全部", "上传数据", "分析结果"),
            default="全部",
            label_visibility="collapsed",
        )
        or "全部"
    )
    filtered = [
        artifact
        for artifact in artifacts
        if kind_filter == "全部"
        or (kind_filter == "上传数据" and artifact.kind == "dataset")
        or (kind_filter == "分析结果" and artifact.kind == "analysis_result")
    ]
    if not filtered:
        _empty_state("该类型下暂无制品", "表")
        return

    artifact_by_id = {artifact.id: artifact for artifact in filtered}
    preferred = st.session_state.get(f"result-selected-{workspace_id}")
    selected_index = list(artifact_by_id).index(preferred) if preferred in artifact_by_id else 0
    selected_id = st.selectbox(
        "选择制品",
        list(artifact_by_id),
        index=selected_index,
        format_func=lambda artifact_id: _artifact_option(artifact_by_id[artifact_id]),
    )
    st.session_state[f"result-selected-{workspace_id}"] = selected_id
    artifact = artifact_by_id[selected_id]

    info_columns = st.columns(4)
    info_columns[0].metric("类型", _artifact_kind(artifact.kind), border=True)
    info_columns[1].metric("行数", f"{artifact.row_count:,}", border=True)
    info_columns[2].metric("字段", len(artifact.schema.get("columns", [])), border=True)
    info_columns[3].metric("脱敏字段", len(artifact.lineage), border=True)

    mode = (
        st.segmented_control(
            "预览模式",
            ("脱敏预览", "授权还原"),
            default="脱敏预览",
        )
        or "脱敏预览"
    )
    visible = services.artifacts.preview(tenant_id, selected_id, restored=mode == "授权还原")
    st.dataframe(visible, use_container_width=True, hide_index=True, height=430)
    st.caption("预览最多读取 1,000 行；下载文件仅在选择格式并准备后生成。")

    _render_export_controls(services, tenant_id, workspace_id, artifact)
    st.caption(f"制品 ID {artifact.id} · 生成于 {_format_timestamp(artifact.created_at)}")

    with st.expander("制品详情", icon=":material/account_tree:"):
        detail_left, detail_right = st.columns(2, gap="large")
        with detail_left:
            st.markdown("**字段结构**")
            st.dataframe(
                pd.DataFrame(artifact.schema.get("columns", [])),
                use_container_width=True,
                hide_index=True,
            )
        with detail_right:
            st.markdown("**脱敏血缘**")
            lineage_rows = [
                {
                    "字段": column,
                    "语义域": lineage.domain,
                    "标准化": lineage.normalizer,
                    "密钥版本": f"V{lineage.key_version}",
                }
                for column, lineage in artifact.lineage.items()
            ]
            if lineage_rows:
                st.dataframe(
                    pd.DataFrame(lineage_rows),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.caption("该制品不包含脱敏字段。")
        st.caption(f"来源制品：{', '.join(artifact.parent_ids) or '加密上传文件'}")


def _render_export_controls(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifact: Any,
) -> None:
    format_label = st.selectbox(
        "下载格式",
        ("CSV", "Parquet", "Excel"),
        help="Excel 仅适用于不超过 100,000 行且估算数据量不超过 50 MiB 的结果。",
    )
    format_name = {"CSV": "csv", "Parquet": "parquet", "Excel": "xlsx"}[format_label]
    state_key = f"prepared-export-{workspace_id}"
    prepared = st.session_state.get(state_key)
    if prepared and prepared["artifact_id"] != artifact.id:
        _cleanup_prepared_export(prepared)
        st.session_state.pop(state_key, None)
        prepared = None

    safe_col, restored_col = st.columns(2)
    if safe_col.button(
        "准备脱敏下载",
        icon=":material/download:",
        use_container_width=True,
    ):
        _prepare_export(services, tenant_id, artifact, format_name, False, state_key, prepared)
    if restored_col.button(
        "准备还原下载",
        icon=":material/lock_open:",
        disabled=format_name == "parquet",
        help="还原版使用血缘逐块恢复；请选择 CSV 或小型 Excel。",
        use_container_width=True,
    ):
        _prepare_export(services, tenant_id, artifact, format_name, True, state_key, prepared)

    prepared = st.session_state.get(state_key)
    if not prepared:
        return
    path = Path(prepared["path"])
    if not path.is_file():
        st.session_state.pop(state_key, None)
        st.warning("已准备的下载文件已过期，请重新准备。")
        return
    with path.open("rb") as stream:
        st.download_button(
            "下载已准备文件",
            data=stream,
            file_name=prepared["file_name"],
            mime=prepared["mime"],
            on_click="ignore",
            icon=":material/download:",
            type="primary",
            use_container_width=True,
        )


def _prepare_export(
    services: ApplicationServices,
    tenant_id: str,
    artifact: Any,
    format_name: str,
    restored: bool,
    state_key: str,
    previous: dict[str, Any] | None,
) -> None:
    try:
        exported = services.artifacts.export(tenant_id, artifact.id, format_name, restored=restored)
    except (ExportLimitError, ValueError) as exc:
        st.error(str(exc))
        return
    if previous:
        _cleanup_prepared_export(previous)
    st.session_state[state_key] = {
        "artifact_id": artifact.id,
        "path": str(exported.path),
        "file_name": exported.file_name,
        "mime": exported.mime,
        "temporary": exported.temporary,
    }


def _cleanup_prepared_export(prepared: dict[str, Any]) -> None:
    if prepared.get("temporary"):
        Path(prepared["path"]).unlink(missing_ok=True)


def _policy_frame(dataframe: pd.DataFrame, detected: set[str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "脱敏": str(column) in detected,
                "字段": str(column),
                "语义域": _default_domain(str(column)),
                "标准化": NORMALIZERS[_normalizer_index(str(column))],
                "检测结果": "敏感" if str(column) in detected else "常规",
            }
            for column in dataframe.columns
        ]
    )


def _column_choices(
    edited: pd.DataFrame,
) -> tuple[dict[str, ColumnPolicy], set[str]]:
    policies: dict[str, ColumnPolicy] = {}
    retained: set[str] = set()
    for row in edited.to_dict("records"):
        column = str(row["字段"])
        if row["脱敏"]:
            policies[column] = ColumnPolicy(
                domain=str(row["语义域"]),
                normalizer=str(row["标准化"]),
            )
        else:
            retained.add(column)
    return policies, retained


def _default_domain(column: str) -> str:
    lowered = column.casefold()
    if any(value in lowered for value in ("门店", "酒店", "store", "hotel")):
        return "STORE"
    if any(value in lowered for value in ("手机", "电话", "phone", "mobile")):
        return "PHONE"
    if any(value in lowered for value in ("邮箱", "email")):
        return "EMAIL"
    if any(value in lowered for value in ("姓名", "名字", "name")):
        return "PERSON"
    if any(value in lowered for value in ("客户", "会员", "customer", "member")):
        return "CUSTOMER"
    return f"FIELD_{hashlib.sha256(column.encode()).hexdigest()[:8].upper()}"


def _normalizer_index(column: str) -> int:
    lowered = column.casefold()
    if any(value in lowered for value in ("手机", "电话", "phone", "mobile")):
        return 2
    if any(value in lowered for value in ("id", "编号", "编码", "证件")):
        return 3
    return 0


def _section_heading(title: str, meta: str) -> None:
    title_col, meta_col = st.columns([4, 1], vertical_alignment="center")
    title_col.subheader(title)
    meta_col.markdown(f'<div class="section-meta">{meta}</div>', unsafe_allow_html=True)


def _empty_state(label: str, symbol: str) -> None:
    st.markdown(
        f'<div class="empty-state"><strong>{symbol}</strong><span>{label}</span></div>',
        unsafe_allow_html=True,
    )


def _artifact_option(artifact: Any) -> str:
    return f"{artifact.name} · {_artifact_kind(artifact.kind)} · {artifact.row_count:,} 行"


def _artifact_kind(kind: str) -> str:
    return "分析结果" if kind == "analysis_result" else "上传数据"


def _event_label(event_type: str) -> str:
    return {
        "file_ingested": "文件已入库",
        "analysis_artifact_created": "分析制品已生成",
        "analysis_failed": "分析请求失败",
    }.get(event_type, event_type)


def _event_detail(event: dict[str, Any]) -> str:
    details = event["details"]
    if event["event_type"] == "file_ingested":
        return f"{details.get('table_count', 0)} 张表 · {details.get('sha256_prefix', '')}"
    if event["event_type"] == "analysis_artifact_created":
        return f"{details.get('row_count', 0):,} 行 · {str(details.get('artifact_id', ''))[:8]}"
    if event["event_type"] == "analysis_failed":
        return f"制品 {str(details.get('source_artifact_id', ''))[:8]}"
    return "已记录"


def _format_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16]


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / 1024 / 1024:.1f} MB"


def _set_flash(level: str, message: str) -> None:
    st.session_state["flash-message"] = {"level": level, "message": message}


def _render_flash() -> None:
    flash = st.session_state.pop("flash-message", None)
    if not flash:
        return
    renderer = getattr(st, flash["level"], st.info)
    renderer(flash["message"])
