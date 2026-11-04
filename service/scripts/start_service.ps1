<#
.SYNOPSIS
Start the watermarks-remover service locally with everything Layer B needs.

.DESCRIPTION
One command, safe to re-run at any time (it only installs or downloads what
is missing). Run it through start-service.cmd at the repo root. Steps:
  1. create .venv
  2. load .env; with no rewrite provider configured yet, run the setup
     wizard (setup_wizard.py) first
  3. only when the strategy has an mlm step: install torch CPU +
     transformers and pre-download roberta-large
  4. ollama backend: start `ollama serve` if it is not answering and pull
     the configured model if it is missing
  5. stop any server.py already listening on the port, start a fresh one
     (log: logs/service.log)
  6. wait for /health
#>
[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765,
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

. (Join-Path $PSScriptRoot "launcher_common.ps1")

# --- 1. Python venv ----------------------------------------------------------
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Step "Creating .venv"
    if (Get-Command py -ErrorAction SilentlyContinue) {
        Invoke-Checked "py" @("-3", "-m", "venv", ".venv")
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        Invoke-Checked "python" @("-m", "venv", ".venv")
    } else {
        Stop-WithError "Python 3.10+ not found. Install it from https://www.python.org/downloads/ and run again."
    }
}

# --- 2. .env + rewrite provider ---------------------------------------------
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Write-Step "Loading .env"
    Import-DotEnv $EnvFile
}

if (-not $env:WATERMARKS_REWRITE_BACKEND) {
    Write-Step "No rewrite provider configured yet; starting the setup"
    & $Py (Join-Path $Root "service\scripts\setup_wizard.py") --no-start-prompt
    if ($LASTEXITCODE -ne 0) { Stop-WithError "Setup was not completed. Run setup.cmd to choose a provider." }
    Import-DotEnv $EnvFile
}

$Strategy = $env:WATERMARKS_CLEAN_STRATEGY
if (-not $Strategy) {
    $Strategy = (Get-Content (Join-Path $Root "config\clean_strategy.json") -Raw | ConvertFrom-Json).default_strategy
}
$NeedsMlm = $Strategy -match '(^|,)\s*mlm@'
$NoRewrite = "$Strategy".Trim() -ieq "none"
if ($NoRewrite) {
    Write-Step "Layer B off (strategy none): wording is kept, only invisible marks are removed"
} else {
    Write-Step "Layer B: $($env:WATERMARKS_REWRITE_BACKEND) / $($env:WATERMARKS_REWRITE_MODEL), strategy $Strategy"
}

# Local models on modest hardware can take minutes per rewrite call.
if (-not $env:WATERMARKS_REWRITE_TIMEOUT) { $env:WATERMARKS_REWRITE_TIMEOUT = "900" }

# --- 3. mlm extras (only when the strategy has an mlm step) ------------------
if ($NeedsMlm) {
    $Requirements = Join-Path $Root "service\requirements-local.txt"
    if (Test-Native $Py @("-c", "import torch, transformers, huggingface_hub")) {
        Write-Step "mlm dependencies already installed"
    } else {
        Write-Step "Installing the mlm dependencies (first run downloads about 1 GB)"
        $torchPin = (Select-String -Path $Requirements -Pattern '^torch==\S+$').Line
        if (-not $torchPin) { Stop-WithError "No torch pin in $Requirements" }
        Invoke-Checked $Py @("-m", "pip", "install", "--disable-pip-version-check", "--index-url", "https://download.pytorch.org/whl/cpu", $torchPin)
        Invoke-Checked $Py @("-m", "pip", "install", "--disable-pip-version-check", "-r", $Requirements)
    }
    # Same Hub id as rewrite_text._MLM_MODEL, so the server finds it in the cache.
    Write-Step "Checking the roberta-large model (one-time download, about 1.4 GB)"
    $fetch = "from huggingface_hub import snapshot_download; snapshot_download('FacebookAI/roberta-large', allow_patterns=['*.json', '*.txt', 'model.safetensors'])"
    Invoke-Checked $Py @("-c", $fetch)
} else {
    Write-Step "Strategy has no mlm step; skipping torch/transformers"
}

