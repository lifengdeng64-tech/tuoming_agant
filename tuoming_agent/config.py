from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from tuoming_agent.settings import (
    LocalSettingsManager,
    ModelSettings,
    NetworkSettings,
    default_app_dir,
)


class ConfigurationError(RuntimeError):
    """Raised when a security-sensitive setting is missing or invalid."""


def decode_master_key(encoded: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except Exception as exc:
        raise ConfigurationError(
            "MASKING_MASTER_KEY must be URL-safe base64. Generate it with "
            "`python -m tuoming_agent.keygen`."
        ) from exc
    if len(key) < 32:
        raise ConfigurationError("MASKING_MASTER_KEY must decode to at least 32 bytes.")
    return key


@dataclass(frozen=True)
class AppConfig:
    master_key: bytes
    key_version: int
    data_dir: Path
    default_tenant: str
    app_dir: Path | None = None
    analyst_provider: str = "deepseek"
    analyst_api_key: str | None = None
    analyst_base_url: str | None = None
    analyst_model_name: str = "deepseek-chat"
    analysis_max_repair_attempts: int = 3
    duckdb_memory_limit: str = "2GiB"
    duckdb_threads: int = 4
    duckdb_max_temp_directory_size: str = "4GiB"
    managed_runtime: bool = False
    network_settings: NetworkSettings = NetworkSettings()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "tuoming.sqlite3"

    @property
    def duckdb_temp_reserve_bytes(self) -> int:
        return _duckdb_size_bytes(self.duckdb_max_temp_directory_size)

    @property
    def model_settings(self) -> ModelSettings:
        return ModelSettings(
            provider=self.analyst_provider,
            base_url=self.analyst_base_url or "",
            model_name=self.analyst_model_name,
        )

    @classmethod
    def from_env(cls) -> AppConfig:
        """Developer mode: load explicit environment variables and never create secrets."""
        load_dotenv()
        encoded_key = os.getenv("MASKING_MASTER_KEY", "").strip()
        if not encoded_key:
            raise ConfigurationError(
                "MASKING_MASTER_KEY is required and must remain stable. Generate it once with "
                "`python -m tuoming_agent.keygen`, then store it in .env."
            )
        try:
            key_version = int(os.getenv("MASKING_KEY_VERSION", "1"))
        except ValueError as exc:
            raise ConfigurationError("MASKING_KEY_VERSION must be a positive integer.") from exc
        if key_version < 1:
            raise ConfigurationError("MASKING_KEY_VERSION must be a positive integer.")
        max_repairs, memory_limit, threads, temp_limit = _runtime_limits()
        return cls(
            master_key=decode_master_key(encoded_key),
            key_version=key_version,
            data_dir=Path(os.getenv("TUOMING_DATA_DIR", ".tuoming-data")).resolve(),
            default_tenant=os.getenv("TUOMING_DEFAULT_TENANT", "local-user").strip()
            or "local-user",
            app_dir=default_app_dir(),
            analyst_provider=os.getenv("ANALYST_PROVIDER", "deepseek"),
            analyst_api_key=os.getenv("ANALYST_API_KEY") or None,
            analyst_base_url=os.getenv("ANALYST_BASE_URL") or None,
            analyst_model_name=os.getenv("ANALYST_MODEL_NAME", "deepseek-chat"),
            analysis_max_repair_attempts=max_repairs,
            duckdb_memory_limit=memory_limit,
            duckdb_threads=threads,
            duckdb_max_temp_directory_size=temp_limit,
        )

    @classmethod
    def from_runtime(cls) -> AppConfig:
        """Load developer overrides or initialize a Windows desktop profile automatically."""
        desktop_mode = os.getenv("TUOMING_DESKTOP") == "1"
        if not desktop_mode:
            load_dotenv()
        if not desktop_mode and os.getenv("MASKING_MASTER_KEY", "").strip():
            return cls.from_env()

        app_dir = default_app_dir()
        data_dir = Path(os.getenv("TUOMING_DATA_DIR", app_dir / "data")).expanduser().resolve()
        try:
            settings_manager = LocalSettingsManager(app_dir)
            master_key = settings_manager.load_or_create_master_key(data_dir)
            model_settings = settings_manager.load_model_settings()
            api_key = settings_manager.get_api_key()
            network_settings = settings_manager.load_network_settings()
        except Exception as exc:
            raise ConfigurationError(str(exc)) from exc

        max_repairs, memory_limit, threads, temp_limit = _runtime_limits()
        return cls(
            master_key=master_key,
            key_version=settings_manager.masking_key_version(),
            data_dir=data_dir,
            default_tenant=os.getenv("TUOMING_DEFAULT_TENANT", "local-user").strip()
            or "local-user",
            app_dir=app_dir,
            analyst_provider=model_settings.provider,
            analyst_api_key=api_key,
            analyst_base_url=model_settings.base_url,
            analyst_model_name=model_settings.model_name,
            analysis_max_repair_attempts=max_repairs,
            duckdb_memory_limit=memory_limit,
            duckdb_threads=threads,
            duckdb_max_temp_directory_size=temp_limit,
            managed_runtime=True,
            network_settings=network_settings,
        )


def _runtime_limits() -> tuple[int, str, int, str]:
    try:
        max_repairs = int(os.getenv("ANALYSIS_MAX_REPAIR_ATTEMPTS", "3"))
    except ValueError as exc:
        raise ConfigurationError(
            "ANALYSIS_MAX_REPAIR_ATTEMPTS must be a non-negative integer."
        ) from exc
    if max_repairs < 0:
        raise ConfigurationError("ANALYSIS_MAX_REPAIR_ATTEMPTS must be a non-negative integer.")
    memory_limit = _validated_duckdb_size(
        "DUCKDB_MEMORY_LIMIT", os.getenv("DUCKDB_MEMORY_LIMIT", "2GiB"), 2 * 1024**3
    )
    temp_limit = _validated_duckdb_size(
        "DUCKDB_MAX_TEMP_DIRECTORY_SIZE",
        os.getenv("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "4GiB"),
        4 * 1024**3,
    )
    try:
        threads = int(os.getenv("DUCKDB_THREADS", "4"))
    except ValueError as exc:
        raise ConfigurationError("DUCKDB_THREADS must be an integer from 1 to 4.") from exc
    if not 1 <= threads <= 4:
        raise ConfigurationError("DUCKDB_THREADS must be an integer from 1 to 4.")
    return max_repairs, memory_limit, threads, temp_limit


def _validated_duckdb_size(name: str, value: str, maximum_bytes: int) -> str:
    value = value.strip()
    match = re.fullmatch(r"([1-9][0-9]*)\s?(B|KB|MB|GB|KiB|MiB|GiB)", value)
    if match is None:
        raise ConfigurationError(f"{name} must be a positive byte size within its safe limit.")
    multipliers = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
    }
    if int(match.group(1)) * multipliers[match.group(2)] > maximum_bytes:
        raise ConfigurationError(f"{name} exceeds its safe limit.")
    return value


def _duckdb_size_bytes(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)\s?(B|KB|MB|GB|KiB|MiB|GiB)", value.strip())
    if match is None:
        raise ConfigurationError("DuckDB byte size is invalid.")
    multipliers = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
    }
    return int(match.group(1)) * multipliers[match.group(2)]


def validate_duckdb_settings(config: AppConfig) -> None:
    _validated_duckdb_size("DUCKDB_MEMORY_LIMIT", config.duckdb_memory_limit, 2 * 1024**3)
    _validated_duckdb_size(
        "DUCKDB_MAX_TEMP_DIRECTORY_SIZE",
        config.duckdb_max_temp_directory_size,
        4 * 1024**3,
    )
    if not isinstance(config.duckdb_threads, int) or not 1 <= config.duckdb_threads <= 4:
        raise ConfigurationError("DUCKDB_THREADS must be an integer from 1 to 4.")
