from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from tuoming_agent.models import ColumnLineage
from tuoming_agent.security.vault import TokenVault


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
            masked[column] = masked[column].map(
                lambda value, selected_policy=policy: value
                if self._is_null(value)
                else self.vault.tokenize(
                    tenant_id,
                    selected_policy.domain,
                    value,
                    selected_policy.normalizer,
                )
            )
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
            restored[column] = restored[column].map(
                lambda value: value
                if self._is_null(value) or not isinstance(value, str)
                else self.vault.resolve(tenant_id, value)
            )
        return restored

    @staticmethod
    def _is_null(value: Any) -> bool:
        result = pd.isna(value)
        return bool(result) if not hasattr(result, "__len__") else False
