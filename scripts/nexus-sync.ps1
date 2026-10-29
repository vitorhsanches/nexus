[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string[]]$Files,

    [Parameter(Mandatory = $true)]
    [string]$Message,

    [string]$Base,
    [string]$Branch,
    [string]$Title,
    [string]$Body,
    [string]$RepoRoot,
    [string]$Remote = "origin",
    [switch]$Draft,
    [switch]$SkipCi
)

# Delivers exactly one authorized change set to GitHub: stage only the
# given files, commit, push to a working branch, verify the push actually
# landed on the remote, then create or update a pull request against an
# explicitly supplied base (never main by default) and report CI status.
# Never merges, never applies, never force-pushes, never touches main.

$ErrorActionPreference = "Stop"

function Write-Ok([string]$Message) { Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Info([string]$Message) { Write-Host "     $Message" -ForegroundColor DarkGray }

function Stop-Sync([string]$Reason, [string[]]$Details) {
    Write-Host ""
    Write-Host "NEXUS SYNC BLOCKED" -ForegroundColor Red
    Write-Host $Reason
    foreach ($line in $Details) { Write-Host "  $line" }
    exit 1
}

$ProtectedBranches = @("main", "master")

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    # PowerShell 5.1 wraps a native command's stderr lines in terminating
    # ErrorRecords when redirected with 2>&1 under $ErrorActionPreference
    # = Stop (true even for a harmless warning and a zero exit code), so
    # stderr is captured with a local, non-terminating preference instead.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & git @GitArgs 2>&1
    } finally {
        $ErrorActionPreference = $previous
    }
    $code = $LASTEXITCODE
    $text = ($output | ForEach-Object { $_.ToString() }) -join "`n"
    [pscustomobject]@{ Output = $text; ExitCode = $code }
}

function Invoke-GitOrFail {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    $result = Invoke-Git @GitArgs
    if ($result.ExitCode -ne 0) {
        Stop-Sync "git $($GitArgs -join ' ') failed" @($result.Output)
    }
    return $result.Output
}

function Invoke-Gh {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GhArgs)
    # Same PowerShell 5.1 stderr-redirection pitfall as Invoke-Git above.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & gh @GhArgs 2>&1
    } finally {
        $ErrorActionPreference = $previous
    }
    $code = $LASTEXITCODE
    $text = ($output | ForEach-Object { $_.ToString() }) -join "`n"
    [pscustomobject]@{ Output = $text; ExitCode = $code }
}

