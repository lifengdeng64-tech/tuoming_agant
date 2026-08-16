from __future__ import annotations

import pytest

from tuoming_agent.providers.factory import (
    AnthropicModelProvider,
    GeminiModelProvider,
    OpenAIModelProvider,
    _test_client,
    classify_provider_error,
    create_provider,
)
from tuoming_agent.settings import ModelSettings


@pytest.mark.parametrize(
    ("settings", "provider_type"),
    [
        (
            ModelSettings("openai", "https://api.openai.com/v1", "gpt-5-mini"),
            OpenAIModelProvider,
        ),
        (
            ModelSettings("deepseek", "https://api.deepseek.com", "deepseek-chat"),
            OpenAIModelProvider,
        ),
        (
            ModelSettings("anthropic", "https://api.anthropic.com", "claude-sonnet-4-6"),
            AnthropicModelProvider,
        ),
        (
            ModelSettings(
                "gemini",
                "https://generativelanguage.googleapis.com",
                "gemini-2.5-flash",
            ),
            GeminiModelProvider,
        ),
    ],
)
def test_provider_factory_selects_protocol(settings, provider_type) -> None:
    assert isinstance(create_provider(settings, "test-key"), provider_type)


def test_provider_factory_rejects_invalid_endpoint() -> None:
    settings = ModelSettings("openai_compatible", "file:///local/model", "model")

    with pytest.raises(ValueError, match="Base URL"):
        create_provider(settings, "test-key")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (type("Unauthorized", (Exception,), {"status_code": 401})("bad key"), "API Key"),
        (type("Missing", (Exception,), {"status_code": 404})("model not found"), "模型不存在"),
        (type("NoCredit", (Exception,), {"status_code": 402})("billing"), "额度不足"),
        (TimeoutError("network timeout"), "模型响应超时"),
    ],
)
def test_provider_errors_are_classified_without_raw_service_details(error, expected) -> None:
    assert expected in classify_provider_error(error)


def test_connection_error_never_exposes_api_key() -> None:
    api_key = "sk-secret-that-must-not-leak"

    class FailingClient:
        def invoke(self, _message):
            raise RuntimeError(f"authentication failed for {api_key}")

    result = _test_client(FailingClient(), api_key)

    assert not result.ok
    assert api_key not in result.message
    assert "API Key" in result.message


def test_openai_compatible_uses_json_mode(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def with_structured_output(self, schema, **kwargs):
            calls.append((schema, kwargs))
            return self

    provider = OpenAIModelProvider(
        ModelSettings("openai_compatible", "https://example.test/v1", "model"),
        "key",
    )
    monkeypatch.setattr(provider, "_client", lambda: FakeClient())

    provider.structured_model(dict)

    assert calls == [(dict, {"method": "json_mode"})]
