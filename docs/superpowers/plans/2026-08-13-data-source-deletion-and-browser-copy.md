# Data Source Deletion and Browser Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add confirmed, recoverable cascade deletion for one uploaded data source and restore normal browser `Ctrl+C` copying by hiding Streamlit developer controls.

**Architecture:** `SQLiteRepository` computes and atomically deletes a tenant/workspace-scoped dependency closure. A new `DataSourceService` validates database-owned paths, stages files into a data-directory trash folder, commits metadata deletion, restores files on transaction failure, and cleans trash after success. Streamlit renders an impact preview and explicit confirmation; project config selects viewer toolbar mode.

**Tech Stack:** Python 3.11+, SQLite, pathlib, Streamlit 1.61, pytest, Ruff.

## Global Constraints

- Deletion unit is exactly one `files.id` within one tenant and workspace.
- Preserve chat text and clear only deleted artifact attachments.
- Preserve tenant-wide token mappings and unrelated uploads.
- Include all descendants found through `artifacts.parent_ids_json`.
- Require explicit confirmation when analysis runs or downstream artifacts are affected.
- Validate every moved path is a regular non-link file below `AppConfig.data_dir`.
- Stage files under `.trash/<operation-id>/`; restore them if SQLite deletion fails.
- Write a `file_deleted` audit event without raw values.
- Configure `[client] toolbarMode = "viewer"`; Streamlit restart is required.

---

### Task 1: Repository impact closure and atomic metadata deletion

**Files:**
- Modify: `tuoming_agent/storage/sqlite.py`
- Create: `tests/test_data_source_deletion.py`

**Interfaces:**
- Produces: `DeletionImpact` dataclass with file identity, artifact/dataset-version/run IDs, paths and counts.
- Produces: `SQLiteRepository.inspect_file_deletion(tenant_id, workspace_id, file_id) -> DeletionImpact`.
- Produces: `SQLiteRepository.delete_file_metadata(tenant_id, workspace_id, impact) -> None`.

- [ ] **Step 1: Write failing closure and authorization tests**

Create tests that ingest one source, create a direct and second-generation analysis artifact, create an analysis run and attach a message, then assert:

```python
impact = services.repository.inspect_file_deletion("tenant-a", workspace.id, result.file_id)
assert set(impact.artifact_ids) == {source.id, direct.id, descendant.id}
assert impact.analysis_run_count == 1
assert set(impact.paths) == {
    Path(file_row["encrypted_path"]), source.path, direct.path, descendant.path
}
```

Add a second-tenant call and require `AuthorizationError`; assert inspection leaves every table unchanged.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_data_source_deletion.py -q`

Expected: import or attribute failure because `DeletionImpact` and repository methods do not exist.

- [ ] **Step 3: Implement read-only impact analysis**

Add a frozen dataclass and use one repository connection to validate `(tenant_id, workspace_id, file_id)`, seed source artifacts from `dataset_versions`, parse all workspace artifact parent IDs, repeatedly add children until stable, and select related runs. Return immutable sorted ID/path tuples and display counts. Reject missing/cross-scope records through existing scoped errors.

- [ ] **Step 4: Add failing atomic deletion tests**

Call `delete_file_metadata`, then assert:

```python
assert repository.list_files("tenant-a", workspace.id) == []
assert repository.list_artifacts("tenant-a", workspace.id) == []
assert repository.list_messages("tenant-a", conversation_id)[-1].artifact_id is None
assert repository.list_mappings("tenant-a") != []
assert repository.list_audit_events("tenant-a", workspace.id)[0]["event_type"] == "file_deleted"
```

Create two versions of the same logical dataset from different files, delete only one file, and assert the dataset remains with the other version and its original version number. Install a temporary SQLite trigger that aborts `DELETE FROM files`; assert all metadata remains after the raised error.

- [ ] **Step 5: Implement one-transaction metadata deletion**

Within `BEGIN IMMEDIATE`, re-run/compare scope authorization and delete in this order: `analysis_plan_versions`, `analysis_attempts`, `analysis_runs`, null affected `messages.artifact_id`, `column_policies`, `dataset_versions`, `artifacts`, target `files`, then datasets with no remaining versions. Insert `file_deleted` audit details in the same transaction. Never accept caller-provided IDs outside the inspected immutable impact.

- [ ] **Step 6: Run GREEN and commit**

Run: `python -m pytest tests/test_data_source_deletion.py -q`

Expected: all repository deletion tests pass.

Commit:

```powershell
git add tuoming_agent/storage/sqlite.py tests/test_data_source_deletion.py
git commit -m "feat: delete data source metadata safely"
```

### Task 2: Recoverable filesystem coordination service

**Files:**
- Create: `tuoming_agent/workspace/data_sources.py`
- Modify: `tuoming_agent/workspace/service.py`
- Modify: `tests/test_data_source_deletion.py`

**Interfaces:**
- Consumes: Task 1 `DeletionImpact`, `inspect_file_deletion`, `delete_file_metadata`.
- Produces: `DataSourceService.inspect(...) -> DeletionImpact` and `DataSourceService.delete(...) -> DeletionImpact`.
- Produces: `ApplicationServices.data_sources: DataSourceService`.

- [ ] **Step 1: Write failing success, rollback and path-boundary tests**

Assert successful deletion removes encrypted/Parquet files and `.trash` operation directory. Monkeypatch `delete_file_metadata` to raise after staging and assert every original file exists with unchanged bytes and trash is empty. Insert an impact path outside `data_dir`, a directory path, and a symlink/reparse path where supported; each must raise before any move or metadata mutation.

- [ ] **Step 2: Run RED**

Run: `python -m pytest tests/test_data_source_deletion.py -q -k 'service or restores or path'`

Expected: import/attribute failures because `DataSourceService` is absent.

- [ ] **Step 3: Implement validated staging and recovery**

`DataSourceService` stores `repository` and resolved `data_dir`. For each database-returned path, use `lstat`, reject links/reparse points/non-files, and require `path.resolve().is_relative_to(data_dir.resolve())`. Move with `Path.replace` into `.trash/<uuid>/` using collision-free numbered names. On metadata exception, restore in reverse order and re-raise. On success, unlink staged files and remove only the empty operation/trash directories.

- [ ] **Step 4: Wire application services and run GREEN**

Instantiate the service in `create_services`; update `ApplicationServices`. Run:

```powershell
python -m pytest tests/test_data_source_deletion.py tests/test_ingestion_workspace.py -q
```

Expected: deletion and existing ingestion tests pass.

- [ ] **Step 5: Commit**

```powershell
git add tuoming_agent/workspace/data_sources.py tuoming_agent/workspace/service.py tests/test_data_source_deletion.py
git commit -m "feat: recover files during source deletion"
```

### Task 3: Streamlit impact confirmation and browser copy configuration

**Files:**
- Modify: `tuoming_agent/ui/app.py`
- Modify: `tests/test_large_file_ingestion.py`
- Create: `.streamlit/config.toml`
- Create: `tests/test_streamlit_config.py`

**Interfaces:**
- Consumes: `services.data_sources.inspect` and `.delete`.
- Produces: `_render_file_deletion(...)` UI helper keyed by workspace and file ID.

- [ ] **Step 1: Write failing UI state/confirmation tests**

Render the data view with a file and stub impact. Assert the first delete click stores only a pending file ID. When `analysis_run_count > 0`, assert the final delete call is not made until the acknowledgment checkbox is true. Assert cancel clears pending state. Assert confirmed deletion calls `services.data_sources.delete` once, sets a success flash, and reruns.

- [ ] **Step 2: Write failing configuration test**

```python
import tomllib

