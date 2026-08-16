from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from tuoming_agent.desktop.updater import (
    CHECKSUM_NAME,
    INSTALLER_NAME,
    UpdateError,
    UpdateManager,
    _parse_checksum,
    _validate_update_url,
)


def test_update_urls_require_https_and_allowlisted_hosts() -> None:
    _validate_update_url("https://github.com/example/file")
    with pytest.raises(UpdateError):
        _validate_update_url("http://github.com/example/file")
    with pytest.raises(UpdateError):
        _validate_update_url("https://attacker.example/file")


def test_checksum_parser_rejects_invalid_content() -> None:
    with pytest.raises(UpdateError):
        _parse_checksum("not-a-checksum")


def test_update_download_verifies_hash_and_keeps_rollback_installers(tmp_path: Path) -> None:
    installer = b"signed-installer-placeholder"
    digest = hashlib.sha256(installer).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "tag_name": "v9.9.9",
                    "html_url": "https://github.com/lifengdeng64-tech/tuoming_agant/releases/tag/v9.9.9",
                    "assets": [
                        {
                            "name": INSTALLER_NAME,
                            "browser_download_url": "https://github.com/download/setup.exe",
                        },
                        {
                            "name": CHECKSUM_NAME,
                            "browser_download_url": "https://github.com/download/setup.sha256",
                        },
                    ],
                },
            )
        if request.url.path.endswith("setup.sha256"):
            return httpx.Response(200, text=f"{digest}  {INSTALLER_NAME}")
        return httpx.Response(200, content=installer)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    manager = UpdateManager(tmp_path, require_authenticode=False, client=client)
    info = manager.check()
    downloaded = manager.download(info)

    assert info.is_newer
    assert downloaded.path.read_bytes() == installer
    assert downloaded.sha256 == digest
    assert len(manager.rollback_candidates()) == 1


def test_update_download_rejects_hash_mismatch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("checksum"):
            return httpx.Response(200, text=f"{'0' * 64}  {INSTALLER_NAME}")
        return httpx.Response(200, content=b"tampered")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    manager = UpdateManager(tmp_path, require_authenticode=False, client=client)
    from tuoming_agent.desktop.updater import UpdateInfo

    with pytest.raises(UpdateError, match="SHA-256"):
        manager.download(
            UpdateInfo(
                "9.9.9",
                "https://github.com/download/setup",
                "https://github.com/download/checksum",
                "https://github.com/release",
            )
        )