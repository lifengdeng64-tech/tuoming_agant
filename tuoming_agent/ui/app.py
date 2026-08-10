from __future__ import annotations

import hashlib
import html
import io
from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st

from tuoming_agent.analysis.executor import AnalysisExecutionError, AnalysisExecutor
from tuoming_agent.analysis.planner import SafeAnalysisPlanner
from tuoming_agent.config import AppConfig, ConfigurationError
from tuoming_agent.ingestion.scanner import detect_sensitive_columns
from tuoming_agent.security.dlp import SensitiveContentError
from tuoming_agent.security.masking import ColumnPolicy
from tuoming_agent.ui.styles import APP_STYLES
from tuoming_agent.workspace.service import ApplicationServices, create_services

VIEW_OPTIONS = ("概览", "数据", "分析", "结果")
NORMALIZERS = ("text", "casefold", "phone", "identifier")


def run() -> None:
    st.set_page_config(
        page_title="透明数据安全工作台",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(APP_STYLES, unsafe_allow_html=True)
    try:
        config = AppConfig.from_env()
    except ConfigurationError as exc:
        _render_configuration_error(str(exc))
        st.stop()

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
    else:
        _render_results_view(services, tenant_id, workspace_id, artifacts)


@st.cache_resource(show_spinner=False)
def _load_services(config: AppConfig) -> ApplicationServices:
    return create_services(config)


def _render_configuration_error(message: str) -> None:
    st.title("数据安全工作台")
    st.error(message)
    st.code("python -m tuoming_agent.keygen", language="powershell")


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
    model_state = "已连接" if config.analyst_api_key else "未配置"
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
    st.sidebar.caption("v0.2 · Local-first")
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
    prepared: list[tuple[str, bytes, dict[str, dict[str, ColumnPolicy]]]] = []

    for uploaded in uploaded_files or []:
        content = uploaded.getvalue()
        file_key = hashlib.sha256(content).hexdigest()[:12]
        try:
            tables = services.ingestion.preview(uploaded.name, content)
        except ValueError as exc:
            st.error(f"{uploaded.name}: {exc}")
            continue
        file_policies: dict[str, dict[str, ColumnPolicy]] = {}
        with st.expander(
            f"{uploaded.name} · {_format_bytes(len(content))} · {len(tables)} 张表",
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
                    f"检测到 {len(detected)} 个敏感字段"
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
                policies: dict[str, ColumnPolicy] = {}
                for row in edited.to_dict("records"):
                    if row["脱敏"]:
                        policies[str(row["字段"])] = ColumnPolicy(
                            domain=str(row["语义域"]),
                            normalizer=str(row["标准化"]),
                        )
                file_policies[table.logical_name] = policies
                with st.expander("数据预览"):
                    st.dataframe(
                        table.dataframe.head(8),
                        use_container_width=True,
                        hide_index=True,
                    )
        prepared.append((uploaded.name, content, file_policies))

    if prepared and st.button(
        f"确认并追加 {len(prepared)} 个文件",
        type="primary",
        icon=":material/upload_file:",
        use_container_width=False,
    ):
        try:
            results = [
                services.ingestion.ingest(tenant_id, workspace_id, filename, content, policies)
                for filename, content, policies in prepared
            ]
            added = sum(not result.duplicate for result in results)
            duplicates = len(results) - added
            _set_flash("success", f"已追加 {added} 个文件，跳过 {duplicates} 个重复文件。")
            st.rerun()
        except ValueError as exc:
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
                        "制品 ID": str(item["artifact_id"])[:8],
                        "更新时间": _format_timestamp(item["version_created_at"]),
                    }
                    for item in datasets
                ]
            )
            st.dataframe(dataset_table, use_container_width=True, hide_index=True)
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
        else:
            _empty_state("暂无上传记录", "文件")


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

    prompt = st.chat_input(
        "输入清洗、合并或分析要求",
        disabled=not config.analyst_api_key,
    )
    if not prompt:
        return

    with st.chat_message("user"):
        st.markdown(prompt)
    try:
        safe_request = services.conversations.add_user_message(tenant_id, conversation_id, prompt)
        context = services.conversations.build_safe_context(
            tenant_id,
            workspace_id,
            conversation_id,
            preferred_artifact_id=source_id,
        )
        planner = SafeAnalysisPlanner(
            config.analyst_api_key,
            config.analyst_base_url,
            config.analyst_model_name,
            services.conversations.sanitizer,
        )
        with st.spinner("正在生成并执行安全分析计划"):
            plan = planner.create_plan(safe_request, context)
            if plan.input_artifact_id != source_id:
                raise AnalysisExecutionError("分析计划未使用选定的主数据源，已在本地阻止。")
            services.conversations.sanitizer.assert_safe(f"{plan.result_name}\n{plan.safe_summary}")
            artifact = AnalysisExecutor(services.artifacts).execute(tenant_id, workspace_id, plan)
            services.conversations.add_assistant_message(
                tenant_id,
                conversation_id,
                plan.safe_summary,
                artifact.id,
            )
        st.session_state[f"result-selected-{workspace_id}"] = artifact.id
        _set_flash("success", f"分析完成，已生成制品 {artifact.id[:8]}。")
        st.rerun()
    except (SensitiveContentError, AnalysisExecutionError, ValueError) as exc:
        st.error(str(exc))
    except Exception:
        services.repository.add_audit_event(
            tenant_id,
            "analysis_failed",
            workspace_id,
            {"source_artifact_id": source_id},
        )
        st.error("分析服务暂时不可用，请检查模型配置后重试。")


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
    artifact, masked = services.artifacts.load(tenant_id, selected_id)

    info_columns = st.columns(4)
    info_columns[0].metric("类型", _artifact_kind(artifact.kind), border=True)
    info_columns[1].metric("行数", f"{artifact.row_count:,}", border=True)
    info_columns[2].metric("字段", len(masked.columns), border=True)
    info_columns[3].metric("脱敏字段", len(artifact.lineage), border=True)

    mode = (
        st.segmented_control(
            "预览模式",
            ("脱敏预览", "授权还原"),
            default="脱敏预览",
        )
        or "脱敏预览"
    )
    restored = services.masking.unmask_dataframe(tenant_id, masked, artifact.lineage)
    visible = masked if mode == "脱敏预览" else restored
    st.dataframe(visible.head(1000), use_container_width=True, hide_index=True, height=430)

    safe_col, restored_col, detail_col = st.columns([1, 1, 2], vertical_alignment="bottom")
    safe_col.download_button(
        "下载脱敏版",
        data=_to_excel(masked),
        file_name=f"masked-{artifact.id[:8]}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:",
        use_container_width=True,
    )
    restored_col.download_button(
        "下载还原版",
        data=_to_excel(restored),
        file_name=f"restored-{artifact.id[:8]}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/lock_open:",
        use_container_width=True,
    )
    detail_col.caption(f"制品 ID {artifact.id} · 生成于 {_format_timestamp(artifact.created_at)}")

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


def _to_excel(dataframe: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        dataframe.to_excel(writer, index=False, sheet_name="Result")
    return buffer.getvalue()


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
