from __future__ import annotations

import hashlib
from io import BytesIO

import pandas as pd
import pytest

from tuoming_agent.config import AppConfig
from tuoming_agent.ingestion.service import UnsafeIngestionError
from tuoming_agent.security.masking import ColumnPolicy
from tuoming_agent.workspace.service import create_services


def _csv(rows: list[tuple[str, int]]) -> bytes:
    return pd.DataFrame(rows, columns=["门店名称", "营收"]).to_csv(index=False).encode("utf-8-sig")


def test_append_preserves_conversation_artifacts_and_survives_restart(
    config: AppConfig, services, workspace
):
    conversation = services.repository.get_or_create_conversation("tenant-a", workspace.id)
    first = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "report.csv",
        _csv([("上海店", 100)]),
        {"report": {"门店名称": ColumnPolicy("store")}},
    )
    services.conversations.add_user_message("tenant-a", conversation["id"], "查看数据")
    services.conversations.add_assistant_message(
        "tenant-a", conversation["id"], "数据已载入", first.artifacts[0].id
    )
    second = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "report.csv",
        _csv([("上海店", 100), ("北京店", 200)]),
        {"report": {"门店名称": ColumnPolicy("store")}},
    )

    restarted = create_services(config)
    restored_workspace = restarted.repository.get_workspace("tenant-a", workspace.id)
    messages = restarted.repository.list_messages("tenant-a", conversation["id"])
    datasets = restarted.repository.list_datasets("tenant-a", workspace.id)
    artifacts = restarted.repository.list_artifacts("tenant-a", workspace.id)
    files = restarted.repository.list_files("tenant-a", workspace.id)
    events = restarted.repository.list_audit_events("tenant-a", workspace.id)

    assert restored_workspace.id == workspace.id
    assert [message.safe_content for message in messages] == ["查看数据", "数据已载入"]
    assert datasets[0]["version"] == 2
    assert {item.id for item in artifacts} >= {first.artifacts[0].id, second.artifacts[0].id}
    assert len(files) == 2
    assert [event["event_type"] for event in events] == ["file_ingested", "file_ingested"]

    context = restarted.conversations.build_safe_context(
        "tenant-a",
        workspace.id,
        conversation["id"],
        preferred_artifact_id=first.artifacts[0].id,
    )
    assert context["preferred_artifact_id"] == first.artifacts[0].id
    assert {item["artifact_id"] for item in context["artifact_catalog"]} >= {
        first.artifacts[0].id,
        second.artifacts[0].id,
    }


def test_duplicate_file_is_detected_by_content_hash(services, workspace):
    content = _csv([("上海店", 100)])
    policies = {"report": {"门店名称": ColumnPolicy("store")}}
    first = services.ingestion.ingest("tenant-a", workspace.id, "report.csv", content, policies)
    duplicate = services.ingestion.ingest(
        "tenant-a", workspace.id, "renamed.csv", content, policies
    )
    assert duplicate.duplicate is True
    assert duplicate.file_id == first.file_id
    assert duplicate.artifacts[0].id == first.artifacts[0].id


def test_excel_all_sheets_and_same_name_versions(services, workspace):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"门店名称": ["上海店"]}).to_excel(writer, sheet_name="门店", index=False)
        pd.DataFrame({"客户姓名": ["张三"]}).to_excel(writer, sheet_name="客户", index=False)
    policies = {
        "book::门店": {"门店名称": ColumnPolicy("store")},
        "book::客户": {"客户姓名": ColumnPolicy("person")},
    }
    result = services.ingestion.ingest(
        "tenant-a", workspace.id, "book.xlsx", buffer.getvalue(), policies
    )
    assert len(result.artifacts) == 2
    assert {item.name for item in result.artifacts} == {"book::门店", "book::客户"}


def test_detected_sensitive_column_fails_closed_before_persistence(services, workspace):
    content = _csv([("上海店", 100)])
    with pytest.raises(UnsafeIngestionError):
        services.ingestion.ingest("tenant-a", workspace.id, "unsafe.csv", content, {"unsafe": {}})
    assert services.repository.list_artifacts("tenant-a", workspace.id) == []
    assert (
        services.repository.find_file_by_hash(
            "tenant-a", workspace.id, hashlib.sha256(content).hexdigest()
        )
        is None
    )

