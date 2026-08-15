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
from tuoming_agent.maintenance import ensure_disk_headroom
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
        temp_reserve_bytes: int,
    ):
        self.repository = repository
        self.masking_service = masking_service
        self.secure_file_store = secure_file_store
        self.artifact_store = artifact_store
        self.temp_reserve_bytes = temp_reserve_bytes

    def preview(self, filename: str, content: bytes) -> list[ParsedTable]:
        return parse_file(filename, content)

    def ingest(
        self,
        tenant_id: str,
        workspace_id: str,
        filename: str,
        content: bytes | BinaryIO,
        policies: dict[str, dict[str, ColumnPolicy]],
        *,
        retained_columns: dict[str, set[str]] | None = None,
    ) -> IngestionResult:
        retained_columns = retained_columns or {}
        retained_sensitive_columns: dict[str, set[str]] = {}
        if isinstance(content, bytes):
            validate_upload_size(filename, len(content))
            source = BytesIO(content)
        else:
            source = content
        byte_size, content_hash = self._measure_and_hash(source)
        validate_upload_size(filename, byte_size)
        ensure_disk_headroom(
            self.artifact_store.root,
            byte_size,
            self.temp_reserve_bytes,
        )
        existing_file = self.repository.find_file_by_hash(
            tenant_id, workspace_id, content_hash
        )
        existing_versions = (
            self.repository.get_file_versions(tenant_id, existing_file["id"])
            if existing_file
            else []
        )
        existing_table_keys = {version["sheet_name"] for version in existing_versions}
        existing_stem = (
            Path(existing_file["original_name"]).stem.strip() or "dataset"
            if existing_file
            else None
        )

        source.seek(0)
        table_names: list[str] = []
        table_keys: set[str | None] = set()
        for table in iter_file_chunks(filename, source):
            if table.logical_name not in table_names:
                table_names.append(table.logical_name)
            table_keys.add(table.sheet_name)
            if table.sheet_name in existing_table_keys:
                continue
            table_policies = policies.get(table.logical_name, {})
            table_retained = retained_columns.get(table.logical_name, set())
            detected = detect_sensitive_columns(table.dataframe, sample_size=None)
            retained_sensitive_columns.setdefault(table.logical_name, set()).update(
                detected & table_retained
            )
            missing_policies = sorted(detected - set(table_policies) - table_retained)
            if missing_policies:
                raise UnsafeIngestionError(
                    "Sensitive columns require a masking policy: " + ", ".join(missing_policies)
                )
        if not table_names:
            raise ValueError("The upload contains no tabular rows.")
        if existing_file and table_keys <= existing_table_keys:
            artifacts = tuple(
                self.repository.get_artifact(tenant_id, version["artifact_id"])
                for version in existing_versions
            )
            return IngestionResult(existing_file["id"], content_hash, artifacts, True)

        pending: list[tuple[ArtifactRecord, str | None, dict[str, ColumnPolicy]]] = []
        created_paths: list[Path] = []
        encrypted_path: Path | None = None
        encrypted_created = False
        source.seek(0)
        try:
            chunks = (
                table
                for table in iter_file_chunks(filename, source)
                if table.sheet_name not in existing_table_keys
            )
            for logical_name, table_chunks in groupby(chunks, key=lambda table: table.logical_name):
                artifact_id = str(uuid.uuid4())
                table_policies = policies.get(logical_name, {})
                table_retained = retained_columns.get(logical_name, set())
                row_count = 0
                schema: dict[str, Any] | None = None
                lineage: dict[str, Any] = {}
                sheet_name: str | None = None

                def masked_chunks(
                    selected_chunks=table_chunks,
                    selected_policies=table_policies,
                    selected_retained=table_retained,
                    selected_logical_name=logical_name,
                ):
                    nonlocal row_count, schema, lineage, sheet_name
                    for table in selected_chunks:
                        detected = detect_sensitive_columns(table.dataframe, sample_size=None)
                        retained_sensitive_columns.setdefault(
                            selected_logical_name, set()
                        ).update(detected & selected_retained)
                        missing_policies = sorted(
                            detected - set(selected_policies) - selected_retained
                        )
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
                schema, row_count = self.artifact_store.inspect_parquet(artifact_path)
                artifact_name = (
                    f"{existing_stem}::{sheet_name}"
                    if existing_stem and sheet_name is not None
                    else existing_stem or logical_name
                )
                created_paths.append(artifact_path)
                pending.append(
                    (
                        ArtifactRecord(
                            id=artifact_id,
                            tenant_id=tenant_id,
                            workspace_id=workspace_id,
                            kind="dataset",
                            name=artifact_name,
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

            if existing_file:
                streamed_size, streamed_hash = self._measure_and_hash(source)
                if streamed_hash != content_hash or streamed_size != byte_size:
                    raise ValueError("Upload changed while it was being ingested.")
                encrypted_path = Path(existing_file["encrypted_path"])
            else:
                source.seek(0)
                (
                    encrypted_path,
                    streamed_hash,
                    streamed_size,
                    encrypted_created,
                ) = self.secure_file_store.write_stream(tenant_id, workspace_id, source)
                if streamed_hash != content_hash or streamed_size != byte_size:
                    raise ValueError("Upload changed while it was being ingested.")
        except Exception:
            for path in created_paths:
                path.unlink(missing_ok=True)
            if encrypted_path is not None and encrypted_created:
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
        if existing_file:
            try:
                published_ids = set(
                    self.repository.publish_tables_for_existing_file(
                        tenant_id,
                        workspace_id,
                        existing_file["id"],
                        publication,
                    )
                )
            except Exception:
                for path in created_paths:
                    path.unlink(missing_ok=True)
                raise
            for artifact, _sheet_name, _policies in pending:
                if artifact.id not in published_ids:
                    artifact.path.unlink(missing_ok=True)
            versions = self.repository.get_file_versions(tenant_id, existing_file["id"])
            artifacts = tuple(
                self.repository.get_artifact(tenant_id, version["artifact_id"])
                for version in versions
            )
            if not published_ids:
                return IngestionResult(existing_file["id"], content_hash, artifacts, True)
            self.repository.touch_workspace(tenant_id, workspace_id)
            self.repository.add_audit_event(
                tenant_id,
                "file_ingested",
                workspace_id,
                {
                    "sha256_prefix": content_hash[:12],
                    "table_count": len(published_ids),
                    "retained_sensitive_columns": {
                        table_name: sorted(columns)
                        for table_name, columns in retained_sensitive_columns.items()
                        if columns
                    },
                },
            )
            return IngestionResult(existing_file["id"], content_hash, artifacts, False)

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
            if encrypted_created:
                encrypted_path.unlink(missing_ok=True)
            raise
        if lost_race:
            for path in created_paths:
                path.unlink(missing_ok=True)
            if encrypted_created:
                encrypted_path.unlink(missing_ok=True)
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
            {
                "sha256_prefix": content_hash[:12],
                "table_count": len(created),
                "retained_sensitive_columns": {
                    table_name: sorted(columns)
                    for table_name, columns in retained_sensitive_columns.items()
                    if columns
                },
            },
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
