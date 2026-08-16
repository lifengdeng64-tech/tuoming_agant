# Analysis Chat Cleanup and Localization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Precisely remove analysis messages with deleted data, restore masked values only in local plan presentation, and require Chinese names for model-generated fields and results.

**Architecture:** Add an explicit run-message association table and extend deletion impact calculation to linked and legacy messages. Keep stored plans masked while injecting a tenant-scoped display resolver into the presentation layer. Enforce generated-name language in the planner with one safe automatic retry, leaving source column names untouched.

**Tech Stack:** Python 3.12, SQLite, Pydantic, pandas, Streamlit, pytest, Ruff.

## Global Constraints

- Deleting one source preserves all messages and runs unrelated to that source.
- Stored plans, model payloads, audit records, and execution inputs remain pseudonymized.
- Only exact token values are restored, only for the current tenant's local display.
- Generated result names, aggregation outputs, derive columns, rename targets, and merge suffixes contain CJK text and no ASCII letters.
- Existing source column names are never translated or renamed automatically.
- Existing databases upgrade through `CREATE TABLE IF NOT EXISTS`; no manual migration is required.
- Implement every behavior test-first and run the focused red test before production edits.

---

### Task 1: Associate analysis messages with runs

**Files:**
- Modify: `tuoming_agent/storage/sqlite.py`
- Modify: `tuoming_agent/workspace/service.py`
- Modify: `tuoming_agent/analysis/workflow.py`
- Modify: `tuoming_agent/ui/app.py`
- Test: `tests/test_ingestion_workspace.py`
- Test: `tests/test_analysis_workflow.py`
- Test: `tests/test_ui_smoke.py`

**Interfaces:**
- Produces: `ConversationService.add_user_message(...) -> MessageRecord`.
- Produces: `SQLiteRepository.create_analysis_run(..., request_message_id: str | None = None)`.
- Produces: `SQLiteRepository.add_message(..., analysis_run_id: str | None = None)`.
- Produces: `AnalysisWorkflowService.start(..., request_message_id: str | None = None)`.

- [ ] **Step 1: Write failing association tests**

Create a request, start a run with that message ID, and save a linked response:

```python
request = services.conversations.add_user_message(
    "tenant-a", conversation["id"], "汇总营收"
)
started = workflow.start(
    "tenant-a", workspace.id, conversation["id"], source.id,
    request.safe_content, {}, request_message_id=request.id,
)
services.conversations.add_assistant_message(
    "tenant-a", conversation["id"], "处理完成",
    analysis_run_id=started.run["id"],
)
links = services.repository.list_analysis_run_messages(
    "tenant-a", started.run["id"]
)
assert [item["kind"] for item in links] == ["request", "response"]
```

Also assert that a message from another conversation or tenant cannot be linked.

- [ ] **Step 2: Run tests and verify RED**

```powershell
python -m pytest tests/test_analysis_workflow.py tests/test_ingestion_workspace.py -k "run_message or request_message" -v
```

Expected: failure because the association table and parameters do not exist.

- [ ] **Step 3: Add schema and atomic repository writes**

Extend `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS analysis_run_messages (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    message_id TEXT NOT NULL REFERENCES messages(id),
    kind TEXT NOT NULL CHECK(kind IN ('request', 'response')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, message_id),
    UNIQUE(message_id)
);
CREATE INDEX IF NOT EXISTS idx_analysis_run_messages_message
    ON analysis_run_messages(tenant_id, message_id);
```

Add `_link_analysis_message(...)` to validate tenant, conversation, message role, and link kind. Insert the request link inside `create_analysis_run` and the response link inside `add_message`. Add a tenant-scoped list method for tests.

- [ ] **Step 4: Thread IDs through services and UI**

Return `MessageRecord` from `ConversationService.add_user_message`. Forward `request_message_id` through `AnalysisWorkflowService.start`. In the UI use `request.safe_content` for planning and pass `request.id`; when execution completes, pass `analysis_run_id=result.run["id"]` to the assistant message write.

