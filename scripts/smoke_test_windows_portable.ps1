param(
    [string]$ExePath = "dist\TuomingAgent\TuomingAgent.exe"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $ExePath)) { throw "Missing portable executable: $ExePath" }

$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()
$runId = [guid]::NewGuid().ToString("N")
$stdoutPath = Join-Path ([IO.Path]::GetTempPath()) "tuoming-smoke-$runId.stdout.log"
$stderrPath = Join-Path ([IO.Path]::GetTempPath()) "tuoming-smoke-$runId.stderr.log"
$process = Start-Process `
    -FilePath (Resolve-Path -LiteralPath $ExePath) `
    -ArgumentList "--streamlit-child", "--port", $port `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -WindowStyle Hidden `
    -PassThru

try {
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    $healthy = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) { break }
        try {
            $healthResponse = Invoke-WebRequest `
                -Uri "http://127.0.0.1:$port/_stcore/health" `
                -TimeoutSec 2 `
                -UseBasicParsing
            if ($healthResponse.StatusCode -eq 200) { $healthy = $true; break }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $healthy) { throw "Packaged Streamlit child did not become healthy." }
    $pageResponse = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$port/" `
        -TimeoutSec 10 `
        -UseBasicParsing
    if ($pageResponse.StatusCode -ne 200 -or $pageResponse.Content -notmatch "<title>Streamlit</title>") {
        throw "Packaged application home page did not load."
    }
} finally {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id
        $null = $process.WaitForExit(10000)
    }
}

$smokeLog = @(
    (Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue),
    (Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue)
) -join "`n"
if ($smokeLog -match "ModuleNotFoundError|No module named|Uncaught app exception") {
    throw "Packaged application log contains a startup error: $smokeLog"
}
Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
Write-Host "Portable application smoke test passed on port $port."
