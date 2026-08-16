from __future__ import annotations

import json

import pytest

from tuoming_agent.analysis.errors import (
    AnalysisPlanValidationError,
    AnalysisProviderError,
)
from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.naming import (
    GeneratedNameValidationError,
    generated_name_issues,
)
from tuoming_agent.analysis.planner import SafeAnalysisPlanner
from tuoming_agent.providers import factory as provider_factory
from tuoming_agent.security.dlp import PromptSanitizer


class FakeChatModel:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.structured_calls = []

    def with_structured_output(self, schema, **kwargs):
        self.structured_calls.append((schema, kwargs))
        return self


class RecordingPlanModel:
    def __init__(self, response):
        self.response = response
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return self.response


class SequencePlanModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return self.responses.pop(0)


def test_deepseek_uses_supported_json_object_output(monkeypatch, services) -> None:
    created = []

    def fake_chat_openai(**kwargs):
        model = FakeChatModel(**kwargs)
        created.append(model)
        return model

    monkeypatch.setattr(provider_factory, "ChatOpenAI", fake_chat_openai)

    SafeAnalysisPlanner(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-pro",
        PromptSanitizer(services.vault),
    )

    assert created[0].structured_calls == [(AnalysisPlan, {"method": "json_mode"})]


def test_non_deepseek_keeps_strict_json_schema_output(monkeypatch, services) -> None:
    created = []

    def fake_chat_openai(**kwargs):
        model = FakeChatModel(**kwargs)
        created.append(model)
        return model

    monkeypatch.setattr(provider_factory, "ChatOpenAI", fake_chat_openai)

    SafeAnalysisPlanner(
        "key",
        "https://api.openai.com/v1",
        "gpt-5-mini",
        PromptSanitizer(services.vault),
        provider_name="openai",
    )

    assert created[0].structured_calls == [(AnalysisPlan, {"method": "json_schema"})]


def test_planner_sends_exact_analysis_plan_schema_with_json_request(services) -> None:
    response = AnalysisPlan(
        input_artifact_id="artifact-id",
        operations=[{"action": "head", "rows": 5}],
    )
    model = RecordingPlanModel(response)
    planner = SafeAnalysisPlanner(
        None,
        None,
        "test",
        PromptSanitizer(services.vault),
        model=model,
    )

    planner.create_plan("group revenue", {"artifact_catalog": []}, "tenant-a")

    user_payload = json.loads(model.messages[1][1])
    assert user_payload["analysis_plan_schema"] == AnalysisPlan.model_json_schema()


def test_generated_name_issues_ignore_english_source_references() -> None:
    plan = AnalysisPlan(
        input_artifact_id="artifact-a",
        result_name="revenue report",
        operations=[
            {
                "action": "groupby",
                "by": ["brand_code"],
                "aggregations": [
                    {
                        "column": "revenue",
                        "function": "sum",
                        "output": "current_revenue",
                    }
                ],
            }
        ],
    )

    assert generated_name_issues(plan) == (
        "result_name: revenue report",
        "operations[0].aggregations[0].output: current_revenue",
    )


def test_generated_name_issues_accept_chinese_names_and_digits() -> None:
    plan = AnalysisPlan(
        input_artifact_id="artifact-a",
        result_name="品牌营收分析",
        operations=[
            {
                "action": "groupby",
                "by": ["brand_code"],
                "aggregations": [{"column": "revenue", "function": "sum", "output": "本期营收"}],
            },
            {"action": "derive", "column": "营收同比2026", "expression": "col('revenue') / 2"},
            {"action": "rename", "mapping": {"brand_code": "品牌编码"}},
            {
                "action": "merge",
                "right_artifact_id": "artifact-b",
                "left_on": ["brand_code"],
                "right_on": ["brand_code"],
            },
        ],
    )

    assert generated_name_issues(plan) == ()


def test_generated_name_issues_reject_mixed_and_english_generated_names() -> None:
    plan = AnalysisPlan(
        input_artifact_id="artifact-a",
        result_name="本期_revenue",
        operations=[
            {"action": "derive", "column": "growth_rate", "expression": "col('revenue')"},
            {"action": "rename", "mapping": {"brand_code": "brand_name"}},
            {
                "action": "merge",
                "right_artifact_id": "artifact-b",
                "left_on": ["brand_code"],
                "right_on": ["brand_code"],
                "suffixes": ["_left", "_右表"],
            },
        ],
    )

    assert generated_name_issues(plan) == (
        "result_name: 本期_revenue",
        "operations[0].column: growth_rate",
        "operations[1].mapping[brand_code]: brand_name",
        "operations[2].suffixes[0]: _left",
    )


