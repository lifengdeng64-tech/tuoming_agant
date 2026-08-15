from __future__ import annotations

import sqlite3
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.storage.errors import AuthorizationError, RecordNotFoundError
from tuoming_agent.storage.sqlite import DeletionImpact, SQLiteRepository
from tuoming_agent.workspace.data_sources import DataSourceDeletionError


def _source_graph(services, workspace):
    content = pd.DataFrame({"事业部": ["华东"], "营收": [100]}).to_csv(index=False).encode()
    ingested = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "source.csv",
        content,
        {"source": {}},
        retained_columns={"source": {"事业部", "营收"}},
    )
    source = ingested.artifacts[0]
    direct = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "direct",
        pd.DataFrame({"营收": [100]}),
        {},
        (source.id,),
    )
    descendant = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "descendant",
        pd.DataFrame({"营收": [100]}),
        {},
        (direct.id,),
    )
    conversation = services.repository.get_or_create_conversation("tenant-a", workspace.id)
    request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "汇总营收"
    )
    run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        source.id,
        request.safe_content,
        {},
        3,
        request_message_id=request.id,
    )
    services.conversations.add_assistant_message(
        "tenant-a",
        conversation["id"],
        "result ready",
        descendant.id,
        analysis_run_id=run["id"],
    )
    return ingested, source, direct, descendant, conversation, run


def _two_sheet_graph(services, workspace):
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame({"value": [100, 200]}).to_excel(
            writer, sheet_name="page", index=False
        )
        pd.DataFrame({"note": ["keep"]}).to_excel(
            writer, sheet_name="notes", index=False
        )
    ingested = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "book.xlsx",
        buffer.getvalue(),
        {"book::page": {}, "book::notes": {}},
    )
    versions = services.repository.get_file_versions("tenant-a", ingested.file_id)
    target_version = next(
        row for row in versions if row["logical_name"] == "book::page"
    )
    sources = {artifact.name: artifact for artifact in ingested.artifacts}
    source = sources["book::page"]
    other_sheet = sources["book::notes"]
    direct = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "direct",
        pd.DataFrame({"value": [300]}),
        {},
        (source.id,),
    )
    descendant = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "descendant",
        pd.DataFrame({"value": [300]}),
        {},
        (direct.id,),
    )
    conversation = services.repository.get_or_create_conversation(
        "tenant-a", workspace.id
    )
    request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "汇总营收"
    )
    run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        source.id,
        request.safe_content,
        {},
        3,
        request_message_id=request.id,
    )
    services.conversations.add_assistant_message(
        "tenant-a",
        conversation["id"],
        "result ready",
        descendant.id,
        analysis_run_id=run["id"],
    )
    file_record = services.repository.find_file_by_hash(
        "tenant-a", workspace.id, ingested.content_hash
    )
    return {
        "ingested": ingested,
        "target_version": target_version,
        "source": source,
        "other_sheet": other_sheet,
        "direct": direct,
        "descendant": descendant,
        "conversation": conversation,
        "run": run,
        "encrypted_path": Path(file_record["encrypted_path"]),
    }


def test_inspect_file_deletion_finds_all_descendants_and_analysis(services, workspace):
    ingested, source, direct, descendant, _conversation, run = _source_graph(
        services, workspace
    )

    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, ingested.file_id
    )

    assert isinstance(impact, DeletionImpact)
    assert set(impact.artifact_ids) == {source.id, direct.id, descendant.id}
    assert impact.analysis_run_ids == (run["id"],)
    assert impact.analysis_run_count == 1
    assert {source.path, direct.path, descendant.path} < set(impact.paths)
    assert impact.dataset_version_count == 1


