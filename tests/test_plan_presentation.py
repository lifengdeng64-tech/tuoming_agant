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

