from __future__ import annotations

import pandas as pd

from tuoming_agent.config import AppConfig
from tuoming_agent.security.masking import ColumnPolicy
from tuoming_agent.security.vault import TokenVault
from tuoming_agent.workspace.service import create_services


def test_same_domain_value_is_stable_across_files_and_restart(config: AppConfig, services):
    first, _ = services.masking.mask_dataframe(
        "tenant-a",
        pd.DataFrame({"门店名称": ["上海店"]}),
        {"门店名称": ColumnPolicy("store")},
    )
    second, _ = services.masking.mask_dataframe(
        "tenant-a",
        pd.DataFrame({"酒店名称": ["  上海店  "]}),
        {"酒店名称": ColumnPolicy("store")},
    )
    restarted = create_services(config)
    third, _ = restarted.masking.mask_dataframe(
        "tenant-a",
        pd.DataFrame({"门店名": ["上海店"]}),
        {"门店名": ColumnPolicy("store")},
    )

    token = first.loc[0, "门店名称"]
    assert token == second.loc[0, "酒店名称"] == third.loc[0, "门店名"]
    assert token.startswith("STORE_V1_")
    assert len(token.rsplit("_", 1)[1]) >= 16


def test_tenant_domain_normalization_and_key_version_are_scoped(config, services):
    base = services.vault.tokenize("tenant-a", "store", "ABC Store", "casefold")
    normalized = services.vault.tokenize("tenant-a", "store", "  abc   store ", "casefold")
    other_tenant = services.vault.tokenize("tenant-b", "store", "ABC Store", "casefold")
    other_domain = services.vault.tokenize("tenant-a", "customer", "ABC Store", "casefold")
    version_two = TokenVault(services.repository, config.master_key, key_version=2).tokenize(
        "tenant-a", "store", "ABC Store", "casefold"
    )

    assert base == normalized
    assert len({base, other_tenant, other_domain, version_two}) == 4
    assert "_V2_" in version_two


def test_phone_and_identifier_normalizers(services):
    phone_a = services.vault.tokenize("tenant-a", "phone", "+86 138-0013-8000", "phone")
    phone_b = services.vault.tokenize("tenant-a", "phone", "8613800138000", "phone")
    identifier_a = services.vault.tokenize("tenant-a", "customer_id", " AB-_ 100 ", "identifier")
    identifier_b = services.vault.tokenize("tenant-a", "customer_id", "ab100", "identifier")
    assert phone_a == phone_b
    assert identifier_a == identifier_b


def test_collision_expands_token(monkeypatch, services):
    original = TokenVault._candidate_token

    def colliding_candidate(domain, version, digest, byte_length):
        if byte_length == 10:
            return f"{domain}_V{version}_FORCEDCOLLISION"
        return original(domain, version, digest, byte_length)

    monkeypatch.setattr(TokenVault, "_candidate_token", staticmethod(colliding_candidate))
    first = services.vault.tokenize("tenant-a", "store", "first")
    second = services.vault.tokenize("tenant-a", "store", "second")
    assert first != second
    assert second.startswith("STORE_V1_")


def test_unmasking_only_uses_declared_lineage(services):
    source = pd.DataFrame({"门店": ["上海店"], "备注": ["STORE_V1_NOT_A_REAL_TOKEN"]})
    masked, lineage = services.masking.mask_dataframe(
        "tenant-a", source, {"门店": ColumnPolicy("store")}
    )
    restored = services.masking.unmask_dataframe("tenant-a", masked, lineage)
    assert restored.loc[0, "门店"] == "上海店"
    assert restored.loc[0, "备注"] == "STORE_V1_NOT_A_REAL_TOKEN"