def test_file_deletion_selects_and_removes_only_associated_messages(
    services, workspace
):
    first = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "first.csv",
        pd.DataFrame({"value": [1]}).to_csv(index=False).encode(),
        {"first": {}},
    )
    second = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "second.csv",
        pd.DataFrame({"value": [2]}).to_csv(index=False).encode(),
        {"second": {}},
    )
    conversation = services.repository.get_or_create_conversation(
        "tenant-a", workspace.id
    )

    first_request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "analyze first"
    )
    first_run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        first.artifacts[0].id,
        first_request.safe_content,
        {},
        3,
        request_message_id=first_request.id,
    )
    first_result = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "first result",
        pd.DataFrame({"value": [1]}),
        {},
        (first.artifacts[0].id,),
    )
    services.conversations.add_assistant_message(
        "tenant-a",
        conversation["id"],
        "first ready",
        first_result.id,
        analysis_run_id=first_run["id"],
    )
    first_response = services.repository.list_messages(
        "tenant-a", conversation["id"], 100
    )[-1]

    second_request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "analyze second"
    )
    second_run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        second.artifacts[0].id,
        second_request.safe_content,
        {},
        3,
        request_message_id=second_request.id,
    )
    second_result = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "second result",
        pd.DataFrame({"value": [2]}),
        {},
        (second.artifacts[0].id,),
    )
    services.conversations.add_assistant_message(
        "tenant-a",
        conversation["id"],
        "second ready",
        second_result.id,
        analysis_run_id=second_run["id"],
    )
    second_response = services.repository.list_messages(
        "tenant-a", conversation["id"], 100
    )[-1]
    services.repository.update_conversation_summary(
        "tenant-a", conversation["id"], "stale summary"
    )

    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, first.file_id
    )

    assert set(impact.message_ids) == {first_request.id, first_response.id}
    assert impact.message_count == 2
    assert second_request.id not in impact.message_ids

    services.data_sources.delete("tenant-a", workspace.id, first.file_id)

    remaining = services.repository.list_messages(
        "tenant-a", conversation["id"], 100
    )
    assert [message.id for message in remaining] == [
        second_request.id,
        second_response.id,
    ]
    assert (
        services.repository.get_conversation("tenant-a", conversation["id"])[
            "safe_summary"
        ]
        == ""
    )


def test_inspect_file_deletion_finds_pending_merge_source_messages(
    services, workspace
):
    left = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "left.csv",
        pd.DataFrame({"key": [1]}).to_csv(index=False).encode(),
        {"left": {}},
    )
    right = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "right.csv",
        pd.DataFrame({"key": [1]}).to_csv(index=False).encode(),
        {"right": {}},
    )
    conversation = services.repository.get_or_create_conversation(
        "tenant-a", workspace.id
    )
    request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "merge the sources"
    )
    run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        left.artifacts[0].id,
        request.safe_content,
        {},
        3,
        request_message_id=request.id,
    )
    plan = AnalysisPlan(
        input_artifact_id=left.artifacts[0].id,
        operations=[
            {
                "action": "merge",
                "right_artifact_id": right.artifacts[0].id,
                "left_on": ["key"],
                "right_on": ["key"],
            }
        ],
    )
    services.repository.create_analysis_plan_version(
        "tenant-a", run["id"], plan.model_dump(mode="json"), "initial"
    )

    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, right.file_id
    )

    assert impact.analysis_run_ids == (run["id"],)
    assert impact.message_ids == (request.id,)


def test_file_deletion_excludes_cross_workspace_artifact_message(
    services, workspace
):
    ingested = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "scoped.csv",
        pd.DataFrame({"value": [1]}).to_csv(index=False).encode(),
        {"scoped": {}},
    )
    other_workspace = services.repository.create_workspace("tenant-a", "other")
    other_conversation = services.repository.create_conversation(
        "tenant-a", other_workspace.id
    )
    unrelated = services.repository.add_message(
        "tenant-a",
        other_conversation["id"],
        "assistant",
        "keep cross-workspace message",
        ingested.artifacts[0].id,
    )

    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, ingested.file_id
    )

    assert unrelated.id not in impact.message_ids


