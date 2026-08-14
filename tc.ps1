#!/usr/bin/env pwsh
# TradingCat Windows PowerShell entry point; mirrors the repository's ./tc shell wrapper.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    $candidates = @(
        [pscustomobject]@{ Label = ".venv\Scripts\python.exe"; Executable = $venvPython; Prefix = @(); RequiredPath = $true },
        [pscustomobject]@{ Label = "python"; Executable = "python"; Prefix = @(); RequiredPath = $false },
        [pscustomobject]@{ Label = "py -3"; Executable = "py"; Prefix = @("-3"); RequiredPath = $false },
        [pscustomobject]@{ Label = "python3"; Executable = "python3"; Prefix = @(); RequiredPath = $false }
    )
    $diagnostics = [System.Collections.Generic.List[string]]::new()

    foreach ($candidate in $candidates) {
        if ($candidate.RequiredPath) {
            if (-not (Test-Path -LiteralPath $candidate.Executable -PathType Leaf)) {
                $null = $diagnostics.Add("$($candidate.Label): not found")
                continue
            }
        } elseif (-not (Get-Command $candidate.Executable -CommandType Application -ErrorAction SilentlyContinue)) {
            $null = $diagnostics.Add("$($candidate.Label): not found")
            continue
        }

        try {
            $probeArgs = @($candidate.Prefix) + @("--version")
            $versionOutput = (& $candidate.Executable @probeArgs 2>&1 | Out-String).Trim()
            $exitCode = $LASTEXITCODE
            if ($exitCode -eq 0 -and $versionOutput -match "Python\s+(?<major>\d+)\.(?<minor>\d+)") {
                $major = [int]$Matches.major
                $minor = [int]$Matches.minor
                if (($major -gt 3) -or ($major -eq 3 -and $minor -ge 10)) {
                    return $candidate
                }
                $null = $diagnostics.Add("$($candidate.Label): Python $major.$minor is older than 3.10")
            } else {
                $detail = if ([string]::IsNullOrWhiteSpace($versionOutput)) { "probe failed" } else { $versionOutput }
                $null = $diagnostics.Add("$($candidate.Label): $detail")
            }
        } catch {
            $null = $diagnostics.Add("$($candidate.Label): $($_.Exception.Message)")
        }
    }

    $checked = ($candidates | ForEach-Object { $_.Label }) -join ", "
    $details = $diagnostics -join "; "
    throw "No usable Python 3.10+ interpreter found. Install Python 3.10+ or create .venv\Scripts\python.exe, then retry. Checked: $checked. Details: $details"
}

$Python = Find-Python
$PreviousPythonPath = $env:PYTHONPATH
try {
    if ([string]::IsNullOrWhiteSpace($PreviousPythonPath)) {
        $env:PYTHONPATH = $Root
    } else {
        $env:PYTHONPATH = "$Root;${PreviousPythonPath}"
    }
    $PythonArgs = @($Python.Prefix) + @((Join-Path $Root "tc.py")) + @($args)
    & $Python.Executable @PythonArgs
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $PreviousPythonPath
}
