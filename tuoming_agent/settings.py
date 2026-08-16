from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from tuoming_agent.security.credentials import SecretStore, get_default_secret_store

MASTER_KEY_CREDENTIAL = "masking-master-key-v1"
MODEL_API_KEY_CREDENTIAL = "analysis-model-api-key"


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    label: str
    base_url: str
    models: tuple[str, ...]
    protocol: str


PROVIDERS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com",
        ("deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"),
        "openai_compatible",
    ),
    ProviderDefinition(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        ("gpt-5.2", "gpt-5-mini", "gpt-4.1"),
        "openai",
    ),
    ProviderDefinition(
        "anthropic",
        "Anthropic Claude",
        "https://api.anthropic.com",
        ("claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"),
        "anthropic",
    ),
    ProviderDefinition(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com",
        ("gemini-3.1-pro-preview", "gemini-3-flash-preview", "gemini-2.5-flash"),
        "gemini",
    ),
    ProviderDefinition(
        "qwen",
        "通义千问",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ("qwen3-max", "qwen3-coder-plus", "qwen-plus"),
        "openai_compatible",
    ),
    ProviderDefinition(
        "zhipu",
        "智谱",
        "https://open.bigmodel.cn/api/paas/v4/",
        ("glm-5", "glm-4.7", "glm-4.5-air"),
        "openai_compatible",
    ),
    ProviderDefinition(
        "openai_compatible",
        "自定义 OpenAI Compatible API",
        "",
        (),
        "openai_compatible",
    ),
)
PROVIDER_BY_ID = {provider.id: provider for provider in PROVIDERS}


@dataclass(frozen=True)
class ModelSettings:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-pro"

    @property
    def definition(self) -> ProviderDefinition:
        return PROVIDER_BY_ID.get(self.provider, PROVIDER_BY_ID["openai_compatible"])


@dataclass(frozen=True)
class NetworkSettings:
    use_system_proxy: bool = True
    proxy_url: str = ""
    ca_bundle_path: str = ""


def default_app_dir() -> Path:
    override = os.getenv("TUOMING_APP_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return (base / "TuomingAgent").resolve()


class LocalSettingsManager:
    """Persists non-sensitive settings and delegates secrets to the OS credential store."""

    def __init__(self, app_dir: Path, secret_store: SecretStore | None = None):
        self.app_dir = Path(app_dir)
        self.settings_path = self.app_dir / "settings.json"
        self.secret_store = secret_store or get_default_secret_store(self.app_dir)

    def load_model_settings(self) -> ModelSettings:
        payload = self._read_settings().get("model", {})
        provider_id = str(payload.get("provider", "deepseek"))
        definition = PROVIDER_BY_ID.get(provider_id, PROVIDER_BY_ID["deepseek"])
        base_url = str(payload.get("base_url", definition.base_url)).strip()
        default_model = definition.models[0] if definition.models else ""
        model_name = str(payload.get("model_name", default_model)).strip()
        return ModelSettings(provider=definition.id, base_url=base_url, model_name=model_name)

    def load_network_settings(self) -> NetworkSettings:
        payload = self._read_settings().get("network", {})
        return NetworkSettings(
            use_system_proxy=bool(payload.get("use_system_proxy", True)),
            proxy_url=str(payload.get("proxy_url", "")).strip(),
            ca_bundle_path=str(payload.get("ca_bundle_path", "")).strip(),
        )

    def save_network_settings(self, settings: NetworkSettings) -> None:
        proxy_url = settings.proxy_url.strip()
        if proxy_url:
            parsed = urlparse(proxy_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("代理地址必须是有效的 HTTP 或 HTTPS URL。")
            if parsed.username or parsed.password:
                raise ValueError("代理凭据不能写入普通设置，请使用 Windows 系统代理。")
        ca_bundle_path = settings.ca_bundle_path.strip()
        if ca_bundle_path:
            ca_path = Path(ca_bundle_path).expanduser().resolve()
            if ca_path.suffix.casefold() not in {".pem", ".crt"} or not ca_path.is_file():
                raise ValueError("企业 CA 必须是存在的 .pem 或 .crt 文件。")
            ca_bundle_path = str(ca_path)
        payload = self._read_settings()
        payload["network"] = asdict(
            NetworkSettings(settings.use_system_proxy, proxy_url, ca_bundle_path)
        )
        self._write_settings(payload)
    def save_model_settings(self, settings: ModelSettings) -> None:
        if settings.provider not in PROVIDER_BY_ID:
            raise ValueError("不支持的模型服务商。")
        parsed_url = urlparse(settings.base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("Base URL 必须是有效的 HTTP 或 HTTPS 地址。")
        if not settings.model_name.strip():
            raise ValueError("模型名称不能为空。")
        payload = self._read_settings()
        payload["model"] = asdict(settings)
        payload.setdefault("security", {})["masking_key_version"] = self.masking_key_version()
        self._write_settings(payload)

    def get_api_key(self) -> str | None:
        value = self.secret_store.get(MODEL_API_KEY_CREDENTIAL)
        return value.decode("utf-8") if value else None

    def save_api_key(self, api_key: str) -> None:
        value = api_key.strip()
        if not value:
            raise ValueError("API Key 不能为空。")
        self.secret_store.set(MODEL_API_KEY_CREDENTIAL, value.encode("utf-8"))

    def clear_api_key(self) -> None:
        self.secret_store.delete(MODEL_API_KEY_CREDENTIAL)

    def masking_key_version(self) -> int:
        value = self._read_settings().get("security", {}).get("masking_key_version", 1)
        try:
            version = int(value)
        except (TypeError, ValueError):
            return 1
        return max(version, 1)

    def load_or_create_master_key(self, data_dir: Path) -> bytes:
        existing = self.secret_store.get(MASTER_KEY_CREDENTIAL)
        if existing:
            if len(existing) < 32:
                raise RuntimeError("本机保存的脱敏主密钥已损坏，禁止自动覆盖。")
            return existing
        if self._protected_data_exists(Path(data_dir)):
            raise RuntimeError(
                "检测到已有历史数据，但 Windows 安全凭据中的脱敏主密钥缺失。"
                "\u4e3a\u907f\u514d\u5386\u53f2\u6570\u636e\u6c38\u4e45\u65e0\u6cd5\u8fd8\u539f\uff0c\u7a0b\u5e8f\u4e0d\u4f1a\u81ea\u52a8\u751f\u6210\u65b0\u5bc6\u94a5\u3002"
                "\u8bf7\u6062\u590d\u539f Windows \u7528\u6237\u51ed\u636e\u6216\u5907\u4efd\u3002"
            )
        master_key = secrets.token_bytes(32)
        self.secret_store.set(MASTER_KEY_CREDENTIAL, master_key)
        payload = self._read_settings()
        payload.setdefault("security", {})["masking_key_version"] = 1
        self._write_settings(payload)
        return master_key

    def _read_settings(self) -> dict:
        if not self.settings_path.exists():
            return {}
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("本地设置文件损坏，无法安全启动。") from exc
        if not isinstance(value, dict):
            raise RuntimeError("本地设置文件格式无效。")
        return value

    def _write_settings(self, payload: dict) -> None:
        self.app_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".settings-", dir=self.app_dir)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.settings_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _protected_data_exists(data_dir: Path) -> bool:
        if not data_dir.exists():
            return False
        database = data_dir / "tuoming.sqlite3"
        if database.exists() and database.stat().st_size > 0:
            return True
        for directory_name in ("uploads", "artifacts"):
            directory = data_dir / directory_name
            if directory.exists() and any(path.is_file() for path in directory.rglob("*")):
                return True
        return False
