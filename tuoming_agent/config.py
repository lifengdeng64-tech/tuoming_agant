from __future__ import annotations

import base64
import os
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

        return cls(
            master_key=decode_master_key(encoded_key),
            key_version=key_version,
            data_dir=Path(os.getenv("TUOMING_DATA_DIR", ".tuoming-data")).resolve(),
            default_tenant=os.getenv("TUOMING_DEFAULT_TENANT", "local-user").strip()
            or "local-user",
            analyst_api_key=os.getenv("ANALYST_API_KEY") or None,
            analyst_base_url=os.getenv("ANALYST_BASE_URL") or None,
            analyst_model_name=os.getenv("ANALYST_MODEL_NAME", "deepseek-chat"),
        )

