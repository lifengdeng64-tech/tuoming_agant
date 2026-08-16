param(
    [switch]$RequireSignature
)

$ErrorActionPreference = "Stop"
$artifacts = @(
    "dist\TuomingAgent\TuomingAgent.exe",
    "dist\TuomingAgent-Setup.exe",
    "dist\TuomingAgent-Windows-x64.zip"
)
foreach ($artifact in $artifacts) {
    if (-not (Test-Path -LiteralPath $artifact)) { throw "Missing release artifact: $artifact" }
    $hashFile = "$artifact.sha256"
    if (-not (Test-Path -LiteralPath $hashFile)) { throw "Missing checksum: $hashFile" }
    $expected = ((Get-Content -LiteralPath $hashFile -Raw) -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $artifact).Hash.ToLowerInvariant()
    if ($expected -ne $actual) { throw "Checksum mismatch: $artifact" }
}
foreach ($signedArtifact in @("dist\TuomingAgent\TuomingAgent.exe", "dist\TuomingAgent-Setup.exe")) {
    $signature = Get-AuthenticodeSignature -LiteralPath $signedArtifact
    if ($RequireSignature -and $signature.Status -ne "Valid") {
        throw "Authenticode signature is not valid: $signedArtifact ($($signature.Status))"
    }
    Write-Host "$signedArtifact signature: $($signature.Status)"
}