from __future__ import annotations

import base64
from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_workspace_renders_without_runtime_errors(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MASKING_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode("ascii"))
    monkeypatch.setenv("TUOMING_DATA_DIR", str(tmp_path / "ui-data"))
    monkeypatch.setenv("TUOMING_DEFAULT_TENANT", "ui-test-tenant")
    app = AppTest.from_file("app.py")
    app.run(timeout=30)
    assert not app.exception
    assert app.title[0].value == "默认工作区"
    assert [metric.label for metric in app.metric] == [
        "数据集",
        "上传文件",
        "数据制品",
        "近期消息",
    ]
