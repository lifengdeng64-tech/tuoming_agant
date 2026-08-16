from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_prerelease_workflow_publishes_only_portable_assets() -> None:
    workflow = (ROOT / ".github/workflows/release-windows.yml").read_text(encoding="utf-8")

    assert "contains(github.ref_name, '-')" in workflow
    assert "-SkipInstaller:$portableOnly" in workflow
    assert '"dist/TuomingAgent-Windows-x64.zip"' in workflow
    assert '"dist/TuomingAgent-Windows-x64.zip.sha256"' in workflow
    assert "--prerelease" in workflow
    assert "unsigned portable beta" in workflow


def test_portable_verification_does_not_require_installer() -> None:
    verifier = (ROOT / "scripts/verify_windows_release.ps1").read_text(encoding="utf-8")

    assert "[switch]$SkipInstaller" in verifier
    assert 'if (-not $SkipInstaller) { $artifacts += "dist\\TuomingAgent-Setup.exe" }' in verifier
    assert (
        'if (-not $SkipInstaller) { $signedArtifacts += "dist\\TuomingAgent-Setup.exe" }'
        in verifier
    )
