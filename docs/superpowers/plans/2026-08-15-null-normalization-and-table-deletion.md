# 空值统一与工作表级删除实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一导入空值语义，并允许安全删除单个 Excel 工作表及其下游分析，同时保留同文件其他工作表和加密原件。

**Architecture:** 在解析边界统一缺失值，确保预览、敏感检测、脱敏和 Parquet 写入看到相同数据；在 SQLite 仓库新增以 `dataset_version_id` 为根的依赖闭包和原子删除接口，并由现有 `DataSourceService` 复用回收区回滚机制。重复上传相同文件时只重建当前缺失的工作表；UI 在数据集区域提供独立删除确认流程。

**Tech Stack:** Python 3.11+、Pandas、OpenPyXL、PyArrow/Parquet、SQLite、Streamlit、Pytest、Ruff。

## Global Constraints

- 空单元格、空白字符串以及忽略大小写的 `NaN`、`N/A`、`NULL`、`None` 必须转换为真正空值。
- `0`、`-`、“无”、“未完成”必须保留原值。
- 工作表删除不得移动或删除加密原始 Excel，也不得影响同文件其他工作表。
- 有关联分析时必须显示影响范围并要求二次确认；聊天文字和租户级 Token 映射必须保留。
- 完整文件删除行为保持不变。
- 上传按钮固定显示“确认添加”。
- 不自动忽略空白、隐藏或仅一行的工作表；不新增可视化图表。

---

### Task 1: 在解析边界统一空值

**Files:**
- Modify: `tuoming_agent/ingestion/parser.py`
- Test: `tests/test_large_file_ingestion.py`
- Test: `tests/test_ingestion_workspace.py`

**Interfaces:**
- Produces: `_normalize_missing_values(dataframe: pd.DataFrame) -> pd.DataFrame`，供 CSV、Excel 预览和分块导入共同调用。
- Preserves: `preview_file(filename: str, source: BinaryIO, sample_rows: int = 500) -> list[ParsedTable]`、`iter_file_chunks(filename: str, source: BinaryIO, chunk_rows: int = 50_000) -> Iterator[ParsedTable]` 和 `parse_file(filename: str, content: bytes) -> list[ParsedTable]` 的现有签名。

- [ ] **Step 1: 写解析层失败测试**

在 `tests/test_large_file_ingestion.py` 新增真实 Excel 测试，证明常见标记变为空而合法业务值保留：

```python
def test_excel_parser_normalizes_missing_markers_without_changing_business_values():
    content = BytesIO()
    with pd.ExcelWriter(content, engine="openpyxl") as writer:
        pd.DataFrame(
            {"营收完成度": [0.8, "NaN", " n/A ", "NULL", "None", "  ", 0, "-", "无"]}
        ).to_excel(writer, sheet_name="page", index=False)

    table = preview_file("sales.xlsx", BytesIO(content.getvalue()))[0]

    assert table.dataframe.loc[0, "营收完成度"] == 0.8
    assert table.dataframe.loc[6:, "营收完成度"].tolist() == [0, "-", "无"]
    assert table.dataframe.loc[1:5, "营收完成度"].isna().all()
```

再增加 CSV 测试，验证 `iter_file_chunks` 与预览规则一致，并明确 `"未完成"` 不被清空。

- [ ] **Step 2: 运行测试并确认按预期失败**

Run:

```powershell
python -m pytest tests/test_large_file_ingestion.py -k "normalizes_missing_markers" -v
```

Expected: FAIL；`"NULL"`、`"None"`、纯空格或大小写变体仍作为字符串存在。

- [ ] **Step 3: 实现单一空值规范化函数并接入所有解析路径**

在 `parser.py` 增加：

```python
MISSING_TEXT_MARKERS = frozenset({"nan", "n/a", "null", "none"})


def _normalize_missing_values(dataframe: pd.DataFrame) -> pd.DataFrame:
    def normalize(value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped or stripped.casefold() in MISSING_TEXT_MARKERS:
            return pd.NA
        return value

    return dataframe.map(normalize).infer_objects(copy=False)
```

所有 `pd.read_csv` 返回值、Excel 预览 DataFrame 和 `iter_file_chunks` 生成的 Excel DataFrame 都必须在构造 `ParsedTable` 前调用该函数。不要在脱敏服务或 Parquet 写入器里重复规范化。

- [ ] **Step 4: 写端到端失败测试，覆盖原始报错**

