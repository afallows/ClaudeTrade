#Requires -Version 5.1
<#
.SYNOPSIS
    One-script setup for ClaudeTrade on a fresh Windows machine: checks/installs
    Python, creates the virtual environment, installs ClaudeTrade, creates the
    database, loads the first 90 days of data, and launches the desktop UI.

.DESCRIPTION
    Implements ADR-0008 Decision 4 ("One-script setup"). Automates the manual
    steps documented in docs/windows-install.md, in this order:

      a) Verify Python 3.11+ is available (tries `py -3.12`, `py -3.11`,
         `py -3.13`, then plain `python`, in that order, keeping the first
         one found that satisfies >= 3.11). If none is found, installs the
         official python.org build via `winget install Python.Python.3.12`
         (winget only ever resolves this to Microsoft's or the publisher's
         own package for that id -- never a third-party source). If winget
         itself isn't available, this script does NOT fall back to any other
         installer: it prints instructions and opens
         https://www.python.org/downloads/ for the user to install manually,
         then exits so they can re-run this script afterwards.
      b) Create .venv if it doesn't already exist; upgrade pip; install
         requirements.txt; install ClaudeTrade itself in editable mode.
      c) `claudetrade init` -- create/migrate the local database.
      d) `claudetrade refresh` -- first data load (defaults to the last 90
         calendar days). A provider problem here is reported as a warning,
         not a fatal error -- the script continues to the UI regardless,
         since the app is designed to run in reduced-capability mode.
      e) `claudetrade ui` -- launch the desktop app (the new React/FastAPI
         interface by default; pass -Classic for the legacy Streamlit UI).

    Every step is idempotent: re-running this script on a machine that
    already has some or all of the above in place skips or repeats each step
    safely (venv creation is skipped if .venv already exists; pip installs,
    `init`, and `refresh` are all safe to repeat).

    All console output is also written to a timestamped transcript log file
    next to this script (scripts\setup-log-<timestamp>.txt).

.PARAMETER SkipData
    Skip step (d), the first `claudetrade refresh`. Useful if you already
    have data loaded and just want to relaunch the UI quickly.

.PARAMETER Classic
    Pass `--classic` through to `claudetrade ui`, launching the legacy
    Streamlit interface instead of the new desktop (React/FastAPI) app.

.PARAMETER NoLaunch
    Do everything except step (e): set up the environment, initialize the
    database, and (unless -SkipData) load data, but do not launch the UI.
    Useful for unattended/scripted provisioning.

.EXAMPLE
    scripts\setup.ps1
    Full first-run flow: install prerequisites, load data, launch the UI.

.EXAMPLE
    scripts\setup.ps1 -SkipData -Classic
    Skip the data refresh (e.g. on a re-run) and open the legacy Streamlit UI.

.EXAMPLE
    scripts\setup.ps1 -NoLaunch
    Provision the environment and database only; do not open the UI.

