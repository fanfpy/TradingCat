#!/usr/bin/env pwsh
# TradingCat Windows PowerShell entry point; mirrors the repository's ./tc shell wrapper.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = "python"
}
$PreviousPythonPath = $env:PYTHONPATH
try {
    if ([string]::IsNullOrWhiteSpace($PreviousPythonPath)) {
        $env:PYTHONPATH = $Root
    } else {
        $env:PYTHONPATH = "$Root;${PreviousPythonPath}"
    }
    & $Python (Join-Path $Root "tc.py") @args
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $PreviousPythonPath
}
