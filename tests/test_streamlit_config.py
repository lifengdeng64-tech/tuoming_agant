import tomllib
from pathlib import Path


def test_streamlit_viewer_toolbar_preserves_browser_copy() -> None:
    config = tomllib.loads(Path(".streamlit/config.toml").read_text("utf-8"))

    assert config["client"]["toolbarMode"] == "viewer"
