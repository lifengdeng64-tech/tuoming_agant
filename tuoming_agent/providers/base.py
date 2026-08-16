from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderConnectionResult:
    ok: bool
    message: str


class AnalysisModelProvider(Protocol):
    def structured_model(self, schema: type[Any]) -> Any: ...

    def test_connection(self) -> ProviderConnectionResult: ...
