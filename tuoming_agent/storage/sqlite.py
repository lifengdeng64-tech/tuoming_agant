from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tuoming_agent.models import (
    ArtifactRecord,
    ColumnLineage,
    MessageRecord,
    WorkspaceRecord,
    utc_now,
)
from tuoming_agent.storage.errors import AuthorizationError, RecordNotFoundError

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspaces_tenant ON workspaces(tenant_id, updated_at);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    sha256 TEXT NOT NULL,
    original_name TEXT NOT NULL,
    encrypted_path TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, workspace_id, sha256)
);

CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    logical_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, workspace_id, logical_name)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    schema_json TEXT NOT NULL,
    lineage_json TEXT NOT NULL,
    parent_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_workspace
    ON artifacts(tenant_id, workspace_id, created_at);

CREATE TABLE IF NOT EXISTS dataset_versions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    file_id TEXT NOT NULL REFERENCES files(id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    sheet_name TEXT,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, version)
);

CREATE TABLE IF NOT EXISTS column_policies (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    dataset_version_id TEXT NOT NULL REFERENCES dataset_versions(id),
    column_name TEXT NOT NULL,
    masking_domain TEXT NOT NULL,
    normalizer TEXT NOT NULL,
    key_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_version_id, column_name)
);

CREATE TABLE IF NOT EXISTS token_mappings (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    domain TEXT NOT NULL,
    key_version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    token TEXT NOT NULL,
    encrypted_value BLOB NOT NULL,
    nonce BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id, domain, key_version, fingerprint),
    UNIQUE(tenant_id, token)
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    title TEXT NOT NULL,
    safe_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    safe_content TEXT NOT NULL,
    artifact_id TEXT REFERENCES artifacts(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(tenant_id, conversation_id, created_at);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    workspace_id TEXT REFERENCES workspaces(id),
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    source_artifact_id TEXT NOT NULL REFERENCES artifacts(id),
    safe_request TEXT NOT NULL,
    context_json TEXT NOT NULL,
    status TEXT NOT NULL,
    repair_count INTEGER NOT NULL DEFAULT 0,
    max_repairs INTEGER NOT NULL,
    result_artifact_id TEXT REFERENCES artifacts(id),
    error_kind TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_workspace
    ON analysis_runs(tenant_id, workspace_id, updated_at);

CREATE TABLE IF NOT EXISTS analysis_plan_versions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    feedback TEXT,
    plan_json TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(run_id, version)
);

