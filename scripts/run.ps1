#Requires -Version 5.1
<#
.SYNOPSIS
    Launcher (PowerShell) — delegates to run.py
.DESCRIPTION
    Resolves Python from .venv or PATH, validates version >= 3.9,
    elevates to Admin via UAC if needed, then launches run.py.
.EXAMPLE
    .\run.ps1
    .\run.ps1 --interval 1 --no-dashboard --verbose
#>

param(
    [switch]$NoElevate
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# ── Resolve Python ──
$venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    $py = $venvPy
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $py = 'python3'
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $py = 'python'
} else {
    Write-Error '[ERROR] No python found. Install Python >= 3.9 or create a venv: python -m venv .venv'
    exit 1
}

# ── Python version gate ──
& $py -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
    $ver = & $py --version 2>&1
    Write-Error "[ERROR] Python >= 3.9 required. Found: $ver"
    exit 1
}

# ── UAC elevation if not admin ──
if (-not $NoElevate) {
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $isAdmin) {
        Write-Host '[*] Elevating to Administrator via UAC...'
        $scriptArgs = ($args | ForEach-Object { "`"$_`"" }) -join ' '
        $psArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" $scriptArgs"
        Start-Process powershell -Verb RunAs -ArgumentList $psArgs
        exit 0
    }
}

# ── Launch ──
$runArgs = $args | Where-Object { $_ -ne '-NoElevate' -and $_ -ne '--no-elevate' }
& $py run.py @runArgs
exit $LASTEXITCODE
