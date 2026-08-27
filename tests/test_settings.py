from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import tuoming_agent.settings as settings_module
from tuoming_agent.security.credentials import MemorySecretStore, WindowsDpapiSecretStore
from tuoming_agent.settings import (
    MASTER_KEY_CREDENTIAL,
    MODEL_API_KEY_CREDENTIAL,
    PROVIDER_BY_ID,
    LocalSettingsManager,
    ModelSettings,
    NetworkSettings,
)


def test_settings_file_never_contains_api_key_or_master_key(tmp_path: Path) -> None:
    secrets = MemorySecretStore()
    manager = LocalSettingsManager(tmp_path, secrets)
    api_key = "sk-local-secret-value"

    manager.save_model_settings(
        ModelSettings(
            provider="openai",
            base_url="https://api.openai.com/v1",
            model_name="gpt-5-mini",
        )
    )
    manager.save_api_key(api_key)
    master_key = manager.load_or_create_master_key(tmp_path / "data")

    settings_text = manager.settings_path.read_text(encoding="utf-8")
    assert api_key not in settings_text
    assert master_key.hex() not in settings_text
    assert secrets.get(MODEL_API_KEY_CREDENTIAL) == api_key.encode()
    assert secrets.get(MASTER_KEY_CREDENTIAL) == master_key


def test_master_key_is_stable_across_manager_instances(tmp_path: Path) -> None:
    secrets = MemorySecretStore()
    first = LocalSettingsManager(tmp_path, secrets)
    second = LocalSettingsManager(tmp_path, secrets)

    first_key = first.load_or_create_master_key(tmp_path / "data")
    second_key = second.load_or_create_master_key(tmp_path / "data")

    assert first_key == second_key
    assert len(first_key) == 32


@pytest.mark.parametrize(
    "protected_path", ["tuoming.sqlite3", "uploads/source.bin", "artifacts/a.parquet"]
)
def test_missing_master_key_never_overwrites_existing_data(
    tmp_path: Path, protected_path: str
) -> None:
    data_dir = tmp_path / "data"
    protected = data_dir / protected_path
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_bytes(b"existing-protected-data")
    manager = LocalSettingsManager(tmp_path / "app", MemorySecretStore())

    with pytest.raises(RuntimeError, match="已有历史数据"):
        manager.load_or_create_master_key(data_dir)


def test_custom_provider_with_incomplete_settings_loads_safely(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "model": {
                    "provider": "openai_compatible",
                    "base_url": "https://models.example.test/v1",
                }
            }
        ),
        encoding="utf-8",
    )

    settings = LocalSettingsManager(tmp_path, MemorySecretStore()).load_model_settings()

    assert settings.provider == "openai_compatible"
    assert settings.model_name == ""


@pytest.mark.parametrize(
    "base_url",
    ["", "api.example.test/v1", "file:///tmp/model", "https:///missing-host"],
)
def test_model_settings_reject_invalid_base_urls(tmp_path: Path, base_url: str) -> None:
    manager = LocalSettingsManager(tmp_path, MemorySecretStore())

    with pytest.raises(ValueError, match="Base URL"):
        manager.save_model_settings(ModelSettings("openai_compatible", base_url, "custom-model"))


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI is Windows-only")
def test_windows_dpapi_round_trip_does_not_write_plaintext(tmp_path: Path) -> None:
    root = tmp_path / "credentials"
    store = WindowsDpapiSecretStore(root)
    secret = b"dpapi-plaintext-must-not-appear"

    store.set("test-credential", secret)

    credential_path = next(root.glob("*.credential"))
    assert secret not in credential_path.read_bytes()
    assert store.get("test-credential") == secret
    store.delete("test-credential")
    assert store.get("test-credential") is None


def test_default_app_dir_respects_explicit_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TUOMING_APP_DIR", str(tmp_path))

    from tuoming_agent.settings import default_app_dir

    assert default_app_dir() == tmp_path.resolve()


def test_network_settings_reject_plaintext_proxy_credentials(tmp_path: Path) -> None:
    manager = LocalSettingsManager(tmp_path, MemorySecretStore())
    with pytest.raises(ValueError, match="代理凭据"):
        manager.save_network_settings(NetworkSettings(proxy_url="http://user:secret@proxy.test"))


def test_network_settings_validate_and_persist_enterprise_ca(tmp_path: Path) -> None:
    ca_path = tmp_path / "enterprise.crt"
    ca_path.write_text("test-ca", encoding="utf-8")
    manager = LocalSettingsManager(tmp_path / "app", MemorySecretStore())
    settings = NetworkSettings(False, "http://proxy.example.test:8080", str(ca_path))
    manager.save_network_settings(settings)
    assert manager.load_network_settings() == NetworkSettings(
        False, "http://proxy.example.test:8080", str(ca_path.resolve())
    )


def test_deepseek_picker_uses_current_official_model_names() -> None:
    provider = PROVIDER_BY_ID["deepseek"]
    model_display_name = getattr(settings_module, "model_display_name", None)

    assert callable(model_display_name), "模型选择器尚未提供官方名称转换"

    options = [(model_id, model_display_name("deepseek", model_id)) for model_id in provider.models]

    assert options == [
        ("deepseek-v4-pro", "DeepSeek-V4-Pro"),
        ("deepseek-v4-flash", "DeepSeek-V4-Flash"),
    ]


@pytest.mark.parametrize(
    ("provider_id", "model_id", "official_name"),
    [
        ("openai", "gpt-5.2", "GPT-5.2"),
        ("anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6"),
        ("gemini", "gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview"),
        ("qwen", "qwen3-max", "Qwen3-Max"),
        ("zhipu", "glm-5", "GLM-5"),
    ],
)
def test_model_picker_preserves_official_brand_capitalization(
    provider_id: str, model_id: str, official_name: str
) -> None:
    model_display_name = getattr(settings_module, "model_display_name", None)

    assert callable(model_display_name), "模型选择器尚未提供官方名称转换"
    assert model_display_name(provider_id, model_id) == official_name
