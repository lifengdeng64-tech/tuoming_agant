from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import IO, Any

from tuoming_agent.backup import BackupError, apply_pending_restore
from tuoming_agent.config import AppConfig, ConfigurationError
from tuoming_agent.settings import LocalSettingsManager, default_app_dir

APP_NAME = "Tuoming Agent"
DEFAULT_PORT = 8501
_MUTEX_NAME = "Local\\TuomingAgentDesktop"
_ALREADY_EXISTS = 183


def main() -> None:
    arguments = _parse_arguments()
    if arguments.streamlit_child:
        _run_streamlit_child(arguments.port)
        return
    _run_desktop(arguments.port, open_browser=not arguments.no_browser)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=not getattr(sys, "frozen", False))
    parser.add_argument("--streamlit-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def _run_desktop(preferred_port: int, *, open_browser: bool) -> None:
    app_dir = default_app_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    mutex = _acquire_single_instance()
    if mutex is None:
        if not _open_running_instance(app_dir):
            _show_message("Tuoming Agent 已经在运行，请从系统托盘打开。", error=False)
        return
    atexit.register(_release_mutex, mutex)

    try:
        settings_manager = LocalSettingsManager(app_dir)
        recovery = apply_pending_restore(app_dir, settings_manager)
        if recovery is not None:
            _show_message("备份恢复完成，原数据已保留在 recovery 目录。", error=False)
        AppConfig.from_runtime()
    except (BackupError, ConfigurationError) as exc:
        _show_message(str(exc), error=True)
        return

    port = _find_available_port(preferred_port)
    shutdown_path = app_dir / "shutdown.request"
    runtime_path = app_dir / "runtime.json"
    shutdown_path.unlink(missing_ok=True)
    log_handle = _open_log(app_dir)
    process = _start_streamlit_process(port, log_handle)
    atexit.register(_stop_process, process, log_handle)
    try:
        _wait_until_ready(process, port)
        runtime_path.write_text(
            json.dumps({"pid": process.pid, "port": port}, separators=(",", ":")),
            encoding="utf-8",
        )
        url = f"http://127.0.0.1:{port}"
        if open_browser:
            webbrowser.open(url, new=2)
        _run_tray(process, log_handle, url, shutdown_path)
    except RuntimeError as exc:
        _show_message(str(exc), error=True)
    except OSError:
        _show_message(
            "Tuoming 本地运行目录不可用，请检查当前 Windows 用户的文件权限。",
            error=True,
        )
    finally:
        _stop_process(process, log_handle)
        runtime_path.unlink(missing_ok=True)
        shutdown_path.unlink(missing_ok=True)


def _run_streamlit_child(port: int) -> None:
    from streamlit.web import bootstrap

    app_path = _bundled_app_path()
    options: dict[str, Any] = {
        "global_developmentMode": False,
        "server_address": "127.0.0.1",
        "server_port": port,
        "server_headless": True,
        "server_fileWatcherType": "none",
        "server_enableCORS": True,
        "server_enableXsrfProtection": True,
        "browser_gatherUsageStats": False,
    }
    bootstrap.load_config_options(options)
    bootstrap.run(str(app_path), False, [], options)


def _start_streamlit_process(port: int, log_handle: IO[bytes]) -> subprocess.Popen[bytes]:
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--streamlit-child", "--port", str(port)]
    else:
        command = [
            sys.executable,
            "-m",
            "tuoming_agent.desktop.launcher",
            "--streamlit-child",
            "--port",
            str(port),
        ]
    environment = os.environ.copy()
    environment["TUOMING_DESKTOP"] = "1"
    app_dir = default_app_dir()
    environment["TUOMING_APP_DIR"] = str(app_dir)
    network = LocalSettingsManager(app_dir).load_network_settings()
    if not network.use_system_proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            environment.pop(name, None)
            environment.pop(name.casefold(), None)
    if network.proxy_url:
        environment["HTTP_PROXY"] = network.proxy_url
        environment["HTTPS_PROXY"] = network.proxy_url
    if network.ca_bundle_path:
        environment["SSL_CERT_FILE"] = network.ca_bundle_path
    startup_info = None
    creation_flags = 0
    if os.name == "nt":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = 0
        creation_flags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=environment,
        startupinfo=startup_info,
        creationflags=creation_flags,
    )


