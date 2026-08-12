from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


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
    analyst_api_key: str | None = None
    analyst_base_url: str | None = None
    analyst_model_name: str = "deepseek-chat"
    analysis_max_repair_attempts: int = 3
    duckdb_memory_limit: str = "2GB"
    duckdb_threads: int = 4
    duckdb_max_temp_directory_size: str = "4GB"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "tuoming.sqlite3"

    @classmethod
    def from_env(cls) -> AppConfig:
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
        try:
            max_repairs = int(os.getenv("ANALYSIS_MAX_REPAIR_ATTEMPTS", "3"))
        except ValueError as exc:
            raise ConfigurationError(
                "ANALYSIS_MAX_REPAIR_ATTEMPTS must be a non-negative integer."
            ) from exc
        if max_repairs < 0:
            raise ConfigurationError("ANALYSIS_MAX_REPAIR_ATTEMPTS must be a non-negative integer.")
        duckdb_memory_limit = _validated_duckdb_size(
            "DUCKDB_MEMORY_LIMIT", os.getenv("DUCKDB_MEMORY_LIMIT", "2GB"), 2 * 1024**3
        )
        duckdb_max_temp_directory_size = _validated_duckdb_size(
            "DUCKDB_MAX_TEMP_DIRECTORY_SIZE",
            os.getenv("DUCKDB_MAX_TEMP_DIRECTORY_SIZE", "4GB"),
            4 * 1024**3,
        )
        try:
            duckdb_threads = int(os.getenv("DUCKDB_THREADS", "4"))
        except ValueError as exc:
            raise ConfigurationError("DUCKDB_THREADS must be an integer from 1 to 4.") from exc
        if not 1 <= duckdb_threads <= 4:
            raise ConfigurationError("DUCKDB_THREADS must be an integer from 1 to 4.")

        return cls(
            master_key=decode_master_key(encoded_key),
            key_version=key_version,
            data_dir=Path(os.getenv("TUOMING_DATA_DIR", ".tuoming-data")).resolve(),
            default_tenant=os.getenv("TUOMING_DEFAULT_TENANT", "local-user").strip()
            or "local-user",
            analyst_api_key=os.getenv("ANALYST_API_KEY") or None,
            analyst_base_url=os.getenv("ANALYST_BASE_URL") or None,
            analyst_model_name=os.getenv("ANALYST_MODEL_NAME", "deepseek-chat"),
            analysis_max_repair_attempts=max_repairs,
            duckdb_memory_limit=duckdb_memory_limit,
            duckdb_threads=duckdb_threads,
            duckdb_max_temp_directory_size=duckdb_max_temp_directory_size,
        )


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
