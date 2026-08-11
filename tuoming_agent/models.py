from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ColumnLineage:
    domain: str
    normalizer: str = "text"
    key_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "normalizer": self.normalizer,
            "key_version": self.key_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ColumnLineage:
        return cls(
            domain=str(data["domain"]),
            normalizer=str(data.get("normalizer", "text")),
            key_version=int(data.get("key_version", 1)),
        )


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    tenant_id: str
    name: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    tenant_id: str
    workspace_id: str
    kind: str
    name: str
    path: Path
    row_count: int
    schema: dict[str, Any]
    lineage: dict[str, ColumnLineage] = field(default_factory=dict)
    parent_ids: tuple[str, ...] = ()
    created_at: str = ""


@dataclass(frozen=True)
class MessageRecord:
    id: str
    tenant_id: str
    conversation_id: str
    role: str
    safe_content: str
    artifact_id: str | None
    created_at: str