if (-not $RepoRoot) { $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
    Stop-Sync "Not a Git repository root" @("RepoRoot: $RepoRoot")
}
Push-Location $RepoRoot
try {
    $ghCommand = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $ghCommand) {
        Stop-Sync "GitHub CLI (gh) is not available on PATH" @("Install gh and sign in, then re-run.")
    }
    $authStatus = Invoke-Gh auth status
    if ($authStatus.ExitCode -ne 0) {
        Stop-Sync "GitHub CLI is not authenticated" @($authStatus.Output)
    }

    if (-not $Branch) { $Branch = (Invoke-GitOrFail status --branch --porcelain=v1 | Select-Object -First 1) -replace '^## ', '' -replace '\.\.\..*$', '' }
    $Branch = $Branch.Trim()
    if (-not $Branch) {
        Stop-Sync "Could not resolve the current working branch" @("Pass -Branch explicitly.")
    }
    if ($ProtectedBranches -contains $Branch.ToLowerInvariant()) {
        Stop-Sync "Refusing to commit/push directly to a protected branch" @("Branch: $Branch")
    }
    if (-not $Base) {
        Stop-Sync "A base branch is required" @("Pass -Base explicitly; this script never assumes main.")
    }
    if ($Base.Trim().ToLowerInvariant() -eq $Branch.ToLowerInvariant()) {
        Stop-Sync "Base and working branch must differ" @("Base: $Base", "Branch: $Branch")
    }

    $currentBranch = (Invoke-GitOrFail rev-parse --abbrev-ref HEAD).Trim()
    if ($currentBranch -ne $Branch) {
        Stop-Sync "Checked-out branch does not match -Branch" @("Checked out: $currentBranch", "Requested: $Branch", "Switch branches yourself before running this script.")
    }

    Write-Info "Repository root: $RepoRoot"
    Write-Info "Working branch:  $Branch"
    Write-Info "PR base:         $Base"

    # Stage only the authorized files -- never `git add -A` / `git add .`.
    $missing = $Files | Where-Object { -not (Test-Path (Join-Path $RepoRoot $_)) }
    if ($missing) {
        Stop-Sync "One or more authorized files do not exist on disk" $missing
    }
    Invoke-GitOrFail add -- @Files | Out-Null
    $staged = (Invoke-GitOrFail diff --cached --name-only) -split "`n" | Where-Object { $_ }
    $unexpected = $staged | Where-Object { $Files -notcontains $_ }
    if ($unexpected) {
        Invoke-GitOrFail reset -- @Files | Out-Null
        Stop-Sync "Staging produced files outside the authorized list" $unexpected
    }
    if (-not $staged) {
        Stop-Sync "None of the authorized files have changes to commit" $Files
    }
    Write-Ok "Staged $($staged.Count) authorized file(s)"

    Invoke-GitOrFail commit -m $Message | Out-Null
    $commitSha = (Invoke-GitOrFail rev-parse HEAD).Trim()
    Write-Ok "Committed $commitSha"

    # Never --force: a rejected push means the remote moved and must be
    # reconciled by a human/another run, not overwritten.
    Invoke-GitOrFail push -u $Remote $Branch | Out-Null
    $localHead = (Invoke-GitOrFail rev-parse HEAD).Trim()
    $trackingRef = (Invoke-GitOrFail rev-parse "@{u}").Trim()
    $remoteLine = (Invoke-GitOrFail ls-remote $Remote $Branch).Trim()
    $remoteSha = ($remoteLine -split "\s+")[0]
    if (-not $remoteSha -or $localHead -ne $trackingRef -or $localHead -ne $remoteSha) {
        Stop-Sync "Remote SHA verification failed after push" @(
            "Local HEAD:    $localHead"
            "Tracking ref:  $trackingRef"
            "Remote (ls-remote): $remoteSha"
        )
    }
    Write-Ok "Remote SHA verified: $remoteSha"

    if (-not $Title) { $Title = $Message.Split("`n")[0] }
    if (-not $Body) { $Body = $Message }

    $existing = Invoke-Gh pr list --head $Branch --base $Base --state open --json "number,url"
    if ($existing.ExitCode -ne 0) {
        Stop-Sync "gh pr list failed" @($existing.Output)
    }
    $existingPrs = @()
    try { $existingPrs = $existing.Output | ConvertFrom-Json } catch { $existingPrs = @() }

    $result = $null
    $prNumber = $null
    $prUrl = $null
    if ($existingPrs -and $existingPrs.Count -gt 0) {
        $prNumber = $existingPrs[0].number
        $editResult = Invoke-Gh pr edit "$prNumber" --title $Title --body $Body
        if ($editResult.ExitCode -ne 0) {
            Stop-Sync "gh pr edit failed" @($editResult.Output)
        }
        $prUrl = $existingPrs[0].url
        $result = "updated"
        Write-Ok "Updated existing PR #$prNumber"
    } else {
        $createArgs = @("pr", "create", "--base", $Base, "--head", $Branch, "--title", $Title, "--body", $Body)
        if ($Draft) { $createArgs += "--draft" }
        $createResult = Invoke-Gh @createArgs
        if ($createResult.ExitCode -ne 0) {
            Stop-Sync "gh pr create failed" @($createResult.Output)
        }
        $prUrl = ($createResult.Output -split "`n" | Select-String -Pattern '^https://\S+$' | Select-Object -Last 1)
        $prUrl = if ($prUrl) { $prUrl.ToString().Trim() } else { $createResult.Output.Trim() }
        $viewResult = Invoke-Gh pr view $Branch --json number
        if ($viewResult.ExitCode -eq 0) {
            try { $prNumber = ($viewResult.Output | ConvertFrom-Json).number } catch { $prNumber = $null }
        }
        $result = "created"
        Write-Ok "Created PR #$prNumber"
    }

    $ciStatus = "skipped"
    if (-not $SkipCi -and $prNumber) {
        $checksResult = Invoke-Gh pr view $prNumber --json statusCheckRollup
        if ($checksResult.ExitCode -eq 0) {
            try {
                $checks = ($checksResult.Output | ConvertFrom-Json).statusCheckRollup
                if (-not $checks -or $checks.Count -eq 0) { $ciStatus = "no checks configured" }
                elseif ($checks | Where-Object { $_.conclusion -eq "FAILURE" -or $_.state -eq "FAILURE" }) { $ciStatus = "failing" }
                elseif ($checks | Where-Object { $_.status -eq "IN_PROGRESS" -or $_.status -eq "QUEUED" -or $_.state -eq "PENDING" }) { $ciStatus = "pending" }
                else { $ciStatus = "passing" }
            } catch {
                $ciStatus = "unknown (could not parse gh output)"
            }
        } else {
            $ciStatus = "unknown (gh pr view checks failed)"
        }
    }

    [pscustomobject]@{
        Result    = $result
        Url       = $prUrl
        Number    = $prNumber
        Base      = $Base
        Branch    = $Branch
        Commit    = $commitSha
        RemoteSha = $remoteSha
        CiStatus  = $ciStatus
    } | Format-List
}
finally {
    Pop-Location
}
