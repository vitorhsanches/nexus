<#
.SYNOPSIS
Starts the Nexus Control Center.

.DESCRIPTION
Native-first: by default Nexus starts without OmniRoute, using the local Codex
and Claude Code logins. OmniRoute is opt-in with -EnableOmniRoute. The launcher
never starts an external service you did not ask for and never selects a model
backend for you; Codex/Claude availability is only reported.

-PreflightOnly runs every check and prints the summary without starting
OmniRoute or the Control Center.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$OmniRouteCommand = "omniroute",
    [int]$OmniRoutePort = 20128,
    [int]$NexusPort = 8000,
    [int]$TimeoutSeconds = 90,
    [switch]$EnableDevelopment,
    [switch]$EnableOmniRoute,
    [switch]$PreflightOnly,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

function Write-Ok([string]$Message) { Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }
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
        # Routing status can wait for OmniRoute telemetry even in Native mode.
        $payload = Invoke-RestMethod -Uri "http://127.0.0.1:$NexusPort/api/dev/execution-status" -TimeoutSec 3
        return ($null -ne $payload.execution_enabled)
    } catch {
        return $false
    }
}

function Get-PortOwner([int]$Port) {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($connection) { return [int]$connection.OwningProcess }
    return $null
}

function Test-ExpectedNexusProcess([int]$ProcessId, [string]$ExpectedRoot) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $process -or -not $process.CommandLine) { return $false }
    $command = [string]$process.CommandLine
    return ($command.IndexOf("nexus.web.app:app", [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $command.IndexOf("--app-dir `"$ExpectedRoot`"", [StringComparison]::OrdinalIgnoreCase) -ge 0)
}

# Only the native claude.exe counts: never a .cmd/.bat/.ps1 shim, another
# extension, or a differently named binary. A PATH shim is resolved to the
# native executable it launches, as the npm install lays it out.
function Test-NativeClaudeExecutable([string]$Path) {
    return ($Path -and
        ([System.IO.Path]::GetFileName($Path) -ieq "claude.exe") -and
        (Test-Path -LiteralPath $Path -PathType Leaf))
}

function Find-ClaudeCli {
    $candidates = @()
    if ($env:NEXUS_CLAUDE_PATH) { $candidates += $env:NEXUS_CLAUDE_PATH }
    $found = Get-Command "claude" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found -and $found.Source) {
        if ([System.IO.Path]::GetFileName($found.Source) -ieq "claude.exe") {
            $candidates += $found.Source
        } else {
            $candidates += (Join-Path (Split-Path -Parent $found.Source) "node_modules\@anthropic-ai\claude-code\bin\claude.exe")
        }
    }
    $profileRoot = if ($env:USERPROFILE) { $env:USERPROFILE } else { $env:HOME }
    if ($profileRoot) { $candidates += (Join-Path $profileRoot ".local\bin\claude.exe") }
    foreach ($candidate in $candidates) {
        if (Test-NativeClaudeExecutable $candidate) { return $candidate }
    }
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
$portOwner = Get-PortOwner $NexusPort
if ($portOwner -and -not (Test-ExpectedNexusProcess $portOwner $root)) {
    Stop-Launch "Port $NexusPort belongs to an unverified Nexus checkout or another process (PID $portOwner)." @(
        "Requested checkout: $root",
        "Use a free port or stop that process yourself; this launcher will not reuse it."
    )
}
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

# ---- Native backends (informational unless OmniRoute is enabled) ------------
# Use the same discovery Nexus uses at runtime (nexus.dispatchers.manager._find_codex),
# so the pre-check never passes on a PATH-only Codex that Nexus cannot launch.
Push-Location $root
try {
    # The probe reports failure only through its exit code: under
    # ErrorActionPreference=Stop, native stderr would abort the launcher silently.
    $codexPath = & $python -c "import sys`ntry:`n    from nexus.dispatchers.manager import _find_codex`n    print(_find_codex())`nexcept Exception:`n    sys.exit(1)"
    $codexExit = $LASTEXITCODE
} finally {
    Pop-Location
}
$codexAvailable = ($codexExit -eq 0 -and [bool]$codexPath)
if ($codexAvailable) {
    Write-Ok "Codex CLI available"
    Write-Info "$codexPath"
} elseif ($EnableOmniRoute) {
    # The OmniRoute backend runs through Codex App-Server workers.
    Stop-Launch "Codex CLI was not found where Nexus launches it." @(
        "Expected: %LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe",
        "Install Codex before using OmniRoute-backed Development missions."
    )
} else {
    Write-Warn "Codex CLI was not found where Nexus launches it; Codex Native is unavailable."
    Write-Info "Expected: %LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe"
}

$claudePath = Find-ClaudeCli
$claudeAvailable = [bool]$claudePath
if ($claudeAvailable) {
    Write-Ok "Claude Code CLI available"
    Write-Info "$claudePath"
} else {
    Write-Warn "Claude Code CLI (claude.exe) was not found; Claude Native is unavailable."
    Write-Info "Install Claude Code or set NEXUS_CLAUDE_PATH to claude.exe."
}
if (-not $codexAvailable -and -not $claudeAvailable) {
    Write-Warn "No native backend is available. Nexus will still start; install Codex or Claude Code, or use -EnableOmniRoute."
}
Write-Info "Sign-in is not checked here; Nexus verifies each login when you pick a backend."

# ---- OmniRoute profile (only when OmniRoute is explicitly enabled) -----------
if ($EnableOmniRoute) {
    $omniProfileRoot = if ($env:NEXUS_OMNIROUTE_CODEX_HOME) {
        $env:NEXUS_OMNIROUTE_CODEX_HOME
    } else {
        Join-Path $env:USERPROFILE ".codex-omniroute-worker"
    }
    $omniProfileConfig = Join-Path $omniProfileRoot "config.toml"
    if (-not (Test-Path -LiteralPath $omniProfileConfig)) {
        Stop-Launch "The dedicated Codex OmniRoute profile is missing." @(
            $omniProfileConfig,
            "Create this local profile before using -EnableOmniRoute; credentials are never stored in the repository."
        )
    }
    Write-Ok "Codex OmniRoute profile available"
}

if ($EnableDevelopment) {
    $env:NEXUS_DEV_EXECUTION_ENABLED = "1"
    Write-Ok "Development execution explicitly enabled for this Nexus process"
} elseif ($env:NEXUS_DEV_EXECUTION_ENABLED -eq "1") {
    Write-Ok "Development execution enabled by the existing process environment"
} else {
    Write-Info "Development execution remains disabled. Use -EnableDevelopment to opt in for this process."
}

# ---- Step 2: OmniRoute (opt-in) -----------------------------------------------
$omniState = "DISABLED (native-first; use -EnableOmniRoute)"
if ($EnableOmniRoute) {
    if (Test-OmniRouteOnline) {
        Write-Ok "OmniRoute online"
        $omniState = "ONLINE"
    } else {
        $omniCommand = Get-Command $OmniRouteCommand -ErrorAction SilentlyContinue
        if (-not $omniCommand) {
            Stop-Launch "OmniRoute is not running and the '$OmniRouteCommand' command is not available." @(
                "Install or fix the OmniRoute command, or start Nexus without -EnableOmniRoute."
            )
        }
        if ($PreflightOnly) {
            Write-Info "OmniRoute is offline; a normal start would launch it in the background"
            $omniState = "OFFLINE (would be started)"
        } else {
            Write-Info "OmniRoute not responding on port $OmniRoutePort; starting it in the background"
            & $omniCommand.Source serve --daemon --no-open | Out-Null
            if (-not (Wait-Until { Test-OmniRouteOnline } $TimeoutSeconds)) {
                Stop-Launch "OmniRoute did not become ready on port $OmniRoutePort within $TimeoutSeconds seconds." @(
                    "Nexus was not started."
                )
            }
            Write-Ok "OmniRoute online"
            $omniState = "ONLINE"
        }
    }
} else {
    Write-Info "OmniRoute is not enabled; it is neither checked nor started."
}

# ---- Step 3: Control Center --------------------------------------------------
$controlCenterState = "ONLINE"
if ($PreflightOnly) {
    $controlCenterState = if (Test-NexusOnline) { "ONLINE" } else { "NOT STARTED (-PreflightOnly)" }
} elseif (Test-NexusOnline) {
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
        -ArgumentList @("-m", "uvicorn", "nexus.web.app:app", "--app-dir", "`"$root`"", "--host", "127.0.0.1", "--port", "$NexusPort") `
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
Write-Host " OmniRoute      $omniState"
Write-Host " Codex Native   $(if ($codexAvailable) { 'AVAILABLE' } else { 'NOT FOUND' })"
Write-Host " Claude Native  $(if ($claudeAvailable) { 'AVAILABLE' } else { 'NOT FOUND' })"
Write-Host " Development    $(if ($env:NEXUS_DEV_EXECUTION_ENABLED -eq '1') { 'EXECUTION ENABLED' } else { 'execution disabled' })"
Write-Host " Control Center $controlCenterState"
Write-Host ""
Write-Host " $url"
Write-Host "========================================"

# ---- Step 5: browser ---------------------------------------------------------
if (-not $NoBrowser -and -not $PreflightOnly) { Start-Process $url | Out-Null }
