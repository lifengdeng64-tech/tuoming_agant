from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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
"""


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