在 `tests/test_ingestion_workspace.py` 新增测试：构造包含数值、`"NaN"` 和空单元格的“营收完成度”列，执行 `services.ingestion.ingest("tenant-a", workspace.id, "completion.xlsx", BytesIO(payload), {"completion::page": {}})`，再通过 `services.artifacts.load("tenant-a", artifact.id)` 读取结果并断言：

```python
assert artifact.row_count == 4
assert loaded["营收完成度"].notna().sum() == 2
assert loaded["营收完成度"].dropna().tolist() == [0.8, 0.0]
```

同时为一个被标记脱敏的列加入空值，断言 `list_mappings("tenant-a")` 中没有空值对应的 Token。

- [ ] **Step 5: 运行解析与导入测试**

Run:

```powershell
python -m pytest tests/test_large_file_ingestion.py tests/test_ingestion_workspace.py tests/test_token_vault.py -v
```

Expected: PASS；不再出现 `ArrowInvalid` 或“无法将 NaN 转换为 double”。

- [ ] **Step 6: 提交空值修复**

```powershell
git add tuoming_agent/ingestion/parser.py tests/test_large_file_ingestion.py tests/test_ingestion_workspace.py tests/test_token_vault.py
git commit -m "fix: normalize missing values during ingestion"
```

---

### Task 2: 新增工作表级依赖闭包和原子删除

**Files:**
- Modify: `tuoming_agent/storage/sqlite.py`
- Modify: `tuoming_agent/workspace/data_sources.py`
- Test: `tests/test_data_source_deletion.py`

**Interfaces:**
- Produces: `TableDeletionImpact`，字段为 `dataset_version_id`、`dataset_id`、`file_id`、`logical_name`、`version`、`row_count`、`artifact_ids`、`analysis_run_ids`、`paths`。
- Produces: `SQLiteRepository.inspect_dataset_version_deletion(tenant_id: str, workspace_id: str, dataset_version_id: str) -> TableDeletionImpact`。
- Produces: `SQLiteRepository.delete_dataset_version_metadata(tenant_id: str, workspace_id: str, impact: TableDeletionImpact) -> TableDeletionImpact`。
- Produces: `DataSourceService.inspect_table(tenant_id: str, workspace_id: str, dataset_version_id: str) -> TableDeletionImpact` 和 `DataSourceService.delete_table(tenant_id: str, workspace_id: str, dataset_version_id: str) -> TableDeletionImpact`。

- [ ] **Step 1: 写仓库层影响分析失败测试**

扩展 `tests/test_data_source_deletion.py`，创建同一 XLSX 的两个工作表，其中目标工作表具有直接和间接分析结果。断言：

```python
impact = services.repository.inspect_dataset_version_deletion(
    "tenant-a", workspace.id, target_version_id
)
assert impact.logical_name.endswith("::page")
assert set(impact.artifact_ids) == {source.id, direct.id, descendant.id}
assert other_sheet.id not in impact.artifact_ids
assert encrypted_source not in impact.paths
assert impact.analysis_run_count == 1
```

跨租户测试使用同一个 `target_version_id` 调用：

```python
services.repository.ensure_tenant("tenant-b")
with pytest.raises(AuthorizationError):
    services.repository.inspect_dataset_version_deletion(
        "tenant-b", workspace.id, target_version_id
    )
assert services.repository.get_artifact("tenant-a", source.id) == source
```

- [ ] **Step 2: 运行影响分析测试并确认失败**

Run:

```powershell
python -m pytest tests/test_data_source_deletion.py -k "dataset_version_deletion" -v
```

Expected: FAIL；`TableDeletionImpact` 或仓库接口尚不存在。

- [ ] **Step 3: 提取共享闭包查询并实现 `TableDeletionImpact`**

在 `sqlite.py` 中增加不可变数据类及计数属性：

```python
@dataclass(frozen=True)
class TableDeletionImpact:
    dataset_version_id: str
    dataset_id: str
    file_id: str
    logical_name: str
    version: int
    row_count: int
    artifact_ids: tuple[str, ...]
    analysis_run_ids: tuple[str, ...]
    paths: tuple[Path, ...]

    @property
    def artifact_count(self) -> int:
        return len(self.artifact_ids)

    @property
    def analysis_run_count(self) -> int:
        return len(self.analysis_run_ids)
```

把现有 `_inspect_file_deletion` 中“从源制品递归查找 `parent_ids_json` 后代、相关分析运行和路径”的逻辑提取成私有辅助函数，文件删除与工作表删除共用。工作表检查必须连接 `dataset_versions`、`datasets`、`files` 和源 `artifacts`，同时约束租户与工作区；`paths` 只包含闭包内 Parquet，不包含 `files.encrypted_path`。