def test_invalid_merge_source_plan_does_not_expand_deletion_impact(
    services, workspace
):
    left = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "invalid-left.csv",
        pd.DataFrame({"key": [1]}).to_csv(index=False).encode(),
        {"invalid-left": {}},
    )
    right = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "invalid-right.csv",
        pd.DataFrame({"key": [2]}).to_csv(index=False).encode(),
        {"invalid-right": {}},
    )
    conversation = services.repository.get_or_create_conversation(
        "tenant-a", workspace.id
    )
    request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "invalid merge plan"
    )
    run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        left.artifacts[0].id,
        request.safe_content,
        {},
        3,
        request_message_id=request.id,
    )
    services.repository.create_analysis_plan_version(
        "tenant-a",
        run["id"],
        {
            "input_artifact_id": left.artifacts[0].id,
            "operations": [
                {
                    "action": "merge",
                    "right_artifact_id": right.artifacts[0].id,
                }
            ],
        },
        "initial",
    )

    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, right.file_id
    )

    assert run["id"] not in impact.analysis_run_ids
    assert request.id not in impact.message_ids


def test_file_deletion_rebuilds_long_conversation_summaries_exactly(
    services, workspace
):
    ingested = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "summary.csv",
        pd.DataFrame({"value": [1]}).to_csv(index=False).encode(),
        {"summary": {}},
    )

    conversation_ids: list[str] = []
    for prefix, padding in (("short", ""), ("long", "x" * 300)):
        conversation = services.repository.create_conversation(
            "tenant-a", workspace.id, prefix
        )
        conversation_ids.append(conversation["id"])
        request = services.conversations.add_user_message(
            "tenant-a", conversation["id"], f"delete {prefix} exchange"
        )
        run = services.repository.create_analysis_run(
            "tenant-a",
            workspace.id,
            conversation["id"],
            ingested.artifacts[0].id,
            request.safe_content,
            {},
            3,
            request_message_id=request.id,
        )
        services.repository.add_message(
            "tenant-a",
            conversation["id"],
            "assistant",
            f"delete {prefix} response",
            ingested.artifacts[0].id,
            analysis_run_id=run["id"],
        )
        for index in range(34):
            message = services.repository.add_message(
                "tenant-a",
                conversation["id"],
                "system",
                f"{prefix}-{index:02d}-{padding}",
            )
            with services.repository._connect() as connection:
                connection.execute(
                    "UPDATE messages SET created_at = ? WHERE id = ?",
                    (f"2026-01-01T00:00:{index:02d}+00:00", message.id),
                )

    services.data_sources.delete("tenant-a", workspace.id, ingested.file_id)

    short_summary = services.repository.get_conversation(
        "tenant-a", conversation_ids[0]
    )["safe_summary"]
    assert short_summary == "\n".join(
        f"system: short-{index:02d}-" for index in range(2, 22)
    )

    long_summary = services.repository.get_conversation(
        "tenant-a", conversation_ids[1]
    )["safe_summary"]
    expected_long_summary = "\n".join(
        f"system: long-{index:02d}-{'x' * 232}" for index in range(2, 22)
    )[-4000:]
    assert long_summary == expected_long_summary
    assert len(long_summary) == 4000
    assert "long-22-" not in long_summary


def test_legacy_request_matching_uses_nearest_prior_duplicate_message(
    services, workspace
):
    first = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "legacy-first.csv",
        pd.DataFrame({"value": [1]}).to_csv(index=False).encode(),
        {"legacy-first": {}},
    )
    second = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "legacy-second.csv",
        pd.DataFrame({"value": [2]}).to_csv(index=False).encode(),
        {"legacy-second": {}},
    )
    conversation = services.repository.get_or_create_conversation(
        "tenant-a", workspace.id
    )

    first_request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "repeat request"
    )
    first_run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        first.artifacts[0].id,
        first_request.safe_content,
        {},
        3,
    )
    first_result = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "legacy first result",
        pd.DataFrame({"value": [1]}),
        {},
        (first.artifacts[0].id,),
    )
    first_response = services.repository.add_message(
        "tenant-a",
        conversation["id"],
        "assistant",
        "legacy first ready",
        first_result.id,
    )

    second_request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "repeat request"
    )
    services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        second.artifacts[0].id,
        second_request.safe_content,
        {},
        3,
    )
    second_result = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "legacy second result",
        pd.DataFrame({"value": [2]}),
        {},
        (second.artifacts[0].id,),
    )
    second_response = services.repository.add_message(
        "tenant-a",
        conversation["id"],
        "assistant",
        "legacy second ready",
        second_result.id,
    )

    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, first.file_id
    )

    assert set(impact.message_ids) == {first_request.id, first_response.id}
    assert second_request.id not in impact.message_ids
    assert second_response.id not in impact.message_ids
    assert impact.analysis_run_ids == (first_run["id"],)