def _run_tray(
    process: subprocess.Popen[bytes],
    log_handle: IO[bytes],
    url: str,
    shutdown_path: Path,
) -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        _stop_process(process, log_handle)
        raise RuntimeError("桌面托盘组件缺失，请重新安装完整版本。") from exc

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=13, fill=(21, 99, 71, 255))
    draw.polygon(((32, 14), (49, 21), (46, 43), (32, 52), (18, 43), (15, 21)), fill="white")
    draw.rectangle((29, 24, 35, 43), fill=(21, 99, 71, 255))

    stopping = threading.Event()

    def open_workspace(_icon: Any = None, _item: Any = None) -> None:
        webbrowser.open(url, new=2)

    def stop_application(icon: Any = None, _item: Any = None) -> None:
        if stopping.is_set():
            return
        stopping.set()
        _stop_process(process, log_handle)
        if icon is not None:
            icon.stop()

    icon = pystray.Icon(
        "TuomingAgent",
        image,
        APP_NAME,
        menu=pystray.Menu(
            pystray.MenuItem("打开工作台", open_workspace, default=True),
            pystray.MenuItem("退出 Tuoming Agent", stop_application),
        ),
    )

    def monitor() -> None:
        while not stopping.wait(0.5):
            if process.poll() is not None or shutdown_path.exists():
                stop_application(icon)
                return

    threading.Thread(target=monitor, name="tuoming-desktop-monitor", daemon=True).start()
    icon.run()


def _wait_until_ready(process: subprocess.Popen[bytes], port: int, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    health_url = f"http://127.0.0.1:{port}/_stcore/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Tuoming 本地服务启动失败，请查看本地 logs 目录。")
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    raise RuntimeError("Tuoming 本地服务启动超时，请检查安全软件或端口占用。")


def _find_available_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("没有可用的本地端口，请关闭其他 Tuoming 实例后重试。")


def _open_running_instance(app_dir: Path) -> bool:
    runtime_path = app_dir / "runtime.json"
    try:
        payload = json.loads(runtime_path.read_text(encoding="utf-8"))
        port = int(payload["port"])
        if not 1 <= port <= 65_535:
            return False
        health_url = f"http://127.0.0.1:{port}/_stcore/health"
        with urllib.request.urlopen(health_url, timeout=1) as response:
            if response.status != 200:
                return False
        webbrowser.open(f"http://127.0.0.1:{port}", new=2)
        return True
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _bundled_app_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "app.py"
    return Path(__file__).resolve().parents[2] / "app.py"


def _open_log(app_dir: Path) -> IO[bytes]:
    log_dir = app_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "desktop.log"
    archived_path = log_dir / "desktop.log.1"
    if log_path.exists() and log_path.stat().st_size >= 5 * 1024 * 1024:
        archived_path.unlink(missing_ok=True)
        log_path.replace(archived_path)
    return log_path.open("ab", buffering=0)


def _stop_process(process: subprocess.Popen[bytes], log_handle: IO[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if not log_handle.closed:
        log_handle.close()


def _acquire_single_instance() -> int | None:
    if os.name != "nt":
        return 1
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not handle:
        return None
    if kernel32.GetLastError() == _ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def _release_mutex(handle: int) -> None:
    if os.name == "nt" and handle:
        ctypes.windll.kernel32.CloseHandle(handle)


def _show_message(message: str, *, error: bool) -> None:
    if os.name == "nt":
        icon = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, icon)
    else:
        print(message, file=sys.stderr if error else sys.stdout)


if __name__ == "__main__":
    main()
