# Plotly Apple-inspired Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local-only Plotly dashboard and an Apple-inspired minimalist visual system to the existing Streamlit desktop application.

**Architecture:** Keep Streamlit as the application shell and all existing ingestion, masking, planning, DuckDB, export, and desktop packaging boundaries. Add a focused dashboard package that infers safe defaults from artifact schemas, streams KPI reductions locally, and reuses the allowlisted DuckDB compiler for bounded grouped chart data; a separate Plotly builder turns those bounded frames into figures. UI modules compose the dashboard without loading a complete artifact into pandas.

**Tech Stack:** Python 3.11+, Streamlit, Plotly, pandas, PyArrow, DuckDB, Pydantic, pytest.

**Spec:** `docs/superpowers/specs/2026-08-25-plotly-apple-dashboard-design.md`

## Global Constraints

- Preserve Streamlit and all existing local desktop, DPAPI, masking, DLP, DuckDB, backup, and export behavior.
- Plotly must render locally through `st.plotly_chart`; never use Chart Studio or any network upload.
- Dashboard queries accept only repository-authorized artifact IDs, exact schema column names, aggregation enums, and bounded row counts.
- Never accept user- or model-authored SQL.
- A chart returns at most 200 points and 4 series; the detail table returns at most 100 rows.
- Dashboard content uses masked artifacts only; restored sensitive values remain available only through the existing authorized result export flow.
- Use `#F5F5F7` canvas, `#1D1D1F` ink, `#0071E3` accent, system font stack, 18–20 px card radii, and responsive one-column chart layout below 900 px.
- Do not copy Apple trademarks, icons, or proprietary assets.

---

### Task 1: Dashboard Models and Schema Defaults

**Files:**
- Create: `tuoming_agent/dashboard/__init__.py`
- Create: `tuoming_agent/dashboard/models.py`
- Create: `tests/test_dashboard_models.py`

**Interfaces:**
- Consumes: `ArtifactRecord.schema` entries shaped as `{"name": str, "dtype": str}`.
- Produces: `AggregationName`, `DashboardSelection`, `DashboardDefaults`, `infer_dashboard_defaults(artifact)`.

- [ ] **Step 1: Write failing tests for numeric/date/category inference and empty schemas**

```python
def test_infer_dashboard_defaults_prefers_date_category_and_numeric_columns():
    artifact = artifact_with_schema([
        ("月份", "datetime64[ns]"), ("事业部", "object"), ("营业收入", "float64")
    ])
    defaults = infer_dashboard_defaults(artifact)
    assert defaults.date_column == "月份"
    assert defaults.category_column == "事业部"
    assert defaults.measure_columns == ("营业收入",)

def test_dashboard_selection_rejects_more_than_four_measures():
    with pytest.raises(ValidationError):
        DashboardSelection(artifact_id="a", measures=("a", "b", "c", "d", "e"))
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run: `python -m pytest tests/test_dashboard_models.py -q`

Expected: import failure because `tuoming_agent.dashboard.models` does not exist.

- [ ] **Step 3: Implement strict immutable models and dtype inference**

```python
AggregationName = Literal["sum", "mean", "min", "max", "count"]

class DashboardSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str = Field(min_length=1)
    measures: tuple[str, ...] = Field(min_length=1, max_length=4)
    aggregation: AggregationName = "sum"
    date_column: str | None = None
    category_column: str | None = None

@dataclass(frozen=True)
class DashboardDefaults:
    numeric_columns: tuple[str, ...]
    date_columns: tuple[str, ...]
    category_columns: tuple[str, ...]
    measure_columns: tuple[str, ...]
    date_column: str | None
    category_column: str | None
