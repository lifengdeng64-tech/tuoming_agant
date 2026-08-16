from __future__ import annotations

import io
import json
import socket
from pathlib import Path

from tuoming_agent.desktop import launcher


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeProcess:
    def __init__(self, *, exit_on_terminate: bool = True):
        self.return_code = None
        self.exit_on_terminate = exit_on_terminate
        self.terminated = False
        self.killed = False
        self.wait_timeouts = []

    def poll(self):
        return self.return_code

    def terminate(self):
        self.terminated = True
        if self.exit_on_terminate:
            self.return_code = 0

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        if self.return_code is None:
            raise launcher.subprocess.TimeoutExpired("tuoming", timeout)
        return self.return_code

    def kill(self):
        self.killed = True
        self.return_code = -9


def test_streamlit_child_loads_cli_style_options_before_start(monkeypatch, tmp_path: Path) -> None:
    from streamlit.web import bootstrap

    loaded: list[dict] = []
    started: list[tuple] = []
    app_path = tmp_path / "app.py"
    monkeypatch.setattr(launcher, "_bundled_app_path", lambda: app_path)
    monkeypatch.setattr(bootstrap, "load_config_options", lambda options: loaded.append(options))
    monkeypatch.setattr(
        bootstrap,
        "run",
        lambda *args: started.append(args),
    )

    launcher._run_streamlit_child(8765)

    assert loaded == [
        {
            "global_developmentMode": False,
            "server_address": "127.0.0.1",
            "server_port": 8765,
            "server_headless": True,
            "server_fileWatcherType": "none",
            "server_enableCORS": True,
            "server_enableXsrfProtection": True,
            "browser_gatherUsageStats": False,
        }
    ]
    assert started == [(str(app_path), False, [], loaded[0])]
def test_find_available_port_skips_an_occupied_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]

        available = launcher._find_available_port(port)

    assert available != port
    assert port < available < port + 20


def test_stop_process_terminates_and_closes_log() -> None:
    process = FakeProcess()
    log_handle = io.BytesIO()

    launcher._stop_process(process, log_handle)

    assert process.terminated
    assert not process.killed
    assert log_handle.closed


def test_stop_process_kills_child_after_timeout() -> None:
    process = FakeProcess(exit_on_terminate=False)
    log_handle = io.BytesIO()

    launcher._stop_process(process, log_handle)

    assert process.terminated
    assert process.killed
    assert process.wait_timeouts == [8, 5]
    assert log_handle.closed


def test_open_running_instance_uses_only_local_runtime_port(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "runtime.json").write_text(
        json.dumps({"pid": 1234, "port": 8507}), encoding="utf-8"
    )
    opened = []
    requested = []
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda url, timeout: requested.append((url, timeout)) or FakeResponse(),
    )
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url, new: opened.append((url, new)))

    assert launcher._open_running_instance(tmp_path)
    assert requested == [("http://127.0.0.1:8507/_stcore/health", 1)]
    assert opened == [("http://127.0.0.1:8507", 2)]


def test_open_running_instance_rejects_tampered_runtime_file(tmp_path: Path) -> None:
    (tmp_path / "runtime.json").write_text(
        json.dumps({"port": "https://attacker.example"}), encoding="utf-8"
    )

    assert not launcher._open_running_instance(tmp_path)


def test_log_is_rotated_at_five_megabytes(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "desktop.log"
    log_path.write_bytes(b"x" * (5 * 1024 * 1024))

    handle = launcher._open_log(tmp_path)
    try:
        assert (log_dir / "desktop.log.1").stat().st_size == 5 * 1024 * 1024
        assert log_path.stat().st_size == 0
    finally:
        handle.close()


def test_bundled_app_path_points_to_top_level_streamlit_entry() -> None:
    expected = Path(__file__).resolve().parents[1] / "app.py"

    assert launcher._bundled_app_path() == expected
