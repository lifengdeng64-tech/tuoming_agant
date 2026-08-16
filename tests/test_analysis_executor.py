from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from tuoming_agent.analysis.executor import AnalysisExecutionError, AnalysisExecutor
from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.models import ColumnLineage


def _source_artifact(services, workspace):
    return services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "source",
        pd.DataFrame(
            {
                "门店": ["STORE_V1_A", "STORE_V1_B", "STORE_V1_A"],
                "营收": [100, 300, 200],
                "成本": [40, 120, 70],
            }
        ),
        {"门店": ColumnLineage("store")},
        (),
    )


def test_allowlisted_plan_creates_reusable_artifact(services, workspace):
    source = _source_artifact(services, workspace)
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        result_name="门店汇总",
        operations=[
            {"action": "derive", "column": "利润", "expression": "col('营收') - col('成本')"},
            {
                "action": "groupby",
                "by": ["门店"],
                "aggregations": [{"column": "利润", "function": "sum", "output": "利润合计"}],
            },
            {"action": "sort", "columns": ["利润合计"], "ascending": False},
        ],
    )
    result = AnalysisExecutor(services.artifacts).execute("tenant-a", workspace.id, plan)
    _, dataframe = services.artifacts.load("tenant-a", result.id)
    assert list(dataframe["利润合计"]) == [190, 180]
    assert result.parent_ids == (source.id,)
    assert "门店" in result.lineage

    follow_up = AnalysisPlan(
        input_artifact_id=result.id,
        result_name="汇总前一名",
        operations=[{"action": "head", "rows": 1}],
    )
    follow_up_result = AnalysisExecutor(services.artifacts).execute(
        "tenant-a", workspace.id, follow_up
    )
    _, follow_up_frame = services.artifacts.load("tenant-a", follow_up_result.id)
    assert len(follow_up_frame) == 1
    assert follow_up_result.parent_ids == (result.id,)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('whoami')",
        "open('secret.txt').read()",
        "col('营收').to_csv('leak.csv')",
        "socket.create_connection(('example.com', 80))",
    ],
)
def test_executor_rejects_import_file_network_and_method_calls(expression, services, workspace):
    source = _source_artifact(services, workspace)
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        operations=[{"action": "derive", "column": "x", "expression": expression}],
    )
    with pytest.raises(AnalysisExecutionError):
        AnalysisExecutor(services.artifacts).execute("tenant-a", workspace.id, plan)


def test_concurrent_artifacts_use_unique_paths(services, workspace):
    def create(index: int):
        return services.artifacts.save_result(
            "tenant-a",
            workspace.id,
            f"result-{index}",
            pd.DataFrame({"value": [index]}),
            {},
            (),
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        artifacts = list(pool.map(create, range(12)))
    paths = {artifact.path for artifact in artifacts}
    assert len(paths) == 12
    assert all(path.exists() for path in paths)
    assert not list(services.artifacts.artifact_store.root.rglob("*.tmp*"))


def test_weighted_revenue_completion_excludes_temporary_closures(services, workspace):
    source = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "营收明细",
        pd.DataFrame(
            {
                "历史营业状态": ["正常营业", "正常营业", "临时停业", "正常营业"],
                "事业部": ["华东", "华东", "华东", "华南"],
                "本期营收": [80.0, 30.0, 1000.0, 45.0],
                "本期目标": [100.0, 50.0, 1000.0, 50.0],
                "去年同期营收": [60.0, 40.0, 800.0, 40.0],
                "去年同期目标": [100.0, 50.0, 1000.0, 50.0],
            }
        ),
        {},
        (),
    )
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        result_name="事业部营收完成度分析",
        operations=[
            {
                "action": "filter",
                "column": "历史营业状态",
                "operator": "ne",
                "value": "临时停业",
            },
            {
                "action": "groupby",
                "by": ["事业部"],
                "aggregations": [
                    {"column": "本期营收", "function": "sum", "output": "本期营收合计"},
                    {"column": "本期目标", "function": "sum", "output": "本期目标合计"},
                    {
                        "column": "去年同期营收",
                        "function": "sum",
                        "output": "去年同期营收合计",
                    },
                    {
                        "column": "去年同期目标",
                        "function": "sum",
                        "output": "去年同期目标合计",
                    },
                ],
            },
            {
                "action": "derive",
                "column": "营收完成度",
                "expression": "col('本期营收合计') / col('本期目标合计')",
            },
            {
                "action": "derive",
                "column": "去年营收完成度",
                "expression": "col('去年同期营收合计') / col('去年同期目标合计')",
            },
            {
                "action": "derive",
                "column": "营收完成度同比",
                "expression": "col('营收完成度') / col('去年营收完成度') - 1",
            },
        ],
    )

    result = AnalysisExecutor(services.artifacts).execute("tenant-a", workspace.id, plan)
    _, dataframe = services.artifacts.load("tenant-a", result.id)
    by_division = dataframe.set_index("事业部")

    assert by_division.loc["华东", "营收完成度"] == pytest.approx(110 / 150)
    assert by_division.loc["华东", "去年营收完成度"] == pytest.approx(100 / 150)
    assert by_division.loc["华东", "营收完成度同比"] == pytest.approx(0.1)
    assert by_division.loc["华南", "营收完成度"] == pytest.approx(0.9)
