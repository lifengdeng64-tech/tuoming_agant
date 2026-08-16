from __future__ import annotations

from pathlib import Path

import pytest

from tuoming_agent.backup import BackupError, BackupManager, apply_pending_restore
from tuoming_agent.security.credentials import MemorySecretStore
from tuoming_agent.settings import LocalSettingsManager, ModelSettings


def _profile(tmp_path: Path):
    app_dir = tmp_path / "app"
    data_dir = app_dir / "data"
    data_dir.mkdir(parents=True)
    secrets = MemorySecretStore()
    settings = LocalSettingsManager(app_dir, secrets)
    settings.save_model_settings(ModelSettings("openai", "https://api.openai.com/v1", "gpt-test"))
    settings.save_api_key("sk-private")
    master_key = settings.load_or_create_master_key(data_dir)
    (data_dir / "artifact.bin").write_bytes(b"encrypted-artifact")
    return app_dir, data_dir, settings, master_key


def test_encrypted_backup_round_trip_migrates_credentials_and_data(tmp_path: Path) -> None:
    app_dir, data_dir, settings, master_key = _profile(tmp_path)
    manager = BackupManager(app_dir, data_dir, settings)
    backup = manager.create_backup(tmp_path / "portable.tmbak", "correct horse battery")
    payload = backup.read_bytes()

    assert b"sk-private" not in payload
    assert master_key not in payload

    restore_app = tmp_path / "restore-app"
    restore_settings = LocalSettingsManager(restore_app, MemorySecretStore())
    restore_manager = BackupManager(restore_app, restore_app / "data", restore_settings)
    restore_manager.stage_restore(backup, "correct horse battery")
    recovery = apply_pending_restore(restore_app, restore_settings)

    assert recovery is not None
    assert (restore_app / "data" / "artifact.bin").read_bytes() == b"encrypted-artifact"
    assert restore_settings.get_api_key() == "sk-private"
    assert restore_settings.load_or_create_master_key(restore_app / "data") == master_key


def test_wrong_backup_password_does_not_schedule_restore(tmp_path: Path) -> None:
    app_dir, data_dir, settings, _ = _profile(tmp_path)
    backup = BackupManager(app_dir, data_dir, settings).create_backup(
        tmp_path / "portable.tmbak", "correct horse battery"
    )
    restore_app = tmp_path / "restore-app"
    restore_settings = LocalSettingsManager(restore_app, MemorySecretStore())

    with pytest.raises(BackupError):
        BackupManager(restore_app, restore_app / "data", restore_settings).stage_restore(
            backup, "incorrect password"
        )

    assert not (restore_app / "restore.pending.json").exists()


def test_short_backup_password_is_rejected(tmp_path: Path) -> None:
    app_dir, data_dir, settings, _ = _profile(tmp_path)
    with pytest.raises(BackupError, match="10"):
        BackupManager(app_dir, data_dir, settings).create_backup(tmp_path / "x.tmbak", "short")