from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pandas as pd
import streamlit as st
from pydantic import ValidationError

from tuoming_agent.analysis.models import DashboardChartIntent
from tuoming_agent.dashboard.charts import (
    ChartDataError,
    area_figure,
    bar_figure,
    line_figure,
    pie_figure,
    scatter_figure,
)
from tuoming_agent.dashboard.models import (
    AggregationName,
    DashboardChartSpec,
    infer_dashboard_defaults,
    resolve_dashboard_chart_specs,
)
from tuoming_agent.dashboard.service import DashboardQueryError, KPIValue
from tuoming_agent.models import ArtifactRecord
from tuoming_agent.storage.errors import RecordNotFoundError
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
_CHART_LABELS = {
    "line": "折线趋势",
    "area": "面积趋势",
    "bar": "分类对比",
    "pie": "占比构成",
    "scatter": "关系分布",
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


def prime_dashboard_state(
    workspace_id: str, artifact: ArtifactRecord, intent: Any
) -> None:
    """Apply only schema-valid model preferences to local Streamlit widget state."""
    defaults = infer_dashboard_defaults(artifact)
    measures = tuple(
        column for column in intent.measures if column in defaults.numeric_columns
    )[:4]
    st.session_state[f"dashboard-artifact-{workspace_id}"] = artifact.id
    st.session_state[f"dashboard-measures-{workspace_id}-{artifact.id}"] = (
        measures or defaults.measure_columns
    )
    st.session_state[f"dashboard-aggregation-{workspace_id}-{artifact.id}"] = (
        intent.aggregation
    )
    st.session_state[f"dashboard-date-{workspace_id}-{artifact.id}"] = (
        intent.date_column if intent.date_column in defaults.date_columns else defaults.date_column
    )
    st.session_state[f"dashboard-category-{workspace_id}-{artifact.id}"] = (
        intent.category_column
        if intent.category_column in defaults.category_columns
        else defaults.category_column
    )
    st.session_state[f"dashboard-plan-{workspace_id}-{artifact.id}"] = [
        chart.model_dump(mode="json") for chart in getattr(intent, "charts", ())
    ]


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

    st.markdown(
        """
        <section class="dashboard-hero">
            <div>
                <span>SMART BI · LOCAL ONLY</span>
                <h3>让问题决定图表，而不是套用固定模板</h3>
                <p>识别趋势、排名、占比与指标关系；图表数据仍只在本机计算。</p>
            </div>
            <div class="dashboard-badges">
                <b>AI 智能选图</b><b>Plotly 交互</b><b>本地还原名称</b>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )

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

    planned = _stored_chart_intents(
        st.session_state.get(f"dashboard-plan-{workspace_id}-{artifact.id}", [])
    )
    chart_specs = resolve_dashboard_chart_specs(
        artifact,
        measures,
        aggregation,
        date_column,
        category_column,
        planned,
    )

    _render_kpis(
        services,
        tenant_id,
        workspace_id,
        artifact,
        measures,
        aggregation,
    )
    st.markdown("### 智能洞察")
    if planned:
        st.caption("已按你的分析要求选择图表类型；字段校验与数据计算均在本机完成。")
    else:
        st.caption("未指定图表类型，已根据字段角色自动组合趋势、对比与关系图。")
    with st.container(key=f"dashboard-charts-{workspace_id}"):
        if not chart_specs:
            render_empty_state("选择时间或分类字段后生成图表", "图")
        for start in range(0, len(chart_specs), 2):
            chart_columns = st.columns(2, gap="large")
            for column, spec in zip(
                chart_columns, chart_specs[start : start + 2], strict=False
            ):
                with column:
                    _render_chart_spec(
                        services, tenant_id, workspace_id, artifact, spec
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


def _render_chart_spec(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifact: ArtifactRecord,
    spec: DashboardChartSpec,
) -> None:
    with st.container(border=True):
        st.markdown(
            f'<span class="chart-kind">{_CHART_LABELS[spec.chart_type]}</span>',
            unsafe_allow_html=True,
        )
        try:
            if spec.chart_type == "scatter":
                figure = _scatter_for_spec(
                    services, tenant_id, workspace_id, artifact, spec
                )
            else:
                figure = _grouped_figure_for_spec(
                    services, tenant_id, workspace_id, artifact, spec
                )
            st.plotly_chart(
                figure,
                width="stretch",
                config=PLOTLY_CONFIG,
                key=(
                    f"chart-{workspace_id}-{artifact.id}-{spec.chart_type}-"
                    f"{spec.dimension}-{'-'.join(spec.measures)}"
                ),
            )
        except (DashboardQueryError, ChartDataError, TypeError, ValueError) as exc:
            st.warning(f"{_CHART_LABELS[spec.chart_type]}暂时无法生成：{exc}")


def _grouped_figure_for_spec(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifact: ArtifactRecord,
    spec: DashboardChartSpec,
):
    if spec.dimension is None:
        raise ChartDataError("This chart requires a dimension.")
    frames = [
        services.dashboard.grouped(
            tenant_id,
            workspace_id,
            artifact.id,
            spec.dimension,
            measure,
            spec.aggregation,
            sort_dimension=spec.chart_type in {"line", "area"},
        )
        for measure in spec.measures
    ]
    grouped = frames[0]
    for frame in frames[1:]:
        grouped = grouped.merge(frame, on=spec.dimension, how="outer")
    grouped = _restore_display_frame(
        services, tenant_id, artifact, grouped, (spec.dimension,)
    )
    if spec.chart_type in {"line", "area"}:
        parsed_dates = _parse_date_dimension(grouped[spec.dimension])
        if parsed_dates.notna().any():
            grouped = grouped.loc[parsed_dates.notna()].copy()
            grouped[spec.dimension] = parsed_dates.loc[parsed_dates.notna()]
        grouped = grouped.sort_values(spec.dimension, kind="stable").tail(200)
        builder = line_figure if spec.chart_type == "line" else area_figure
        return builder(grouped, spec.dimension, spec.measures, title=spec.title)
    measure = spec.measures[0]
    if spec.chart_type == "bar":
        return bar_figure(
            grouped.sort_values(measure, kind="stable").tail(15),
            spec.dimension,
            measure,
            title=spec.title,
        )
    return pie_figure(grouped, spec.dimension, measure, title=spec.title)


def _scatter_for_spec(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifact: ArtifactRecord,
    spec: DashboardChartSpec,
):
    columns = (*spec.measures, *((spec.dimension,) if spec.dimension else ()))
    points = services.dashboard.points(
        tenant_id,
        workspace_id,
        artifact.id,
        columns,
    )
    if spec.dimension:
        points = _restore_display_frame(
            services, tenant_id, artifact, points, (spec.dimension,)
        )
    return scatter_figure(
        points,
        spec.measures[0],
        spec.measures[1],
        title=spec.title,
        label=spec.dimension,
    )


def _stored_chart_intents(values: Any) -> tuple[DashboardChartIntent, ...]:
    if not isinstance(values, list):
        return ()
    intents: list[DashboardChartIntent] = []
    for value in values[:4]:
        try:
            intents.append(DashboardChartIntent.model_validate(value))
        except (ValidationError, TypeError):
            continue
    return tuple(intents)


def _render_detail(
    services: ApplicationServices,
    tenant_id: str,
    workspace_id: str,
    artifact: ArtifactRecord,
) -> None:
    st.markdown("### 数据明细")
    st.caption("仅在本机还原并展示前 100 行，原始内容不会上传。")
    try:
        detail = services.dashboard.detail(
            tenant_id, workspace_id, artifact.id, limit=100
        )
    except DashboardQueryError as exc:
        st.warning(f"明细暂时无法读取：{exc}")
        return
    detail = _restore_display_frame(
        services, tenant_id, artifact, detail, tuple(artifact.lineage)
    )
    st.dataframe(detail, width="stretch", hide_index=True, height=360)


def _restore_display_frame(
    services: ApplicationServices,
    tenant_id: str,
    artifact: ArtifactRecord,
    frame: pd.DataFrame,
    columns: tuple[str, ...],
) -> pd.DataFrame:
    lineage = {
        column: artifact.lineage[column]
        for column in columns
        if column in frame.columns and column in artifact.lineage
    }
    if not lineage:
        return frame
    try:
        return services.masking.unmask_dataframe(tenant_id, frame, lineage)
    except RecordNotFoundError:
        return frame


def _parse_date_dimension(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip()
    normalized = (
        text.str.replace("年", "-", regex=False)
        .str.replace("月", "-", regex=False)
        .str.replace("日", "", regex=False)
        .str.rstrip("-")
    )
    parsed = pd.to_datetime(normalized, errors="coerce")
    unresolved = parsed.isna() & normalized.str.fullmatch(r"\d{6}", na=False)
    if unresolved.any():
        parsed.loc[unresolved] = pd.to_datetime(
            normalized.loc[unresolved], format="%Y%m", errors="coerce"
        )
    return parsed


def _artifact_option(artifact: ArtifactRecord) -> str:
    kind = "分析结果" if artifact.kind == "analysis_result" else "上传数据"
    return f"{artifact.name} · {kind} · {artifact.row_count:,} 行"
