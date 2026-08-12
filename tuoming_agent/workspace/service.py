from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pandas as pd

from tuoming_agent.config import AppConfig
from tuoming_agent.ingestion.service import IngestionService
from tuoming_agent.models import ArtifactRecord, ColumnLineage, utc_now
from tuoming_agent.security.dlp import PromptSanitizer
from tuoming_agent.security.masking import MaskingService
from tuoming_agent.security.vault import TokenVault
from tuoming_agent.storage.files import ArtifactStore, SecureFileStore
from tuoming_agent.storage.sqlite import SQLiteRepository


class ArtifactService:
    def __init__(
        self,
        repository: SQLiteRepository,
        artifact_store: ArtifactStore,
        config: AppConfig,
    ):
        self.repository = repository
        self.artifact_store = artifact_store
        self.config = config

    def load(self, tenant_id: str, artifact_id: str) -> tuple[ArtifactRecord, pd.DataFrame]:
        artifact = self.repository.get_artifact(tenant_id, artifact_id)
        return artifact, self.artifact_store.read_dataframe(artifact.path)

    def save_result(
        self,
        tenant_id: str,
        workspace_id: str,
        name: str,
        dataframe: pd.DataFrame,
        lineage: dict[str, ColumnLineage],
        parent_ids: tuple[str, ...],
    ) -> ArtifactRecord:
        artifact_id = str(uuid.uuid4())
        path = self.artifact_store.write_dataframe(tenant_id, workspace_id, artifact_id, dataframe)
        artifact = ArtifactRecord(
            id=artifact_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            kind="analysis_result",
            name=name,
            path=path,
            row_count=len(dataframe),
            schema={
                "columns": [
                    {"name": str(column), "dtype": str(dataframe[column].dtype)}
                    for column in dataframe.columns
                ]
            },
            lineage=lineage,
            parent_ids=parent_ids,
            created_at=utc_now(),
        )
        self.repository.create_artifact(artifact)
        self.repository.touch_workspace(tenant_id, workspace_id)
        self.repository.add_audit_event(
            tenant_id,
            "analysis_artifact_created",
            workspace_id,
            {"artifact_id": artifact.id, "row_count": len(dataframe)},
        )
        return artifact

    def publish_candidate(
        self,
        tenant_id: str,
        workspace_id: str,
        candidate: Any,
    ) -> ArtifactRecord:
        if (
            candidate.path is None
            or candidate.schema is None
            or candidate.row_count is None
            or not candidate.owns_path
        ):
            raise ValueError("Only an owned disk-backed candidate can be published.")
        artifact_id = str(uuid.uuid4())
        path = self.artifact_store.publish_candidate(
            tenant_id, workspace_id, artifact_id, candidate.path
        )
        artifact = ArtifactRecord(
            id=artifact_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            kind="analysis_result",
            name=candidate.name,
            path=path,
            row_count=candidate.row_count,
            schema=candidate.schema,
            lineage=candidate.lineage,
            parent_ids=candidate.parent_ids,
            created_at=utc_now(),
        )
        try:
            with self.repository._connect() as connection:
                self.repository._publish_artifact(connection, artifact)
                cursor = connection.execute(
                    "UPDATE workspaces SET updated_at = ? WHERE id = ? AND tenant_id = ?",
                    (utc_now(), workspace_id, tenant_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Workspace is not authorized for candidate publication.")
                connection.execute(
                    """INSERT INTO audit_events(
                        id, tenant_id, workspace_id, event_type, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        tenant_id,
                        workspace_id,
                        "analysis_artifact_created",
                        json.dumps(
                            {"artifact_id": artifact.id, "row_count": artifact.row_count},
                            ensure_ascii=True,
                        ),
                        utc_now(),
                    ),
                )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return artifact


class ConversationService:
    def __init__(self, repository: SQLiteRepository, sanitizer: PromptSanitizer):
        self.repository = repository
        self.sanitizer = sanitizer

    def add_user_message(self, tenant_id: str, conversation_id: str, raw_content: str) -> str:
        safe_content = self.sanitizer.sanitize(tenant_id, raw_content)
        self.repository.add_message(tenant_id, conversation_id, "user", safe_content=safe_content)
        self._roll_up_summary(tenant_id, conversation_id)
        return safe_content

    def add_assistant_message(
        self,
        tenant_id: str,
        conversation_id: str,
        safe_content: str,
        artifact_id: str | None = None,
    ) -> None:
        self.sanitizer.assert_safe(safe_content)
        self.repository.add_message(
            tenant_id,
            conversation_id,
            "assistant",
            safe_content=safe_content,
            artifact_id=artifact_id,
        )
        self._roll_up_summary(tenant_id, conversation_id)

    def build_safe_context(
        self,
        tenant_id: str,
        workspace_id: str,
        conversation_id: str,
        recent_limit: int = 12,
        preferred_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        conversation = self.repository.get_conversation(tenant_id, conversation_id)
        if conversation["workspace_id"] != workspace_id:
            raise ValueError("Conversation is not part of the selected workspace.")
        messages = self.repository.list_messages(tenant_id, conversation_id, recent_limit)
        artifacts = self.repository.list_artifacts(tenant_id, workspace_id)
        artifact_ids = {artifact.id for artifact in artifacts}
        if preferred_artifact_id and preferred_artifact_id not in artifact_ids:
            raise ValueError("Preferred artifact is not part of the selected workspace.")
        return {
            "safe_summary": conversation["safe_summary"],
            "preferred_artifact_id": preferred_artifact_id,
            "recent_messages": [
                {
                    "role": message.role,
                    "content": message.safe_content,
                    "artifact_id": message.artifact_id,
                }
                for message in messages
            ],
            "artifact_catalog": [
                {
                    "artifact_id": artifact.id,
                    "alias": f"artifact_{artifact.id[:8]}",
                    "kind": artifact.kind,
                    "row_count": artifact.row_count,
                    "schema": artifact.schema,
                    "masked_columns": sorted(artifact.lineage),
                }
                for artifact in artifacts[:50]
            ],
        }

    def _roll_up_summary(self, tenant_id: str, conversation_id: str) -> None:
        messages = self.repository.list_messages(tenant_id, conversation_id, limit=100)
        if len(messages) <= 20:
            return
        older = messages[:-12]
        summary = "\n".join(
            f"{message.role}: {message.safe_content[:240]}" for message in older[-20:]
        )[-4000:]
        self.sanitizer.assert_safe(summary)
        self.repository.update_conversation_summary(tenant_id, conversation_id, summary)


@dataclass(frozen=True)
class ApplicationServices:
    repository: SQLiteRepository
    vault: TokenVault
    masking: MaskingService
    ingestion: IngestionService
    artifacts: ArtifactService
    conversations: ConversationService


def create_services(config: AppConfig) -> ApplicationServices:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    repository = SQLiteRepository(config.database_path)
    repository.initialize()
    vault = TokenVault(repository, config.master_key, config.key_version)
    masking = MaskingService(vault)
    artifact_store = ArtifactStore(config.data_dir)
    sanitizer = PromptSanitizer(vault)
    return ApplicationServices(
        repository=repository,
        vault=vault,
        masking=masking,
        ingestion=IngestionService(
            repository,
            masking,
            SecureFileStore(config.data_dir, config.master_key),
            artifact_store,
        ),
        artifacts=ArtifactService(repository, artifact_store, config),
        conversations=ConversationService(repository, sanitizer),
    )