CREATE TABLE IF NOT EXISTS analysis_attempts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    plan_version INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    quality_json TEXT,
    error_kind TEXT,
    error_message TEXT,
    result_artifact_id TEXT REFERENCES artifacts(id),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(run_id, attempt_number)
);
"""


@dataclass(frozen=True)
class DeletionImpact:
    file_id: str
    original_name: str
    sha256: str
    dataset_version_ids: tuple[str, ...]
    dataset_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    analysis_run_ids: tuple[str, ...]
    paths: tuple[Path, ...]

    @property
    def dataset_version_count(self) -> int:
        return len(self.dataset_version_ids)

    @property
    def artifact_count(self) -> int:
        return len(self.artifact_ids)

    @property
    def analysis_run_count(self) -> int:
        return len(self.analysis_run_ids)


class SQLiteRepository:
    """Tenant-scoped SQLite repository with one short-lived connection per operation."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_tenant(self, tenant_id: str, name: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO tenants(id, name, created_at) VALUES (?, ?, ?)",
                (tenant_id, name or tenant_id, utc_now()),
            )

    def create_workspace(self, tenant_id: str, name: str) -> WorkspaceRecord:
        self.ensure_tenant(tenant_id)
        workspace_id = str(uuid.uuid4())
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO workspaces(id, tenant_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)""",
                (workspace_id, tenant_id, name, created_at, created_at),
            )
        return WorkspaceRecord(workspace_id, tenant_id, name, created_at, created_at)

    def get_workspace(self, tenant_id: str, workspace_id: str) -> WorkspaceRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ? AND tenant_id = ?",
                (workspace_id, tenant_id),
            ).fetchone()
            if row is None:
                self._raise_scoped(connection, "workspaces", workspace_id, tenant_id)
        return self._workspace_from_row(row)

    def list_workspaces(self, tenant_id: str) -> list[WorkspaceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workspaces WHERE tenant_id = ? ORDER BY updated_at DESC",
                (tenant_id,),
            ).fetchall()
        return [self._workspace_from_row(row) for row in rows]

    def touch_workspace(self, tenant_id: str, workspace_id: str) -> None:
        self.get_workspace(tenant_id, workspace_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE workspaces SET updated_at = ? WHERE id = ? AND tenant_id = ?",
                (utc_now(), workspace_id, tenant_id),
            )

    def find_mapping_by_fingerprint(
        self, tenant_id: str, domain: str, key_version: int, fingerprint: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM token_mappings
                WHERE tenant_id = ? AND domain = ? AND key_version = ? AND fingerprint = ?""",
                (tenant_id, domain, key_version, fingerprint),
            ).fetchone()
        return dict(row) if row else None

    def find_mapping_by_token(self, tenant_id: str, token: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM token_mappings WHERE tenant_id = ? AND token = ?",
                (tenant_id, token),
            ).fetchone()
        return dict(row) if row else None

    def insert_mapping(self, mapping: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO token_mappings(
                    tenant_id, domain, key_version, fingerprint, token,
                    encrypted_value, nonce, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mapping["tenant_id"],
                    mapping["domain"],
                    mapping["key_version"],
                    mapping["fingerprint"],
                    mapping["token"],
                    mapping["encrypted_value"],
                    mapping["nonce"],
                    mapping.get("created_at", utc_now()),
                ),
            )

    def list_mappings(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM token_mappings WHERE tenant_id = ? ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_file_by_hash(
        self, tenant_id: str, workspace_id: str, sha256: str
    ) -> dict[str, Any] | None:
        self.get_workspace(tenant_id, workspace_id)
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM files
                WHERE tenant_id = ? AND workspace_id = ? AND sha256 = ?""",
                (tenant_id, workspace_id, sha256),
            ).fetchone()
        return dict(row) if row else None

    def create_file(
        self,
        tenant_id: str,
        workspace_id: str,
        sha256: str,
        original_name: str,
        encrypted_path: str,
        byte_size: int,
    ) -> dict[str, Any]:
        self.get_workspace(tenant_id, workspace_id)
        record = {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "sha256": sha256,
            "original_name": original_name,
            "encrypted_path": encrypted_path,
            "byte_size": byte_size,
            "created_at": utc_now(),
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO files(
                    id, tenant_id, workspace_id, sha256, original_name,
                    encrypted_path, byte_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(record.values()),
            )
        return record

    def publish_ingestion(
        self,
        tenant_id: str,
        workspace_id: str,
        sha256: str,
        original_name: str,
        encrypted_path: str,
        byte_size: int,
        tables: list[
            tuple[ArtifactRecord, str | None, dict[str, tuple[str, str, int]]]
        ],
    ) -> tuple[dict[str, Any], bool]:
        """Publish one ingestion atomically, returning the winner of duplicate races."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workspace = connection.execute(
                "SELECT tenant_id FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            if workspace is None:
                raise RecordNotFoundError("Workspace not found.")
            if workspace["tenant_id"] != tenant_id:
                raise AuthorizationError("Workspace belongs to another tenant.")
            existing = connection.execute(
                """SELECT * FROM files
                WHERE tenant_id = ? AND workspace_id = ? AND sha256 = ?""",
                (tenant_id, workspace_id, sha256),
            ).fetchone()
            if existing:
                return dict(existing), True

            file_record = {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "sha256": sha256,
                "original_name": original_name,
                "encrypted_path": encrypted_path,
                "byte_size": byte_size,
                "created_at": utc_now(),
            }
            connection.execute(
                """INSERT INTO files(
                    id, tenant_id, workspace_id, sha256, original_name,
                    encrypted_path, byte_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(file_record.values()),
            )
            for artifact, sheet_name, policies in tables:
                self._publish_artifact(connection, artifact)
                connection.execute(
                    """INSERT OR IGNORE INTO datasets(
                        id, tenant_id, workspace_id, logical_name, created_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), tenant_id, workspace_id, artifact.name, utc_now()),
                )
                dataset = connection.execute(
                    """SELECT id FROM datasets
                    WHERE tenant_id = ? AND workspace_id = ? AND logical_name = ?""",
                    (tenant_id, workspace_id, artifact.name),
                ).fetchone()
                next_version = connection.execute(
                    """SELECT COALESCE(MAX(version), 0) + 1 FROM dataset_versions
                    WHERE dataset_id = ?""",
                    (dataset["id"],),
                ).fetchone()[0]
                version_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO dataset_versions(
                        id, tenant_id, dataset_id, file_id, artifact_id,
                        sheet_name, version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        version_id,
                        tenant_id,
                        dataset["id"],
                        file_record["id"],
                        artifact.id,
                        sheet_name,
                        next_version,
                        utc_now(),
                    ),
                )
                for column_name, (domain, normalizer, key_version) in policies.items():
                    connection.execute(
                        """INSERT INTO column_policies(
                            id, tenant_id, dataset_version_id, column_name,
                            masking_domain, normalizer, key_version, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(uuid.uuid4()),
                            tenant_id,
                            version_id,
                            column_name,
                            domain,
                            normalizer,
                            key_version,
                            utc_now(),
                        ),
                    )
            return file_record, False

    @staticmethod
    def _publish_artifact(connection: sqlite3.Connection, artifact: ArtifactRecord) -> None:
        lineage = {name: value.to_dict() for name, value in artifact.lineage.items()}
        connection.execute(
            """INSERT INTO artifacts(
                id, tenant_id, workspace_id, kind, name, path, row_count,
                schema_json, lineage_json, parent_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact.id,
                artifact.tenant_id,
                artifact.workspace_id,
                artifact.kind,
                artifact.name,
                str(artifact.path),
                artifact.row_count,
                json.dumps(artifact.schema, ensure_ascii=False),
                json.dumps(lineage, ensure_ascii=False),
                json.dumps(artifact.parent_ids),
                artifact.created_at or utc_now(),
            ),
        )

    def get_file_versions(self, tenant_id: str, file_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            owner = connection.execute(
                "SELECT tenant_id FROM files WHERE id = ?", (file_id,)
            ).fetchone()
            if owner is None:
                raise RecordNotFoundError("File not found.")
            if owner["tenant_id"] != tenant_id:
                raise AuthorizationError("File belongs to another tenant.")
            rows = connection.execute(
                """SELECT dv.*, d.logical_name FROM dataset_versions dv
                JOIN datasets d ON d.id = dv.dataset_id
                WHERE dv.tenant_id = ? AND dv.file_id = ? ORDER BY d.logical_name""",
                (tenant_id, file_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def inspect_file_deletion(
        self, tenant_id: str, workspace_id: str, file_id: str
    ) -> DeletionImpact:
        with self._connect() as connection:
            return self._inspect_file_deletion(
                connection, tenant_id, workspace_id, file_id
            )

    def delete_file_metadata(
        self,
        tenant_id: str,
        workspace_id: str,
        impact: DeletionImpact,
    ) -> DeletionImpact:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actual = self._inspect_file_deletion(
                connection, tenant_id, workspace_id, impact.file_id
            )
            self._delete_ids(
                connection,
                "analysis_plan_versions",
                "run_id",
                actual.analysis_run_ids,
                tenant_id,
            )
            self._delete_ids(
                connection,
                "analysis_attempts",
                "run_id",
                actual.analysis_run_ids,
                tenant_id,
            )
            self._delete_ids(
                connection,
                "analysis_runs",
                "id",
                actual.analysis_run_ids,
                tenant_id,
            )
            if actual.artifact_ids:
                placeholders = ",".join("?" for _ in actual.artifact_ids)
                connection.execute(
                    f"""UPDATE messages SET artifact_id = NULL
                    WHERE tenant_id = ? AND artifact_id IN ({placeholders})""",
                    (tenant_id, *actual.artifact_ids),
                )
            self._delete_ids(
                connection,
                "column_policies",
                "dataset_version_id",
                actual.dataset_version_ids,
                tenant_id,
            )
            self._delete_ids(
                connection,
                "dataset_versions",
                "id",
                actual.dataset_version_ids,
                tenant_id,
            )
            self._delete_ids(
                connection,
                "artifacts",
                "id",
                actual.artifact_ids,
                tenant_id,
            )
            cursor = connection.execute(
                """DELETE FROM files
                WHERE id = ? AND tenant_id = ? AND workspace_id = ?""",
                (actual.file_id, tenant_id, workspace_id),
            )
            if cursor.rowcount != 1:
                raise RecordNotFoundError("File not found during deletion.")
            if actual.dataset_ids:
                placeholders = ",".join("?" for _ in actual.dataset_ids)
                connection.execute(
                    f"""DELETE FROM datasets
                    WHERE tenant_id = ? AND workspace_id = ?
                    AND id IN ({placeholders})
                    AND NOT EXISTS (
                        SELECT 1 FROM dataset_versions dv WHERE dv.dataset_id = datasets.id
                    )""",
                    (tenant_id, workspace_id, *actual.dataset_ids),
                )
            connection.execute(
                """INSERT INTO audit_events(
                    id, tenant_id, workspace_id, event_type, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    tenant_id,
                    workspace_id,
                    "file_deleted",
                    json.dumps(
                        {
                            "file_id": actual.file_id,
                            "sha256_prefix": actual.sha256[:12],
                            "dataset_version_count": actual.dataset_version_count,
                            "artifact_count": actual.artifact_count,
                            "analysis_run_count": actual.analysis_run_count,
                        },
                        ensure_ascii=True,
                    ),
                    utc_now(),
                ),
            )
            return actual

    @staticmethod
    def _delete_ids(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        record_ids: tuple[str, ...],
        tenant_id: str,
    ) -> None:
        if not record_ids:
            return
        allowed = {
            ("analysis_plan_versions", "run_id"),
            ("analysis_attempts", "run_id"),
            ("analysis_runs", "id"),
            ("column_policies", "dataset_version_id"),
            ("dataset_versions", "id"),
            ("artifacts", "id"),
        }
        if (table, column) not in allowed:
            raise ValueError("Unsupported scoped deletion target.")
        placeholders = ",".join("?" for _ in record_ids)
        connection.execute(
            f"DELETE FROM {table} WHERE tenant_id = ? AND {column} IN ({placeholders})",
            (tenant_id, *record_ids),
        )

    @staticmethod
    def _inspect_file_deletion(
        connection: sqlite3.Connection,
        tenant_id: str,
        workspace_id: str,
        file_id: str,
    ) -> DeletionImpact:
        file_row = connection.execute(
            "SELECT * FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        if file_row is None:
            raise RecordNotFoundError("File not found.")
        if file_row["tenant_id"] != tenant_id:
            raise AuthorizationError("File belongs to another tenant.")
        if file_row["workspace_id"] != workspace_id:
            raise AuthorizationError("File belongs to another workspace.")

        versions = connection.execute(
            """SELECT id, dataset_id, artifact_id FROM dataset_versions
            WHERE tenant_id = ? AND file_id = ?""",
            (tenant_id, file_id),
        ).fetchall()
        artifact_ids = {row["artifact_id"] for row in versions}
        all_artifacts = connection.execute(
            """SELECT id, path, parent_ids_json FROM artifacts
            WHERE tenant_id = ? AND workspace_id = ?""",
            (tenant_id, workspace_id),
        ).fetchall()
        changed = True
        while changed:
            changed = False
            for artifact in all_artifacts:
                parents = set(json.loads(artifact["parent_ids_json"]))
                if artifact["id"] not in artifact_ids and parents & artifact_ids:
                    artifact_ids.add(artifact["id"])
                    changed = True

        if artifact_ids:
            placeholders = ",".join("?" for _ in artifact_ids)
            runs = connection.execute(
                f"""SELECT id FROM analysis_runs
                WHERE tenant_id = ? AND workspace_id = ?
                AND (source_artifact_id IN ({placeholders})
                     OR result_artifact_id IN ({placeholders}))""",
                (tenant_id, workspace_id, *artifact_ids, *artifact_ids),
            ).fetchall()
        else:
            runs = []
        artifact_paths = [
            Path(row["path"]) for row in all_artifacts if row["id"] in artifact_ids
        ]
        return DeletionImpact(
            file_id=file_id,
            original_name=file_row["original_name"],
            sha256=file_row["sha256"],
            dataset_version_ids=tuple(sorted(row["id"] for row in versions)),
            dataset_ids=tuple(sorted({row["dataset_id"] for row in versions})),
            artifact_ids=tuple(sorted(artifact_ids)),
            analysis_run_ids=tuple(sorted(row["id"] for row in runs)),
            paths=(Path(file_row["encrypted_path"]), *sorted(artifact_paths)),
        )

    def list_files(
        self, tenant_id: str, workspace_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.get_workspace(tenant_id, workspace_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, sha256, original_name, byte_size, created_at
                FROM files WHERE tenant_id = ? AND workspace_id = ?
                ORDER BY created_at DESC LIMIT ?""",
                (tenant_id, workspace_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_or_create_dataset(
        self, tenant_id: str, workspace_id: str, logical_name: str
    ) -> dict[str, Any]:
        self.get_workspace(tenant_id, workspace_id)
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM datasets
                WHERE tenant_id = ? AND workspace_id = ? AND logical_name = ?""",
                (tenant_id, workspace_id, logical_name),
            ).fetchone()
            if row:
                return dict(row)
            record = {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "logical_name": logical_name,
                "created_at": utc_now(),
            }
            connection.execute(
                """INSERT INTO datasets(id, tenant_id, workspace_id, logical_name, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                tuple(record.values()),
            )
        return record

    def create_dataset_version(
        self,
        tenant_id: str,
        dataset_id: str,
        file_id: str,
        artifact_id: str,
        sheet_name: str | None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            dataset = connection.execute(
                "SELECT * FROM datasets WHERE id = ? AND tenant_id = ?",
                (dataset_id, tenant_id),
            ).fetchone()
            if dataset is None:
                self._raise_scoped(connection, "datasets", dataset_id, tenant_id)
            next_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM dataset_versions WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()[0]
            record = {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "dataset_id": dataset_id,
                "file_id": file_id,
                "artifact_id": artifact_id,
                "sheet_name": sheet_name,
                "version": next_version,
                "created_at": utc_now(),
            }
            connection.execute(
                """INSERT INTO dataset_versions(
                    id, tenant_id, dataset_id, file_id, artifact_id,
                    sheet_name, version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(record.values()),
            )
        return record

    def add_column_policy(
        self,
        tenant_id: str,
        dataset_version_id: str,
        column_name: str,
        masking_domain: str,
        normalizer: str,
        key_version: int,
    ) -> None:
        with self._connect() as connection:
            owner = connection.execute(
                "SELECT tenant_id FROM dataset_versions WHERE id = ?", (dataset_version_id,)
            ).fetchone()
            if owner is None:
                raise RecordNotFoundError("Dataset version not found.")
            if owner["tenant_id"] != tenant_id:
                raise AuthorizationError("Dataset version belongs to another tenant.")
            connection.execute(
                """INSERT INTO column_policies(
                    id, tenant_id, dataset_version_id, column_name,
                    masking_domain, normalizer, key_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    tenant_id,
                    dataset_version_id,
                    column_name,
                    masking_domain,
                    normalizer,
                    key_version,
                    utc_now(),
                ),
            )

    def list_datasets(self, tenant_id: str, workspace_id: str) -> list[dict[str, Any]]:
        self.get_workspace(tenant_id, workspace_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT d.*, dv.version, dv.artifact_id, dv.sheet_name,
                    dv.created_at AS version_created_at
                FROM datasets d
                LEFT JOIN dataset_versions dv ON dv.id = (
                    SELECT id FROM dataset_versions
                    WHERE dataset_id = d.id ORDER BY version DESC LIMIT 1
                )
                WHERE d.tenant_id = ? AND d.workspace_id = ?
                ORDER BY d.logical_name""",
                (tenant_id, workspace_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_artifact(self, artifact: ArtifactRecord) -> None:
        self.get_workspace(artifact.tenant_id, artifact.workspace_id)
        lineage = {name: value.to_dict() for name, value in artifact.lineage.items()}
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO artifacts(
                    id, tenant_id, workspace_id, kind, name, path, row_count,
                    schema_json, lineage_json, parent_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    artifact.id,
                    artifact.tenant_id,
                    artifact.workspace_id,
                    artifact.kind,
                    artifact.name,
                    str(artifact.path),
                    artifact.row_count,
                    json.dumps(artifact.schema, ensure_ascii=False),
                    json.dumps(lineage, ensure_ascii=False),
                    json.dumps(artifact.parent_ids),
                    artifact.created_at or utc_now(),
                ),
            )

    def get_artifact(self, tenant_id: str, artifact_id: str) -> ArtifactRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ? AND tenant_id = ?",
                (artifact_id, tenant_id),
            ).fetchone()
            if row is None:
                self._raise_scoped(connection, "artifacts", artifact_id, tenant_id)
        return self._artifact_from_row(row)

    def list_artifacts(self, tenant_id: str, workspace_id: str) -> list[ArtifactRecord]:
        self.get_workspace(tenant_id, workspace_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM artifacts WHERE tenant_id = ? AND workspace_id = ?
                ORDER BY created_at DESC""",
                (tenant_id, workspace_id),
            ).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def create_conversation(
        self, tenant_id: str, workspace_id: str, title: str = "数据分析"
    ) -> dict[str, Any]:
        self.get_workspace(tenant_id, workspace_id)
        now = utc_now()
        record = {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "title": title,
            "safe_summary": "",
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO conversations(
                    id, tenant_id, workspace_id, title, safe_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                tuple(record.values()),
            )
        return record

    def get_or_create_conversation(self, tenant_id: str, workspace_id: str) -> dict[str, Any]:
        self.get_workspace(tenant_id, workspace_id)
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM conversations
                WHERE tenant_id = ? AND workspace_id = ? ORDER BY updated_at DESC LIMIT 1""",
                (tenant_id, workspace_id),
            ).fetchone()
        return dict(row) if row else self.create_conversation(tenant_id, workspace_id)

    def get_conversation(self, tenant_id: str, conversation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ? AND tenant_id = ?",
                (conversation_id, tenant_id),
            ).fetchone()
            if row is None:
                self._raise_scoped(connection, "conversations", conversation_id, tenant_id)
        return dict(row)

    def update_conversation_summary(
        self, tenant_id: str, conversation_id: str, safe_summary: str
    ) -> None:
        self.get_conversation(tenant_id, conversation_id)
        with self._connect() as connection:
            connection.execute(
                """UPDATE conversations SET safe_summary = ?, updated_at = ?
                WHERE id = ? AND tenant_id = ?""",
                (safe_summary, utc_now(), conversation_id, tenant_id),
            )

    def add_message(
        self,
        tenant_id: str,
        conversation_id: str,
        role: str,
        safe_content: str,
        artifact_id: str | None = None,
    ) -> MessageRecord:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Unsupported message role.")
        message_id = str(uuid.uuid4())
        created_at = utc_now()
        with self._connect() as connection:
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE id = ? AND tenant_id = ?",
                (conversation_id, tenant_id),
            ).fetchone()
            if conversation is None:
                self._raise_scoped(connection, "conversations", conversation_id, tenant_id)
            if artifact_id:
                artifact = connection.execute(
                    "SELECT id FROM artifacts WHERE id = ? AND tenant_id = ?",
                    (artifact_id, tenant_id),
                ).fetchone()
                if artifact is None:
                    self._raise_scoped(connection, "artifacts", artifact_id, tenant_id)
            connection.execute(
                """INSERT INTO messages(
                    id, tenant_id, conversation_id, role, safe_content, artifact_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    tenant_id,
                    conversation_id,
                    role,
                    safe_content,
                    artifact_id,
                    created_at,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (created_at, conversation_id),
            )
        return MessageRecord(
            message_id,
            tenant_id,
            conversation_id,
            role,
            safe_content,
            artifact_id,
            created_at,
        )

    def list_messages(
        self, tenant_id: str, conversation_id: str, limit: int = 20
    ) -> list[MessageRecord]:
        with self._connect() as connection:
            conversation = connection.execute(
                "SELECT tenant_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation is None:
                raise RecordNotFoundError("Conversation not found.")
            if conversation["tenant_id"] != tenant_id:
                raise AuthorizationError("Conversation belongs to another tenant.")
            rows = connection.execute(
                """SELECT * FROM (
                    SELECT * FROM messages WHERE tenant_id = ? AND conversation_id = ?
                    ORDER BY created_at DESC LIMIT ?
                ) ORDER BY created_at""",
                (tenant_id, conversation_id, limit),
            ).fetchall()
        return [
            MessageRecord(
                row["id"],
                row["tenant_id"],
                row["conversation_id"],
                row["role"],
                row["safe_content"],
                row["artifact_id"],
                row["created_at"],
            )
            for row in rows
        ]

    def add_audit_event(
        self,
        tenant_id: str,
        event_type: str,
        workspace_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        safe_details = details or {}
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO audit_events(
                    id, tenant_id, workspace_id, event_type, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    tenant_id,
                    workspace_id,
                    event_type,
                    json.dumps(safe_details, ensure_ascii=True),
                    utc_now(),
                ),
            )

    def list_audit_events(
        self, tenant_id: str, workspace_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        self.get_workspace(tenant_id, workspace_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, event_type, details_json, created_at
                FROM audit_events WHERE tenant_id = ? AND workspace_id = ?
                ORDER BY created_at DESC LIMIT ?""",
                (tenant_id, workspace_id, limit),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "details": json.loads(row["details_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def create_analysis_run(
        self,
        tenant_id: str,
        workspace_id: str,
        conversation_id: str,
        source_artifact_id: str,
        safe_request: str,
        context: dict[str, Any],
        max_repairs: int,
    ) -> dict[str, Any]:
        self.get_workspace(tenant_id, workspace_id)
        conversation = self.get_conversation(tenant_id, conversation_id)
        artifact = self.get_artifact(tenant_id, source_artifact_id)
        if conversation["workspace_id"] != workspace_id or artifact.workspace_id != workspace_id:
            raise AuthorizationError("Analysis inputs must belong to the selected workspace.")
        now = utc_now()
        record = {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "conversation_id": conversation_id,
            "source_artifact_id": source_artifact_id,
            "safe_request": safe_request,
            "context_json": json.dumps(context, ensure_ascii=False),
            "status": "planning",
            "repair_count": 0,
            "max_repairs": max_repairs,
            "result_artifact_id": None,
            "error_kind": None,
            "error_message": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO analysis_runs(
                    id, tenant_id, workspace_id, conversation_id, source_artifact_id,
                    safe_request, context_json, status, repair_count, max_repairs,
                    result_artifact_id, error_kind, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(record.values()),
            )
        return self._analysis_run_from_record(record)

    def get_analysis_run(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_runs WHERE id = ? AND tenant_id = ?",
                (run_id, tenant_id),
            ).fetchone()
            if row is None:
                self._raise_scoped(connection, "analysis_runs", run_id, tenant_id)
        return self._analysis_run_from_record(dict(row))

    def list_analysis_runs(
        self, tenant_id: str, workspace_id: str, conversation_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.get_workspace(tenant_id, workspace_id)
        query = "SELECT * FROM analysis_runs WHERE tenant_id = ? AND workspace_id = ?"
        params: list[Any] = [tenant_id, workspace_id]
        if conversation_id:
            query += " AND conversation_id = ?"
            params.append(conversation_id)
        query += " ORDER BY updated_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._analysis_run_from_record(dict(row)) for row in rows]

    def update_analysis_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        expected_status: str | tuple[str, ...] | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        allowed = {
            "status",
            "repair_count",
            "result_artifact_id",
            "error_kind",
            "error_message",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("Unsupported analysis run update.")
        self.get_analysis_run(tenant_id, run_id)
        assignments = [f"{name} = ?" for name in changes]
        values = list(changes.values())
        assignments.append("updated_at = ?")
        values.append(utc_now())
        query = f"UPDATE analysis_runs SET {', '.join(assignments)} WHERE id = ? AND tenant_id = ?"
        values.extend([run_id, tenant_id])
        if expected_status is not None:
            statuses = (expected_status,) if isinstance(expected_status, str) else expected_status
            query += f" AND status IN ({','.join('?' for _ in statuses)})"
            values.extend(statuses)
        with self._connect() as connection:
            cursor = connection.execute(query, values)
            if cursor.rowcount != 1:
                raise ValueError("Analysis run state changed; refresh and try again.")
        return self.get_analysis_run(tenant_id, run_id)

    def create_analysis_plan_version(
        self,
        tenant_id: str,
        run_id: str,
        plan: dict[str, Any],
        reason: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        self.get_analysis_run(tenant_id, run_id)
        created_at = utc_now()
        with self._connect() as connection:
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM analysis_plan_versions WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            record = {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "run_id": run_id,
                "version": version,
                "reason": reason,
                "feedback": feedback,
                "plan_json": json.dumps(plan, ensure_ascii=False, sort_keys=True),
                "decision": "pending",
                "created_at": created_at,
                "decided_at": None,
            }
            connection.execute(
                """INSERT INTO analysis_plan_versions(
                    id, tenant_id, run_id, version, reason, feedback, plan_json,
                    decision, created_at, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(record.values()),
            )
        return self._analysis_plan_from_record(record)

    def list_analysis_plan_versions(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        self.get_analysis_run(tenant_id, run_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM analysis_plan_versions
                WHERE tenant_id = ? AND run_id = ? ORDER BY version""",
                (tenant_id, run_id),
            ).fetchall()
        return [self._analysis_plan_from_record(dict(row)) for row in rows]

    def decide_analysis_plan_version(
        self, tenant_id: str, run_id: str, version: int, decision: str
    ) -> None:
        if decision not in {"confirmed", "rejected", "superseded"}:
            raise ValueError("Unsupported plan decision.")
        self.get_analysis_run(tenant_id, run_id)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE analysis_plan_versions SET decision = ?, decided_at = ?
                WHERE tenant_id = ? AND run_id = ? AND version = ? AND decision = 'pending'""",
                (decision, utc_now(), tenant_id, run_id, version),
            )
            if cursor.rowcount != 1:
                raise ValueError("Plan version is no longer pending.")

    def create_analysis_attempt(
        self, tenant_id: str, run_id: str, plan_version: int
    ) -> dict[str, Any]:
        self.get_analysis_run(tenant_id, run_id)
        created_at = utc_now()
        with self._connect() as connection:
            attempt_number = connection.execute(
                """SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM analysis_attempts WHERE run_id = ?""",
                (run_id,),
            ).fetchone()[0]
            record = {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "run_id": run_id,
                "plan_version": plan_version,
                "attempt_number": attempt_number,
                "status": "executing",
                "quality_json": None,
                "error_kind": None,
                "error_message": None,
                "result_artifact_id": None,
                "created_at": created_at,
                "completed_at": None,
            }
            connection.execute(
                """INSERT INTO analysis_attempts(
                    id, tenant_id, run_id, plan_version, attempt_number, status,
                    quality_json, error_kind, error_message, result_artifact_id,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(record.values()),
            )
        return self._analysis_attempt_from_record(record)

    def finish_analysis_attempt(
        self,
        tenant_id: str,
        attempt_id: str,
        *,
        status: str,
        quality: dict[str, Any] | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
        result_artifact_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE analysis_attempts SET status = ?, quality_json = ?, error_kind = ?,
                    error_message = ?, result_artifact_id = ?, completed_at = ?
                WHERE id = ? AND tenant_id = ? AND status = 'executing'""",
                (
                    status,
                    json.dumps(quality, ensure_ascii=False) if quality is not None else None,
                    error_kind,
                    error_message,
                    result_artifact_id,
                    utc_now(),
                    attempt_id,
                    tenant_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Analysis attempt is no longer active.")

    def list_analysis_attempts(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        self.get_analysis_run(tenant_id, run_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM analysis_attempts
                WHERE tenant_id = ? AND run_id = ? ORDER BY attempt_number""",
                (tenant_id, run_id),
            ).fetchall()
        return [self._analysis_attempt_from_record(dict(row)) for row in rows]

    @staticmethod
    def _analysis_run_from_record(record: dict[str, Any]) -> dict[str, Any]:
        value = dict(record)
        value["context"] = json.loads(value.pop("context_json"))
        return value

    @staticmethod
    def _analysis_plan_from_record(record: dict[str, Any]) -> dict[str, Any]:
        value = dict(record)
        value["plan"] = json.loads(value.pop("plan_json"))
        return value

    @staticmethod
    def _analysis_attempt_from_record(record: dict[str, Any]) -> dict[str, Any]:
        value = dict(record)
        raw_quality = value.pop("quality_json")
        value["quality"] = json.loads(raw_quality) if raw_quality else None
        return value

    @staticmethod
    def _workspace_from_row(row: sqlite3.Row) -> WorkspaceRecord:
        return WorkspaceRecord(
            row["id"], row["tenant_id"], row["name"], row["created_at"], row["updated_at"]
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
        lineage_json = json.loads(row["lineage_json"])
        return ArtifactRecord(
            id=row["id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            kind=row["kind"],
            name=row["name"],
            path=Path(row["path"]),
            row_count=row["row_count"],
            schema=json.loads(row["schema_json"]),
            lineage={name: ColumnLineage.from_dict(value) for name, value in lineage_json.items()},
            parent_ids=tuple(json.loads(row["parent_ids_json"])),
            created_at=row["created_at"],
        )

    @staticmethod
    def _raise_scoped(
        connection: sqlite3.Connection, table: str, record_id: str, tenant_id: str
    ) -> None:
        row = connection.execute(
            f"SELECT tenant_id FROM {table} WHERE id = ?", (record_id,)
        ).fetchone()
        if row and row["tenant_id"] != tenant_id:
            raise AuthorizationError("Record belongs to another tenant.")
        raise RecordNotFoundError("Record not found.")
