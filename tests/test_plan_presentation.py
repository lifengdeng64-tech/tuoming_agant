from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.presentation import describe_plan


def test_describe_plan_returns_readable_chinese_instead_of_json():
    plan = AnalysisPlan(
        input_artifact_id="artifact-12345678",
        result_name="门店汇总",
        operations=[
            {"action": "filter", "column": "销售额", "operator": "gt", "value": 100},
            {
                "action": "groupby",
                "by": ["门店"],
                "aggregations": [{"column": "销售额", "function": "sum", "output": "总销售额"}],
            },
        ],
    )
    preview = describe_plan(plan)
    assert preview[0] == "数据源：artifact-12345678"
    assert preview[1] == "结果名称：门店汇总"
    assert "筛选“销售额”" in preview[2]
    assert "按“门店”分组" in preview[3]
    assert not any("{\"" in line for line in preview)


def test_describe_plan_includes_local_dashboard_intent():
    plan = AnalysisPlan(
        input_artifact_id="artifact-12345678",
        operations=[],
        result_name="经营看板",
        dashboard={
            "measures": ["营收", "完成率"],
            "aggregation": "sum",
            "date_column": "月份",
            "category_column": "事业部",
        },
    )

    rendered = "\n".join(describe_plan(plan))

    assert "本地生成 BI 仪表盘" in rendered
    assert "“营收”" in rendered
    assert "“事业部”" in rendered


def test_describe_plan_translates_safe_calculations_and_aggregation_to_chinese():
    plan = AnalysisPlan(
        input_artifact_id="source-id",
        result_name="完成率分析",
        operations=[
            {
                "action": "derive",
                "column": "本期完成率",
                "expression": "safe_divide(col('本期实际'), col('本期预算'))",
            },
            {
                "action": "derive",
                "column": "同比差异",
                "expression": "safe_divide(col('本期完成率'), col('上期完成率')) - 1",
            },
        ],
        dashboard={
            "measures": ["本期完成率", "同比差异"],
            "aggregation": "sum",
            "category_column": "品牌名称",
        },
    )

    rendered = "\n".join(describe_plan(plan))

    assert "计算新列“本期完成率”：“本期实际” ÷ “本期预算”（分母为 0 时结果留空）" in rendered
    assert "计算新列“同比差异”：“本期完成率” ÷ “上期完成率” − 1（分母为 0 时结果留空）" in rendered
    assert "聚合方式：求和" in rendered
    assert "safe_divide" not in rendered
    assert "col(" not in rendered


def test_restore_display_value_preserves_non_tokens_and_tenant_boundaries(services):
    tenant_token = services.vault.tokenize("tenant-a", "brand", "华住")
    other_tenant_token = services.vault.tokenize("tenant-b", "brand", "如家")
    unknown_token = "brand_V1_UNKNOWN"

    value = {
        "items": [tenant_token, "普通文本", unknown_token, other_tenant_token],
        "nested": {"tuple": (tenant_token, 7)},
    }

    restored = services.masking.restore_display_value("tenant-a", value)

    assert restored == {
        "items": ["华住", "普通文本", unknown_token, other_tenant_token],
        "nested": {"tuple": ("华住", 7)},
    }


def test_describe_plan_restores_all_names_and_artifact_labels_only_for_display(services):
    token = services.vault.tokenize("tenant-a", "brand", "华住")
    column_token = services.vault.tokenize("tenant-a", "metadata", "品牌名称")
    result_token = services.vault.tokenize("tenant-a", "metadata", "品牌完成率分析")
    plan = AnalysisPlan(
        input_artifact_id="source-id",
        result_name=result_token,
        operations=[
            {"action": "filter", "column": column_token, "operator": "eq", "value": token},
            {
                "action": "fillna",
                "values": {column_token: {"preferred": token}, "备注": [token, "未知"]},
            },
            {
                "action": "merge",
                "right_artifact_id": "budget-id",
                "left_on": [column_token],
                "right_on": [column_token],
            },
        ],
    )

    lines = describe_plan(
        plan,
        resolve_value=lambda value: services.masking.restore_display_value("tenant-a", value),
        resolve_artifact=lambda artifact_id: {
            "source-id": "酒店营收.xlsx｜本期",
            "budget-id": "酒店预算.xlsx｜预算表",
        }.get(artifact_id, artifact_id),
    )

    rendered = "\n".join(lines)
    assert "数据源：酒店营收.xlsx｜本期" in rendered
    assert "结果名称：品牌完成率分析" in rendered
    assert "与“酒店预算.xlsx｜预算表”合并" in rendered
    assert "品牌名称" in rendered
    assert "'华住'" in rendered
    assert token not in rendered
    assert column_token not in rendered
    assert result_token not in rendered
    assert "source-id" not in rendered
    assert "budget-id" not in rendered
    assert plan.operations[0].value == token
    assert plan.operations[1].values[column_token]["preferred"] == token

