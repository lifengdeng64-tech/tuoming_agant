from __future__ import annotations

import json

import pytest

from tuoming_agent.analysis import planner as planner_module
from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.naming import (
    GeneratedNameValidationError,
    generated_name_issues,
)
from tuoming_agent.analysis.planner import SafeAnalysisPlanner
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

    monkeypatch.setattr(planner_module, "ChatOpenAI", fake_chat_openai)

    SafeAnalysisPlanner(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-pro",
        PromptSanitizer(services.vault),
    )

    assert created[0].structured_calls == [
        (AnalysisPlan, {"method": "json_mode"})
    ]


def test_non_deepseek_keeps_strict_json_schema_output(monkeypatch, services) -> None:
    created = []

    def fake_chat_openai(**kwargs):
        model = FakeChatModel(**kwargs)
        created.append(model)
        return model

    monkeypatch.setattr(planner_module, "ChatOpenAI", fake_chat_openai)

    SafeAnalysisPlanner(
        "key",
        "https://api.openai.com/v1",
        "gpt-5-mini",
        PromptSanitizer(services.vault),
    )

    assert created[0].structured_calls == [
        (AnalysisPlan, {"method": "json_schema"})
    ]


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

    planner.create_plan("group revenue", {"artifact_catalog": []})

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
                "aggregations": [
                    {"column": "revenue", "function": "sum", "output": "本期营收"}
                ],
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
                result_name="revenue report",
                operations=[
                    {
                        "action": "filter",
                        "column": "brand_code",
                        "operator": "eq",
                        "value": token,
                    }
                ],
            ),
            AnalysisPlan(
                input_artifact_id="artifact-id",
                result_name="品牌营收分析",
                operations=[
                    {
                        "action": "filter",
                        "column": "brand_code",
                        "operator": "eq",
                        "value": token,
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

    plan = planner.create_plan("汇总营收", {"artifact_catalog": []})

    assert plan.result_name == "品牌营收分析"
    assert len(model.calls) == 2
    assert "generated_name_feedback" not in json.loads(model.calls[0][1][1])
    feedback = json.loads(model.calls[1][1][1])["generated_name_feedback"]
    assert feedback["issues"] == ["result_name: revenue report"]
    assert "rule" in feedback
    serialized_feedback = json.dumps(feedback, ensure_ascii=False)
    assert "brand_code" not in serialized_feedback
    assert token not in serialized_feedback
    assert "华住" not in serialized_feedback


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
        planner.create_plan("汇总营收", {"artifact_catalog": []})

    assert len(model.calls) == 2
