<#
.SYNOPSIS
Start the watermarks-remover service locally with everything Layer B needs.

.DESCRIPTION
One command, safe to re-run at any time (it only installs or downloads what
is missing). Run it through start-service.cmd at the repo root. Steps:
  1. create .venv and install the mlm extras (torch CPU + transformers)
  2. load .env into this process; the server inherits it
  3. ollama backend: start `ollama serve` if it is not answering and pull
     the configured model if it is missing
  4. pre-download roberta-large so the first /clean does not time out
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

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

function Stop-WithError([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Invoke-Checked([string]$Exe, [string[]]$ArgList) {
    & $Exe @ArgList
    if ($LASTEXITCODE -ne 0) { Stop-WithError "'$Exe $($ArgList -join ' ')' failed (exit $LASTEXITCODE)" }
}

function Test-Native([string]$Exe, [string[]]$ArgList) {
    # Windows PowerShell turns redirected native stderr into errors under
    # "Stop"; a probe must only look at the exit code.
    $ErrorActionPreference = "Continue"
    & $Exe @ArgList 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-Url([string]$Url, [hashtable]$Headers = @{}) {
    try {
        $null = Invoke-WebRequest -Uri $Url -Headers $Headers -UseBasicParsing -TimeoutSec 3
        return $true
    } catch {
        return $false
    }
}

# --- 1. Python venv + mlm extras ------------------------------------------
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

$Requirements = Join-Path $Root "service\requirements-local.txt"
if (Test-Native $Py @("-c", "import torch, transformers, huggingface_hub")) {
    Write-Step "Python dependencies already installed"
} else {
    Write-Step "Installing Python dependencies (first run downloads about 1 GB)"
    $torchPin = (Select-String -Path $Requirements -Pattern '^torch==\S+$').Line
    if (-not $torchPin) { Stop-WithError "No torch pin in $Requirements" }
    Invoke-Checked $Py @("-m", "pip", "install", "--disable-pip-version-check", "--index-url", "https://download.pytorch.org/whl/cpu", $torchPin)
    Invoke-Checked $Py @("-m", "pip", "install", "--disable-pip-version-check", "-r", $Requirements)
}

# --- 2. .env ----------------------------------------------------------------
$EnvFile = Join-Path $Root ".env"
if (Test-Path $EnvFile) {
    Write-Step "Loading .env"
    foreach ($line in Get-Content $EnvFile -Encoding UTF8) {
        $text = $line.Trim()
        if ($text -eq "" -or $text.StartsWith("#")) { continue }
        $eq = $text.IndexOf("=")
        if ($eq -lt 1) { continue }
        $key = $text.Substring(0, $eq).Trim()
        $value = $text.Substring($eq + 1).Trim()
        if ($value.Length -ge 2 -and $value[0] -eq $value[-1] -and ($value[0] -eq '"' -or $value[0] -eq "'")) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
} else {
    Write-Step "No .env found; using the local Ollama defaults"
}

# A fresh clone has no rewrite backend configured; default to local Ollama so
# Layer B works out of the box once Ollama is installed.
if (-not $env:WATERMARKS_REWRITE_BACKEND) {
    $env:WATERMARKS_REWRITE_BACKEND = "ollama"
    if (-not $env:WATERMARKS_REWRITE_MODEL) { $env:WATERMARKS_REWRITE_MODEL = "qwen2.5:7b" }
    if (-not $env:WATERMARKS_REWRITE_BASE_URL) { $env:WATERMARKS_REWRITE_BASE_URL = "http://127.0.0.1:11434" }
}

# Local models on modest hardware can take minutes per rewrite call.
if (-not $env:WATERMARKS_REWRITE_TIMEOUT) { $env:WATERMARKS_REWRITE_TIMEOUT = "900" }

# --- 3. Ollama --------------------------------------------------------------
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

# --- 4. roberta-large (mlm step) -------------------------------------------
# Same Hub id as rewrite_text._MLM_MODEL, so the server finds it in the cache.
Write-Step "Checking the roberta-large model (one-time download, about 1.4 GB)"
$fetch = "from huggingface_hub import snapshot_download; snapshot_download('FacebookAI/roberta-large', allow_patterns=['*.json', '*.txt', 'model.safetensors'])"
Invoke-Checked $Py @("-c", $fetch)

# --- 5. (Re)start the server ------------------------------------------------
# Re-query every pass: several old servers can share the port (e.g. one
# started via the py launcher and one via python3), and a single snapshot
# does not always list all of them.
$deadline = (Get-Date).AddSeconds(15)
while ($true) {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($listeners.Count -eq 0) { break }
    foreach ($procId in $listeners) {
        $proc = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -eq $procId }
        if (-not $proc) { continue }
        if ($proc.CommandLine -match "server\.py") {
            Write-Step "Stopping the old server (PID $procId)"
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        } else {
            Stop-WithError "Port $Port is used by another program ($($proc.Name), PID $procId). Close it or pass -Port."
        }
    }
    if ((Get-Date) -gt $deadline) { Stop-WithError "Port $Port is still busy (PIDs: $($listeners -join ', '))." }
    Start-Sleep -Milliseconds 500
}

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "service.log"
$ErrLog = Join-Path $LogDir "service.err.log"
Write-Step "Starting the service on http://${BindHost}:$Port"
$server = Start-Process -FilePath $Py -ArgumentList @("service\scripts\server.py", "--host", $BindHost, "--port", "$Port") `
    -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $Log -RedirectStandardError $ErrLog -PassThru

# --- 6. Wait for /health ----------------------------------------------------
$headers = @{}
if ($env:WATERMARKS_SERVER_API_KEY) { $headers["Authorization"] = "Bearer $($env:WATERMARKS_SERVER_API_KEY)" }
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
Write-Host "  Layer B backend: $($env:WATERMARKS_REWRITE_BACKEND) / $($env:WATERMARKS_REWRITE_MODEL)"
if ($env:WATERMARKS_SERVER_API_KEY) { Write-Host "  API key required: send 'Authorization: Bearer <WATERMARKS_SERVER_API_KEY from .env>'" }
Write-Host "  Logs: $Log, $ErrLog"
