#Requires -Version 5.1
<#
.SYNOPSIS
    Bootstrap the networkInterface capture agent on Windows.
.DESCRIPTION
    1. Vérifie Python 3.11
    2. Détecte Npcap — si absent, le télécharge et lance l'installeur
    3. Crée le venv + installe les dépendances
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PROJECT_ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$VENV_DIR     = Join-Path $PROJECT_ROOT ".venv"
$PYTHON       = "python3.11"
$NPCAP_VERSION = "1.80"
$NPCAP_URL    = "https://npcap.com/dist/npcap-$NPCAP_VERSION.exe"
$NPCAP_INSTALLER = Join-Path $env:TEMP "npcap-$NPCAP_VERSION.exe"

# ═══════════════════════════════════════════════════════════════════════
# 1. Python 3.11
# ═══════════════════════════════════════════════════════════════════════
try {
    $ver = & $PYTHON --version 2>&1
    Write-Host ('[OK] ' + $ver) -ForegroundColor Green
} catch {
    try {
        $PYTHON = "python"
        $ver = & $PYTHON --version 2>&1
        if ($ver -notmatch "3\.11") { throw "wrong version" }
        Write-Host ('[OK] ' + $ver) -ForegroundColor Green
    } catch {
        Write-Host '[FATAL] Python 3.11 is required but not found in PATH.' -ForegroundColor Red
        exit 1
    }
}

# ═══════════════════════════════════════════════════════════════════════
# 2. Npcap
# ═══════════════════════════════════════════════════════════════════════
function Test-NpcapInstalled {
    $searchPaths = @(
        "$env:SystemRoot\System32\Npcap\wpcap.dll",
        "$env:SystemRoot\System32\wpcap.dll",
        "$env:SystemRoot\SysWOW64\Npcap\wpcap.dll",
        "$env:ProgramFiles\Npcap\wpcap.dll"
    )
    foreach ($p in $searchPaths) {
        if (Test-Path $p) {
            Write-Host ('[OK] Npcap found: ' + $p) -ForegroundColor Green
            return $true
        }
    }
    $svc = Get-Service -Name "npcap" -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host ('[OK] Npcap service detected (status: ' + $svc.Status + ')') -ForegroundColor Green
        return $true
    }
    return $false
}

if (-not (Test-NpcapInstalled)) {
    Write-Host ""
    Write-Host "╔══════════════════════════════════════════════════════════╗" -ForegroundColor Yellow
    Write-Host "║  Npcap is required for packet capture on Windows.       ║" -ForegroundColor Yellow
    Write-Host "║  It was not detected on this system.                    ║" -ForegroundColor Yellow
    Write-Host "╚══════════════════════════════════════════════════════════╝" -ForegroundColor Yellow
    Write-Host ""

    $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )

    if (-not $isAdmin) {
        Write-Host '[FATAL] Npcap installation requires Administrator privileges.' -ForegroundColor Red
        Write-Host "        Re-run this script as Administrator:" -ForegroundColor Red
        Write-Host "        Start-Process powershell -Verb RunAs -ArgumentList '-File', '$($MyInvocation.MyCommand.Path)'" -ForegroundColor Cyan
        exit 1
    }

    $choice = Read-Host "Download and install Npcap $NPCAP_VERSION now? (Y/n)"
    if ($choice -eq "" -or $choice -match "^[Yy]") {

        Write-Host ('[*] Downloading Npcap ' + $NPCAP_VERSION + '...') -ForegroundColor Cyan
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $NPCAP_URL -OutFile $NPCAP_INSTALLER -UseBasicParsing
        } catch {
            Write-Host ('[FATAL] Download failed: ' + $_) -ForegroundColor Red
            Write-Host "        Download manually from https://npcap.com/#download" -ForegroundColor Yellow
            exit 1
        }

        Write-Host '[*] Launching Npcap installer...' -ForegroundColor Cyan
        Write-Host "    IMPORTANT: Check 'Install Npcap in WinPcap API-compatible mode'" -ForegroundColor Yellow
        Write-Host ""

        $proc = Start-Process -FilePath $NPCAP_INSTALLER -Wait -PassThru
        if ($proc.ExitCode -ne 0) {
            Write-Host ('[WARN] Installer exited with code ' + $proc.ExitCode) -ForegroundColor Yellow
        }

        Remove-Item $NPCAP_INSTALLER -ErrorAction SilentlyContinue

        if (-not (Test-NpcapInstalled)) {
            Write-Host '[FATAL] Npcap still not detected after installation.' -ForegroundColor Red
            Write-Host "        Try rebooting, then re-run this script." -ForegroundColor Yellow
            exit 1
        }
    } else {
        Write-Host '[SKIP] Npcap not installed. capture_agent.py will fail without it.' -ForegroundColor Yellow
    }
}

# ═══════════════════════════════════════════════════════════════════════
# 3. Virtual environment
# ═══════════════════════════════════════════════════════════════════════
if (-not (Test-Path $VENV_DIR)) {
    Write-Host '[*] Creating virtual environment...' -ForegroundColor Cyan
    & $PYTHON -m venv --upgrade-deps --prompt="networkInterface" $VENV_DIR
} else {
    Write-Host ('[OK] Venv already exists at ' + $VENV_DIR) -ForegroundColor Green
}

# ═══════════════════════════════════════════════════════════════════════
# 4. Dependencies
# ═══════════════════════════════════════════════════════════════════════
$activateScript = Join-Path $VENV_DIR "Scripts\Activate.ps1"
. $activateScript

Write-Host '[*] Installing dependencies...' -ForegroundColor Cyan
pip install --quiet --upgrade pip
pip install --quiet -r (Join-Path $PROJECT_ROOT "requirements.txt")

# ═══════════════════════════════════════════════════════════════════════
# Done
# ═══════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "  Activate:  .\.venv\Scripts\Activate.ps1" -ForegroundColor Cyan
Write-Host "  Capture:   python capture_agent.py         (run as Administrator)" -ForegroundColor Cyan
Write-Host "  Pipeline:  python databricks_pipeline.py --local" -ForegroundColor Cyan
