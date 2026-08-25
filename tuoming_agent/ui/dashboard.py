from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import streamlit as st

from tuoming_agent.dashboard.charts import ChartDataError, bar_figure, line_figure
from tuoming_agent.dashboard.models import AggregationName, infer_dashboard_defaults
from tuoming_agent.dashboard.service import DashboardQueryError, KPIValue
from tuoming_agent.models import ArtifactRecord
from tuoming_agent.ui.components import render_empty_state, render_kpi_card, render_page_intro
from tuoming_agent.ui.theme import PLOTLY_CONFIG
from tuoming_agent.workspace.service import ApplicationServices

_AGGREGATIONS: tuple[AggregationName, ...] = ("sum", "mean", "min", "max", "count")
_AGGREGATION_LABELS = {
    "sum": "求和",
    "mean": "平均值",
    "min": "最小值",
    "max": "最大值",
    "count": "有效值计数",
}


def preferred_dashboard_artifact(
    artifacts: Sequence[ArtifactRecord],
) -> ArtifactRecord | None:
    analyses = [artifact for artifact in artifacts if artifact.kind == "analysis_result"]
    candidates = analyses or [artifact for artifact in artifacts if artifact.kind == "dataset"]
    return max(candidates, key=lambda artifact: artifact.created_at, default=None)


def format_kpi_value(kpi: KPIValue) -> str:
    value = kpi.value
    if value is None:
        return "—"
    if kpi.aggregation == "count":
        return f"{int(value):,}"
    absolute = abs(float(value))
    if absolute >= 100_000_000:
        return f"{float(value) / 100_000_000:,.2f} 亿"
    if absolute >= 10_000:
        return f"{float(value) / 10_000:,.2f} 万"
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{float(value):,.2f}"


def render_dashboard_view(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifacts: list[ArtifactRecord],
) -> None:
    render_page_intro(
        "LOCAL INTELLIGENCE",
        "经营仪表盘",
        "从本地脱敏数据生成可交互洞察。聚合计算不会上传原始表格。",
    )
    if not artifacts:
        render_empty_state("上传数据或完成一次分析后，即可生成仪表盘", "图")
        return

    preferred = preferred_dashboard_artifact(artifacts)
    artifact_by_id = {artifact.id: artifact for artifact in artifacts}
    default_id = preferred.id if preferred else artifacts[0].id
    selected_id = st.selectbox(
        "数据来源",
        list(artifact_by_id),
        index=list(artifact_by_id).index(default_id),
        format_func=lambda artifact_id: _artifact_option(artifact_by_id[artifact_id]),
        key=f"dashboard-artifact-{workspace_id}",
    )
    artifact = artifact_by_id[selected_id]
    defaults = infer_dashboard_defaults(artifact)
    if not defaults.numeric_columns:
        st.info("这个数据制品没有可聚合的数值字段，可前往“结果”查看明细。")
        _render_detail(services, tenant_id, workspace_id, artifact)
        return

    controls = st.container(border=True)
    with controls:
        st.markdown("#### 显示设置")
        measure_col, aggregation_col, date_col, category_col = st.columns(4)
        measures = tuple(
            measure_col.multiselect(
                "指标（最多 4 个）",
                defaults.numeric_columns,
                default=defaults.measure_columns,
                max_selections=4,
                key=f"dashboard-measures-{workspace_id}-{artifact.id}",
            )
        )
        aggregation = aggregation_col.selectbox(
            "计算方式",
            _AGGREGATIONS,
            format_func=lambda value: _AGGREGATION_LABELS[value],
            key=f"dashboard-aggregation-{workspace_id}-{artifact.id}",
        )
        date_column = _optional_column_select(
            date_col,
            "趋势时间字段",
            defaults.date_columns,
            defaults.date_column,
            key=f"dashboard-date-{workspace_id}-{artifact.id}",
        )
        category_column = _optional_column_select(
            category_col,
            "对比分类字段",
            defaults.category_columns,
            defaults.category_column,
            key=f"dashboard-category-{workspace_id}-{artifact.id}",
        )

    if not measures:
        st.info("请至少选择一个指标。")
        _render_detail(services, tenant_id, workspace_id, artifact)
        return

    _render_kpis(
        services,
        tenant_id,
        workspace_id,
        artifact,
        measures,
        aggregation,
    )
    with st.container(key=f"dashboard-charts-{workspace_id}"):
        chart_columns = st.columns(2, gap="large")
        with chart_columns[0]:
            _render_trend(
                services,
                tenant_id,
                workspace_id,
                artifact,
                measures,
                aggregation,
                date_column,
            )
        with chart_columns[1]:
            _render_category(
                services,
                tenant_id,
                workspace_id,
                artifact,
                measures[0],
                aggregation,
                category_column,
            )
    _render_detail(services, tenant_id, workspace_id, artifact)


