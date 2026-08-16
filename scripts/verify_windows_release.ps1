param(
    [switch]$RequireSignature,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$artifacts = @(
    "dist\TuomingAgent\TuomingAgent.exe",
    "dist\TuomingAgent-Windows-x64.zip"
)
if (-not $SkipInstaller) { $artifacts += "dist\TuomingAgent-Setup.exe" }
foreach ($artifact in $artifacts) {
    if (-not (Test-Path -LiteralPath $artifact)) { throw "Missing release artifact: $artifact" }
    $hashFile = "$artifact.sha256"
    if (-not (Test-Path -LiteralPath $hashFile)) { throw "Missing checksum: $hashFile" }
    $expected = ((Get-Content -LiteralPath $hashFile -Raw) -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact).Hash.ToLowerInvariant()
    if ($expected -ne $actual) { throw "Checksum mismatch: $artifact" }
}
$signedArtifacts = @("dist\TuomingAgent\TuomingAgent.exe")
if (-not $SkipInstaller) { $signedArtifacts += "dist\TuomingAgent-Setup.exe" }
foreach ($signedArtifact in $signedArtifacts) {
    $signature = Get-AuthenticodeSignature -LiteralPath $signedArtifact
    if ($RequireSignature -and $signature.Status -ne "Valid") {
        throw "Authenticode signature is not valid: $signedArtifact ($($signature.Status))"
    }
    Write-Host "$signedArtifact signature: $($signature.Status)"
}
& "$PSScriptRoot\smoke_test_windows_portable.ps1"
