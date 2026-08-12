# 200MB Large-File DuckDB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support importing and analyzing 200MB CSV files on a 16GB Windows computer without materializing whole datasets in Pandas.

**Architecture:** Stream uploads into encrypted originals and chunked Parquet artifacts, then compile the existing structured `AnalysisPlan` into locally controlled DuckDB queries. Keep Pandas at sample/preview boundaries and preserve the current confirmation, lineage, and workflow semantics.

**Tech Stack:** Python 3.11+, DuckDB, PyArrow, Pandas, openpyxl read-only mode, SQLite, Streamlit, pytest.

## Global Constraints

- CSV maximum: 200 MiB; XLSX/XLSM maximum: 100 MiB.
- Merge maximum: primary artifact 200 MiB and secondary artifact 50 MiB.
- DuckDB receives no model-authored SQL or paths.
- DuckDB per-query limits: 2 GiB memory, 4 threads, 4 GiB temporary storage.
- Preserve `AnalysisPlan`, explicit confirmation, tenant/workspace authorization, lineage, and repair behavior.

---

### Task 1: File limits and sampled preview

**Files:**
- Create: `tuoming_agent/ingestion/limits.py`
- Modify: `tuoming_agent/ingestion/parser.py`
- Modify: `tuoming_agent/ui/app.py`
- Test: `tests/test_large_file_ingestion.py`

**Interfaces:**
- Produces `validate_upload_size(filename: str, size: int) -> None`.
- Produces `preview_file(filename: str, source: BinaryIO, sample_rows: int = 500) -> list[ParsedTable]`.

- [ ] Write failing tests proving exact CSV/Excel size boundaries and proving preview returns at most 500 rows.
- [ ] Run `python -m pytest tests/test_large_file_ingestion.py -v` and confirm boundary/preview failures.
- [ ] Implement extension-aware limits and sampled CSV/Excel preview while retaining `parse_file` compatibility.
- [ ] Update Streamlit to check `UploadedFile.size` before reading and to use sampled preview.
- [ ] Run the focused tests and existing ingestion/UI tests.
- [ ] Commit `feat: add bounded upload preview`.

### Task 2: Chunked Parquet ingestion and streaming original encryption

**Files:**
- Modify: `tuoming_agent/security/crypto.py`
- Modify: `tuoming_agent/security/vault.py`
- Modify: `tuoming_agent/security/masking.py`
- Modify: `tuoming_agent/storage/files.py`
- Modify: `tuoming_agent/ingestion/parser.py`
- Modify: `tuoming_agent/ingestion/service.py`
- Test: `tests/test_large_file_ingestion.py`
- Test: `tests/test_token_vault.py`

**Interfaces:**
- Produces `iter_file_chunks(filename, source, chunk_rows=50_000)` yielding `ParsedTable` batches.
- Produces `SecureFileStore.write_stream(...)` with incremental hashing/encryption and atomic replacement.
- Produces `ArtifactStore.write_chunks(...)` with one stable Arrow schema.
- Produces `TokenVault.tokenize_many(...)` and batch masking without per-cell connections.

- [ ] Write failing tests using a stream that rejects unbounded reads and multiple chunks with repeated sensitive values.
- [ ] Run focused tests and verify failures are caused by current whole-file reads/per-cell behavior.
- [ ] Implement streaming AES-GCM output and chunk iterators for CSV and read-only Excel.
- [ ] Implement batch token lookup/insertion and chunked Parquet writing with temporary cleanup.
- [ ] Refactor `IngestionService` to validate every batch and publish metadata only after successful files exist.
- [ ] Run ingestion, vault, DLP, and workspace tests.
- [ ] Commit `feat: stream masked dataset ingestion`.

### Task 3: Safe DuckDB compiler

**Files:**
- Create: `tuoming_agent/analysis/duckdb_compiler.py`
- Create: `tuoming_agent/analysis/duckdb_runtime.py`
- Modify: `tuoming_agent/config.py`
- Modify: `.env.example`
- Modify: `pyproject.toml`
- Test: `tests/test_duckdb_compiler.py`

**Interfaces:**
- Produces `CompiledQuery(sql: str, parameters: tuple[Any, ...], sources: tuple[AuthorizedSource, ...], lineage: dict[str, ColumnLineage])`.
- Produces `DuckDBRuntime.connection()` configured with resource and external-access restrictions.

