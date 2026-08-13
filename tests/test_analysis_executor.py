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