- [ ] **Step 5: Run focused tests and lint**

```powershell
python -m pytest tests/test_analysis_workflow.py tests/test_ingestion_workspace.py tests/test_ui_smoke.py -q
python -m ruff check tuoming_agent/storage/sqlite.py tuoming_agent/workspace/service.py tuoming_agent/analysis/workflow.py tuoming_agent/ui/app.py tests/test_analysis_workflow.py tests/test_ingestion_workspace.py tests/test_ui_smoke.py
```

- [ ] **Step 6: Commit**

```powershell
git add tuoming_agent/storage/sqlite.py tuoming_agent/workspace/service.py tuoming_agent/analysis/workflow.py tuoming_agent/ui/app.py tests/test_analysis_workflow.py tests/test_ingestion_workspace.py tests/test_ui_smoke.py
git commit -m "feat: link analysis messages to runs"
```

---

### Task 2: Delete associated requests and responses atomically

**Files:**
- Modify: `tuoming_agent/storage/sqlite.py`
- Modify: `tuoming_agent/ui/app.py`
- Test: `tests/test_data_source_deletion.py`
- Test: `tests/test_ui_smoke.py`

**Interfaces:**
- Consumes: `analysis_run_messages` and artifact dependency traversal.
- Produces: `DeletionImpact.message_ids`, `TableDeletionImpact.message_ids`, and `message_count`.
- Produces: scoped run discovery that includes `merge.right_artifact_id`.

- [ ] **Step 1: Write failing deletion tests**

Create two source analyses in one conversation. Delete the first and assert only its exchange is selected and removed:

```python
impact = services.repository.inspect_file_deletion(
    "tenant-a", workspace.id, first.file_id
)
assert set(impact.message_ids) == {first_request.id, first_response.id}
assert second_request.id not in impact.message_ids

services.data_sources.delete("tenant-a", workspace.id, first.file_id)
remaining = services.repository.list_messages("tenant-a", conversation["id"], 100)
assert [message.id for message in remaining] == [second_request.id, second_response.id]
```

Add tests for a pending merge plan referencing the deleted source, a legacy run matched to the nearest prior identical request, two repeated request texts, and a SQLite failure restoring files, messages, runs, and conversation summary.

- [ ] **Step 2: Run tests and verify RED**

```powershell
python -m pytest tests/test_data_source_deletion.py -k "message or merge_source or legacy_request" -v
```

- [ ] **Step 3: Extend impact and run discovery**

Add `message_ids: tuple[str, ...]` and `message_count`. Include runs selected by source artifact, result artifact, or exact `merge.right_artifact_id` found in validated stored plan JSON. Resolve messages from explicit links, assistant artifact references, and the legacy nearest-message fallback constrained by tenant, conversation, role, matching `safe_request`, and `created_at <= run.created_at`.

- [ ] **Step 4: Delete and rebuild summaries in one transaction**

Capture affected conversations, delete `analysis_run_messages`, delete only `actual.message_ids`, then delete plans, attempts, and runs. Null any remaining message artifact references before deleting artifacts. Rebuild each affected `safe_summary` with the existing roll-up rule: empty at 20 or fewer messages; otherwise use the older messages preceding the newest 12, take the last 20, truncate each to 240 characters, and cap at 4,000 characters.

- [ ] **Step 5: Update confirmation UI**

Show `关联对话 {impact.message_count} 条` for file and worksheet deletion. Require acknowledgement when analysis runs, downstream artifacts, or messages exist. Add UI assertions for the count.

- [ ] **Step 6: Run tests and lint**

```powershell
python -m pytest tests/test_data_source_deletion.py tests/test_ui_smoke.py -q
python -m ruff check tuoming_agent/storage/sqlite.py tuoming_agent/ui/app.py tests/test_data_source_deletion.py tests/test_ui_smoke.py
```

