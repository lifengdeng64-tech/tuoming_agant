from __future__ import annotations

from tuoming_agent.analysis import planner as planner_module
from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.planner import SafeAnalysisPlanner
from tuoming_agent.security.dlp import PromptSanitizer


class FakeChatModel:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.structured_calls = []

    def with_structured_output(self, schema, **kwargs):
        self.structured_calls.append((schema, kwargs))
        return self


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
