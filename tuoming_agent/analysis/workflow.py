from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from tuoming_agent.analysis.errors import (
    EXECUTION_FAILED_MESSAGE,
    QUALITY_FAILED_MESSAGE,
    REPAIR_FAILED_MESSAGE,
    RESOURCE_LIMIT_MESSAGE,
    SECURITY_BLOCKED_MESSAGE,
    TERMINAL_FAILED_MESSAGE,
    VALIDATION_FAILED_MESSAGE,
    AnalysisServiceError,
    SecurityPolicyViolation,
)
from tuoming_agent.analysis.executor import (
    AnalysisExecutionError,
    AnalysisExecutor,
    AnalysisResourceError,
)
from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.naming import GeneratedNameValidationError
from tuoming_agent.analysis.quality import AnalysisQualityValidator, QualityReport
from tuoming_agent.security.dlp import SensitiveContentError
from tuoming_agent.storage.errors import AuthorizationError
from tuoming_agent.storage.sqlite import SQLiteRepository
from tuoming_agent.workspace.service import ArtifactService


class Planner(Protocol):
    def create_plan(
        self, safe_request: str, safe_context: dict[str, Any], tenant_id: str
    ) -> AnalysisPlan: ...


@dataclass(frozen=True)
class PlanVersion:
    id: str
    version: int
    reason: str
    feedback: str | None
    plan: AnalysisPlan
    decision: str
    created_at: str
    decided_at: str | None


@dataclass(frozen=True)
class AnalysisAttempt:
    id: str
    plan_version: int
    attempt_number: int
    status: str
    quality_report: QualityReport | None
    error_kind: str | None
    error_message: str | None
    result_artifact_id: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class WorkflowSnapshot:
    run: dict[str, Any]
    plan_versions: tuple[PlanVersion, ...]
    attempts: tuple[AnalysisAttempt, ...]

    @property
    def current_plan(self) -> PlanVersion:
        if not self.plan_versions:
            raise ValueError("Analysis run has no plan version.")
        return self.plan_versions[-1]


