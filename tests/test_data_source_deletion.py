from __future__ import annotations

import sqlite3
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from tuoming_agent.storage.errors import AuthorizationError, RecordNotFoundError
from tuoming_agent.storage.sqlite import DeletionImpact


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
    run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        source.id,
        "group revenue",
        {},
        3,
    )
    services.conversations.add_assistant_message(
        "tenant-a", conversation["id"], "result ready", descendant.id
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
    run = services.repository.create_analysis_run(
        "tenant-a",
        workspace.id,
        conversation["id"],
        source.id,
        "sum values",
        {},
        3,
    )
    services.conversations.add_assistant_message(
        "tenant-a", conversation["id"], "result ready", descendant.id
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


def test_inspect_dataset_version_deletion_rejects_cross_tenant(services, workspace):
    graph = _two_sheet_graph(services, workspace)
    services.repository.ensure_tenant("tenant-b")

    with pytest.raises(AuthorizationError):
        services.repository.inspect_dataset_version_deletion(
            "tenant-b", workspace.id, graph["target_version"]["id"]
        )

    assert services.repository.get_artifact("tenant-a", graph["source"].id)


def test_inspect_file_deletion_rejects_cross_tenant_without_mutation(services, workspace):
    ingested, *_ = _source_graph(services, workspace)
    services.repository.ensure_tenant("tenant-b")

    with pytest.raises(AuthorizationError):
        services.repository.inspect_file_deletion(
            "tenant-b", workspace.id, ingested.file_id
        )

    assert len(services.repository.list_files("tenant-a", workspace.id)) == 1
    assert len(services.repository.list_artifacts("tenant-a", workspace.id)) == 3


def test_delete_file_metadata_cascades_but_preserves_chat_and_token_mappings(
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
    messages = services.repository.list_messages("tenant-a", conversation["id"])
    assert messages[-1].safe_content == "result ready"
    assert messages[-1].artifact_id is None
    assert services.repository.list_mappings("tenant-a") != []
    event = services.repository.list_audit_events("tenant-a", workspace.id)[0]
    assert event["event_type"] == "file_deleted"
    assert event["details"]["artifact_count"] == 3


def test_delete_dataset_version_metadata_cascades_but_preserves_file_and_other_sheet(
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
    messages = services.repository.list_messages(
        "tenant-a", graph["conversation"]["id"]
    )
    assert messages[-1].safe_content == "result ready"
    assert messages[-1].artifact_id is None
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
    ingested, *_ = _source_graph(services, workspace)
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


def test_data_source_service_removes_files_and_metadata(services, workspace):
    ingested, source, direct, descendant, *_ = _source_graph(services, workspace)
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

    with pytest.raises(sqlite3.IntegrityError, match="metadata unavailable"):
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