- [ ] **Step 7: Commit**

```powershell
git add tuoming_agent/storage/sqlite.py tuoming_agent/ui/app.py tests/test_data_source_deletion.py tests/test_ui_smoke.py
git commit -m "feat: remove conversations tied to deleted data"
```

---

### Task 3: Restore token values only in plan presentation

**Files:**
- Modify: `tuoming_agent/security/masking.py`
- Modify: `tuoming_agent/analysis/presentation.py`
- Modify: `tuoming_agent/ui/app.py`
- Test: `tests/test_plan_presentation.py`
- Test: `tests/test_ui_smoke.py`

**Interfaces:**
- Produces: `MaskingService.restore_display_value(tenant_id: str, value: Any) -> Any`.
- Produces: `describe_plan(plan, resolve_value: Callable[[Any], Any] | None = None) -> list[str]`.

- [ ] **Step 1: Write failing security and presentation tests**

```python
token = services.vault.tokenize("tenant-a", "field", "华住")
plan = AnalysisPlan(
    input_artifact_id="artifact-a",
    result_name="品牌营收分析",
    operations=[
        {"action": "filter", "column": "品牌名称", "operator": "eq", "value": token}
    ],
)
lines = describe_plan(
    plan,
    resolve_value=lambda value: services.masking.restore_display_value(
        "tenant-a", value
    ),
)
assert any("'华住'" in line for line in lines)
assert token not in "\n".join(lines)
assert plan.operations[0].value == token
```

Also cover lists, nested fill values, ordinary strings, unknown tokens, and another tenant's token. Unknown and unauthorized values remain unchanged.

- [ ] **Step 2: Run tests and verify RED**

```powershell
python -m pytest tests/test_plan_presentation.py -v
```

- [ ] **Step 3: Implement display-only restoration**

`MaskingService.restore_display_value` recursively handles dictionaries, lists, and tuples. For strings, call `vault.resolve`; catch only `RecordNotFoundError` and return the original string. In `presentation.py`, apply the optional resolver only to `filter.value` and values inside `fillna.values`. Never resolve column names, expressions, result names, or artifact IDs.

- [ ] **Step 4: Wire the local UI**

```python
for line in describe_plan(
    snapshot.current_plan.plan,
    resolve_value=lambda value: services.masking.restore_display_value(
        tenant_id, value
    ),
):
    st.markdown(f"- {line}")
```

Add a UI test that sees restored text and then re-reads the stored plan to prove the token is unchanged.

- [ ] **Step 5: Run tests and lint**

```powershell
python -m pytest tests/test_plan_presentation.py tests/test_ui_smoke.py -q
python -m ruff check tuoming_agent/security/masking.py tuoming_agent/analysis/presentation.py tuoming_agent/ui/app.py tests/test_plan_presentation.py tests/test_ui_smoke.py
```

- [ ] **Step 6: Commit**

```powershell
git add tuoming_agent/security/masking.py tuoming_agent/analysis/presentation.py tuoming_agent/ui/app.py tests/test_plan_presentation.py tests/test_ui_smoke.py
git commit -m "feat: restore masked values in local plan display"
```

---

### Task 4: Require Chinese names for generated outputs

**Files:**
- Create: `tuoming_agent/analysis/naming.py`
- Modify: `tuoming_agent/analysis/models.py`
- Modify: `tuoming_agent/analysis/planner.py`
- Modify: `tuoming_agent/ui/app.py`
- Test: `tests/test_analysis_planner.py`
- Test: `tests/test_analysis_workflow.py`
- Test: `tests/test_ui_smoke.py`