- [ ] **Step 4: 写元数据级联删除与回滚失败测试**

覆盖以下断言：

```python
deleted = services.repository.delete_dataset_version_metadata(
    "tenant-a", workspace.id, impact
)
assert deleted.dataset_version_id == target_version_id
assert services.repository.find_file_by_hash("tenant-a", workspace.id, sha256)
assert services.repository.get_artifact("tenant-a", other_sheet.id) == other_sheet
assert services.repository.list_messages("tenant-a", conversation_id)[-1].artifact_id is None
assert services.repository.list_mappings("tenant-a")
assert services.repository.list_audit_events("tenant-a", workspace.id)[0]["event_type"] == (
    "dataset_version_deleted"
)
```

再安装一个阻止 `dataset_versions` 删除的 SQLite trigger，验证事务失败后目标版本、制品、分析运行和聊天附件全部仍存在。

- [ ] **Step 5: 实现元数据原子删除**

`delete_dataset_version_metadata(tenant_id, workspace_id, impact)` 必须：

1. `BEGIN IMMEDIATE` 后重新计算影响并与用户确认时的 `impact` 全量比较；
2. 按顺序删除计划版本、分析尝试、分析运行；
3. 清空闭包内聊天附件引用；
4. 删除目标版本的字段策略和目标 `dataset_versions` 行；
5. 删除闭包内制品；
6. 仅当没有剩余版本时删除对应 `datasets` 行；
7. 保留 `files` 行、加密路径和 Token 映射；
8. 写入 `dataset_version_deleted` 审计事件，记录工作表名、版本和删除计数，不记录单元格数据。

- [ ] **Step 6: 写文件暂存与恢复失败测试**

为 `DataSourceService.delete_table("tenant-a", workspace.id, target_version_id)` 增加成功和失败测试。成功时所有 `impact.paths` 消失，但加密原文件和其他工作表 Parquet 仍存在；失败测试用 `monkeypatch.setattr(services.repository, "delete_dataset_version_metadata", fail_delete)` 模拟 `sqlite3.IntegrityError`，随后比较删除前后的 `{path: path.read_bytes() for path in impact.paths}` 并断言 `.trash` 不存在。

- [ ] **Step 7: 复用回收区机制实现服务接口**

把 `DataSourceService.delete(tenant_id, workspace_id, file_id)` 当前的路径验证、移动、恢复和清理流程提取为私有 `_delete_paths(paths: tuple[Path, ...], metadata_delete: Callable[[], T]) -> T`。文件删除继续传入 `lambda: repository.delete_file_metadata(...)`，工作表删除传入 `lambda: repository.delete_dataset_version_metadata(...)`。两条路径都必须使用 `_validate_path`，且不能放宽链接、重解析点或数据目录边界检查。

- [ ] **Step 8: 运行删除测试并提交**

Run:

```powershell
python -m pytest tests/test_data_source_deletion.py -v
python -m ruff check tuoming_agent/storage/sqlite.py tuoming_agent/workspace/data_sources.py tests/test_data_source_deletion.py
```

Expected: PASS，且现有完整文件删除测试继续通过。

```powershell
git add tuoming_agent/storage/sqlite.py tuoming_agent/workspace/data_sources.py tests/test_data_source_deletion.py
git commit -m "feat: delete individual worksheet datasets safely"
```

---

### Task 3: 相同文件重新上传时只恢复缺失工作表

**Files:**
- Modify: `tuoming_agent/ingestion/service.py`
- Modify: `tuoming_agent/storage/sqlite.py`
- Test: `tests/test_ingestion_workspace.py`
- Test: `tests/test_data_source_deletion.py`

**Interfaces:**
- Produces: `SQLiteRepository.publish_tables_for_existing_file(tenant_id: str, workspace_id: str, file_id: str, tables: list[tuple[ArtifactRecord, str | None, dict[str, tuple[str, str, int]]]]) -> tuple[str, ...]`，返回成功发布的 artifact IDs。
- Preserves: `IngestionResult` 签名；如果至少恢复一张表则 `duplicate=False`，全部工作表仍存在时 `duplicate=True`。

- [ ] **Step 1: 写删除后重复上传的失败测试**

构造包含 `Sheet1` 和 `page` 的相同 XLSX：第一次上传后删除 `page`，第二次上传完全相同的字节。测试主体使用以下明确流程：

