from __future__ import annotations

import json

from tuoming_agent.analysis.models import AnalysisPlan
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

    assert created[0].structured_calls == [
        (AnalysisPlan, {"method": "json_mode"})
    ]


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
