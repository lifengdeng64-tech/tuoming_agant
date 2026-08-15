from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from tuoming_agent.models import ColumnLineage
from tuoming_agent.security.vault import TokenVault
from tuoming_agent.storage.errors import RecordNotFoundError


@dataclass(frozen=True)
class ColumnPolicy:
    domain: str
    normalizer: str = "text"


class MaskingService:
    def __init__(self, vault: TokenVault):
        self.vault = vault

    def mask_dataframe(
        self,
        tenant_id: str,
        dataframe: pd.DataFrame,
        policies: dict[str, ColumnPolicy],
    ) -> tuple[pd.DataFrame, dict[str, ColumnLineage]]:
        missing = sorted(set(policies) - set(dataframe.columns))
        if missing:
            raise ValueError(f"Masking policies reference missing columns: {', '.join(missing)}")
        masked = dataframe.copy()
        lineage: dict[str, ColumnLineage] = {}
        for column, policy in policies.items():
            values = masked[column].tolist()
            non_null_values = [value for value in values if not self._is_null(value)]
            tokens = iter(
                self.vault.tokenize_many(
                    tenant_id, policy.domain, non_null_values, policy.normalizer
                )
            )
            masked[column] = [
                value if self._is_null(value) else next(tokens) for value in values
            ]
            lineage[column] = ColumnLineage(
                domain=policy.domain,
                normalizer=policy.normalizer,
                key_version=self.vault.key_version,
            )
        return masked, lineage

    def unmask_dataframe(
        self,
        tenant_id: str,
        dataframe: pd.DataFrame,
        lineage: dict[str, ColumnLineage],
    ) -> pd.DataFrame:
        restored = dataframe.copy()
        for column in lineage:
            if column not in restored.columns:
                continue
            tokens = list(
                dict.fromkeys(
                    value
                    for value in restored[column]
                    if isinstance(value, str) and not self._is_null(value)
                )
            )
            values = self.vault.resolve_many(tenant_id, tokens)
            replacements = dict(zip(tokens, values, strict=True))
            restored[column] = restored[column].map(
                lambda value, replacements=replacements: replacements.get(value, value)
                if isinstance(value, str)
                else value
            )
        return restored

    def restore_display_value(self, tenant_id: str, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self.restore_display_value(tenant_id, item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.restore_display_value(tenant_id, item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.restore_display_value(tenant_id, item) for item in value)
        if isinstance(value, str):
            try:
                return self.vault.resolve(tenant_id, value)
            except RecordNotFoundError:
                return value
        return value

    @staticmethod
    def _is_null(value: Any) -> bool:
        result = pd.isna(value)
        return bool(result) if not hasattr(result, "__len__") else False
