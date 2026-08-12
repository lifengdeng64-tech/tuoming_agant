from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from tuoming_agent.ingestion.limits import validate_upload_size
from tuoming_agent.ingestion.parser import ParsedTable, parse_file
from tuoming_agent.ingestion.scanner import detect_sensitive_columns
from tuoming_agent.models import ArtifactRecord, utc_now
from tuoming_agent.security.masking import ColumnPolicy, MaskingService
from tuoming_agent.storage.files import ArtifactStore, SecureFileStore
from tuoming_agent.storage.sqlite import SQLiteRepository


@dataclass(frozen=True)
class IngestionResult:
    file_id: str
    content_hash: str
    artifacts: tuple[ArtifactRecord, ...]
    duplicate: bool


class UnsafeIngestionError(ValueError):
    """Raised when locally detected sensitive columns lack an explicit policy."""


class IngestionService:
    def __init__(
        self,
        repository: SQLiteRepository,
        masking_service: MaskingService,
        secure_file_store: SecureFileStore,
        artifact_store: ArtifactStore,
    ):
        self.repository = repository
        self.masking_service = masking_service
        self.secure_file_store = secure_file_store
        self.artifact_store = artifact_store

    def preview(self, filename: str, content: bytes) -> list[ParsedTable]:
        return parse_file(filename, content)

    def ingest(
        self,
        tenant_id: str,
        workspace_id: str,
        filename: str,
        content: bytes,
        policies: dict[str, dict[str, ColumnPolicy]],
    ) -> IngestionResult:
        validate_upload_size(filename, len(content))
        content_hash = hashlib.sha256(content).hexdigest()
        duplicate = self.repository.find_file_by_hash(tenant_id, workspace_id, content_hash)
        if duplicate:
            versions = self.repository.get_file_versions(tenant_id, duplicate["id"])
            artifacts = tuple(
                self.repository.get_artifact(tenant_id, version["artifact_id"])
                for version in versions
            )
            return IngestionResult(duplicate["id"], content_hash, artifacts, True)

        tables = parse_file(filename, content)
        for table in tables:
            table_policies = policies.get(table.logical_name, {})
            detected = detect_sensitive_columns(table.dataframe)
            missing_policies = sorted(detected - set(table_policies))
            if missing_policies:
                raise UnsafeIngestionError(
                    "Sensitive columns require a masking policy: " + ", ".join(missing_policies)
                )
        encrypted_path = self.secure_file_store.write(
            tenant_id, workspace_id, content_hash, content
        )
        file_record = self.repository.create_file(
            tenant_id,
            workspace_id,
            content_hash,
            filename,
            str(encrypted_path),
            len(content),
        )
        created: list[ArtifactRecord] = []
        for table in tables:
            table_policies = policies.get(table.logical_name, {})
            masked, lineage = self.masking_service.mask_dataframe(
                tenant_id, table.dataframe, table_policies
            )
            artifact_id = str(uuid.uuid4())
            artifact_path = self.artifact_store.write_dataframe(
                tenant_id, workspace_id, artifact_id, masked
            )
            artifact = ArtifactRecord(
                id=artifact_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                kind="dataset",
                name=table.logical_name,
                path=artifact_path,
                row_count=len(masked),
                schema=self._schema(masked),
                lineage=lineage,
                parent_ids=(),
                created_at=utc_now(),
            )
            self.repository.create_artifact(artifact)
            dataset = self.repository.get_or_create_dataset(
                tenant_id, workspace_id, table.logical_name
            )
            version = self.repository.create_dataset_version(
                tenant_id,
                dataset["id"],
                file_record["id"],
                artifact.id,
                table.sheet_name,
            )
            for column_name, policy in table_policies.items():
                self.repository.add_column_policy(
                    tenant_id,
                    version["id"],
                    column_name,
                    policy.domain,
                    policy.normalizer,
                    self.masking_service.vault.key_version,
                )
            created.append(artifact)

        self.repository.touch_workspace(tenant_id, workspace_id)
        self.repository.add_audit_event(
            tenant_id,
            "file_ingested",
            workspace_id,
            {"sha256_prefix": content_hash[:12], "table_count": len(created)},
        )
        return IngestionResult(file_record["id"], content_hash, tuple(created), False)

    @staticmethod
    def _schema(dataframe: Any) -> dict[str, Any]:
        return {
            "columns": [
                {"name": str(column), "dtype": str(dataframe[column].dtype)}
                for column in dataframe.columns
            ]
        }