- [ ] Write failing compiler tests for every operation plus hostile column names, hostile values, cross-workspace sources, and oversized secondary merge artifacts.
- [ ] Run compiler tests and confirm missing compiler/runtime failures.
- [ ] Implement schema-checked identifier quoting and parameter binding for all existing `AnalysisPlan` operations.
- [ ] Implement authorized local source registration and per-connection memory/thread/temp limits with external access disabled after registration.
- [ ] Add bounded DuckDB dependency and validated configuration defaults.
- [ ] Run compiler/config tests.
- [ ] Commit `feat: compile analysis plans for duckdb`.

### Task 4: Disk-backed execution and metadata quality validation

**Files:**
- Modify: `tuoming_agent/analysis/executor.py`
- Modify: `tuoming_agent/analysis/quality.py`
- Modify: `tuoming_agent/analysis/workflow.py`
- Modify: `tuoming_agent/workspace/service.py`
- Modify: `tuoming_agent/storage/files.py`
- Test: `tests/test_duckdb_executor.py`
- Modify: `tests/test_analysis_workflow.py`

**Interfaces:**
- `AnalysisExecutor.prepare` returns a disk-backed candidate with path, schema, row count, lineage, and parents.
- `AnalysisQualityValidator.validate` accepts disk-backed candidates without loading full data.
- `ArtifactService.publish_candidate(...)` atomically registers a validated result.

- [ ] Write failing parity tests covering select/filter/sort/rename/cast/fill/drop/deduplicate/merge/groupby/derive/head/tail.
- [ ] Write failing tests proving result publication occurs only after quality success and resource failures are classified.
- [ ] Run focused executor/workflow tests and verify expected failures.
- [ ] Execute compiled queries directly to temporary Parquet and inspect schema/count/quality via DuckDB.
- [ ] Preserve the legacy DataFrame executor only as a small-test compatibility adapter, not the production workflow path.
- [ ] Run executor, quality, workflow, authorization, and lineage tests.
- [ ] Commit `feat: execute analysis on disk with duckdb`.

### Task 5: Bounded preview and export

**Files:**
- Modify: `tuoming_agent/workspace/service.py`
- Modify: `tuoming_agent/ui/app.py`
- Create: `tuoming_agent/exporting.py`
- Test: `tests/test_bounded_preview_export.py`
- Modify: `tests/test_ui_smoke.py`

**Interfaces:**
- Produces `ArtifactService.preview(..., limit=1000, restored=False) -> pd.DataFrame`.
- Produces streaming masked CSV/Parquet export and chunked restored CSV export.
- Excel export rejects results exceeding a documented safe row/byte estimate.

- [ ] Write failing tests proving preview reads no more than 1000 rows and restored CSV is emitted in chunks.
- [ ] Run focused tests and confirm current full `read_parquet`/`BytesIO` behavior fails them.
- [ ] Implement DuckDB-limited preview and file-backed/iterator exports.
- [ ] Update result UI to avoid eagerly restoring the complete artifact.
- [ ] Run export and UI tests.
- [ ] Commit `feat: bound result preview and export`.

### Task 6: Recovery, documentation, and performance acceptance

**Files:**
- Create: `tuoming_agent/maintenance.py`
- Create: `tests/performance/test_200mb_pipeline.py`
- Modify: `README.md`
- Modify: `tests/test_config.py`
- Test: `tests/test_large_file_ingestion.py`

**Interfaces:**
- Produces stale temporary-file cleanup scoped to the configured data directory.
- Produces an opt-in performance command reporting elapsed time and peak RSS.

- [ ] Write failing tests for stale temporary cleanup and disk-space rejection.
- [ ] Run focused tests and confirm missing maintenance behavior.
- [ ] Implement scoped cleanup and actionable resource error messages.
- [ ] Document supported limits, DuckDB settings, performance command, and fallback behavior.
- [ ] Run `python -m pytest`, `python -m ruff check .`, `python -m compileall -q tuoming_agent`, and `git diff --check`.
- [ ] Run the opt-in 200MB pipeline benchmark and record whether it meets the 10-minute target.
- [ ] Commit `feat: complete large file pipeline`.

