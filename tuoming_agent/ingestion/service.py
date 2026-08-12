from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from io import BytesIO
from itertools import groupby
from pathlib import Path
from typing import Any, BinaryIO

from tuoming_agent.ingestion.limits import validate_upload_size
from tuoming_agent.ingestion.parser import ParsedTable, iter_file_chunks, parse_file
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
        content: bytes | BinaryIO,
        policies: dict[str, dict[str, ColumnPolicy]],
    ) -> IngestionResult:
        if isinstance(content, bytes):
            validate_upload_size(filename, len(content))
            source = BytesIO(content)
        else:
            source = content
        byte_size, content_hash = self._measure_and_hash(source)
        validate_upload_size(filename, byte_size)
        duplicate = self.repository.find_file_by_hash(tenant_id, workspace_id, content_hash)
        if duplicate:
            versions = self.repository.get_file_versions(tenant_id, duplicate["id"])
            artifacts = tuple(
                self.repository.get_artifact(tenant_id, version["artifact_id"])
                for version in versions
            )
            return IngestionResult(duplicate["id"], content_hash, artifacts, True)

        source.seek(0)
        table_names: list[str] = []
        for table in iter_file_chunks(filename, source):
            if table.logical_name not in table_names:
                table_names.append(table.logical_name)
            table_policies = policies.get(table.logical_name, {})
            detected = detect_sensitive_columns(table.dataframe, sample_size=None)
            missing_policies = sorted(detected - set(table_policies))
            if missing_policies:
                raise UnsafeIngestionError(
                    "Sensitive columns require a masking policy: " + ", ".join(missing_policies)
                )
        if not table_names:
            raise ValueError("The upload contains no tabular rows.")

        pending: list[tuple[ArtifactRecord, str | None, dict[str, ColumnPolicy]]] = []
        created_paths: list[Path] = []
        encrypted_path: Path | None = None
        source.seek(0)
        try:
            chunks = iter_file_chunks(filename, source)
            for logical_name, table_chunks in groupby(chunks, key=lambda table: table.logical_name):
                artifact_id = str(uuid.uuid4())
                table_policies = policies.get(logical_name, {})
                row_count = 0
                schema: dict[str, Any] | None = None
                lineage: dict[str, Any] = {}
                sheet_name: str | None = None

                def masked_chunks(
                    selected_chunks=table_chunks, selected_policies=table_policies
                ):
                    nonlocal row_count, schema, lineage, sheet_name
                    for table in selected_chunks:
                        detected = detect_sensitive_columns(table.dataframe, sample_size=None)
                        missing_policies = sorted(detected - set(selected_policies))
                        if missing_policies:
                            raise UnsafeIngestionError(
                                "Sensitive columns require a masking policy: "
                                + ", ".join(missing_policies)
                            )
                        masked, current_lineage = self.masking_service.mask_dataframe(
                            tenant_id, table.dataframe, selected_policies
                        )
                        row_count += len(masked)
                        schema = schema or self._schema(masked)
                        lineage = current_lineage
                        sheet_name = table.sheet_name
                        yield masked

                artifact_path = self.artifact_store.write_chunks(
                    tenant_id, workspace_id, artifact_id, masked_chunks()
                )
                created_paths.append(artifact_path)
                pending.append(
                    (
                        ArtifactRecord(
                            id=artifact_id,
                            tenant_id=tenant_id,
                            workspace_id=workspace_id,
                            kind="dataset",
                            name=logical_name,
                            path=artifact_path,
                            row_count=row_count,
                            schema=schema or {"columns": []},
                            lineage=lineage,
                            parent_ids=(),
                            created_at=utc_now(),
                        ),
                        sheet_name,
                        table_policies,
                    )
                )

            source.seek(0)
            encrypted_path, streamed_hash, streamed_size = self.secure_file_store.write_stream(
                tenant_id, workspace_id, source
            )
            if streamed_hash != content_hash or streamed_size != byte_size:
                raise ValueError("Upload changed while it was being ingested.")
        except Exception:
            for path in created_paths:
                path.unlink(missing_ok=True)
            if encrypted_path is not None:
                encrypted_path.unlink(missing_ok=True)
            raise

        publication = [
            (
                artifact,
                sheet_name,
                {
                    column_name: (
                        policy.domain,
                        policy.normalizer,
                        self.masking_service.vault.key_version,
                    )
                    for column_name, policy in table_policies.items()
                },
            )
            for artifact, sheet_name, table_policies in pending
        ]
        try:
            file_record, lost_race = self.repository.publish_ingestion(
                tenant_id,
                workspace_id,
                content_hash,
                filename,
                str(encrypted_path),
                byte_size,
                publication,
            )
        except Exception:
            for path in created_paths:
                path.unlink(missing_ok=True)
            encrypted_path.unlink(missing_ok=True)
            raise
        if lost_race:
            for path in created_paths:
                path.unlink(missing_ok=True)
            versions = self.repository.get_file_versions(tenant_id, file_record["id"])
            artifacts = tuple(
                self.repository.get_artifact(tenant_id, version["artifact_id"])
                for version in versions
            )
            return IngestionResult(file_record["id"], content_hash, artifacts, True)

        created = [artifact for artifact, _, _ in pending]

        self.repository.touch_workspace(tenant_id, workspace_id)
        self.repository.add_audit_event(
            tenant_id,
            "file_ingested",
            workspace_id,
            {"sha256_prefix": content_hash[:12], "table_count": len(created)},
        )
        return IngestionResult(file_record["id"], content_hash, tuple(created), False)

    @staticmethod
    def _measure_and_hash(source: BinaryIO, chunk_size: int = 1024 * 1024) -> tuple[int, str]:
        digest = hashlib.sha256()
        byte_size = 0
        source.seek(0)
        while chunk := source.read(chunk_size):
            byte_size += len(chunk)
            digest.update(chunk)
        source.seek(0)
        return byte_size, digest.hexdigest()

    @staticmethod
    def _schema(dataframe: Any) -> dict[str, Any]:
        return {
            "columns": [
                {"name": str(column), "dtype": str(dataframe[column].dtype)}
                for column in dataframe.columns
            ]
        }