def test_streamlit_viewer_toolbar_preserves_browser_copy():
    config = tomllib.loads(Path(".streamlit/config.toml").read_text("utf-8"))
    assert config["client"]["toolbarMode"] == "viewer"
```

Run: `python -m pytest tests/test_large_file_ingestion.py tests/test_streamlit_config.py -q -k 'delet or toolbar'`

Expected: helper/config missing failures.

- [ ] **Step 3: Implement the UI confirmation flow**

Replace the read-only file dataframe with per-file rows containing metadata and a keyed delete button. Store `pending-delete-<workspace>` in session state. Render impact counts below the selected row; require `st.checkbox("我了解关联内容也会删除")` only when analysis/downstream counts are nonzero, and disable the final destructive button until checked. Cancel must only pop session state. Catch `AuthorizationError`, `RecordNotFoundError`, path validation errors and filesystem errors with user-actionable Chinese messages.

- [ ] **Step 4: Add project Streamlit configuration**

Create exactly:

```toml
[client]
toolbarMode = "viewer"
```

- [ ] **Step 5: Run GREEN and commit**

Run:

```powershell
python -m pytest tests/test_large_file_ingestion.py tests/test_ui_smoke.py tests/test_streamlit_config.py -q
```

Expected: UI and toolbar tests pass.

Commit:

```powershell
git add tuoming_agent/ui/app.py tests/test_large_file_ingestion.py .streamlit/config.toml tests/test_streamlit_config.py
git commit -m "feat: confirm data source deletion in ui"
```

### Task 4: Documentation and full verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: completed deletion and viewer-toolbar behavior.
- Produces: user instructions for deletion consequences and required Streamlit restart.

- [ ] **Step 1: Document observable behavior**

Add concise README guidance: delete is file-scoped; impact is previewed; related analysis/downstream results require explicit acknowledgment; chat text and token mappings remain; browser copy fix takes effect after restarting Streamlit.

- [ ] **Step 2: Run full verification**

Run:

```powershell
python -m pytest -q
python -m ruff check .
python -m compileall -q tuoming_agent tests
git diff --check
```

Expected: pytest has zero failures (environment-only skips allowed), Ruff reports `All checks passed!`, compileall and diff check exit 0.

- [ ] **Step 3: Commit final documentation**

```powershell
git add README.md
git commit -m "docs: explain data source deletion"
```

- [ ] **Step 4: Push the existing branch**

```powershell
git push origin codex/agent-core-loop
```

Expected: remote branch advances to the final documentation commit; `.superpowers/` and `.tmp_pytest/` remain untracked.