def test_pre_message_link_schema_upgrade_preserves_data_and_legacy_fallback(
    services, workspace, config
):
    ingested = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "legacy-schema.csv",
        pd.DataFrame({"value": [1]}).to_csv(index=False).encode(),
        {"legacy-schema": {}},
    )
    source = ingested.artifacts[0]
    conversation = services.repository.create_conversation("tenant-a", workspace.id)
    legacy_request = services.conversations.add_user_message(
        "tenant-a", conversation["id"], "legacy schema request"
    )
    legacy_run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        source.id,
        legacy_request.safe_content,
        {},
        3,
    )
    with services.repository._connect() as connection:
        connection.execute("DROP TABLE analysis_run_messages")

    upgraded = SQLiteRepository(config.database_path)
    upgraded.initialize()

    preserved_workspace = upgraded.get_workspace("tenant-a", workspace.id)
    assert preserved_workspace.id == workspace.id
    assert preserved_workspace.name == workspace.name
    assert upgraded.get_analysis_run("tenant-a", legacy_run["id"])["id"] == legacy_run["id"]
    assert upgraded.list_analysis_run_messages("tenant-a", legacy_run["id"]) == []
    impact = upgraded.inspect_file_deletion("tenant-a", workspace.id, ingested.file_id)
    assert legacy_request.id in impact.message_ids

    linked_request = upgraded.add_message(
        "tenant-a", conversation["id"], "user", "linked after upgrade"
    )
    linked_run = upgraded.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        source.id,
        linked_request.safe_content,
        {},
        3,
        request_message_id=linked_request.id,
    )
    links = upgraded.list_analysis_run_messages("tenant-a", linked_run["id"])
    assert [(item["id"], item["kind"]) for item in links] == [
        (linked_request.id, "request")
    ]


def test_inspect_dataset_version_deletion_finds_only_selected_sheet_descendants(
    services, workspace
):
    graph = _two_sheet_graph(services, workspace)

    impact = services.repository.inspect_dataset_version_deletion(
        "tenant-a", workspace.id, graph["target_version"]["id"]
    )

    assert impact.logical_name == "book::page"
    assert impact.version == 1
    assert impact.row_count == 2
    assert set(impact.artifact_ids) == {
        graph["source"].id,
        graph["direct"].id,
        graph["descendant"].id,
    }
    assert graph["other_sheet"].id not in impact.artifact_ids
    assert graph["encrypted_path"] not in impact.paths
    assert impact.analysis_run_ids == (graph["run"]["id"],)
    assert impact.message_count == 2


def test_inspect_dataset_version_deletion_rejects_cross_tenant(services, workspace):
    graph = _two_sheet_graph(services, workspace)
    services.repository.ensure_tenant("tenant-b")

    with pytest.raises(AuthorizationError):
        services.repository.inspect_dataset_version_deletion(
            "tenant-b", workspace.id, graph["target_version"]["id"]
        )

    assert services.repository.get_artifact("tenant-a", graph["source"].id)


def test_inspect_dataset_version_deletion_rejects_cross_workspace_artifact(
    services, workspace
):
    graph = _two_sheet_graph(services, workspace)
    other_workspace = services.repository.create_workspace("tenant-a", "other")
    with services.repository._connect() as connection:
        connection.execute(
            "UPDATE artifacts SET workspace_id = ? WHERE id = ?",
            (other_workspace.id, graph["source"].id),
        )

    with pytest.raises(AuthorizationError, match="artifact"):
        services.repository.inspect_dataset_version_deletion(
            "tenant-a", workspace.id, graph["target_version"]["id"]
        )


