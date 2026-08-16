from __future__ import annotations

import re

from tuoming_agent.security.vault import TokenVault


class SensitiveContentError(ValueError):
    """Raised when plaintext sensitive data is about to leave the local boundary."""


PII_PATTERNS = {
    "email": re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE),
    "phone": re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    "cn_id": re.compile(r"(?<!\d)\d{17}[\dXx](?!\w)"),
}


def _known_value_pattern(value: str) -> re.Pattern[str] | None:
    parts = [part for part in re.split(r"\s+", value.strip()) if part]
    if len("".join(parts)) < 2:
        return None
    return re.compile(r"\s+".join(re.escape(part) for part in parts), re.IGNORECASE)


class PromptSanitizer:
    def __init__(self, vault: TokenVault):
        self.vault = vault

    def sanitize(self, tenant_id: str, text: str) -> str:
        safe_text = text
        known_values = sorted(
            self.vault.iter_plaintext_tokens(tenant_id), key=lambda item: len(item[0]), reverse=True
        )
        for plaintext, token in known_values:
            pattern = _known_value_pattern(plaintext)
            if pattern is not None:
                safe_text = pattern.sub(
                    lambda _match, replacement=token: replacement,
                    safe_text,
                )
        self.assert_safe(safe_text, forbidden_values=[value for value, _ in known_values])
        return safe_text

    def assert_tenant_safe(self, tenant_id: str, text: str) -> None:
        self.assert_safe(
            text,
            forbidden_values=[
                plaintext for plaintext, _token in self.vault.iter_plaintext_tokens(tenant_id)
            ],
        )

    @staticmethod
    def assert_safe(text: str, forbidden_values: list[str] | None = None) -> None:
        for category, pattern in PII_PATTERNS.items():
            if pattern.search(text):
                raise SensitiveContentError(
                    f"Outbound content contains suspected {category}; request was blocked locally."
                )
        for value in forbidden_values or []:
            pattern = _known_value_pattern(value)
            if pattern is not None and pattern.search(text):
                raise SensitiveContentError(
                    "Outbound content still contains a known plaintext mapping value."
                )
