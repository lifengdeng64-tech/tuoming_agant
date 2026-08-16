from __future__ import annotations

import json

import pandas as pd
import pytest

from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.analysis.planner import SafeAnalysisPlanner
from tuoming_agent.models import ArtifactRecord, utc_now
from tuoming_agent.security.dlp import PromptSanitizer, SensitiveContentError
from tuoming_agent.storage.errors import AuthorizationError, RecordNotFoundError


class RecordingModel:
    def __init__(self, response: AnalysisPlan):
        self.response = response
        self.messages = None

    def invoke(self, messages):
        self.messages = messages
        return self.response


def test_outbound_model_request_contains_no_plaintext_pii(services, workspace):
    services.vault.tokenize("tenant-a", "phone", "13800138000", "phone")
    sanitizer = PromptSanitizer(services.vault)
    safe_request = sanitizer.sanitize("tenant-a", "筛选手机号 13800138000")
    plan = AnalysisPlan(
        input_artifact_id="artifact-id",
        operations=[{"action": "head", "rows": 5}],
    )
    model = RecordingModel(plan)
    planner = SafeAnalysisPlanner(None, None, "test", sanitizer, model=model)
    planner.create_plan(
        safe_request, {"artifact_catalog": [], "recent_messages": []}, "tenant-a"
    )
    serialized = json.dumps(model.messages, ensure_ascii=False)
    assert "13800138000" not in serialized
    assert "PHONE_V1_" in serialized


def test_planner_rejects_sensitive_content_in_model_output(services):
    sanitizer = PromptSanitizer(services.vault)
    plan = AnalysisPlan(
        input_artifact_id="artifact-id",
        result_name="private@example.com",
        operations=[{"action": "head", "rows": 5}],
    )
    planner = SafeAnalysisPlanner(None, None, "test", sanitizer, model=RecordingModel(plan))
    with pytest.raises(SensitiveContentError):
        planner.create_plan("safe request", {}, "tenant-a")


def test_unknown_plaintext_pii_is_blocked(services):
    sanitizer = PromptSanitizer(services.vault)
    with pytest.raises(SensitiveContentError):
        sanitizer.sanitize("tenant-a", "联系 13900139000")
    with pytest.raises(SensitiveContentError):
        sanitizer.sanitize("tenant-a", "发送到 private@example.com")


def test_known_text_variants_are_replaced_before_model_request(services):
    token = services.vault.tokenize("tenant-a", "store", "ABC Store", "casefold")
    sanitizer = PromptSanitizer(services.vault)
    safe = sanitizer.sanitize("tenant-a", "筛选 abc    store 的记录")
    assert safe == f"筛选 {token} 的记录"


def test_tenant_plaintext_check_blocks_case_and_whitespace_variants(services):
    services.vault.tokenize("tenant-a", "store", "ABC Store", "casefold")
    sanitizer = PromptSanitizer(services.vault)

    for leaked_text in (
        "结果包含 abc    store",
        "结果包含 ABC\tSTORE",
        "结果包含 AbC\nStore",
    ):
        with pytest.raises(SensitiveContentError):
            sanitizer.assert_tenant_safe("tenant-a", leaked_text)


def test_tenant_cannot_read_or_restore_other_tenant_data(services, workspace):
    token = services.vault.tokenize("tenant-a", "person", "张三")
    artifact_id = "private-artifact"
    path = services.artifacts.artifact_store.write_dataframe(
        "tenant-a", workspace.id, artifact_id, pd.DataFrame({"姓名": [token]})
    )
    services.repository.create_artifact(
        ArtifactRecord(
            id=artifact_id,
            tenant_id="tenant-a",
            workspace_id=workspace.id,
            kind="dataset",
            name="private",
            path=path,
            row_count=1,
            schema={"columns": [{"name": "姓名", "dtype": "object"}]},
            created_at=utc_now(),
        )
    )
    with pytest.raises(AuthorizationError):
        services.repository.get_artifact("tenant-b", artifact_id)
    with pytest.raises(RecordNotFoundError):
        services.vault.resolve("tenant-b", token)