**Interfaces:**
- Produces: `GeneratedNameValidationError(ValueError)`.
- Produces: `generated_name_issues(plan: AnalysisPlan) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing name-scope tests**

```python
plan = AnalysisPlan(
    input_artifact_id="artifact-a",
    result_name="revenue report",
    operations=[{
        "action": "groupby",
        "by": ["brand_code"],
        "aggregations": [{
            "column": "revenue", "function": "sum", "output": "current_revenue"
        }],
    }],
)
assert generated_name_issues(plan) == (
    "result_name: revenue report",
    "operations[0].aggregations[0].output: current_revenue",
)
```

Assert English source references are not reported. Add passing cases for “品牌营收分析”, “本期营收”, and “营收同比2026”; reject `本期_revenue`, English derive columns, rename targets, and merge suffixes.

- [ ] **Step 2: Write failing planner retry tests**

Use a sequence model returning an English plan then a Chinese plan. Assert two calls and `generated_name_feedback` in the second payload. If both plans are invalid, assert `GeneratedNameValidationError` with “模型未能生成合规的中文字段名称”.

- [ ] **Step 3: Run tests and verify RED**

```powershell
python -m pytest tests/test_analysis_planner.py -k "chinese or generated_name" -v
```

- [ ] **Step 4: Implement scoped validation**

Create `naming.py` with CJK regex `[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]` and ASCII regex `[A-Za-z]`. Validate only result name, aggregation outputs, derive columns, rename targets, and merge suffixes. Change default suffixes to `("_左表", "_右表")`. Do not add Pydantic validators, so historical plans and executor tests stay readable.

- [ ] **Step 5: Add one safe retry**

Update `PLANNER_SYSTEM_PROMPT` with the exact rule and Chinese examples. Invoke at most twice. On the first invalid response, add only invalid field paths, generated names, and the rule to `generated_name_feedback`, sanitize the new JSON payload, and retry. On the second failure raise `GeneratedNameValidationError`. Never add source values, restored tokens, or plaintext mappings to feedback.

- [ ] **Step 6: Render the specific error**

Catch `GeneratedNameValidationError` before the generic UI exception and show its Chinese message. Ensure no invalid plan version is persisted.

- [ ] **Step 7: Run compatibility tests and lint**

```powershell
python -m pytest tests/test_analysis_planner.py tests/test_analysis_workflow.py tests/test_ui_smoke.py -q
python -m ruff check tuoming_agent/analysis/naming.py tuoming_agent/analysis/models.py tuoming_agent/analysis/planner.py tuoming_agent/ui/app.py tests/test_analysis_planner.py tests/test_analysis_workflow.py tests/test_ui_smoke.py
```

- [ ] **Step 8: Commit**

```powershell
git add tuoming_agent/analysis/naming.py tuoming_agent/analysis/models.py tuoming_agent/analysis/planner.py tuoming_agent/ui/app.py tests/test_analysis_planner.py tests/test_analysis_workflow.py tests/test_ui_smoke.py
git commit -m "feat: require Chinese generated analysis names"
```

---

### Task 5: Full regression and review

**Files:**
- Modify only when a failing verification test proves a regression.
- Verify: all project tests and source files.

**Interfaces:**
- Consumes all interfaces from Tasks 1–4.
- Produces a clean, reviewed branch ready for integration.

- [ ] **Step 1: Run complete verification**

```powershell
python -m pytest -q
python -m ruff check .
python -m compileall -q tuoming_agent
git diff --check
git status --short
```

Expected: test completion at 100% with zero failures, no Ruff or compile errors, no whitespace errors, and no uncommitted files.

- [ ] **Step 2: Review the final Git range**

Review from commit `736c7b7` through `HEAD`. Check tenant/workspace scoping, transaction ordering, legacy-message matching, absence of plaintext persistence, and source-column compatibility.

- [ ] **Step 3: Fix review findings test-first**

For each Critical or Important finding, add a focused failing regression test, run it to verify RED, implement the smallest correction, rerun the focused and full suites, and commit the fix.

- [ ] **Step 4: Prepare the handoff**

Report branch, worktree, commits, verification commands, and that chart generation remains outside this change.