```python
buffer = BytesIO()
with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
    pd.DataFrame({"value": [1]}).to_excel(writer, sheet_name="Sheet1", index=False)
    pd.DataFrame({"value": [2, 3]}).to_excel(writer, sheet_name="page", index=False)
payload = buffer.getvalue()
policies = {"sales::Sheet1": {}, "sales::page": {}}
original = services.ingestion.ingest(
    "tenant-a", workspace.id, "sales.xlsx", BytesIO(payload), policies
)
versions = services.repository.get_file_versions("tenant-a", original.file_id)
target_version_id = next(
    row["id"] for row in versions if row["logical_name"] == "sales::page"
)
file_record = services.repository.find_file_by_hash(
    "tenant-a", workspace.id, original.content_hash
)
encrypted_path = Path(file_record["encrypted_path"])
encrypted_before = encrypted_path.read_bytes()
services.data_sources.delete_table("tenant-a", workspace.id, target_version_id)

restored = services.ingestion.ingest(
    "tenant-a", workspace.id, "sales.xlsx", BytesIO(payload), policies
)
versions = services.repository.get_file_versions("tenant-a", original.file_id)
assert restored.file_id == original.file_id
assert restored.duplicate is False
assert {version["logical_name"] for version in versions} == {
    "sales::Sheet1",
    "sales::page",
}
assert len([version for version in versions if version["logical_name"] == "sales::Sheet1"]) == 1
assert encrypted_path.read_bytes() == encrypted_before
```

未删除任何表的测试对同一 `payload` 连续调用两次 `ingest`，记录第一次后的 `set(data_dir.rglob("*.parquet"))`，再断言第二次 `duplicate is True` 且 Parquet 路径集合完全相同。

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/test_ingestion_workspace.py tests/test_data_source_deletion.py -k "reupload" -v
```

Expected: FAIL；当前 `find_file_by_hash` 分支直接返回，缺失工作表不会重建。

- [ ] **Step 3: 实现现有文件的并发安全发布接口**

`publish_tables_for_existing_file(tenant_id, workspace_id, file_id, tables)` 在 `BEGIN IMMEDIATE` 中：

- 验证文件属于同一租户和工作区；
- 查询该文件当前已有的 `datasets.logical_name`；
- 仅发布尚不存在的逻辑表；
- 使用与 `publish_ingestion(...)` 相同的数据集、版本、字段策略写入逻辑；
- 返回真正发布的 artifact IDs，供调用者清理并发竞争中未采用的临时 Parquet。

把重复的单表发布 SQL 提取为 `_publish_dataset_table(connection: sqlite3.Connection, tenant_id: str, workspace_id: str, file_id: str, artifact: ArtifactRecord, sheet_name: str | None, policies: dict[str, tuple[str, str, int]]) -> str`，返回新建的 `dataset_version_id`，由新文件发布和已有文件补表共同调用。

- [ ] **Step 4: 调整导入流程，只处理缺失逻辑表**

`IngestionService.ingest(tenant_id, workspace_id, filename, content, policies, retained_columns=retained_columns)` 在命中相同哈希后不能立即返回。先读取现有 `logical_name` 集合，再通过 `iter_file_chunks(filename, source)` 得到文件内表名：

```python
missing_names = incoming_names - existing_names
if not missing_names:
    return IngestionResult(existing["id"], content_hash, existing_artifacts, True)