def test_planner_retries_once_with_safe_generated_name_feedback(services) -> None:
    token = services.vault.tokenize("tenant-a", "brand", "华住")
    model = SequencePlanModel(
        [
            AnalysisPlan(
                input_artifact_id="artifact-id",
                result_name=token,
                operations=[
                    {
                        "action": "groupby",
                        "by": ["brand_code"],
                        "aggregations": [
                            {
                                "column": "revenue",
                                "function": "sum",
                                "output": "current_revenue",
                            }
                        ],
                    }
                ],
            ),
            AnalysisPlan(
                input_artifact_id="artifact-id",
                result_name="品牌营收分析",
                operations=[
                    {
                        "action": "groupby",
                        "by": ["brand_code"],
                        "aggregations": [
                            {"column": "revenue", "function": "sum", "output": "本期营收"}
                        ],
                    }
                ],
            ),
        ]
    )
    planner = SafeAnalysisPlanner(
        None,
        None,
        "test",
        PromptSanitizer(services.vault),
        model=model,
    )

    plan = planner.create_plan("汇总营收", {"artifact_catalog": []}, "tenant-a")

    assert plan.result_name == "品牌营收分析"
    assert len(model.calls) == 2
    assert "generated_name_feedback" not in json.loads(model.calls[0][1][1])
    feedback = json.loads(model.calls[1][1][1])["generated_name_feedback"]
    assert feedback["issues"] == [
        "result_name",
        "operations[0].aggregations[0].output",
    ]
    assert "rule" in feedback
    retry_payload = model.calls[1][1][1]
    assert token not in retry_payload
    assert "华住" not in retry_payload
    assert "current_revenue" not in retry_payload


def test_planner_rejects_second_invalid_generated_name_response(services) -> None:
    invalid = AnalysisPlan(
        input_artifact_id="artifact-id",
        result_name="revenue report",
        operations=[{"action": "head", "rows": 5}],
    )
    model = SequencePlanModel([invalid, invalid])
    planner = SafeAnalysisPlanner(
        None,
        None,
        "test",
        PromptSanitizer(services.vault),
        model=model,
    )

    with pytest.raises(
        GeneratedNameValidationError,
        match="模型未能生成合规的中文字段名称",
    ):
        planner.create_plan("汇总营收", {"artifact_catalog": []}, "tenant-a")

    assert len(model.calls) == 2


class FailingPlanModel:
    def __init__(self, error: Exception):
        self.error = error

    def invoke(self, _messages):
        raise self.error


class ProviderStatusError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def test_planner_classifies_provider_failure_without_exposing_secret(services) -> None:
    api_key = "sk-sensitive-provider-key"
    planner = SafeAnalysisPlanner(
        api_key,
        None,
        "test",
        PromptSanitizer(services.vault),
        model=FailingPlanModel(ProviderStatusError(401, f"invalid api key: {api_key}")),
    )

    with pytest.raises(AnalysisProviderError) as captured:
        planner.create_plan("汇总营收", {"artifact_catalog": []}, "tenant-a")

    assert captured.value.error_code == "provider_auth"
    assert str(captured.value) == "API Key 无效或没有访问权限。"
    assert api_key not in str(captured.value)


def test_planner_reports_invalid_structured_plan_separately(services) -> None:
    planner = SafeAnalysisPlanner(
        None,
        None,
        "test",
        PromptSanitizer(services.vault),
        model=RecordingPlanModel({"input_artifact_id": "artifact-id", "operations": []}),
    )

    with pytest.raises(AnalysisPlanValidationError) as captured:
        planner.create_plan("汇总营收", {"artifact_catalog": []}, "tenant-a")

    assert captured.value.error_code == "plan_validation"
    assert "计划格式不符合安全规则" in str(captured.value)


def test_planner_prompt_defines_weighted_completion_recipe() -> None:
    from tuoming_agent.analysis.planner import PLANNER_SYSTEM_PROMPT

    assert "do not average row-level completion percentages" in PLANNER_SYSTEM_PROMPT
    assert 'operator "ne" and value “临时停业”' in PLANNER_SYSTEM_PROMPT
    assert "summed actual / summed target" in PLANNER_SYSTEM_PROMPT
    assert "current completion / prior completion - 1" in PLANNER_SYSTEM_PROMPT
