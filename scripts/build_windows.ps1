param(
    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$RequireSignature,
    [switch]$RequireAntivirus
)

$ErrorActionPreference = "Stop"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
python -m pip install -e ".[desktop,dev]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
if (-not $SkipTests) {
    python -m pytest
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
    python -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed." }
    python -m compileall -q tuoming_agent
    if ($LASTEXITCODE -ne 0) { throw "compileall failed." }
}
python -m PyInstaller --clean --noconfirm TuomingAgent.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

& "$PSScriptRoot\sign_windows.ps1" -Files "dist\TuomingAgent\TuomingAgent.exe" -Required:$RequireSignature
Compress-Archive -Path "dist\TuomingAgent\*" -DestinationPath "dist\TuomingAgent-Windows-x64.zip" -CompressionLevel Optimal -Force

if (-not $SkipInstaller) {
    $iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if (-not $iscc) {
        $innoCandidates = @(
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
        )
        $innoPath = $innoCandidates |
            Where-Object { Test-Path -LiteralPath $_ } |
            Select-Object -First 1
        if ($innoPath) { $iscc = Get-Item $innoPath }
    }
    if (-not $iscc) { throw "Inno Setup 6 was not found. Install it or pass -SkipInstaller." }
    $appVersion = (& python -c "from tuoming_agent import __version__; print(__version__)").Trim()
    $isccPath = if ($iscc.Path) { $iscc.Path } else { $iscc.FullName }
    $sourceRoot = (Resolve-Path "dist\TuomingAgent").Path
    $junction = Join-Path ([IO.Path]::GetTempPath()) ("tuoming-inno-" + [guid]::NewGuid())
    New-Item -ItemType Junction -Path $junction -Target $sourceRoot | Out-Null
    try {
        & $isccPath "/DMyAppVersion=$appVersion" "/DSourceRoot=$junction" "installer\TuomingAgent.iss"
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup build failed." }
    } finally {
        if (Test-Path -LiteralPath $junction) { Remove-Item -LiteralPath $junction -Force }
    }
    & "$PSScriptRoot\sign_windows.ps1" -Files "dist\TuomingAgent-Setup.exe" -Required:$RequireSignature
}

$artifacts = @("dist\TuomingAgent\TuomingAgent.exe", "dist\TuomingAgent-Windows-x64.zip")
if (-not $SkipInstaller) { $artifacts += "dist\TuomingAgent-Setup.exe" }
foreach ($artifact in $artifacts) {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact).Hash.ToLowerInvariant()
    $name = Split-Path -Leaf $artifact
    Set-Content -LiteralPath "$artifact.sha256" -Value "$hash  $name" -Encoding ascii
}
& "$PSScriptRoot\scan_windows.ps1" -Paths $artifacts -Required:$RequireAntivirus
Write-Host "Built Windows onedir, portable ZIP and installer artifacts."