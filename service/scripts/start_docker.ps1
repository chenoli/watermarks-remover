<#
.SYNOPSIS
Start the watermarks-remover service in Docker with one command.

.DESCRIPTION
Run it through start-docker.cmd at the repo root. Needs only Docker Desktop
(no Python on the host) and is safe to re-run at any time. Steps:
  1. start Docker Desktop if the engine is not answering
  2. with no rewrite provider in .env yet, run the setup wizard inside a
     container (`docker compose run --rm wr-setup`), which writes .env
  3. stop any local server.py holding the port, then
     `docker compose up -d --build wr-core`
  4. wait for /health
  5. when the strategy has an mlm step, pre-download roberta-large into the
     container's cache volume
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

. (Join-Path $PSScriptRoot "launcher_common.ps1")

# Docker Desktop's port proxies own the published port while wr-core runs.
$DockerProxies = @("com.docker.backend", "wslrelay", "vpnkit", "docker-proxy", "com.docker.proxy")

# --- 1. Docker engine ---------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Stop-WithError "Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and run again."
}
if (-not (Test-Native "docker" @("info"))) {
    # Machine-wide installs live in Program Files, per-user ones in
    # %LOCALAPPDATA%; the CLI sits in <install>\resources\bin either way.
    $cliRoot = Split-Path (Split-Path (Split-Path (Get-Command docker).Source))
    $desktop = @(
        (Join-Path $cliRoot "Docker Desktop.exe"),
        (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe")
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $desktop) { Stop-WithError "The Docker engine is not running. Start Docker Desktop and run again." }
    Write-Step "Starting Docker Desktop (can take a minute)"
    Start-Process -FilePath $desktop | Out-Null
    $deadline = (Get-Date).AddSeconds(240)
    while (-not (Test-Native "docker" @("info"))) {
        if ((Get-Date) -gt $deadline) { Stop-WithError "Docker Desktop did not start within 4 minutes." }
        Start-Sleep -Seconds 3
    }
}
Write-Step "Docker engine is running"

# --- 2. Rewrite provider (.env) -------------------------------------------------
$EnvFile = Join-Path $Root ".env"
Import-DotEnv $EnvFile
if (-not $env:WATERMARKS_REWRITE_BACKEND) {
    Write-Step "No rewrite provider configured yet; starting the setup"
    & docker compose --profile setup run --build --rm wr-setup
    if ($LASTEXITCODE -ne 0) { Stop-WithError "Setup was not completed. Run start-docker.cmd again to choose a provider." }
    Import-DotEnv $EnvFile
}
$Strategy = $env:WATERMARKS_CLEAN_STRATEGY
if (-not $Strategy) { $Strategy = "paraphrase@0.8" }
$NoRewrite = "$Strategy".Trim() -ieq "none"
if ($NoRewrite) {
    Write-Step "Layer B off (strategy none): wording is kept, only invisible marks are removed"
} else {
    Write-Step "Layer B: $($env:WATERMARKS_REWRITE_BACKEND) / $($env:WATERMARKS_REWRITE_MODEL), strategy $Strategy"
}

# --- 3. Build and start wr-core -------------------------------------------------
Stop-PortListeners $Port $DockerProxies
Write-Step "Building and starting the container (first run takes a few minutes)"
Invoke-Checked "docker" @("compose", "up", "-d", "--build", "wr-core")

# --- 4. Wait for /health --------------------------------------------------------
$headers = Get-HealthHeaders
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while (-not (Test-Url "http://127.0.0.1:$Port/health" $headers)) {
    if ((Get-Date) -gt $deadline) {
        & docker compose logs --tail 30 wr-core
        Stop-WithError "The container did not answer /health within $TimeoutSeconds s."
    }
    Start-Sleep -Seconds 2
}

# --- 5. roberta-large for the mlm step ------------------------------------------
if ($Strategy -match '(^|,)\s*mlm@') {
    # Same Hub id as rewrite_text._MLM_MODEL; the cache lives in a volume.
    Write-Step "Checking the roberta-large model (one-time download, about 1.4 GB)"
    $fetch = "from huggingface_hub import snapshot_download; snapshot_download('FacebookAI/roberta-large', allow_patterns=['*.json', '*.txt', 'model.safetensors'])"
    Invoke-Checked "docker" @("compose", "exec", "-T", "wr-core", "python3", "-c", $fetch)
}

Write-Host ""
Write-Host "Service ready at http://127.0.0.1:$Port (Docker container wr-core)" -ForegroundColor Green
if ($NoRewrite) {
    Write-Host "  Layer B: off (strategy none)"
} else {
    Write-Host "  Layer B backend: $($env:WATERMARKS_REWRITE_BACKEND) / $($env:WATERMARKS_REWRITE_MODEL)"
}
if ($env:WATERMARKS_SERVER_API_KEY) { Write-Host "  API key required: send 'Authorization: Bearer <WATERMARKS_SERVER_API_KEY from .env>'" }
Write-Host "  Logs: docker compose logs -f wr-core    Stop: docker compose down"
