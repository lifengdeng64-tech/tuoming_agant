from __future__ import annotations

import pandas as pd
import pytest

from tuoming_agent.analysis.executor import AnalysisResourceError
from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.quality import QualityIssue, QualityReport
from tuoming_agent.analysis.workflow import AnalysisWorkflowService
from tuoming_agent.storage.errors import AuthorizationError


class QueuePlanner:
    def __init__(self, plans: list[AnalysisPlan]):
        self.plans = list(plans)
        self.calls: list[tuple[str, dict]] = []

    def create_plan(self, safe_request: str, safe_context: dict) -> AnalysisPlan:
        self.calls.append((safe_request, safe_context))
        return self.plans.pop(0)


def _source(services, workspace):
    return services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "source",
        pd.DataFrame({"store": ["A", "B"], "sales": [10, 20]}),
        {},
        (),
    )


def _conversation(services, workspace):
    return services.repository.create_conversation("tenant-a", workspace.id)


def _plan(source_id: str, column: str = "sales") -> AnalysisPlan:
    return AnalysisPlan(
        input_artifact_id=source_id,
        result_name="summary",
        safe_summary="analysis complete",
        operations=[{"action": "select", "columns": [column]}],
    )


def test_plan_requires_confirmation_and_survives_repository_reopen(services, workspace):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    planner = QueuePlanner([_plan(source.id)])
    workflow = AnalysisWorkflowService(services.repository, services.artifacts, planner)

    snapshot = workflow.start(
        "tenant-a", workspace.id, conversation["id"], source.id, "summarize", {"schema": []}
    )

    assert snapshot.run["status"] == "awaiting_confirmation"
    assert snapshot.current_plan.version == 1
    assert services.repository.list_artifacts("tenant-a", workspace.id)[0].id == source.id

    reopened = AnalysisWorkflowService(services.repository, services.artifacts, planner)
    restored = reopened.get_snapshot("tenant-a", snapshot.run["id"])
    assert restored.current_plan.plan == _plan(source.id)


def test_request_message_links_an_analysis_run_and_response(services, workspace):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    workflow = AnalysisWorkflowService(
        services.repository, services.artifacts, QueuePlanner([_plan(source.id)])
    )
    request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "汇总营收"
    )

    started = workflow.start(
        "tenant-a",
        workspace.id,
        conversation["id"],
        source.id,
        request.safe_content,
        {},
        request_message_id=request.id,
    )
    services.conversations.add_assistant_message(
        "tenant-a",
        conversation["id"],
        "处理完成",
        analysis_run_id=started.run["id"],
    )

    links = services.repository.list_analysis_run_messages("tenant-a", started.run["id"])

    assert [item["kind"] for item in links] == ["request", "response"]


def test_analysis_run_message_links_reject_other_conversation_and_tenant(services, workspace):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    other_conversation = _conversation(services, workspace)
    workflow = AnalysisWorkflowService(
        services.repository, services.artifacts, QueuePlanner([_plan(source.id)])
    )
    other_request = services.conversations.add_user_message(
        "tenant-a", other_conversation["id"], "汇总营收"
    )

    with pytest.raises(AuthorizationError, match="conversation"):
        workflow.start(
            "tenant-a",
            workspace.id,
            conversation["id"],
            source.id,
            other_request.safe_content,
            {},
            request_message_id=other_request.id,
        )

    started = workflow.start(
        "tenant-a", workspace.id, conversation["id"], source.id, "汇总营收", {}
    )
    other_workspace = services.repository.create_workspace("tenant-b", "其他工作区")
    tenant_b_conversation = services.repository.create_conversation("tenant-b", other_workspace.id)

    with pytest.raises(AuthorizationError):
        services.conversations.add_assistant_message(
            "tenant-b",
            tenant_b_conversation["id"],
            "处理完成",
            analysis_run_id=started.run["id"],
        )


def test_confirm_executes_validates_and_persists_result(services, workspace):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    workflow = AnalysisWorkflowService(
        services.repository, services.artifacts, QueuePlanner([_plan(source.id)])
    )
    started = workflow.start(
        "tenant-a", workspace.id, conversation["id"], source.id, "summarize", {}
    )

    finished = workflow.confirm("tenant-a", started.run["id"])

    assert finished.run["status"] == "completed", finished.run["error_message"]
    assert finished.run["result_artifact_id"]
    assert finished.attempts[-1].quality_report.passed is True
    _, frame = services.artifacts.load("tenant-a", finished.run["result_artifact_id"])
    assert list(frame.columns) == ["sales"]


def test_reject_and_feedback_create_auditable_plan_versions(services, workspace):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    planner = QueuePlanner([_plan(source.id), _plan(source.id, "store")])
    workflow = AnalysisWorkflowService(services.repository, services.artifacts, planner)
    started = workflow.start("tenant-a", workspace.id, conversation["id"], source.id, "x", {})

    revised = workflow.revise("tenant-a", started.run["id"], "only store", {})
    assert revised.run["status"] == "awaiting_confirmation"
    assert [version.version for version in revised.plan_versions] == [1, 2]
    assert revised.plan_versions[0].decision == "superseded"
    assert revised.current_plan.plan.operations[0].columns == ["store"]

    rejected = workflow.reject("tenant-a", started.run["id"])
    assert rejected.run["status"] == "rejected"
    assert rejected.current_plan.decision == "rejected"