```

Normalize dtype strings with casefolded markers. Treat `int`, `float`, `double`, `decimal`, and unsigned integer types as numeric; treat `date`, `time`, and `timestamp` as dates; all remaining columns are category candidates. Preserve schema order and select no more than four default measures.

- [ ] **Step 4: Run focused tests and commit**

Run: `python -m pytest tests/test_dashboard_models.py -q`

Expected: PASS.

Commit:

```powershell
git add tuoming_agent/dashboard/__init__.py tuoming_agent/dashboard/models.py tests/test_dashboard_models.py
git commit -m "feat: infer safe dashboard defaults"
```

---

### Task 2: Bounded Local Dashboard Data Service

**Files:**
- Create: `tuoming_agent/dashboard/service.py`
- Modify: `tuoming_agent/workspace/service.py`
- Create: `tests/test_dashboard_service.py`

**Interfaces:**
- Consumes: `SQLiteRepository.get_artifact`, `DuckDBRuntime`, `AnalysisPlan`, `GroupByOperation`, `SortOperation`, `HeadOperation`.
- Produces: `DashboardService.kpis(...) -> tuple[KPIValue, ...]`, `DashboardService.grouped(...) -> pd.DataFrame`, `DashboardService.detail(...) -> pd.DataFrame`.
- Adds: `ApplicationServices.dashboard: DashboardService`.

- [ ] **Step 1: Write RED tests for authorization, full-file KPI streaming, grouped limits, and detail limits**

```python
def test_kpis_stream_all_batches_without_artifact_service_load(services, monkeypatch):
    monkeypatch.setattr(services.artifacts, "load", lambda *_: pytest.fail("full load"))
    values = services.dashboard.kpis(TENANT, WORKSPACE, artifact.id, ("revenue",), "sum")
    assert values[0].value == pytest.approx(expected_full_sum)

def test_grouped_rejects_unknown_column(services):
    with pytest.raises(DashboardQueryError, match="column"):
        services.dashboard.grouped(TENANT, WORKSPACE, artifact.id, "forged", "revenue", "sum")

def test_grouped_returns_at_most_200_points(services):
    frame = services.dashboard.grouped(TENANT, WORKSPACE, artifact.id, "hotel", "revenue", "sum")
    assert len(frame) <= 200
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python -m pytest tests/test_dashboard_service.py -q`

Expected: import or missing service failures.

- [ ] **Step 3: Implement authorization and local KPI streaming**

Resolve the artifact through `repository.get_artifact(tenant_id, artifact_id)`, require `artifact.workspace_id == workspace_id`, validate the selected columns against the exact schema set, and open only the repository-returned Parquet path. Iterate `pyarrow.parquet.ParquetFile.iter_batches(columns=..., batch_size=65_536)` and reduce each numeric measure with `pyarrow.compute`, ignoring null and non-finite values. Never call `ArtifactService.load`.

```python
@dataclass(frozen=True)
class KPIValue:
    column: str
    aggregation: AggregationName
    value: float | int | None

class DashboardQueryError(ValueError):
    pass
```

- [ ] **Step 4: Implement bounded grouped queries using the existing compiler**

Construct an `AnalysisPlan` in code with `GroupByOperation`, `SortOperation`, and `HeadOperation(rows=200)`. Compile it through a fresh `DuckDBRuntime(config).compiler(repository)`, execute inside `runtime.connection(compiled.sources)`, and call `.fetch_df()` only after the plan has reduced output to 200 rows. Use deterministic aliases owned by the service, not user-supplied output names.

- [ ] **Step 5: Implement bounded detail reads and wire `ApplicationServices`**

`detail` delegates to `ArtifactService.preview(..., limit=min(limit, 100), restored=False)`. Add a `dashboard` field to `ApplicationServices` and instantiate it in `create_services` with repository, artifacts, and config.

- [ ] **Step 6: Run focused tests and commit**

Run: `python -m pytest tests/test_dashboard_service.py tests/test_dashboard_models.py -q`

Expected: PASS.

Commit:

```powershell
git add tuoming_agent/dashboard/service.py tuoming_agent/workspace/service.py tests/test_dashboard_service.py
git commit -m "feat: query bounded dashboard data locally"
```

---

### Task 3: Plotly Figure Builder and Theme

**Files:**
- Modify: `pyproject.toml`
- Create: `tuoming_agent/ui/theme.py`
- Create: `tuoming_agent/dashboard/charts.py`
- Create: `tests/test_dashboard_charts.py`

**Interfaces:**
- Consumes: bounded pandas frames and `KPIValue` from Task 2.
- Produces: `line_figure(frame, x, measures)`, `bar_figure(frame, category, measure)`, `PLOTLY_CONFIG`, `PLOTLY_LAYOUT`.

- [ ] **Step 1: Add Plotly dependency and RED figure tests**

Add `"plotly>=6.0,<7"` to project dependencies.

```python
def test_line_figure_uses_local_minimal_theme():
    figure = line_figure(frame, "month", ("revenue",))
    assert figure.layout.paper_bgcolor == "rgba(0,0,0,0)"
    assert figure.layout.font.color == "#1D1D1F"
    assert len(figure.data[0].x) <= 200