def _optional_column_select(
    container: Any,
    label: str,
    columns: tuple[str, ...],
    default: str | None,
    *,
    key: str,
) -> str | None:
    options: tuple[str | None, ...] = (None, *columns)
    index = options.index(default) if default in options else 0
    return container.selectbox(
        label,
        options,
        index=index,
        format_func=lambda value: "不显示" if value is None else value,
        key=key,
    )


def _render_kpis(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifact: ArtifactRecord,
    measures: tuple[str, ...],
    aggregation: AggregationName,
) -> None:
    try:
        kpis = services.dashboard.kpis(
            tenant_id, workspace_id, artifact.id, measures, aggregation
        )
    except DashboardQueryError as exc:
        st.error(f"指标计算失败：{exc}")
        return
    columns = st.columns(len(kpis))
    for column, kpi in zip(columns, kpis, strict=True):
        with column:
            render_kpi_card(
                kpi.column,
                format_kpi_value(kpi),
                _AGGREGATION_LABELS[kpi.aggregation],
            )


def _render_trend(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifact: ArtifactRecord,
    measures: tuple[str, ...],
    aggregation: AggregationName,
    date_column: str | None,
) -> None:
    with st.container(border=True):
        if date_column is None:
            render_empty_state("选择时间字段后显示趋势", "线")
            return
        try:
            frames = [
                services.dashboard.grouped(
                    tenant_id,
                    workspace_id,
                    artifact.id,
                    date_column,
                    measure,
                    aggregation,
                    sort_dimension=True,
                )
                for measure in measures
            ]
            trend = frames[0]
            for frame in frames[1:]:
                trend = trend.merge(frame, on=date_column, how="outer")
            trend = trend.sort_values(date_column, kind="stable").tail(200)
            figure = line_figure(
                trend,
                date_column,
                measures,
                title=f"{date_column}趋势",
            )
            st.plotly_chart(figure, width="stretch", config=PLOTLY_CONFIG)
        except (DashboardQueryError, ChartDataError, TypeError, ValueError) as exc:
            st.warning(f"趋势图暂时无法生成：{exc}")


def _render_category(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifact: ArtifactRecord,
    measure: str,
    aggregation: AggregationName,
    category_column: str | None,
) -> None:
    with st.container(border=True):
        if category_column is None:
            render_empty_state("选择分类字段后显示对比", "柱")
            return
        try:
            grouped = services.dashboard.grouped(
                tenant_id,
                workspace_id,
                artifact.id,
                category_column,
                measure,
                aggregation,
            )
            figure = bar_figure(
                grouped.sort_values(measure, kind="stable").tail(20),
                category_column,
                measure,
                title=f"{category_column}对比 · 前 20 项",
            )
            st.plotly_chart(figure, width="stretch", config=PLOTLY_CONFIG)
        except (DashboardQueryError, ChartDataError, TypeError, ValueError) as exc:
            st.warning(f"分类图暂时无法生成：{exc}")


def _render_detail(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifact: ArtifactRecord,
) -> None:
    st.markdown("### 数据明细")
    st.caption("仅展示本地脱敏数据的前 100 行。")
    try:
        detail = services.dashboard.detail(
            tenant_id, workspace_id, artifact.id, limit=100
        )
    except DashboardQueryError as exc:
        st.warning(f"明细暂时无法读取：{exc}")
        return
    st.dataframe(detail, width="stretch", hide_index=True, height=360)


def _artifact_option(artifact: ArtifactRecord) -> str:
    kind = "分析结果" if artifact.kind == "analysis_result" else "上传数据"
    return f"{artifact.name} · {kind} · {artifact.row_count:,} 行"