.NOTES
    VALIDATION METHOD -- read this before trusting this script blindly.

    This script was authored and reviewed on a Linux development machine.
    `pwsh`/PowerShell is not installed in that environment (checked with
    `Get-Command`/`which pwsh` -- absent), so it has NOT been executed, on
    Windows or otherwise. It has NOT been run against a real `py.exe`,
    `winget`, or Windows filesystem. Validation was therefore static only:
    careful line-by-line review for PowerShell 5.1 syntax compatibility
    (no PS7-only operators such as `??` or `?:` are used, since the
    companion setup.bat launches plain `powershell.exe`, i.e. Windows
    PowerShell 5.1, which ships on every Windows 10/11 machine with no
    extra install), matching the exact CLI surface read from
    src/claudetrade/cli.py (`init`, `refresh`, `ui --classic`), and manual
    trace-through of every branch below against the test matrix.

    TEST MATRIX (each row: precondition -> expected behaviour). Anyone with
    access to a real Windows machine should exercise these before relying on
    this script for onboarding at scale; they are listed here specifically
    because they could not be exercised in this authoring environment:

      1. Fresh Windows 11 VM, nothing installed, winget present (default on
         current Windows 11) -> installs Python via winget, creates .venv,
         installs deps, inits db, refreshes data, opens the desktop UI.
      2. Same, but winget absent (older Windows 10 image) -> prints manual
         install instructions, opens the python.org downloads page, exits 1
         without touching the filesystem further.
      3. Python 3.11 or 3.12 already on PATH via `py` launcher -> skips the
         install branch entirely, proceeds straight to venv creation.
      4. Only a bare `python` (no `py` launcher) >= 3.11 on PATH -> detected
         by the `python` fallback candidate, used for venv creation.
      5. Python present but < 3.11 (e.g. 3.9) -> treated as "not found";
         falls through to the winget/manual-install branch rather than
         silently using the too-old interpreter.
      6. Re-run after a fully successful prior run -> every step reports
         "already satisfied" / re-installs cleanly; no duplicate venv
         creation; exits 0.
      7. `.venv` exists but is corrupt (missing Scripts\python.exe) ->
         re-creates it via `py -m venv` (venv module repairs/recreates
         in-place) rather than silently reusing a broken environment.
      8. `pip install -r requirements.txt` fails (e.g. no network for a
         first-time install with no cached wheels) -> exits non-zero (3)
         with a pointed message; does not proceed to init/refresh/ui.
      9. `claudetrade refresh` fails or reports degraded sources (e.g. a
         corporate proxy blocking an optional live host) -> prints a
         warning and the `claudetrade probe` hint, then still proceeds to
         launch the UI (non-fatal, per the app's own graceful-degradation
         design already exercised by `refresh`/`scan`).
      10. `-SkipData` -> step (d) is skipped entirely, no network calls,
          straight to UI launch.
      11. `-NoLaunch` -> steps (a)-(d) run, step (e) is skipped, script
          exits 0 with instructions for launching the UI manually later.
      12. `-Classic` -> `claudetrade ui --classic` is invoked instead of the
          new desktop app's default invocation.
      13. Run from a directory other than the repo root (e.g. double-click
          from Explorer, or invoked via a shortcut) -> resolves paths from
          $PSScriptRoot, not the current directory, so this should not
          matter; not independently verified on Windows.
      14. Non-admin user account -> `py -m venv`, pip installs, and winget's
          per-user install path should all work without elevation; a
          machine-wide winget install requiring UAC elevation has not been
          exercised here and may prompt the user -- this is expected winget
          behaviour, not a bug in this script.

    Do not claim this script was executed or verified end-to-end on Windows;
    it was not. Report it as statically reviewed only, until someone runs
    the above matrix on real hardware.
