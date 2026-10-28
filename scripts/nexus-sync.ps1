[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $Message,

    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string[]] $Files
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]] $Arguments)
    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Get-GitOutput {
    param([Parameter(Mandatory = $true)][string[]] $Arguments)
    $output = & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
    return ($output -join "`n").Trim()
}

$root = Get-GitOutput @("rev-parse", "--show-toplevel")
Set-Location -LiteralPath $root
$branch = Get-GitOutput @("branch", "--show-current")
if ([string]::IsNullOrWhiteSpace($branch) -or $branch -in @("main", "master")) {
    throw "Refusing to publish from '$branch'. Use a dedicated work branch."
}

$remote = Get-GitOutput @("remote", "get-url", "origin")
if ([string]::IsNullOrWhiteSpace($remote)) {
    throw "Remote origin is not configured."
}

$normalized = @($Files | ForEach-Object { $_ -split ',' } | ForEach-Object {
    $value = $_.Trim().Replace('\', '/')
    if ([string]::IsNullOrWhiteSpace($value) -or $value.StartsWith('/') -or $value.Contains('..')) {
        throw "Invalid repository-relative path: '$($_)'."
    }
    $value
})

$sensitivePath = '(?i)(^|/)(\.env(?:\..*)?|\.codex(?:/|$)|\.aws(?:/|$)|\.ssh(?:/|$)|auth\.json|credentials?|secrets?|.*private.*key.*)(/|$)|(?i)(token|password|secret|api[_-]?key)'
foreach ($path in $normalized) {
    if ($path -match $sensitivePath) {
        throw "Refusing sensitive path '$path'."
    }
}

$status = @(git status --porcelain=v1 -- $normalized)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect selected files."
}
if ($status.Count -eq 0) {
    Write-Output "No changes in selected files; nothing to commit or push."
    exit 0
}

Invoke-Git (@("add", "--") + $normalized)
$cachedNames = @(git diff --cached --name-only -- $normalized)
if ($LASTEXITCODE -ne 0 -or $cachedNames.Count -eq 0) {
    throw "No selected changes were staged."
}
foreach ($path in $cachedNames) {
    if ($path -match $sensitivePath) {
        Invoke-Git (@("reset", "--") + $normalized)
        throw "Refusing staged sensitive path '$path'."
    }
}

$staged = Get-GitOutput @("diff", "--cached", "--no-ext-diff", "--unified=0")
$secretPattern = '(?i)(-----BEGIN [A-Z ]+ PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b|\bBearer\s+[A-Za-z0-9._~+/-]{16,})'
if ($staged -match $secretPattern) {
    Invoke-Git (@("reset", "--") + $normalized)
    throw "Refusing staged content that resembles a credential."
}

Invoke-Git @("commit", "-m", $Message)
$localSha = Get-GitOutput @("rev-parse", "HEAD")
Invoke-Git @("push", "-u", "origin", "HEAD")

$remoteRef = "refs/heads/$branch"
$remoteLine = Get-GitOutput @("ls-remote", "origin", $remoteRef)
$remoteSha = ($remoteLine -split '\s+')[0]
if ($remoteSha -ne $localSha) {
    throw "Remote SHA '$remoteSha' does not match local SHA '$localSha'."
}

Write-Output "PUSH_OK branch=$branch sha=$localSha remote=$remote"