def test_business_error_creates_repair_plan_but_never_executes_without_confirmation(
    services, workspace
):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    planner = QueuePlanner([_plan(source.id, "missing"), _plan(source.id, "sales")])
    workflow = AnalysisWorkflowService(
        services.repository, services.artifacts, planner, max_repair_attempts=3
    )
    started = workflow.start("tenant-a", workspace.id, conversation["id"], source.id, "x", {})

    repaired = workflow.confirm("tenant-a", started.run["id"])

    assert repaired.run["status"] == "awaiting_confirmation"
    assert repaired.run["repair_count"] == 1
    assert repaired.current_plan.reason == "repair"
    assert repaired.current_plan.decision == "pending"
    assert len(repaired.attempts) == 1
    assert repaired.attempts[0].error_kind == "repairable"
    assert len(services.repository.list_artifacts("tenant-a", workspace.id)) == 1

    completed = workflow.confirm("tenant-a", started.run["id"])
    assert completed.run["status"] == "completed"


def test_security_rejection_never_calls_planner_for_repair(services, workspace):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    unsafe = AnalysisPlan(
        input_artifact_id=source.id,
        operations=[
            {
                "action": "derive",
                "column": "x",
                "expression": "__import__('os').system('whoami')",
            }
        ],
    )
    planner = QueuePlanner([unsafe, _plan(source.id)])
    workflow = AnalysisWorkflowService(services.repository, services.artifacts, planner)
    started = workflow.start("tenant-a", workspace.id, conversation["id"], source.id, "x", {})

    blocked = workflow.confirm("tenant-a", started.run["id"])

    assert blocked.run["status"] == "security_blocked"
    assert blocked.run["repair_count"] == 0
    assert len(planner.calls) == 1
    assert blocked.attempts[-1].error_kind == "security"


def test_repair_limit_stops_replanning(services, workspace):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    planner = QueuePlanner([_plan(source.id, "bad1"), _plan(source.id, "bad2")])
    workflow = AnalysisWorkflowService(
        services.repository, services.artifacts, planner, max_repair_attempts=1
    )
    started = workflow.start("tenant-a", workspace.id, conversation["id"], source.id, "x", {})
    repaired = workflow.confirm("tenant-a", started.run["id"])
    failed = workflow.confirm("tenant-a", repaired.run["id"])

    assert failed.run["status"] == "failed"
    assert failed.run["repair_count"] == 1
    assert len(planner.calls) == 2


def test_quality_failure_repairs_before_any_candidate_is_saved(services, workspace):
    class FailingValidator:
        def validate(self, candidate):
            return QualityReport(
                False,
                failures=(QualityIssue("test_failure", "deterministic check failed"),),
            )

    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    planner = QueuePlanner([_plan(source.id, "sales"), _plan(source.id, "store")])
    workflow = AnalysisWorkflowService(
        services.repository,
        services.artifacts,
        planner,
        validator=FailingValidator(),
    )
    started = workflow.start("tenant-a", workspace.id, conversation["id"], source.id, "x", {})

    repaired = workflow.confirm("tenant-a", started.run["id"])

    assert repaired.run["status"] == "awaiting_confirmation"
    assert repaired.attempts[-1].quality_report.passed is False
    assert repaired.attempts[-1].error_kind == "repairable"
    assert len(services.repository.list_artifacts("tenant-a", workspace.id)) == 1
    assert not list(
        (services.artifacts.artifact_store.root / "analysis-candidates").rglob("*.parquet")
    )


def test_unexpected_execution_error_is_terminal_and_does_not_repair(
    services, workspace, monkeypatch
):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    planner = QueuePlanner([_plan(source.id)])
    workflow = AnalysisWorkflowService(services.repository, services.artifacts, planner)
    started = workflow.start("tenant-a", workspace.id, conversation["id"], source.id, "x", {})

    def fail_prepare(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(workflow.executor, "prepare", fail_prepare)
    failed = workflow.confirm("tenant-a", started.run["id"])

    assert failed.run["status"] == "failed"
    assert failed.run["error_kind"] == "terminal"
    assert failed.attempts[-1].error_kind == "terminal"
    assert len(planner.calls) == 1


def test_workflow_publishes_disk_candidate_only_after_quality_success(
    services, workspace, monkeypatch
):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    workflow = AnalysisWorkflowService(
        services.repository, services.artifacts, QueuePlanner([_plan(source.id)])
    )
    started = workflow.start("tenant-a", workspace.id, conversation["id"], source.id, "x", {})

    def reject_dataframe_save(*_args, **_kwargs):
        raise AssertionError("production workflow must publish the disk candidate")

    monkeypatch.setattr(services.artifacts, "save_result", reject_dataframe_save)
    finished = workflow.confirm("tenant-a", started.run["id"])

    assert finished.run["status"] == "completed", finished.run["error_message"]
    result = services.repository.get_artifact(
        "tenant-a", finished.run["result_artifact_id"]
    )
    assert result.path.is_file()
    assert not list(
        (services.artifacts.artifact_store.root / "analysis-candidates").rglob("*.parquet")
    )


def test_resource_limit_error_is_actionable_terminal_and_never_repaired(
    services, workspace, monkeypatch
):
    source = _source(services, workspace)
    conversation = _conversation(services, workspace)
    planner = QueuePlanner([_plan(source.id), _plan(source.id, "store")])
    workflow = AnalysisWorkflowService(services.repository, services.artifacts, planner)
    started = workflow.start("tenant-a", workspace.id, conversation["id"], source.id, "x", {})

    def exhaust_memory(*_args, **_kwargs):
        raise AnalysisResourceError(
            "DuckDB resource limit reached; reduce input size or simplify the plan."
        )

    monkeypatch.setattr(workflow.executor, "prepare", exhaust_memory)
    failed = workflow.confirm("tenant-a", started.run["id"])

    assert failed.run["status"] == "failed"
    assert failed.run["error_kind"] == "terminal"
    assert "reduce input size" in failed.run["error_message"]
    assert failed.attempts[-1].error_kind == "terminal"
    assert len(planner.calls) == 1