#>
[CmdletBinding()]
param(
    [switch]$SkipData,
    [switch]$Classic,
    [switch]$NoLaunch
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

$ScriptDir = $PSScriptRoot
if (-not $ScriptDir) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$ProjectRoot = Split-Path -Parent $ScriptDir
$VenvPath = Join-Path $ProjectRoot '.venv'
$RequirementsPath = Join-Path $ProjectRoot 'requirements.txt'
$LogPath = Join-Path $ScriptDir ("setup-log-{0}.txt" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

# ---------------------------------------------------------------------------
# Console + transcript helpers
# ---------------------------------------------------------------------------

$Script:StepNumber = 0
$Script:TotalSteps = 5

function Write-Step {
    param([string]$Message)
    $Script:StepNumber++
    Write-Host ""
    Write-Host "[$($Script:StepNumber)/$($Script:TotalSteps)] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    OK      $Message" -ForegroundColor Green
}

function Write-Info {
    param([string]$Message)
    Write-Host "    ..      $Message" -ForegroundColor Gray
}

function Write-Warn {
    param([string]$Message)
    Write-Host "    WARNING $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host "    ERROR   $Message" -ForegroundColor Red
}

function Exit-Setup {
    param([int]$Code)
    Write-Host ""
    if ($Code -eq 0) {
        Write-Host "Setup finished (exit code 0). Transcript: $LogPath" -ForegroundColor Cyan
    } else {
        Write-Host "Setup stopped early (exit code $Code). Transcript: $LogPath" -ForegroundColor Red
    }
    try { Stop-Transcript | Out-Null } catch { }
    exit $Code
}

try {
    Start-Transcript -Path $LogPath -NoClobber | Out-Null
} catch {
    Write-Warning "Could not start transcript logging at $LogPath -- continuing without it: $($_.Exception.Message)"
}

Write-Host "ClaudeTrade setup" -ForegroundColor Cyan
Write-Host "=================" -ForegroundColor Cyan
Write-Host "Project root: $ProjectRoot"
Write-Host "Log file:     $LogPath"

if (-not (Test-Path (Join-Path $ProjectRoot 'pyproject.toml'))) {
    Write-Fail "pyproject.toml not found under $ProjectRoot -- this script must live in the ClaudeTrade repo's scripts\ folder. Aborting."
    Exit-Setup 1
}

Set-Location -Path $ProjectRoot

# ---------------------------------------------------------------------------
# Step a: Python 3.11+
# ---------------------------------------------------------------------------

function Test-PythonCandidate {
    param(
        [string]$Exe,
        [string[]]$ExeArgs
    )
    $callArgs = @($ExeArgs) + @('--version')
    try {
        $output = & $Exe @callArgs 2>&1
        $exitCode = $LASTEXITCODE
    } catch {
        return $null
    }
    if ($exitCode -ne 0) { return $null }
    $text = ($output | Out-String).Trim()
    if ($text -match 'Python\s+(\d+)\.(\d+)(\.(\d+))?') {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if (($major -eq 3 -and $minor -ge 11) -or ($major -gt 3)) {
            return [PSCustomObject]@{
                Exe     = $Exe
                Args    = $ExeArgs
                Version = ($text -replace 'Python\s+', '')
            }
        }
    }
    return $null
}

function Find-Python {
    $candidates = @(
        @{ Exe = 'py'; Args = @('-3.12') },
        @{ Exe = 'py'; Args = @('-3.11') },
        @{ Exe = 'py'; Args = @('-3.13') },
        @{ Exe = 'python'; Args = @() }
    )
    foreach ($candidate in $candidates) {
        $found = Test-PythonCandidate -Exe $candidate.Exe -ExeArgs $candidate.Args
        if ($found) { return $found }
    }
    return $null
}

function Install-PythonViaWinget {
    Write-Info "Checking for winget (Windows Package Manager) ..."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Fail "winget is not available on this machine, and no Python 3.11+ was found."
        Write-Host ""
        Write-Host "  Install Python yourself from the OFFICIAL source, then re-run this script:"
        Write-Host "    1. Go to https://www.python.org/downloads/ (opening it now if possible)"
        Write-Host "    2. Download the latest Python 3.11.x or 3.12.x installer for Windows"
        Write-Host "    3. On the installer's FIRST screen, tick 'Add python.exe to PATH'"
        Write-Host "    4. Open a NEW terminal window and re-run scripts\setup.bat"
        Write-Host ""
        try { Start-Process 'https://www.python.org/downloads/' | Out-Null } catch { }
        return $false
    }
    Write-Info "winget found. Installing the official Python.Python.3.12 package ..."
    & winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "winget install of Python failed (exit code $LASTEXITCODE)."
        Write-Host "  Install Python yourself from https://www.python.org/downloads/ and re-run this script."
        return $false
    }
    Write-Ok "winget reported success. Refreshing PATH for this session ..."
    $machinePath = [System.Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machinePath, $userPath) -join ';')
    return $true
}

Write-Step "Checking for Python 3.11+"
$python = Find-Python
if (-not $python) {
    Write-Warn "No Python 3.11+ found on PATH (checked 'py -3.12', 'py -3.11', 'py -3.13', 'python')."
    $installed = Install-PythonViaWinget
    if ($installed) {
        $python = Find-Python
    }
    if (-not $python) {
        Write-Fail "Python 3.11+ still not found. If you just installed it, close this window, open a NEW PowerShell/Command Prompt window (so it sees the updated PATH), and re-run scripts\setup.bat."
        Exit-Setup 1
    }
}
$pythonCommandDisplay = ($python.Args -join ' ')
Write-Ok "Using Python $($python.Version) -- command: $($python.Exe) $pythonCommandDisplay"

# ---------------------------------------------------------------------------
# Step b: virtual environment + dependencies
# ---------------------------------------------------------------------------

Write-Step "Setting up the Python environment (.venv, pip, dependencies)"

$venvPythonExe = Join-Path $VenvPath 'Scripts\python.exe'

