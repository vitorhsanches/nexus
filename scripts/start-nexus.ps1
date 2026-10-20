[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$OmniRouteCommand = "omniroute",
    [int]$OmniRoutePort = 20128,
    [int]$NexusPort = 8000,
    [int]$TimeoutSeconds = 90,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

function Write-Ok([string]$Message) { Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Info([string]$Message) { Write-Host "     $Message" -ForegroundColor DarkGray }

function Stop-Launch([string]$Reason, [string[]]$Details) {
    Write-Host ""
    Write-Host "NEXUS STARTUP FAILED" -ForegroundColor Red
    Write-Host $Reason
    foreach ($line in $Details) { Write-Host "  $line" }
    exit 1
}

function Test-HttpResponse([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return [int]$response.StatusCode
    } catch [System.Net.WebException] {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    } catch {
        return 0
    }
}

function Test-OmniRouteOnline {
    $status = Test-HttpResponse "http://127.0.0.1:$OmniRoutePort/v1/models"
    return ($status -ge 200 -and $status -lt 500)
}

function Test-NexusOnline {
    try {
        $payload = Invoke-RestMethod -Uri "http://127.0.0.1:$NexusPort/api/routing/status" -TimeoutSec 3
        return ($null -ne $payload.runtime -and $null -ne $payload.summary)
    } catch {
        return $false
    }
}

function Get-PortOwner([int]$Port) {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($connection) { return [int]$connection.OwningProcess }
    return $null
}

function Wait-Until([scriptblock]$Ready, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Ready) { return $true }
        Start-Sleep -Milliseconds 800
    }
    return $false
}

# ---- Step 1: preflight -------------------------------------------------------
if (-not $RepoRoot) { $RepoRoot = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) ".." }
$root = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction SilentlyContinue)
if (-not $root) { Stop-Launch "Nexus repository not found:" @($RepoRoot) }
$root = $root.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    Stop-Launch "Python environment not found:" @($python, "Create the .venv before starting Nexus.")
}
Push-Location $root
try {
    & $python -c "import nexus.web.app" 2>$null
    $importExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($importExit -ne 0) {
    Stop-Launch "Nexus failed to import with the project environment:" @($python)
}

# ---- Step 2: OmniRoute -------------------------------------------------------
if (Test-OmniRouteOnline) {
    Write-Ok "OmniRoute online"
} else {
    $omniCommand = Get-Command $OmniRouteCommand -ErrorAction SilentlyContinue
    if (-not $omniCommand) {
        Stop-Launch "OmniRoute is not running and the '$OmniRouteCommand' command is not available." @(
            "Install or fix the OmniRoute command, then run nexus again."
        )
    }
    Write-Info "OmniRoute not responding on port $OmniRoutePort; starting it in the background"
    & $omniCommand.Source serve --daemon --no-open | Out-Null
    if (-not (Wait-Until { Test-OmniRouteOnline } $TimeoutSeconds)) {
        Stop-Launch "OmniRoute did not become ready on port $OmniRoutePort within $TimeoutSeconds seconds." @(
            "Nexus was not started."
        )
    }
    Write-Ok "OmniRoute online"
}

# ---- Step 3: Control Center --------------------------------------------------
if (Test-NexusOnline) {
    Write-Ok "Nexus already running"
} else {
    $owner = Get-PortOwner $NexusPort
    if ($owner) {
        Stop-Launch "Port $NexusPort is in use by a process that is not the Nexus Control Center (PID $owner)." @(
            "Stop that process yourself, then run nexus again. The launcher does not kill unrelated processes."
        )
    }
    $logDir = Join-Path $env:LOCALAPPDATA "Nexus\Logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdout = Join-Path $logDir "control-center-$stamp.out.log"
    $stderr = Join-Path $logDir "control-center-$stamp.err.log"
    Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "nexus.web.app:app", "--host", "127.0.0.1", "--port", "$NexusPort") `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr | Out-Null
    if (-not (Wait-Until { Test-NexusOnline } $TimeoutSeconds)) {
        Stop-Launch "Control Center did not become ready within $TimeoutSeconds seconds." @("Logs: $stderr")
    }
    Write-Ok "Control Center started"
}

# ---- Step 4: readiness summary ----------------------------------------------
$url = "http://127.0.0.1:$NexusPort/nexus"
Write-Host ""
Write-Host "========================================"
Write-Host " NEXUS"
Write-Host "========================================"
Write-Host " OmniRoute      ONLINE"
Write-Host " Control Center ONLINE"
Write-Host ""
Write-Host " $url"
Write-Host "========================================"

# ---- Step 5: browser ---------------------------------------------------------
if (-not $NoBrowser) { Start-Process $url | Out-Null }