def test_bar_figure_has_no_remote_chart_studio_configuration():
    figure = bar_figure(frame, "division", "revenue")
    assert figure.layout.template.layout.paper_bgcolor == "rgba(0,0,0,0)"
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python -m pytest tests/test_dashboard_charts.py -q`

Expected: missing module/functions.

- [ ] **Step 3: Implement Plotly theme and builders**

```python
PLOTLY_CONFIG = {
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["sendDataToCloud", "editInChartStudio"],
    "toImageButtonOptions": {"format": "png", "filename": "tuoming-dashboard"},
}

PLOTLY_LAYOUT = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "rgba(0,0,0,0)",
    "font": {"family": "-apple-system, BlinkMacSystemFont, PingFang SC", "color": "#1D1D1F"},
    "colorway": ["#0071E3", "#5AC8FA", "#34C759", "#FF9F0A"],
    "margin": {"l": 16, "r": 16, "t": 44, "b": 16},
}
```

Use `plotly.graph_objects`, not Plotly Express implicit network features. Reject frames above 200 rows or more than four measures before building traces. Use unified hover labels and horizontal bars for category comparisons.

- [ ] **Step 4: Run tests and commit**

Run: `python -m pytest tests/test_dashboard_charts.py -q`

Expected: PASS.

Commit:

```powershell
git add pyproject.toml tuoming_agent/ui/theme.py tuoming_agent/dashboard/charts.py tests/test_dashboard_charts.py
git commit -m "feat: render local plotly dashboard charts"
```

---

### Task 4: Apple-inspired Design System and Dashboard UI

**Files:**
- Create: `tuoming_agent/ui/components.py`
- Create: `tuoming_agent/ui/dashboard.py`
- Modify: `tuoming_agent/ui/styles.py`
- Modify: `tuoming_agent/ui/app.py`
- Create: `tests/test_dashboard_ui.py`
- Modify: `tests/test_ui.py`

**Interfaces:**
- Consumes: `ApplicationServices.dashboard`, `infer_dashboard_defaults`, chart builders and `PLOTLY_CONFIG`.
- Produces: `render_dashboard_view(services, tenant_id, workspace_id, artifacts)` and reusable HTML-safe card/header components.

- [ ] **Step 1: Write RED UI tests**

```python
def test_navigation_includes_dashboard():
    assert "仪表盘" in VIEW_OPTIONS

def test_dashboard_uses_plotly_and_never_restored_preview(monkeypatch):
    calls = []
    monkeypatch.setattr(st, "plotly_chart", lambda *args, **kwargs: calls.append(kwargs))
    render_dashboard_view(services, TENANT, WORKSPACE, artifacts)
    assert calls
    assert all(call.get("config") == PLOTLY_CONFIG for call in calls)
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `python -m pytest tests/test_dashboard_ui.py tests/test_ui.py -q`

Expected: dashboard navigation/function missing.

- [ ] **Step 3: Replace global visual tokens and reusable components**