if (Test-Path $venvPythonExe) {
    Write-Ok ".venv already exists at $VenvPath -- skipping creation."
} else {
    Write-Info "Creating virtual environment at $VenvPath ..."
    $venvArgs = @($python.Args) + @('-m', 'venv', $VenvPath)
    & $python.Exe @venvArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPythonExe)) {
        Write-Fail "Failed to create the virtual environment (exit code $LASTEXITCODE). Check the transcript above for the Python error."
        Exit-Setup 2
    }
    Write-Ok "Virtual environment created."
}

Write-Info "Upgrading pip ..."
& $venvPythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip upgrade failed (exit code $LASTEXITCODE). Check your network connection and the transcript above, then re-run this script."
    Exit-Setup 3
}
Write-Ok "pip is up to date."

Write-Info "Installing dependencies from requirements.txt (pandas, SQLAlchemy, the desktop UI stack, ...) -- this can take a few minutes the first time ..."
& $venvPythonExe -m pip install -r $RequirementsPath
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Installing requirements.txt failed (exit code $LASTEXITCODE). Check your network connection and the transcript above, then re-run this script."
    Exit-Setup 3
}
Write-Ok "Dependencies installed."

Write-Info "Installing ClaudeTrade itself (editable install; registers the 'claudetrade' command) ..."
& $venvPythonExe -m pip install -e $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    Write-Fail "'pip install -e .' failed (exit code $LASTEXITCODE). Check the transcript above, then re-run this script."
    Exit-Setup 3
}
Write-Ok "ClaudeTrade installed."

$venvClaudetrade = Join-Path $VenvPath 'Scripts\claudetrade.exe'
if (-not (Test-Path $venvClaudetrade)) {
    Write-Fail "Expected $venvClaudetrade to exist after installation but it does not. Something went wrong with the editable install; check the transcript above."
    Exit-Setup 3
}

# ---------------------------------------------------------------------------
# Step c: claudetrade init
# ---------------------------------------------------------------------------

Write-Step "Initializing the database (claudetrade init)"
& $venvClaudetrade init
if ($LASTEXITCODE -ne 0) {
    Write-Fail "'claudetrade init' failed (exit code $LASTEXITCODE). Check the transcript above -- this usually means a permissions problem writing to %LOCALAPPDATA%\ClaudeTrade, or a corrupt existing database (see docs\troubleshooting.md)."
    Exit-Setup 4
}
Write-Ok "Database ready."

# ---------------------------------------------------------------------------
# Step d: claudetrade refresh (first data load)
# ---------------------------------------------------------------------------

if ($SkipData) {
    Write-Step "Skipping first data load (-SkipData passed)"
} else {
    Write-Step "Loading data (claudetrade refresh -- defaults to the last 90 days)"
    & $venvClaudetrade refresh
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "'claudetrade refresh' reported a problem (exit code $LASTEXITCODE)."
        Write-Warn "This is not fatal -- ClaudeTrade's default synthetic data needs no network, so this usually means a live provider (Stooq, Reddit, etc.) is unreachable or missing credentials."
        Write-Warn "Run '.venv\Scripts\claudetrade.exe probe' to see exactly which host or credential is the problem. The UI will still start below, using whatever data is already in the database."
    } else {
        Write-Ok "Data refresh completed."
    }
}

# ---------------------------------------------------------------------------
# Step e: launch the UI
# ---------------------------------------------------------------------------

$finalExitCode = 0

if ($NoLaunch) {
    Write-Step "Skipping UI launch (-NoLaunch passed)"
    Write-Info "Setup is complete. Launch the UI later with:"
    Write-Info "  .venv\Scripts\claudetrade.exe ui"
    Write-Info "or by re-running scripts\setup.bat without -NoLaunch."
} else {
    $uiArgs = @('ui')
    if ($Classic) {
        $uiArgs += '--classic'
        Write-Step "Starting the ClaudeTrade UI (--classic: legacy Streamlit interface)"
    } else {
        Write-Step "Starting the ClaudeTrade UI (desktop app)"
    }
    Write-Info "This window will stay busy while the UI is running. Close the app window (or press Ctrl+C here) to stop it."
    & $venvClaudetrade @uiArgs
    $finalExitCode = $LASTEXITCODE
    if ($finalExitCode -ne 0) {
        Write-Warn "The UI process exited with code $finalExitCode. Check the transcript above for details."
    } else {
        Write-Ok "UI closed normally."
    }
}

Exit-Setup $finalExitCode
