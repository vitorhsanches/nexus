# Resolves the installed Codex CLI without depending on PATH.
#
# Same rule as nexus/dispatchers/manager.py::_find_codex and
# nexus/dispatchers/review.py::_find_codex: the newest
# %LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe by file modification time.
# Ties are broken by full path (descending) so the choice is deterministic.

function Resolve-CodexExecutable {
    [CmdletBinding()]
    param(
        [string]$LocalAppData = $env:LOCALAPPDATA
    )

    if ([string]::IsNullOrWhiteSpace($LocalAppData)) {
        throw "CODEX_EXECUTABLE_NOT_FOUND: LOCALAPPDATA is not set; cannot locate the Codex installation."
    }

    $binRoot = Join-Path (Join-Path (Join-Path $LocalAppData "OpenAI") "Codex") "bin"
    $candidates = @(
        Get-ChildItem -LiteralPath $binRoot -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "codex.exe" } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            ForEach-Object { Get-Item -LiteralPath $_ }
    )

    if ($candidates.Count -eq 0) {
        throw "CODEX_EXECUTABLE_NOT_FOUND: no codex.exe found at $(Join-Path $binRoot '*\codex.exe'). Searched only the OpenAI Codex installation directory; Codex is not required on PATH."
    }

    $selected = $candidates |
        Sort-Object -Property LastWriteTimeUtc, FullName -Descending |
        Select-Object -First 1

    return $selected.FullName
}
