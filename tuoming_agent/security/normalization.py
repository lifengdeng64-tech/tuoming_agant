from __future__ import annotations

import re
import unicodedata
from typing import Any

SUPPORTED_NORMALIZERS = {"text", "casefold", "phone", "identifier"}


def normalize_value(value: Any, normalizer: str = "text") -> str:
    if normalizer not in SUPPORTED_NORMALIZERS:
        raise ValueError(f"Unsupported normalizer: {normalizer}")
    if hasattr(value, "item"):
        value = value.item()
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"\s+", " ", text)

    if normalizer in {"text", "casefold"}:
        return text.casefold()
    if normalizer == "phone":
        digits = re.sub(r"\D", "", text)
        if digits.startswith("00"):
            digits = digits[2:]
        return digits
    if normalizer == "identifier":
        return re.sub(r"[\s_-]+", "", text).casefold()
    raise AssertionError("Normalizer validation is incomplete.")


def normalize_domain(domain: str) -> str:
    normalized = unicodedata.normalize("NFKC", domain).strip().upper()
    normalized = re.sub(r"[^A-Z0-9]+", "_", normalized).strip("_")
    if not normalized:
        raise ValueError("Masking domain must contain letters or numbers.")
    return normalized[:32]

