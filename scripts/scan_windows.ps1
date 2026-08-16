param(
    [Parameter(Mandatory = $true)][string[]]$Paths,
    [switch]$Required
)

$ErrorActionPreference = "Stop"

function Find-DefenderScanner {
    $command = Get-Command MpCmdRun.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $legacy = Join-Path $env:ProgramFiles "Windows Defender\MpCmdRun.exe"
    if (Test-Path -LiteralPath $legacy) { return $legacy }

    $platformRoot = Join-Path $env:ProgramData "Microsoft\Windows Defender\Platform"
    if (Test-Path -LiteralPath $platformRoot) {
        $candidate = Get-ChildItem -LiteralPath $platformRoot -Directory |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName "MpCmdRun.exe" } |
            Where-Object { Test-Path -LiteralPath $_ } |
            Select-Object -First 1
        if ($candidate) { return $candidate }
    }
    return $null
}

$defender = Find-DefenderScanner
if (-not $defender) {
    if ($Required) { throw "Windows Defender command-line scanner was not found." }
    Write-Host "Windows Defender scanner unavailable; skipping local antivirus scan."
    exit 0
}
foreach ($path in $Paths) {
    $resolved = (Resolve-Path -LiteralPath $path).Path
    & $defender -Scan -ScanType 3 -File $resolved -DisableRemediation
    if ($LASTEXITCODE -ne 0) {
        throw "Windows Defender rejected or could not scan $resolved (exit code $LASTEXITCODE)."
    }
}