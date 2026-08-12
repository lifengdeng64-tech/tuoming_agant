from __future__ import annotations

from io import BytesIO

import pandas as pd
import pytest

from tuoming_agent.ingestion.limits import validate_upload_size
from tuoming_agent.ingestion.parser import preview_file
from tuoming_agent.ui import app as ui_app

MIB = 1024 * 1024


@pytest.mark.parametrize(
    ("filename", "limit"),
    [("report.csv", 200 * MIB), ("report.xlsx", 100 * MIB), ("report.xlsm", 100 * MIB)],
)
def test_upload_size_accepts_exact_file_limit(filename: str, limit: int) -> None:
    validate_upload_size(filename, limit)


@pytest.mark.parametrize(
    ("filename", "limit"),
    [("report.csv", 200 * MIB), ("report.xlsx", 100 * MIB), ("report.xlsm", 100 * MIB)],
)
def test_upload_size_rejects_files_larger_than_limit(filename: str, limit: int) -> None:
    with pytest.raises(ValueError):
        validate_upload_size(filename, limit + 1)


def test_ingestion_rejects_an_oversized_upload_before_parsing_or_writing(
    services, workspace
) -> None:
    class OversizedCsv(bytes):
        def __len__(self) -> int:
            return 200 * MIB + 1

    content = OversizedCsv(b"value\n1\n")

    with pytest.raises(ValueError, match="CSV uploads must be 200 MiB or smaller"):
        services.ingestion.ingest(
            "tenant-a", workspace.id, "too-large.csv", content, policies={}
        )

    assert services.repository.list_files("tenant-a", workspace.id) == []
    assert services.repository.list_artifacts("tenant-a", workspace.id) == []


def test_csv_preview_reads_at_most_default_sample_rows() -> None:
    content = pd.DataFrame({"value": range(501)}).to_csv(index=False).encode("utf-8")

    tables = preview_file("report.csv", BytesIO(content))

    assert len(tables) == 1
    assert tables[0].logical_name == "report"
    assert tables[0].dataframe["value"].tolist() == list(range(500))


def test_excel_preview_reads_at_most_default_sample_rows_per_sheet() -> None:
    content = BytesIO()
    with pd.ExcelWriter(content, engine="openpyxl") as writer:
        pd.DataFrame({"value": range(501)}).to_excel(writer, sheet_name="First", index=False)
        pd.DataFrame({"value": range(501, 1002)}).to_excel(writer, sheet_name="Second", index=False)

    tables = preview_file("report.xlsx", BytesIO(content.getvalue()))

    assert [len(table.dataframe) for table in tables] == [500, 500]
    assert tables[1].dataframe["value"].tolist() == list(range(501, 1001))


def test_ui_rejects_an_oversized_upload_before_reading_its_content(monkeypatch) -> None:
    class OversizedUpload:
        name = "too-large.csv"
        size = 200 * MIB + 1

        def getvalue(self) -> bytes:
            raise AssertionError("the UI must reject an oversized file before getvalue()")

    class EmptyContainer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    errors: list[str] = []
    monkeypatch.setattr(ui_app.st, "file_uploader", lambda *args, **kwargs: [OversizedUpload()])
    monkeypatch.setattr(ui_app.st, "error", errors.append)
    monkeypatch.setattr(ui_app.st, "divider", lambda: None)
    monkeypatch.setattr(
        ui_app.st,
        "columns",
        lambda *args, **kwargs: [EmptyContainer(), EmptyContainer()],
    )
    monkeypatch.setattr(ui_app.st, "button", lambda *args, **kwargs: False)
    monkeypatch.setattr(ui_app, "_section_heading", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_app, "_empty_state", lambda *args, **kwargs: None)

    ui_app._render_data_view(object(), "tenant", "workspace", [], [])

    assert errors == ["too-large.csv: CSV uploads must be 200 MiB or smaller."]