# --- 4. Ollama --------------------------------------------------------------
if ($env:WATERMARKS_REWRITE_BACKEND -eq "ollama") {
    $ollamaUrl = $env:WATERMARKS_REWRITE_BASE_URL
    if (-not $ollamaUrl) { $ollamaUrl = "http://127.0.0.1:11434" }
    $ollamaUrl = $ollamaUrl.TrimEnd("/")
    $model = $env:WATERMARKS_REWRITE_MODEL
    if (-not $model) { Stop-WithError "WATERMARKS_REWRITE_MODEL is empty; set it in .env (e.g. qwen2.5:7b)." }

    $ollamaExe = $null
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) {
        $ollamaExe = $cmd.Source
    } elseif (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe") {
        $ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    }

    if (Test-Url "$ollamaUrl/api/tags") {
        Write-Step "Ollama is running at $ollamaUrl"
    } else {
        $ollamaHost = ([Uri]$ollamaUrl).Host
        if ($ollamaHost -notin @("127.0.0.1", "localhost", "::1")) {
            Stop-WithError "Ollama at $ollamaUrl is not answering (remote host; start it there)."
        }
        if (-not $ollamaExe) {
            Stop-WithError "Ollama is not installed. Install it from https://ollama.com/download and run again."
        }
        Write-Step "Starting Ollama"
        Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden | Out-Null
        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-Url "$ollamaUrl/api/tags")) {
            if ((Get-Date) -gt $deadline) { Stop-WithError "Ollama did not start within 30 s." }
            Start-Sleep -Seconds 1
        }
    }

    # Ollama lists untagged models as "<name>:latest".
    $wanted = $model
    if ($wanted -notmatch ":") { $wanted = "${wanted}:latest" }
    $installed = @((Invoke-RestMethod -Uri "$ollamaUrl/api/tags" -TimeoutSec 10).models | ForEach-Object { $_.name })
    if ($installed -contains $wanted) {
        Write-Step "Ollama model $model already downloaded"
    } else {
        Write-Step "Downloading Ollama model $model (one-time, several GB)"
        if ($ollamaExe) {
            Invoke-Checked $ollamaExe @("pull", $model)
        } else {
            $body = @{ model = $model; stream = $false } | ConvertTo-Json
            $null = Invoke-RestMethod -Method Post -Uri "$ollamaUrl/api/pull" -Body $body -ContentType "application/json" -TimeoutSec 7200
        }
    }
}

# --- 5. (Re)start the server ------------------------------------------------
Stop-PortListeners $Port

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "service.log"
$ErrLog = Join-Path $LogDir "service.err.log"
Write-Step "Starting the service on http://${BindHost}:$Port"
$server = Start-Process -FilePath $Py -ArgumentList @("service\scripts\server.py", "--host", $BindHost, "--port", "$Port") `
    -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $Log -RedirectStandardError $ErrLog -PassThru

# --- 6. Wait for /health ----------------------------------------------------
$headers = Get-HealthHeaders
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while (-not (Test-Url "http://${BindHost}:$Port/health" $headers)) {
    if ($server.HasExited) {
        Get-Content $ErrLog -Tail 20 -ErrorAction SilentlyContinue | Write-Host
        Stop-WithError "The service exited during startup; see $ErrLog."
    }
    if ((Get-Date) -gt $deadline) { Stop-WithError "The service did not answer /health within $TimeoutSeconds s; see $ErrLog." }
    Start-Sleep -Seconds 1
}

Write-Host ""
Write-Host "Service ready at http://${BindHost}:$Port (PID $($server.Id))" -ForegroundColor Green
if ($NoRewrite) {
    Write-Host "  Layer B: off (strategy none)"
} else {
    Write-Host "  Layer B backend: $($env:WATERMARKS_REWRITE_BACKEND) / $($env:WATERMARKS_REWRITE_MODEL)"
}
if ($env:WATERMARKS_SERVER_API_KEY) { Write-Host "  API key required: send 'Authorization: Bearer <WATERMARKS_SERVER_API_KEY from .env>'" }
Write-Host "  Logs: $Log, $ErrLog"
