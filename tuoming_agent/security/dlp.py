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


class PromptSanitizer:
    def __init__(self, vault: TokenVault):
        self.vault = vault

    def sanitize(self, tenant_id: str, text: str) -> str:
        safe_text = text
        known_values = sorted(
            self.vault.iter_plaintext_tokens(tenant_id), key=lambda item: len(item[0]), reverse=True
        )
        for plaintext, token in known_values:
            parts = [re.escape(part) for part in re.split(r"\s+", plaintext.strip()) if part]
            if parts:
                safe_text = re.sub(
                    r"\s+".join(parts),
                    lambda _match, replacement=token: replacement,
                    safe_text,
                    flags=re.IGNORECASE,
                )
        self.assert_safe(safe_text, forbidden_values=[value for value, _ in known_values])
        return safe_text

    @staticmethod
    def assert_safe(text: str, forbidden_values: list[str] | None = None) -> None:
        for category, pattern in PII_PATTERNS.items():
            if pattern.search(text):
                raise SensitiveContentError(
                    f"Outbound content contains suspected {category}; request was blocked locally."
                )
        for value in forbidden_values or []:
            if len(value) >= 2 and value in text:
                raise SensitiveContentError(
                    "Outbound content still contains a known plaintext mapping value."
                )
