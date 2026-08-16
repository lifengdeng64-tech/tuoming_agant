param(
    [Parameter(Mandatory = $true)][string[]]$Files,
    [switch]$Required
)

$ErrorActionPreference = "Stop"

function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path $kits) {
        $candidate = Get-ChildItem -LiteralPath $kits -Recurse -Filter signtool.exe |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) { return $candidate.FullName }
    }
    throw "signtool.exe was not found. Install the Windows SDK signing tools."
}

$pfxBase64 = $env:WINDOWS_SIGNING_PFX_BASE64
$password = $env:WINDOWS_SIGNING_PASSWORD
$timestampUrl = if ($env:WINDOWS_SIGNING_TIMESTAMP_URL) {
    $env:WINDOWS_SIGNING_TIMESTAMP_URL
} else {
    "https://timestamp.digicert.com"
}

if (-not $pfxBase64 -or -not $password) {
    if ($Required) { throw "Authenticode certificate secrets are required for this release." }
    Write-Host "Signing secrets are not configured; keeping development artifacts unsigned."
    exit 0
}

$signTool = Find-SignTool
$pfxPath = Join-Path ([IO.Path]::GetTempPath()) ("tuoming-signing-" + [guid]::NewGuid() + ".pfx")
try {
    [IO.File]::WriteAllBytes($pfxPath, [Convert]::FromBase64String($pfxBase64))
    foreach ($file in $Files) {
        $resolved = (Resolve-Path -LiteralPath $file).Path
        & $signTool sign /fd SHA256 /f $pfxPath /p $password /tr $timestampUrl /td SHA256 $resolved
        if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed for $resolved" }
        & $signTool verify /pa /all /v $resolved
        if ($LASTEXITCODE -ne 0) { throw "Authenticode verification failed for $resolved" }
    }
} finally {
    if (Test-Path -LiteralPath $pfxPath) {
        Remove-Item -LiteralPath $pfxPath -Force
    }
}