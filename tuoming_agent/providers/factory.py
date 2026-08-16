from __future__ import annotations

import ssl
from typing import Any
from urllib.parse import urlparse

import httpx
from langchain_openai import ChatOpenAI

from tuoming_agent.providers.base import AnalysisModelProvider, ProviderConnectionResult
from tuoming_agent.settings import PROVIDER_BY_ID, ModelSettings, NetworkSettings


class OpenAIModelProvider:
    def __init__(
        self, settings: ModelSettings, api_key: str, network: NetworkSettings | None = None
    ):
        self.settings = settings
        self.api_key = api_key
        self.network = network or NetworkSettings()

    def _client(self) -> Any:
        return ChatOpenAI(
            api_key=self.api_key,
            base_url=self.settings.base_url,
            model=self.settings.model_name,
            temperature=0,
            max_retries=0,
            timeout=20,
            http_client=_http_client(self.network),
        )

    def structured_model(self, schema: type[Any]) -> Any:
        method = "json_schema" if self.settings.provider == "openai" else "json_mode"
        return self._client().with_structured_output(schema, method=method)

    def test_connection(self) -> ProviderConnectionResult:
        return _test_client(self._client(), self.api_key)


class AnthropicModelProvider:
    def __init__(
        self, settings: ModelSettings, api_key: str, network: NetworkSettings | None = None
    ):
        self.settings = settings
        self.api_key = api_key
        self.network = network or NetworkSettings()

    def _client(self) -> Any:
        try:
            from langchain_anthropic import ChatAnthropic
        except ImportError as exc:
            raise RuntimeError("当前安装缺少 Claude Provider，请安装桌面完整依赖。") from exc
        options: dict[str, Any] = {}
        if self.network.proxy_url:
            options["anthropic_proxy"] = self.network.proxy_url
        return ChatAnthropic(
            api_key=self.api_key,
            base_url=self.settings.base_url,
            model_name=self.settings.model_name,
            temperature=0,
            max_retries=0,
            timeout=20,
            **options,
        )

    def structured_model(self, schema: type[Any]) -> Any:
        return self._client().with_structured_output(schema)

    def test_connection(self) -> ProviderConnectionResult:
        return _test_client(self._client(), self.api_key)


class GeminiModelProvider:
    def __init__(
        self, settings: ModelSettings, api_key: str, network: NetworkSettings | None = None
    ):
        self.settings = settings
        self.api_key = api_key
        self.network = network or NetworkSettings()

    def _client(self) -> Any:
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as exc:
            raise RuntimeError("当前安装缺少 Gemini Provider，请安装桌面完整依赖。") from exc
        parsed_url = urlparse(self.settings.base_url)
        client_options = None
        if parsed_url.netloc != "generativelanguage.googleapis.com":
            client_options = {"api_endpoint": parsed_url.netloc}
        return ChatGoogleGenerativeAI(
            api_key=self.api_key,
            model=self.settings.model_name,
            temperature=0,
            retries=0,
            request_timeout=20,
            client_options=client_options,
        )

    def structured_model(self, schema: type[Any]) -> Any:
        return self._client().with_structured_output(schema)

    def test_connection(self) -> ProviderConnectionResult:
        return _test_client(self._client(), self.api_key)


def create_provider(
    settings: ModelSettings, api_key: str, network: NetworkSettings | None = None
) -> AnalysisModelProvider:
    if not api_key.strip():
        raise ValueError("API Key 不能为空。")
    definition = PROVIDER_BY_ID.get(settings.provider)
    if definition is None:
        raise ValueError("不支持的模型服务商。")
    parsed_url = urlparse(settings.base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("Base URL 无效，请填写完整的 HTTP 或 HTTPS 地址。")
    network = network or NetworkSettings()
    if definition.protocol == "anthropic":
        return AnthropicModelProvider(settings, api_key, network)
    if definition.protocol == "gemini":
        return GeminiModelProvider(settings, api_key, network)
    return OpenAIModelProvider(settings, api_key, network)


def _http_client(network: NetworkSettings) -> httpx.Client:
    verify: bool | ssl.SSLContext = True
    if network.ca_bundle_path:
        verify = ssl.create_default_context(cafile=network.ca_bundle_path)
    return httpx.Client(
        proxy=network.proxy_url or None,
        verify=verify,
        trust_env=network.use_system_proxy,
        follow_redirects=False,
    )


def _test_client(client: Any, api_key: str) -> ProviderConnectionResult:
    try:
        client.invoke("Reply with only OK.")
    except Exception as exc:
        return ProviderConnectionResult(False, classify_provider_error(exc, api_key))
    return ProviderConnectionResult(True, "模型连接成功")


def classify_provider_error(exc: Exception, api_key: str = "") -> str:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    raw_message = str(exc)
    if api_key:
        raw_message = raw_message.replace(api_key, "***")
    message = raw_message.casefold()
    if status_code in {401, 403} or any(
        token in message for token in ("invalid api key", "authentication", "unauthorized")
    ):
        return "API Key 无效或没有访问权限。"
    if status_code == 404 or any(
        token in message for token in ("model not found", "does not exist", "unknown model")
    ):
        return "模型不存在或当前账号没有该模型的访问权限。"
    if status_code == 402 or any(
        token in message for token in ("insufficient balance", "insufficient quota", "billing")
    ):
        return "API 余额或额度不足。"
    if any(token in message for token in ("certificate verify", "tls", "ssl")):
        return "TLS 证书验证失败，请检查企业 CA 文件或 TLS 检查设备。"
    if any(token in message for token in ("proxy", "407")):
        return "企业代理连接失败，请检查 Windows 系统代理或代理地址。"
    if any(token in message for token in ("connection", "connect", "timeout", "network")):
        return "网络连接失败，请检查网络、代理和防火墙设置。"
    if any(token in message for token in ("base url", "invalid url", "name resolution")):
        return "Base URL 无效或无法访问。"
    if status_code:
        return f"服务商返回错误（HTTP {status_code}）。"
    return "模型连接失败，请核对服务商、模型名称与 Base URL。"