def test_list_datasets_exposes_current_version_identity_and_row_count(
    services, workspace
):
    graph = _two_sheet_graph(services, workspace)

    datasets = services.repository.list_datasets("tenant-a", workspace.id)
    page = next(row for row in datasets if row["logical_name"] == "book::page")

    assert page["dataset_version_id"] == graph["target_version"]["id"]
    assert page["file_id"] == graph["ingested"].file_id
    assert page["row_count"] == 2


def test_inspect_file_deletion_rejects_cross_tenant_without_mutation(services, workspace):
    ingested, *_ = _source_graph(services, workspace)
    services.repository.ensure_tenant("tenant-b")

    with pytest.raises(AuthorizationError):
        services.repository.inspect_file_deletion(
            "tenant-b", workspace.id, ingested.file_id
        )

    assert len(services.repository.list_files("tenant-a", workspace.id)) == 1
    assert len(services.repository.list_artifacts("tenant-a", workspace.id)) == 3


def test_delete_file_metadata_cascades_messages_but_preserves_token_mappings(
    services, workspace
):
    ingested, _source, _direct, _descendant, conversation, _run = _source_graph(
        services, workspace
    )
    services.vault.tokenize("tenant-a", "store", "华东事业部")
    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, ingested.file_id
    )

    deleted = services.repository.delete_file_metadata(
        "tenant-a", workspace.id, impact
    )

    assert deleted.artifact_count == 3
    assert services.repository.list_files("tenant-a", workspace.id) == []
    assert services.repository.list_artifacts("tenant-a", workspace.id) == []
    assert services.repository.list_messages("tenant-a", conversation["id"]) == []
    assert services.repository.list_mappings("tenant-a") != []
    event = services.repository.list_audit_events("tenant-a", workspace.id)[0]
    assert event["event_type"] == "file_deleted"
    assert event["details"]["artifact_count"] == 3


def test_delete_dataset_version_metadata_cascades_messages_but_preserves_other_sheet(
    services, workspace
):
    graph = _two_sheet_graph(services, workspace)
    services.vault.tokenize("tenant-a", "store", "keep mapping")
    impact = services.repository.inspect_dataset_version_deletion(
        "tenant-a", workspace.id, graph["target_version"]["id"]
    )

    deleted = services.repository.delete_dataset_version_metadata(
        "tenant-a", workspace.id, impact
    )

    assert deleted.dataset_version_id == graph["target_version"]["id"]
    assert len(services.repository.list_files("tenant-a", workspace.id)) == 1
    assert services.repository.get_artifact(
        "tenant-a", graph["other_sheet"].id
    ) == graph["other_sheet"]
    with pytest.raises(RecordNotFoundError):
        services.repository.get_artifact("tenant-a", graph["source"].id)
    assert (
        services.repository.list_messages(
            "tenant-a", graph["conversation"]["id"]
        )
        == []
    )
    assert services.repository.list_mappings("tenant-a")
    events = services.repository.list_audit_events("tenant-a", workspace.id)
    deletion_event = next(
        event for event in events if event["event_type"] == "dataset_version_deleted"
    )
    assert deletion_event["details"]["logical_name"] == "book::page"
    assert deletion_event["details"]["artifact_count"] == 3


