# Helpers shared by start_service.ps1 (local Python) and start_docker.ps1
# (Docker). Dot-source it: . (Join-Path $PSScriptRoot "launcher_common.ps1")

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

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content $Path -Encoding UTF8) {
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
}

function Get-HealthHeaders {
    $headers = @{}
    if ($env:WATERMARKS_SERVER_API_KEY) { $headers["Authorization"] = "Bearer $($env:WATERMARKS_SERVER_API_KEY)" }
    return $headers
}

function Stop-PortListeners([int]$Port, [string[]]$KeepNames = @()) {
    # Stops every server.py listening on $Port and returns once the port is
    # free or only processes named in $KeepNames (e.g. Docker's port proxy)
    # remain. Anything else on the port is an error. Re-query every pass:
    # several old servers can share the port (e.g. one started via the py
    # launcher and one via python3), and a single snapshot does not always
    # list all of them.
    $deadline = (Get-Date).AddSeconds(15)
    while ($true) {
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
        $blocking = @()
        foreach ($procId in $listeners) {
            $proc = Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -eq $procId }
            if (-not $proc) { continue }
            if ($KeepNames -contains [IO.Path]::GetFileNameWithoutExtension($proc.Name)) { continue }
            if ($proc.CommandLine -match "server\.py") {
                Write-Step "Stopping the old server (PID $procId)"
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                $blocking += $procId
            } else {
                Stop-WithError "Port $Port is used by another program ($($proc.Name), PID $procId). Close it or pass -Port."
            }
        }
        if ($blocking.Count -eq 0) { return }
        if ((Get-Date) -gt $deadline) { Stop-WithError "Port $Port is still busy (PIDs: $($blocking -join ', '))." }
        Start-Sleep -Milliseconds 500
    }
}
