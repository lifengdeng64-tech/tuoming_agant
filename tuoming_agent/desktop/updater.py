from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from tuoming_agent import __version__
from tuoming_agent.settings import NetworkSettings

RELEASE_API_URL = "https://api.github.com/repos/lifengdeng64-tech/tuoming_agant/releases/latest"
ALLOWED_UPDATE_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
INSTALLER_NAME = "TuomingAgent-Setup.exe"
CHECKSUM_NAME = f"{INSTALLER_NAME}.sha256"
MAX_INSTALLER_BYTES = 500 * 1024 * 1024


class UpdateError(RuntimeError):
    """Raised when an update cannot be trusted or prepared safely."""


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    installer_url: str
    checksum_url: str
    release_url: str

    @property
    def is_newer(self) -> bool:
        return _version_tuple(self.version) > _version_tuple(__version__)


@dataclass(frozen=True)
class DownloadedUpdate:
    version: str
    path: Path
    sha256: str
    signature_status: str


class UpdateManager:
    def __init__(
        self,
        app_dir: Path,
        network: NetworkSettings | None = None,
        expected_signer_thumbprint: str = "",
        require_authenticode: bool | None = None,
        client: httpx.Client | None = None,
    ):
        self.app_dir = Path(app_dir).resolve()
        self.update_dir = self.app_dir / "updates"
        self.expected_signer_thumbprint = _normalize_thumbprint(expected_signer_thumbprint)
        self.require_authenticode = (
            os.name == "nt" if require_authenticode is None else require_authenticode
        )
        self._owns_client = client is None
        self.client = client or _client_for(network or NetworkSettings())

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def check(self) -> UpdateInfo:
        response = self._get(RELEASE_API_URL)
        try:
            payload = response.json()
            version = str(payload["tag_name"]).removeprefix("v")
            assets = {asset["name"]: asset["browser_download_url"] for asset in payload["assets"]}
            installer_url = assets[INSTALLER_NAME]
            checksum_url = assets[CHECKSUM_NAME]
            release_url = str(payload["html_url"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise UpdateError("GitHub Release 缺少安装包或校验文件。") from exc
        _version_tuple(version)
        for url in (installer_url, checksum_url, release_url):
            _validate_update_url(url)
        return UpdateInfo(version, installer_url, checksum_url, release_url)

    def download(self, update: UpdateInfo) -> DownloadedUpdate:
        if not update.is_newer:
            raise UpdateError("目标版本不高于当前版本。")
        self.update_dir.mkdir(parents=True, exist_ok=True)
        checksum_text = self._get(update.checksum_url).text
        expected_hash = _parse_checksum(checksum_text)
        versioned_name = f"TuomingAgent-Setup-{update.version}.exe"
        destination = self.update_dir / versioned_name
        descriptor, temporary_name = tempfile.mkstemp(prefix=".update-", dir=self.update_dir)
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        total = 0
        try:
            with (
                self._stream(update.installer_url) as response,
                temporary_path.open("wb") as target,
            ):
                for chunk in response.iter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_INSTALLER_BYTES:
                        raise UpdateError("更新安装包超过安全体积上限。")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            actual_hash = digest.hexdigest()
            if actual_hash != expected_hash:
                raise UpdateError("更新安装包 SHA-256 校验失败。")
            os.replace(temporary_path, destination)
            signature_status = verify_authenticode(
                destination,
                self.expected_signer_thumbprint,
                required=self.require_authenticode,
            )
            self._record(update.version, destination, actual_hash, signature_status)
            self._prune()
            return DownloadedUpdate(update.version, destination, actual_hash, signature_status)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            raise

    def rollback_candidates(self) -> list[dict[str, Any]]:
        manifest = self._load_manifest()
        return [
            item
            for item in manifest.get("installers", [])
            if Path(item.get("path", "")).is_file()
        ]

    def launch_installer(self, installer: Path) -> subprocess.Popen[bytes]:
        installer = Path(installer).resolve()
        if self.update_dir not in installer.parents or not installer.is_file():
            raise UpdateError("安装包不在受控更新目录内。")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.Popen(
            [str(installer), "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def _get(self, url: str) -> httpx.Response:
        with self._stream(url) as response:
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=response.read(),
                request=response.request,
            )

    @contextmanager
    def _stream(self, url: str):
        current = url
        for _ in range(4):
            _validate_update_url(current)
            request = self.client.build_request(
                "GET", current, headers={"Accept": "application/vnd.github+json"}
            )
            response = self.client.send(request, stream=True)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location", "")
                response.close()
                current = str(httpx.URL(current).join(location))
                continue
            try:
                response.raise_for_status()
            except Exception as exc:
                response.close()
                raise UpdateError("更新服务器返回错误。") from exc
            try:
                yield response
            finally:
                response.close()
            return
        raise UpdateError("更新下载重定向次数过多。")

    def _record(self, version: str, path: Path, sha256: str, signature_status: str) -> None:
        manifest = self._load_manifest()
        items = [item for item in manifest.get("installers", []) if item.get("version") != version]
        items.insert(
            0,
            {
                "version": version,
                "path": str(path),
                "sha256": sha256,
                "signature_status": signature_status,
            },
        )
        _atomic_json(self.update_dir / "manifest.json", {"format": 1, "installers": items})

    def _load_manifest(self) -> dict[str, Any]:
        path = self.update_dir / "manifest.json"
        if not path.is_file():
            return {"format": 1, "installers": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError("本地更新清单已损坏。") from exc
        return payload if isinstance(payload, dict) else {"format": 1, "installers": []}

    def _prune(self) -> None:
        manifest = self._load_manifest()
        items = manifest.get("installers", [])
        for item in items[2:]:
            Path(item.get("path", "")).unlink(missing_ok=True)
        manifest["installers"] = items[:2]
        _atomic_json(self.update_dir / "manifest.json", manifest)


def verify_authenticode(
    path: Path, expected_thumbprint: str = "", *, required: bool = False
) -> str:
    if os.name != "nt":
        if expected_thumbprint:
            raise UpdateError("当前平台无法验证 Windows Authenticode 签名。")
        return "not-verified-on-this-platform"
    escaped = str(Path(path).resolve()).replace("'", "''")
    command = (
        "$s=Get-AuthenticodeSignature -LiteralPath '"
        + escaped
        + "'; $t=if($s.SignerCertificate){$s.SignerCertificate.Thumbprint}else{''}; "
        "[pscustomobject]@{Status=$s.Status.ToString();Thumbprint=$t}"
        " | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        if required or expected_thumbprint:
            raise UpdateError("无法验证更新安装包的 Authenticode 签名。")
        return "Unavailable"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UpdateError("Authenticode 验证结果无效。") from exc
    status = str(payload.get("Status", "Unknown"))
    thumbprint = _normalize_thumbprint(str(payload.get("Thumbprint", "")))
    if required and status != "Valid":
        raise UpdateError("更新安装包没有有效的 Authenticode 签名。")
    if expected_thumbprint and thumbprint != expected_thumbprint:
        raise UpdateError("更新安装包发布者签名不匹配。")
    return status


def _client_for(network: NetworkSettings) -> httpx.Client:
    verify: bool | str = network.ca_bundle_path or True
    return httpx.Client(
        proxy=network.proxy_url or None,
        verify=verify,
        trust_env=network.use_system_proxy,
        follow_redirects=False,
        timeout=30,
    )


def _validate_update_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_UPDATE_HOSTS:
        raise UpdateError("更新地址不在允许的 HTTPS 主机列表中。")
    if parsed.username or parsed.password:
        raise UpdateError("更新地址不得包含凭据。")


def _parse_checksum(value: str) -> str:
    match = re.search(r"\b([0-9a-fA-F]{64})\b", value)
    if not match:
        raise UpdateError("更新校验文件格式无效。")
    return match.group(1).lower()


def _version_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", version)
    if not match:
        raise UpdateError("Release 版本号格式无效。")
    return tuple(int(part) for part in match.groups())


def _normalize_thumbprint(value: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", value).upper()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)