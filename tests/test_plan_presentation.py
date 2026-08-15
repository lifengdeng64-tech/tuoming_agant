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


def test_describe_plan_restores_only_displayed_filter_and_fill_values(services):
    token = services.vault.tokenize("tenant-a", "brand", "华住")
    protected_token = services.vault.tokenize("tenant-a", "metadata", "不应展示")
    plan = AnalysisPlan(
        input_artifact_id=protected_token,
        result_name=protected_token,
        operations=[
            {"action": "filter", "column": protected_token, "operator": "eq", "value": token},
            {
                "action": "fillna",
                "values": {protected_token: {"preferred": token}, "备注": [token, "未知"]},
            },
            {"action": "derive", "column": protected_token, "expression": protected_token},
        ],
    )

    lines = describe_plan(
        plan,
        resolve_value=lambda value: services.masking.restore_display_value("tenant-a", value),
    )

    rendered = "\n".join(lines)
    assert "'华住'" in rendered
    assert token not in rendered
    assert rendered.count(protected_token) == 6
    assert plan.operations[0].value == token
    assert plan.operations[1].values[protected_token]["preferred"] == token