class AnalysisWorkflowService:
    def __init__(
        self,
        repository: SQLiteRepository,
        artifact_service: ArtifactService,
        planner: Planner,
        max_repair_attempts: int = 3,
        validator: AnalysisQualityValidator | None = None,
    ):
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts must be non-negative.")
        self.repository = repository
        self.artifact_service = artifact_service
        self.planner = planner
        self.max_repair_attempts = max_repair_attempts
        self.validator = validator or AnalysisQualityValidator()
        self.executor = AnalysisExecutor(artifact_service)

    def start(
        self,
        tenant_id: str,
        workspace_id: str,
        conversation_id: str,
        source_artifact_id: str,
        safe_request: str,
        safe_context: dict[str, Any],
        request_message_id: str | None = None,
    ) -> WorkflowSnapshot:
        run = self.repository.create_analysis_run(
            tenant_id,
            workspace_id,
            conversation_id,
            source_artifact_id,
            safe_request,
            safe_context,
            self.max_repair_attempts,
            request_message_id,
        )
        try:
            plan = self.planner.create_plan(safe_request, safe_context, tenant_id)
            self._assert_selected_source(plan, run)
            self.executor.preflight(tenant_id, workspace_id, plan)
            self.repository.create_analysis_plan_version(
                tenant_id, run["id"], plan.model_dump(mode="json"), "initial"
            )
            self.repository.update_analysis_run(
                tenant_id,
                run["id"],
                expected_status="planning",
                status="awaiting_confirmation",
            )
        except SecurityPolicyViolation:
            self.repository.update_analysis_run(
                tenant_id,
                run["id"],
                expected_status="planning",
                status="security_blocked",
                error_kind="security",
                error_message=SECURITY_BLOCKED_MESSAGE,
            )
        except (GeneratedNameValidationError, SensitiveContentError):
            self.repository.update_analysis_run(
                tenant_id,
                run["id"],
                expected_status="planning",
                status="failed",
                error_kind="validation",
                error_message=VALIDATION_FAILED_MESSAGE,
            )
            raise
        except AnalysisServiceError as exc:
            self.repository.update_analysis_run(
                tenant_id,
                run["id"],
                expected_status="planning",
                status="failed",
                error_kind=exc.error_code,
                error_message=exc.public_message,
            )
            raise
        except Exception as exc:
            public_message = "分析计划生成失败，请重试；如持续失败，请检查模型设置。"
            self.repository.update_analysis_run(
                tenant_id,
                run["id"],
                expected_status="planning",
                status="failed",
                error_kind="terminal",
                error_message=public_message,
            )
            raise AnalysisServiceError(public_message, "planning_internal") from exc
        return self.get_snapshot(tenant_id, run["id"])

    def get_snapshot(self, tenant_id: str, run_id: str) -> WorkflowSnapshot:
        run = self.repository.get_analysis_run(tenant_id, run_id)
        plans = tuple(
            PlanVersion(
                id=item["id"],
                version=item["version"],
                reason=item["reason"],
                feedback=item["feedback"],
                plan=AnalysisPlan.model_validate(item["plan"]),
                decision=item["decision"],
                created_at=item["created_at"],
                decided_at=item["decided_at"],
            )
            for item in self.repository.list_analysis_plan_versions(tenant_id, run_id)
        )
        attempts = tuple(
            AnalysisAttempt(
                id=item["id"],
                plan_version=item["plan_version"],
                attempt_number=item["attempt_number"],
                status=item["status"],
                quality_report=QualityReport.from_dict(item["quality"])
                if item["quality"]
                else None,
                error_kind=item["error_kind"],
                error_message=item["error_message"],
                result_artifact_id=item["result_artifact_id"],
                created_at=item["created_at"],
                completed_at=item["completed_at"],
            )
            for item in self.repository.list_analysis_attempts(tenant_id, run_id)
        )
        return WorkflowSnapshot(run=run, plan_versions=plans, attempts=attempts)

    def latest_for_conversation(
        self, tenant_id: str, workspace_id: str, conversation_id: str
    ) -> WorkflowSnapshot | None:
        runs = self.repository.list_analysis_runs(tenant_id, workspace_id, conversation_id)
        return self.get_snapshot(tenant_id, runs[0]["id"]) if runs else None

    def revise(
        self,
        tenant_id: str,
        run_id: str,
        feedback: str,
        safe_context: dict[str, Any] | None = None,
    ) -> WorkflowSnapshot:
        snapshot = self.get_snapshot(tenant_id, run_id)
        self._require_status(snapshot, "awaiting_confirmation")
        context = dict(safe_context or snapshot.run["context"])
        context["user_feedback"] = feedback
        context["previous_plan"] = snapshot.current_plan.plan.model_dump(mode="json")
        plan = self.planner.create_plan(snapshot.run["safe_request"], context, tenant_id)
        self._assert_selected_source(plan, snapshot.run)
        self.executor.preflight(
            tenant_id, snapshot.run["workspace_id"], plan
        )
        self.repository.decide_analysis_plan_version(
            tenant_id, run_id, snapshot.current_plan.version, "superseded"
        )
        self.repository.create_analysis_plan_version(
            tenant_id, run_id, plan.model_dump(mode="json"), "feedback", feedback
        )
        self.repository.update_analysis_run(
            tenant_id,
            run_id,
            expected_status="awaiting_confirmation",
            status="awaiting_confirmation",
            error_kind=None,
            error_message=None,
        )
        return self.get_snapshot(tenant_id, run_id)

    def reject(self, tenant_id: str, run_id: str) -> WorkflowSnapshot:
        snapshot = self.get_snapshot(tenant_id, run_id)
        self._require_status(snapshot, "awaiting_confirmation")
        self.repository.decide_analysis_plan_version(
            tenant_id, run_id, snapshot.current_plan.version, "rejected"
        )
        self.repository.update_analysis_run(
            tenant_id, run_id, expected_status="awaiting_confirmation", status="rejected"
        )
        return self.get_snapshot(tenant_id, run_id)

    def confirm(self, tenant_id: str, run_id: str) -> WorkflowSnapshot:
        snapshot = self.get_snapshot(tenant_id, run_id)
        self._require_status(snapshot, "awaiting_confirmation")
        plan_version = snapshot.current_plan
        self.repository.decide_analysis_plan_version(
            tenant_id, run_id, plan_version.version, "confirmed"
        )
        self.repository.update_analysis_run(
            tenant_id, run_id, expected_status="awaiting_confirmation", status="executing"
        )
        attempt = self.repository.create_analysis_attempt(tenant_id, run_id, plan_version.version)
        candidate = None

        try:
            self._assert_selected_source(plan_version.plan, snapshot.run)
            if not plan_version.plan.operations:
                artifact = self.repository.get_artifact(
                    tenant_id, plan_version.plan.input_artifact_id
                )
                if artifact.workspace_id != snapshot.run["workspace_id"]:
                    raise SecurityPolicyViolation("Dashboard source is outside the workspace.")
                self.repository.finish_analysis_attempt(
                    tenant_id,
                    attempt["id"],
                    status="completed",
                    result_artifact_id=artifact.id,
                )
                self.repository.update_analysis_run(
                    tenant_id,
                    run_id,
                    expected_status="executing",
                    status="completed",
                    result_artifact_id=artifact.id,
                    error_kind=None,
                    error_message=None,
                )
                return self.get_snapshot(tenant_id, run_id)
            candidate = self.executor.prepare(
                tenant_id, snapshot.run["workspace_id"], plan_version.plan
            )
            self.repository.update_analysis_run(
                tenant_id, run_id, expected_status="executing", status="validating"
            )
            report = self.validator.validate(candidate)
            if not report.passed:
                self.repository.finish_analysis_attempt(
                    tenant_id,
                    attempt["id"],
                    status="failed",
                    quality=report.to_dict(),
                    error_kind="repairable",
                    error_message=QUALITY_FAILED_MESSAGE,
                )
                return self._repair(
                    tenant_id,
                    run_id,
                    "quality_validation_failed",
                    QUALITY_FAILED_MESSAGE,
                    plan_version.plan,
                )

            artifact = self.artifact_service.publish_candidate(
                tenant_id, snapshot.run["workspace_id"], candidate
            )
            self.repository.finish_analysis_attempt(
                tenant_id,
                attempt["id"],
                status="completed",
                quality=report.to_dict(),
                result_artifact_id=artifact.id,
            )
            self.repository.update_analysis_run(
                tenant_id,
                run_id,
                expected_status="validating",
                status="completed",
                result_artifact_id=artifact.id,
                error_kind=None,
                error_message=None,
            )
        except (SecurityPolicyViolation, AuthorizationError):
            self.repository.finish_analysis_attempt(
                tenant_id,
                attempt["id"],
                status="blocked",
                error_kind="security",
                error_message=SECURITY_BLOCKED_MESSAGE,
            )
            self.repository.update_analysis_run(
                tenant_id,
                run_id,
                expected_status=("executing", "validating"),
                status="security_blocked",
                error_kind="security",
                error_message=SECURITY_BLOCKED_MESSAGE,
            )
        except AnalysisExecutionError:
            self.repository.finish_analysis_attempt(
                tenant_id,
                attempt["id"],
                status="failed",
                error_kind="repairable",
                error_message=EXECUTION_FAILED_MESSAGE,
            )
            return self._repair(
                tenant_id,
                run_id,
                "local_execution_failed",
                EXECUTION_FAILED_MESSAGE,
                plan_version.plan,
            )
        except AnalysisResourceError:
            self.repository.finish_analysis_attempt(
                tenant_id,
                attempt["id"],
                status="failed",
                error_kind="terminal",
                error_message=RESOURCE_LIMIT_MESSAGE,
            )
            self.repository.update_analysis_run(
                tenant_id,
                run_id,
                expected_status=("executing", "validating"),
                status="failed",
                error_kind="terminal",
                error_message=RESOURCE_LIMIT_MESSAGE,
            )
        except Exception:
            self.repository.finish_analysis_attempt(
                tenant_id,
                attempt["id"],
                status="failed",
                error_kind="terminal",
                error_message=TERMINAL_FAILED_MESSAGE,
            )
            self.repository.update_analysis_run(
                tenant_id,
                run_id,
                expected_status=("executing", "validating"),
                status="failed",
                error_kind="terminal",
                error_message=TERMINAL_FAILED_MESSAGE,
            )
        finally:
            if candidate is not None:
                candidate.cleanup()
        return self.get_snapshot(tenant_id, run_id)

    def _repair(
        self,
        tenant_id: str,
        run_id: str,
        failure_code: str,
        public_message: str,
        previous_plan: AnalysisPlan,
    ) -> WorkflowSnapshot:
        run = self.repository.get_analysis_run(tenant_id, run_id)
        self.repository.update_analysis_run(
            tenant_id,
            run_id,
            expected_status=("executing", "validating"),
            status="repairable_error",
            error_kind="repairable",
            error_message=public_message,
        )
        if run["repair_count"] >= run["max_repairs"]:
            self.repository.update_analysis_run(
                tenant_id,
                run_id,
                expected_status="repairable_error",
                status="failed",
                error_kind="repairable",
                error_message=public_message,
            )
            return self.get_snapshot(tenant_id, run_id)

        self.repository.update_analysis_run(
            tenant_id,
            run_id,
            expected_status="repairable_error",
            status="repairing",
            error_kind="repairable",
            error_message=public_message,
        )
        context = dict(run["context"])
        context["repair"] = {
            "failure_code": failure_code,
            "previous_plan": previous_plan.model_dump(mode="json"),
            "rule": (
                "Return a different corrected allowlisted plan. Do not infer or request raw "
                "failure details. The corrected plan will require user confirmation."
            ),
        }
        try:
            plan = self.planner.create_plan(run["safe_request"], context, tenant_id)
            self._assert_selected_source(plan, run)
            self.executor.preflight(tenant_id, run["workspace_id"], plan)
            if self._canonical(plan) == self._canonical(previous_plan):
                raise ValueError("Repair returned the same plan.")
            self.repository.create_analysis_plan_version(
                tenant_id, run_id, plan.model_dump(mode="json"), "repair", public_message
            )
            self.repository.update_analysis_run(
                tenant_id,
                run_id,
                expected_status="repairing",
                status="awaiting_confirmation",
                repair_count=run["repair_count"] + 1,
            )
        except SecurityPolicyViolation:
            self.repository.update_analysis_run(
                tenant_id,
                run_id,
                expected_status="repairing",
                status="security_blocked",
                error_kind="security",
                error_message=SECURITY_BLOCKED_MESSAGE,
            )
        except AnalysisServiceError as exc:
            self.repository.update_analysis_run(
                tenant_id,
                run_id,
                expected_status="repairing",
                status="failed",
                error_kind=exc.error_code,
                error_message=exc.public_message,
            )
        except Exception:
            self.repository.update_analysis_run(
                tenant_id,
                run_id,
                expected_status="repairing",
                status="failed",
                error_kind="terminal",
                error_message=REPAIR_FAILED_MESSAGE,
            )
        return self.get_snapshot(tenant_id, run_id)

    @staticmethod
    def _assert_selected_source(plan: AnalysisPlan, run: dict[str, Any]) -> None:
        if plan.input_artifact_id != run["source_artifact_id"]:
            raise SecurityPolicyViolation("计划试图切换到未选择的数据源。")

    @staticmethod
    def _canonical(plan: AnalysisPlan) -> str:
        return json.dumps(plan.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _require_status(snapshot: WorkflowSnapshot, expected: str) -> None:
        if snapshot.run["status"] != expected:
            raise ValueError(f"Analysis run is {snapshot.run['status']}, expected {expected}.")