def test_delete_dataset_version_metadata_rolls_back_on_database_failure(
    services, workspace
):
    graph = _two_sheet_graph(services, workspace)
    impact = services.repository.inspect_dataset_version_deletion(
        "tenant-a", workspace.id, graph["target_version"]["id"]
    )
    with services.repository._connect() as connection:
        connection.execute(
            """CREATE TRIGGER fail_dataset_version_delete
            BEFORE DELETE ON dataset_versions
            BEGIN SELECT RAISE(ABORT, 'dataset version delete unavailable'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="dataset version delete unavailable"):
        services.repository.delete_dataset_version_metadata(
            "tenant-a", workspace.id, impact
        )

    assert services.repository.get_artifact("tenant-a", graph["source"].id)
    assert services.repository.get_artifact("tenant-a", graph["descendant"].id)
    assert services.repository.get_analysis_run("tenant-a", graph["run"]["id"])
    messages = services.repository.list_messages(
        "tenant-a", graph["conversation"]["id"]
    )
    assert messages[-1].artifact_id == graph["descendant"].id


def test_delete_one_file_keeps_other_dataset_version_and_original_number(
    services, workspace
):
    first = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "sales.csv",
        pd.DataFrame({"营收": [100]}).to_csv(index=False).encode(),
        {"sales": {}},
    )
    second = services.ingestion.ingest(
        "tenant-a",
        workspace.id,
        "sales.csv",
        pd.DataFrame({"营收": [200]}).to_csv(index=False).encode(),
        {"sales": {}},
    )

    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, first.file_id
    )
    services.repository.delete_file_metadata("tenant-a", workspace.id, impact)

    datasets = services.repository.list_datasets("tenant-a", workspace.id)
    assert len(datasets) == 1
    assert datasets[0]["version"] == 2
    assert datasets[0]["artifact_id"] == second.artifacts[0].id


def test_delete_file_metadata_rolls_back_when_file_delete_fails(services, workspace):
    ingested, _source, _direct, _descendant, conversation, run = _source_graph(
        services, workspace
    )
    services.repository.update_conversation_summary(
        "tenant-a", conversation["id"], "summary before failure"
    )
    messages_before = services.repository.list_messages(
        "tenant-a", conversation["id"], 100
    )
    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, ingested.file_id
    )
    with services.repository._connect() as connection:
        connection.execute(
            """CREATE TRIGGER fail_file_delete BEFORE DELETE ON files
            BEGIN SELECT RAISE(ABORT, 'file delete unavailable'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="file delete unavailable"):
        services.repository.delete_file_metadata("tenant-a", workspace.id, impact)

    assert len(services.repository.list_files("tenant-a", workspace.id)) == 1
    assert len(services.repository.list_artifacts("tenant-a", workspace.id)) == 3
    assert services.repository.get_analysis_run("tenant-a", run["id"])
    assert services.repository.list_messages(
        "tenant-a", conversation["id"], 100
    ) == messages_before
    assert (
        services.repository.get_conversation("tenant-a", conversation["id"])[
            "safe_summary"
        ]
        == "summary before failure"
    )


def test_data_source_service_restores_files_messages_runs_and_summary_on_sqlite_failure(
    services, workspace
):
    ingested, _source, _direct, _descendant, conversation, run = _source_graph(
        services, workspace
    )
    services.repository.update_conversation_summary(
        "tenant-a", conversation["id"], "summary before failure"
    )
    impact = services.data_sources.inspect("tenant-a", workspace.id, ingested.file_id)
    path_contents = {path: path.read_bytes() for path in impact.paths}
    messages_before = services.repository.list_messages(
        "tenant-a", conversation["id"], 100
    )
    with services.repository._connect() as connection:
        connection.execute(
            """CREATE TRIGGER fail_atomic_audit_insert BEFORE INSERT ON audit_events
            BEGIN SELECT RAISE(ABORT, 'atomic audit unavailable'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="atomic audit unavailable"):
        services.data_sources.delete("tenant-a", workspace.id, ingested.file_id)

    assert {path: path.read_bytes() for path in impact.paths} == path_contents
    assert services.repository.get_analysis_run("tenant-a", run["id"])
    assert services.repository.list_messages(
        "tenant-a", conversation["id"], 100
    ) == messages_before
    assert (
        services.repository.get_conversation("tenant-a", conversation["id"])[
            "safe_summary"
        ]
        == "summary before failure"
    )


def test_data_source_service_removes_files_and_metadata(services, workspace):
    ingested, source, direct, descendant, conversation, _run = _source_graph(services, workspace)
    impact = services.data_sources.inspect("tenant-a", workspace.id, ingested.file_id)
    original_paths = tuple(impact.paths)

    deleted = services.data_sources.delete("tenant-a", workspace.id, ingested.file_id)

    assert deleted.artifact_count == 3
    assert all(not path.exists() for path in original_paths)
    assert services.repository.list_files("tenant-a", workspace.id) == []
    assert not (services.data_sources.data_dir / ".trash").exists()
    assert not source.path.exists()
    assert not direct.path.exists()
    assert not descendant.path.exists()
    assert services.repository.list_messages("tenant-a", conversation["id"]) == []


def test_data_source_service_deletes_only_selected_table_files(services, workspace):
    graph = _two_sheet_graph(services, workspace)
    impact = services.data_sources.inspect_table(
        "tenant-a", workspace.id, graph["target_version"]["id"]
    )
    encrypted_before = graph["encrypted_path"].read_bytes()

    deleted = services.data_sources.delete_table(
        "tenant-a", workspace.id, graph["target_version"]["id"]
    )

    assert deleted.artifact_count == 3
    assert all(not path.exists() for path in impact.paths)
    assert graph["encrypted_path"].read_bytes() == encrypted_before
    assert graph["other_sheet"].path.exists()
    assert len(services.repository.list_files("tenant-a", workspace.id)) == 1
    assert not (services.data_sources.data_dir / ".trash").exists()
    assert [
        message.safe_content
        for message in services.repository.list_messages("tenant-a", graph["conversation"]["id"])
    ] == []


def test_data_source_service_restores_table_files_when_metadata_delete_fails(
    monkeypatch, services, workspace
):
    graph = _two_sheet_graph(services, workspace)
    impact = services.data_sources.inspect_table(
        "tenant-a", workspace.id, graph["target_version"]["id"]
    )
    original = {path: path.read_bytes() for path in impact.paths}

    def fail_delete(*_args, **_kwargs):
        raise sqlite3.IntegrityError("metadata unavailable")

    monkeypatch.setattr(
        services.repository, "delete_dataset_version_metadata", fail_delete
    )

    with pytest.raises(DataSourceDeletionError, match="metadata unavailable"):
        services.data_sources.delete_table(
            "tenant-a", workspace.id, graph["target_version"]["id"]
        )

    assert {path: path.read_bytes() for path in impact.paths} == original
    assert graph["encrypted_path"].exists()
    assert graph["other_sheet"].path.exists()
    assert not (services.data_sources.data_dir / ".trash").exists()


def test_data_source_service_restores_files_when_metadata_delete_fails(
    monkeypatch, services, workspace
):
    ingested, *_ = _source_graph(services, workspace)
    impact = services.data_sources.inspect("tenant-a", workspace.id, ingested.file_id)
    original = {path: path.read_bytes() for path in impact.paths}

    def fail_delete(*_args, **_kwargs):
        raise sqlite3.IntegrityError("metadata unavailable")

    monkeypatch.setattr(services.repository, "delete_file_metadata", fail_delete)

    with pytest.raises(sqlite3.IntegrityError, match="metadata unavailable"):
        services.data_sources.delete("tenant-a", workspace.id, ingested.file_id)

    assert {path: path.read_bytes() for path in impact.paths} == original
    assert not (services.data_sources.data_dir / ".trash").exists()
    assert len(services.repository.list_files("tenant-a", workspace.id)) == 1


def test_data_source_service_rejects_database_path_outside_data_dir(
    monkeypatch, services, workspace, tmp_path: Path
):
    ingested, *_ = _source_graph(services, workspace)
    impact = services.repository.inspect_file_deletion(
        "tenant-a", workspace.id, ingested.file_id
    )
    outside = tmp_path / "outside.enc"
    outside.write_bytes(b"outside")
    unsafe = replace(impact, paths=(*impact.paths, outside))
    monkeypatch.setattr(services.repository, "inspect_file_deletion", lambda *_args: unsafe)

    with pytest.raises(ValueError, match="data directory"):
        services.data_sources.delete("tenant-a", workspace.id, ingested.file_id)

    assert outside.read_bytes() == b"outside"
    assert all(path.exists() for path in impact.paths)
