from __future__ import annotations

from pathlib import Path

import pytest

from tuoming_agent.config import AppConfig
from tuoming_agent.workspace.service import ApplicationServices, create_services


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        master_key=b"test-master-key-material-32-bytes!",
        key_version=1,
        data_dir=tmp_path / "data",
        default_tenant="tenant-a",
    )


@pytest.fixture
def services(config: AppConfig) -> ApplicationServices:
    return create_services(config)


@pytest.fixture
def workspace(services: ApplicationServices):
    return services.repository.create_workspace("tenant-a", "测试工作区")

