import base64

import pytest

from tuoming_agent.config import AppConfig, ConfigurationError


def test_analysis_repair_limit_defaults_to_three(monkeypatch, tmp_path):
    monkeypatch.setenv("MASKING_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    monkeypatch.setenv("TUOMING_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANALYSIS_MAX_REPAIR_ATTEMPTS", raising=False)
    assert AppConfig.from_env().analysis_max_repair_attempts == 3


def test_analysis_repair_limit_must_be_non_negative(monkeypatch):
    monkeypatch.setenv("MASKING_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    monkeypatch.setenv("ANALYSIS_MAX_REPAIR_ATTEMPTS", "-1")
    with pytest.raises(ConfigurationError):
        AppConfig.from_env()


def test_duckdb_temp_reserve_bytes_matches_configured_size(monkeypatch, tmp_path):
    monkeypatch.setenv("MASKING_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    monkeypatch.setenv("TUOMING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "4096MiB")

    assert AppConfig.from_env().duckdb_temp_reserve_bytes == 4 * 1024**3
