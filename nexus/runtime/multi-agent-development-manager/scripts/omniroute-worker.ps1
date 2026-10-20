[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$Repo,
    [Parameter(Mandatory = $true)] [string]$Model,
    [Parameter(Mandatory = $true)]
    [ValidateSet("low", "medium", "high", "xhigh", "max", "ultra")]
    [string]$Effort,
    [Parameter(Mandatory = $true)] [string]$Task,
    [Parameter(Mandatory = $true)] [string]$ResultFile,
    [Parameter(Mandatory = $true)]
    [ValidateSet("workspace-write", "read-only")]
    [string]$Sandbox
)

$ErrorActionPreference = "Stop"

if (-not [System.IO.Path]::IsPathRooted($Repo)) {
    throw "Repo must be an absolute path."
}
if (-not (Test-Path -LiteralPath $Repo -PathType Container)) {
    throw "Repo does not exist or is not a directory."
}
$repoItem = Get-Item -LiteralPath $Repo -Force
$pathItem = $repoItem
while ($null -ne $pathItem) {
    if (($pathItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Repo path must not cross a reparse point."
    }
    $pathItem = $pathItem.Parent
}
if ($Sandbox -eq "workspace-write") {
    $gitLink = Join-Path $repoItem.FullName ".git"
    if (-not (Test-Path -LiteralPath $gitLink -PathType Leaf)) {
        throw "A workspace-write Worker requires an assigned linked worktree."
    }
    $gitDirectory = & git -C $repoItem.FullName rev-parse --path-format=absolute --git-dir
    $gitProbeExitCode = $LASTEXITCODE
    if ($gitProbeExitCode -ne 0 -or $gitDirectory -notmatch '[\\/]worktrees[\\/]') {
        throw "Repo is not an authorized linked Git worktree."
    }
}
if (-not [System.IO.Path]::IsPathRooted($ResultFile)) {
    throw "ResultFile must be an absolute path."
}
if ([string]::IsNullOrWhiteSpace($env:CODEX_HOME)) {
    throw "Nexus Worker requires an isolated CODEX_HOME."
}

$configPath = Join-Path $env:CODEX_HOME "config.toml"
$rulesPath = Join-Path $env:CODEX_HOME "rules\nexus-worker.rules"
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $rulesPath -PathType Leaf)) {
    throw "Nexus Worker safety configuration is missing."
}

$configText = Get-Content -LiteralPath $configPath -Raw
if ($configText -notmatch '(?m)^sandbox_mode\s*=\s*"workspace-write"\s*$' -or
    $configText -match 'danger-full-access') {
    throw "Nexus Worker safety configuration is not enforceable."
}

# NOTE: this installed Codex CLI has removed elevated/experimental Windows
# sandboxing (see `codex features list`: elevated_windows_sandbox, stage
# "removed"). workspace-write + approval_policy="never" plus the repository
# filesystem safety guard (nexus/worktree_safety.py) are the real boundary;
# a Windows-elevation config override was tried before and has no effect.
. (Join-Path $PSScriptRoot "codex-resolution.ps1")
$codexExecutable = Resolve-CodexExecutable
$codexArguments = @(
    "exec",
    "--model", $Model,
    "--sandbox", $Sandbox,
    "--config", ('model_reasoning_effort="{0}"' -f $Effort),
    "--config", 'approval_policy="never"',
    "--output-last-message", $ResultFile,
    "--cd", $Repo,
    "-"
)

$Task | & $codexExecutable @codexArguments
$workerExitCode = $LASTEXITCODE
exit $workerExitCode
