from __future__ import annotations

import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from tuoming_agent.storage.sqlite import (
    DeletionImpact,
    SQLiteRepository,
    TableDeletionImpact,
)

T = TypeVar("T")


class DataSourceService:
    def __init__(self, repository: SQLiteRepository, data_dir: Path):
        self.repository = repository
        self.data_dir = data_dir.resolve()

    def inspect(
        self, tenant_id: str, workspace_id: str, file_id: str
    ) -> DeletionImpact:
        return self.repository.inspect_file_deletion(tenant_id, workspace_id, file_id)

    def delete(
        self, tenant_id: str, workspace_id: str, file_id: str
    ) -> DeletionImpact:
        impact = self.inspect(tenant_id, workspace_id, file_id)
        return self._delete_paths(
            impact.paths,
            lambda: self.repository.delete_file_metadata(
                tenant_id, workspace_id, impact
            ),
        )

    def inspect_table(
        self, tenant_id: str, workspace_id: str, dataset_version_id: str
    ) -> TableDeletionImpact:
        return self.repository.inspect_dataset_version_deletion(
            tenant_id, workspace_id, dataset_version_id
        )

    def delete_table(
        self, tenant_id: str, workspace_id: str, dataset_version_id: str
    ) -> TableDeletionImpact:
        impact = self.inspect_table(tenant_id, workspace_id, dataset_version_id)
        return self._delete_paths(
            impact.paths,
            lambda: self.repository.delete_dataset_version_metadata(
                tenant_id, workspace_id, impact
            ),
        )

    def _delete_paths(
        self, paths: tuple[Path, ...], metadata_delete: Callable[[], T]
    ) -> T:
        paths = tuple(self._validate_path(path) for path in paths)
        operation_dir = self.data_dir / ".trash" / str(uuid.uuid4())
        staged: list[tuple[Path, Path]] = []
        try:
            operation_dir.mkdir(parents=True)
            for index, original in enumerate(paths):
                temporary = operation_dir / f"{index:04d}-{original.name}"
                original.replace(temporary)
                staged.append((original, temporary))
            deleted = metadata_delete()
        except Exception:
            for original, temporary in reversed(staged):
                if temporary.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    temporary.replace(original)
            self._remove_empty_trash(operation_dir)
            raise

        for _original, temporary in staged:
            temporary.unlink(missing_ok=True)
        self._remove_empty_trash(operation_dir)
        return deleted

    def _validate_path(self, path: Path) -> Path:
        candidate = Path(path)
        metadata = candidate.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if candidate.is_symlink() or attributes & reparse:
            raise ValueError("Deletion path must not be a link or reparse point.")
        if not candidate.is_file():
            raise ValueError("Deletion path must be a regular file.")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.data_dir):
            raise ValueError("Deletion path is outside the configured data directory.")
        return resolved

    def _remove_empty_trash(self, operation_dir: Path) -> None:
        if operation_dir.exists():
            operation_dir.rmdir()
        trash_root = self.data_dir / ".trash"
        if trash_root.exists() and not any(trash_root.iterdir()):
            trash_root.rmdir()