Update `APP_STYLES` to use the spec colors, font stack, 20 px cards, subtle borders, responsive KPI grids, pill controls, and a visually lighter sidebar. Limit DOM selectors to documented Streamlit `data-testid` values already covered by UI tests. Add HTML-escaped `render_kpi_card`, `render_security_badge`, and `render_empty_state` helpers.

- [ ] **Step 4: Implement dashboard composition**

The dashboard page:

1. chooses newest analysis result, otherwise newest dataset;
2. lets the user select another artifact;
3. infers defaults and exposes measure, aggregation, date, and category controls;
4. renders up to four KPI cards;
5. renders a trend chart when a date column exists;
6. renders a category comparison when a category column exists;
7. renders a 100-row detail table;
8. isolates each chart in `try/except DashboardQueryError` so one failure does not hide other content.

Use stable per-workspace session keys. Never request `restored=True`.

- [ ] **Step 5: Integrate navigation and simplify the application shell**

Add `"仪表盘"` to `VIEW_OPTIONS`, route it before `"结果"`, and update the brand copy to `旅数智析 / Local Intelligence`. Keep all current first-run, shutdown, workspace, query-param, and flash behavior.

- [ ] **Step 6: Run focused tests and commit**

Run: `python -m pytest tests/test_dashboard_ui.py tests/test_ui.py tests/test_workspace_ui.py -q`

Expected: PASS.

Commit:

```powershell
git add tuoming_agent/ui/components.py tuoming_agent/ui/dashboard.py tuoming_agent/ui/styles.py tuoming_agent/ui/app.py tests/test_dashboard_ui.py tests/test_ui.py
git commit -m "feat: add minimalist plotly dashboard ui"
```

---

### Task 5: Documentation, Desktop Packaging, and End-to-End Verification

**Files:**
- Modify: `README.md`
- Modify: `TuomingAgent.spec` only if Plotly collection is absent from the generated build analysis.
- Modify: `.gitignore` to ignore `.superpowers/brainstorm/` only if `.superpowers/` is not already ignored.
- Create: `tests/test_dashboard_acceptance.py`

**Interfaces:**
- Consumes: all prior task interfaces.
- Produces: documented dashboard usage and a release-ready dependency/package configuration.

- [ ] **Step 1: Add acceptance tests for a real local Parquet artifact**

```python
def test_dashboard_acceptance_queries_full_artifact_and_bounds_visual_data(services, artifact):
    selection = DashboardSelection(
        artifact_id=artifact.id,
        measures=("revenue",),
        aggregation="sum",
        date_column="month",
        category_column="division",
    )
    kpis = services.dashboard.kpis(TENANT, WORKSPACE, artifact.id, selection.measures, "sum")
    trend = services.dashboard.grouped(TENANT, WORKSPACE, artifact.id, "month", "revenue", "sum")
    assert kpis[0].value == expected_sum
    assert len(trend) <= 200
```

- [ ] **Step 2: Update README**

Document the new `仪表盘` page, supported Plotly interactions, default masked-only behavior, local query boundaries, and the fact that Plotly does not upload to Chart Studio.

- [ ] **Step 3: Run focused and full verification**

Run:

```powershell
python -m pytest tests/test_dashboard_models.py tests/test_dashboard_service.py tests/test_dashboard_charts.py tests/test_dashboard_ui.py tests/test_dashboard_acceptance.py -q
python -m pytest -q
python -m ruff check .
python -m compileall -q tuoming_agent
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 4: Verify the Windows build contains Plotly**

Run:

```powershell
.\scripts\build_windows.ps1 -SkipTests -SkipInstaller
& .\dist\TuomingAgent\TuomingAgent.exe --no-browser
```

Confirm the local service starts without a missing Plotly import and the dashboard renders while network access is unavailable. Stop the process through the application shutdown action.

- [ ] **Step 5: Final commit**

```powershell
git add README.md TuomingAgent.spec .gitignore tests/test_dashboard_acceptance.py
git commit -m "docs: explain local interactive dashboards"
```
