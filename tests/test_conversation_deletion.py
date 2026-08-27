from __future__ import annotations

import pandas as pd
import pytest

from tuoming_agent.analysis.models import AnalysisPlan
from tuoming_agent.storage.errors import AuthorizationError


def test_delete_conversation_removes_messages_and_workflow_but_keeps_artifacts(
    services, workspace
):
    source = services.artifacts.save_result(
        "tenant-a",
        workspace.id,
        "source",
        pd.DataFrame({"sales": [10, 20]}),
        {},
        (),
    )
    conversation = services.repository.create_conversation("tenant-a", workspace.id)
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
        request.id,
    )
    plan = AnalysisPlan(
        input_artifact_id=source.id,
        operations=[{"action": "head", "rows": 1}],
    )
    services.repository.create_analysis_plan_version(
        "tenant-a", run["id"], plan.model_dump(mode="json"), "initial"
    )
    services.repository.create_analysis_attempt("tenant-a", run["id"], 1)

    deleted = services.conversations.delete(
        "tenant-a", workspace.id, conversation["id"]
    )

    assert deleted == {"message_count": 1, "analysis_run_count": 1}
    assert services.repository.list_artifacts("tenant-a", workspace.id) == [source]
    assert services.repository.list_analysis_runs("tenant-a", workspace.id) == []
    replacement = services.repository.get_or_create_conversation("tenant-a", workspace.id)
    assert replacement["id"] != conversation["id"]
    event = services.repository.list_audit_events("tenant-a", workspace.id)[0]
    assert event["event_type"] == "conversation_deleted"
    assert event["details"] == {"message_count": 1, "analysis_run_count": 1}


def test_delete_conversation_rejects_wrong_workspace(services, workspace):
    conversation = services.repository.create_conversation("tenant-a", workspace.id)
    other = services.repository.create_workspace("tenant-a", "other")

    with pytest.raises(AuthorizationError):
        services.conversations.delete("tenant-a", other.id, conversation["id"])

    assert services.repository.get_conversation("tenant-a", conversation["id"])