```

敏感字段检查、脱敏和 Parquet 写入只处理 `missing_names`。已有文件分支不调用 `SecureFileStore.write_stream`，而是调用 `publish_tables_for_existing_file(...)`；发布竞争中未采用的制品路径立即删除。至少发布一张表时返回 `duplicate=False`。

- [ ] **Step 5: 运行导入、删除和大文件回归测试**

Run:

```powershell
python -m pytest tests/test_ingestion_workspace.py tests/test_data_source_deletion.py tests/test_large_file_ingestion.py -v
```

Expected: PASS；新文件、完整重复文件和缺失工作表恢复三条路径都通过。

- [ ] **Step 6: 提交恢复能力**

```powershell
git add tuoming_agent/ingestion/service.py tuoming_agent/storage/sqlite.py tests/test_ingestion_workspace.py tests/test_data_source_deletion.py
git commit -m "feat: restore missing worksheets on duplicate upload"
```

---

### Task 4: 工作表删除 UI 与“确认添加”文案

**Files:**
- Modify: `tuoming_agent/storage/sqlite.py`
- Modify: `tuoming_agent/ui/app.py`
- Test: `tests/test_ui_smoke.py`

**Interfaces:**
- Extends: `SQLiteRepository.list_datasets(...)` 每项增加 `dataset_version_id`、`file_id` 和 `row_count`。
- Consumes: `DataSourceService.inspect_table(...)`、`delete_table(...)` 和 `TableDeletionImpact`。

- [ ] **Step 1: 写 UI 失败测试**

在 `tests/test_ui_smoke.py` 中创建包含两个工作表的数据文件并渲染“数据”视图，断言：

```python
labels = [button.label for button in app.button]
assert "确认添加" in labels
assert not any(label.startswith("确认并追加") for label in labels)
assert labels.count("删除工作表") == 2
```

模拟点击一个工作表的“删除工作表”，断言警告包含工作表名、行数、关联分析数，并且存在“我了解关联分析和结果也会删除”复选框。点击取消后数据不变；确认后只剩另一张表和原文件记录。

- [ ] **Step 2: 运行 UI 测试并确认失败**

Run:

```powershell
python -m pytest tests/test_ui_smoke.py -k "upload_button or worksheet_deletion" -v
```

Expected: FAIL；当前按钮仍含文件数量，且数据集区域没有独立删除操作。

- [ ] **Step 3: 扩展数据集列表查询**

修改 `list_datasets(...)` 的 SELECT：

```sql
SELECT d.*, dv.id AS dataset_version_id, dv.file_id, dv.version,
       dv.artifact_id, dv.sheet_name, a.row_count,
       dv.created_at AS version_created_at
FROM datasets d
LEFT JOIN dataset_versions dv ON dv.id = (
    SELECT id FROM dataset_versions
    WHERE dataset_id = d.id ORDER BY version DESC LIMIT 1
)
LEFT JOIN artifacts a ON a.id = dv.artifact_id
WHERE d.tenant_id = ? AND d.workspace_id = ?
ORDER BY d.logical_name
```

- [ ] **Step 4: 实现工作表删除交互和固定按钮文案**

把上传按钮标签改为常量 `"确认添加"`。在“数据集与版本”区域保留摘要表，并为每个数据集版本渲染名称、版本、行数和“删除工作表”按钮。

新增 `_render_dataset_version_deletion(services: ApplicationServices, tenant_id: str, workspace_id: str, dataset: dict[str, Any]) -> None`，使用独立状态键：

```python
state_key = f"pending-table-delete-{workspace_id}"
```

确认区必须显示 `logical_name`、`version`、`row_count`、`artifact_count` 和 `analysis_run_count`。当 `analysis_run_count > 0` 或 `artifact_count > 1` 时，要求勾选“我了解关联分析和结果也会删除”。说明文字明确写出“加密原文件和同一文件的其他工作表会保留”。成功后清理状态、设置 flash 并 `st.rerun()`。

- [ ] **Step 5: 运行 UI 与完整删除回归测试**

Run:

```powershell
python -m pytest tests/test_ui_smoke.py tests/test_data_source_deletion.py -v
python -m ruff check tuoming_agent/ui/app.py tuoming_agent/storage/sqlite.py tests/test_ui_smoke.py
```

Expected: PASS；“删除整个文件”和“删除工作表”两个入口互不影响。

- [ ] **Step 6: 提交 UI 改动**

```powershell
git add tuoming_agent/storage/sqlite.py tuoming_agent/ui/app.py tests/test_ui_smoke.py
git commit -m "feat: add worksheet deletion controls"
```

---

### Task 5: 全量验证与交付检查

**Files:**
- Verify: `tuoming_agent/`
- Verify: `tests/`
- Verify: `docs/superpowers/specs/2026-08-15-null-normalization-and-table-deletion-design.md`

**Interfaces:**
- Consumes: Tasks 1–4 的全部接口。
- Produces: 可复现的验证证据，不新增业务行为。

- [ ] **Step 1: 运行全量测试**

```powershell
python -m pytest
```

Expected: 全部测试通过，0 failures，0 errors。

- [ ] **Step 2: 运行静态检查和编译检查**

```powershell
python -m ruff check .
python -m compileall -q tuoming_agent
git diff --check
```

Expected: 每个命令退出码为 0，且无错误输出。

- [ ] **Step 3: 检查需求覆盖和改动范围**

```powershell
git status --short
git log --oneline -5
```

逐项核对：空值统一、合法业务值保留、工作表级级联删除、原文件和其他工作表保留、重新上传恢复缺失表、按钮文案、完整文件删除回归。确认 `.venv-broken/`、`.tmp_pytest/` 和其他用户文件没有进入提交。
