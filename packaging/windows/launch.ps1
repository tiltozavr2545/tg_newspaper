# Launches the TG Newspaper console on Windows (the counterpart of scripts/launch.sh in release mode).
# Lives in the install directory next to the project sources (pyproject.toml, src\, scripts\);
# user data, the venv and logs live in %LOCALAPPDATA%\TG Newspaper, so that
# reinstalling/updating the program doesn't touch them.
#
# Environment variables (for testing; not set during normal use):
#   TG_NEWSPAPER_PORT     - console port (default 8420)
#   TG_NEWSPAPER_NO_OPEN  - don't open the browser
#   TG_NEWSPAPER_HOME     - user data directory (default %LOCALAPPDATA%\TG Newspaper)

# Continue, not Stop: in Windows PowerShell 5.1, under Stop any stderr line from a native
# program (uv and playwright write progress there) turns into an exception.
# We check for errors explicitly via $LASTEXITCODE and try/catch.
$ErrorActionPreference = 'Continue'

# Without this, the process's stdout written to a file comes out as cp1251, and Cyrillic/emoji in logs crash the console.
$env:PYTHONUTF8 = '1'

Add-Type -AssemblyName System.Windows.Forms

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Port = if ($env:TG_NEWSPAPER_PORT) { $env:TG_NEWSPAPER_PORT } else { '8420' }
$Url = "http://localhost:$Port/"
# Availability checks go against 127.0.0.1, not localhost: on Windows, localhost first
# resolves to ::1 (IPv6), while the console listens only on IPv4. A request to a closed IPv6 port
# retries for about 2s and doesn't fit within -TimeoutSec, so the port "isn't seen".
# We still open $Url (localhost) in the browser.
$ProbeUrl = "http://127.0.0.1:$Port/"
$HomeDir = if ($env:TG_NEWSPAPER_HOME) { $env:TG_NEWSPAPER_HOME } else { Join-Path $env:LOCALAPPDATA 'TG Newspaper' }
$NoOpen = [bool]$env:TG_NEWSPAPER_NO_OPEN

# Release mode is enabled the same way as in launch.sh: the console and uv see these variables.
$env:TG_NEWSPAPER_HOME = $HomeDir
$env:UV_PROJECT_ENVIRONMENT = Join-Path $HomeDir 'venv'
$RunDir = Join-Path $HomeDir 'data'
$env:TG_NEWSPAPER_RUN_DIR = $RunDir
$Py = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts\python.exe'
$Log = Join-Path $RunDir 'console.log'
$ErrLog = Join-Path $RunDir 'console.err.log'
$PidFile = Join-Path $RunDir 'console.pid'
$SetupLog = Join-Path $RunDir 'setup.log'

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

function Show-Error([string]$Message) {
    # In tests/CI there's no one to close the dialog - it would hang forever, so stderr only.
    if ($NoOpen -or $env:CI) { [Console]::Error.WriteLine($Message); return }
    [void][System.Windows.Forms.MessageBox]::Show($Message, 'TG Newspaper',
        [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning)
}

function Get-Tail([string]$Path) {
    if (Test-Path $Path) { (Get-Content -Path $Path -Tail 15 -ErrorAction SilentlyContinue) -join "`n" } else { '' }
}

function Test-Up {
    try {
        Invoke-WebRequest -Uri $ProbeUrl -UseBasicParsing -TimeoutSec 2 -MaximumRedirection 0 -ErrorAction Stop | Out-Null
        return $true
    } catch {
        # PowerShell treats a 303 on /setup on a fresh install as an error, but it's
        # still a server response, meaning the console is alive; whereas "no connection" has no response.
        return ($null -ne $_.Exception.Response)
    }
}

function Open-Url([string]$Target) {
    if ($NoOpen) { Write-Output "open $Target" } else { Start-Process $Target }
}

# Repeat click on the shortcut: the console is already running - just open a tab.
if (Test-Up) {
    Open-Url $Url
    exit 0
}

# Look for uv: PATH, the usual uv installer location, our own directory.
function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($c in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'), (Join-Path $HomeDir 'uv\uv.exe'))) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

# Preparation is needed if there's no environment, the build differs (BUILD_ID doesn't match the
# stamp in the venv - e.g. after an update; no BUILD_ID - always), or Chromium is
# missing. Otherwise we start right away, without uv or the network.
$BuildFile = Join-Path $Root 'BUILD_ID'
$BuildId = if (Test-Path $BuildFile) { (Get-Content -Raw $BuildFile).Trim() } else { '' }
$Stamp = Join-Path $env:UV_PROJECT_ENVIRONMENT '.tg-newspaper-build'
$NeedPrepare = -not (Test-Path $Py)
if (-not $NeedPrepare) {
    $stamped = if (Test-Path $Stamp) { (Get-Content -Raw $Stamp).Trim() } else { '' }
    if (-not $BuildId -or $stamped -ne $BuildId) { $NeedPrepare = $true }
}
if (-not $NeedPrepare) {
    & $Py -c "import os, sys; from playwright.sync_api import sync_playwright as sp; p = sp().start(); ok = os.path.exists(p.chromium.executable_path); p.stop(); sys.exit(0 if ok else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) { $NeedPrepare = $true }
}
$PreparingOpened = $false
if ($NeedPrepare -and -not $NoOpen) {
    $PrepPage = Join-Path $RunDir 'preparing.html'
    $tpl = Get-Content -Raw -Encoding UTF8 (Join-Path $Root 'scripts\preparing.html')
    # UTF-8 without BOM explicitly: Set-Content in Windows PowerShell 5.1 writes ANSI and breaks Cyrillic.
    [System.IO.File]::WriteAllText($PrepPage, $tpl.Replace('__PORT__', $Port), (New-Object System.Text.UTF8Encoding($false)))
    Start-Process $PrepPage
    $PreparingOpened = $true
}

try {
    if ($NeedPrepare) {
        Set-Content -Path $SetupLog -Value '' -Encoding utf8
        $Uv = Find-Uv
        # Only to test the uv-install code path.
        if ($env:TG_NEWSPAPER_FORCE_UV_INSTALL) { $Uv = $null }
        if (-not $Uv) {
            Add-Content -Path $SetupLog -Value '== installing uv ==' -Encoding utf8
            # Install uv into our own directory, without touching the user's PATH.
            $env:UV_INSTALL_DIR = Join-Path $HomeDir 'uv'
            $env:UV_NO_MODIFY_PATH = '1'
            try {
                Invoke-RestMethod https://astral.sh/uv/install.ps1 -ErrorAction Stop | Invoke-Expression *>&1 | ForEach-Object { "$_" } | Out-File -FilePath $SetupLog -Append -Encoding utf8
            } catch {
                throw "Не удалось установить uv (менеджер Python). Проверьте интернет и запустите TG Newspaper снова.`n`n$($_.Exception.Message)"
            }
            $Uv = Find-Uv
            if (-not $Uv) { throw 'uv установлен, но не найден. Запустите TG Newspaper снова.' }
        }

        Set-Location $Root
        # --no-editable: the program's path can change on update; --reinstall-package:
        # after an update, the venv must end up with the new code even at the same package version.
        Add-Content -Path $SetupLog -Value "== uv sync ($Uv) ==" -Encoding utf8
        # Output in UTF-8 via ForEach-Object: stderr lines become plain text rather than an ErrorRecord with "At line...".
        & $Uv sync --frozen --no-editable --reinstall-package tg-newspaper --quiet 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $SetupLog -Append -Encoding utf8
        $uvCode = $LASTEXITCODE
        if ($uvCode -ne 0) {
            throw "Не удалось установить зависимости (uv sync):`n`n$(Get-Tail $SetupLog)"
        }
        # Idempotent: returns immediately if Chromium is already installed.
        Add-Content -Path $SetupLog -Value '== playwright install chromium ==' -Encoding utf8
        & $Py -m playwright install chromium 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $SetupLog -Append -Encoding utf8
        $pwCode = $LASTEXITCODE
        if ($pwCode -ne 0) {
            throw "Не удалось установить Chromium для вёрстки (playwright install):`n`n$(Get-Tail $SetupLog)"
        }
        if ($BuildId) { Set-Content -Path $Stamp -Value $BuildId -Encoding ascii }
    } else {
        'preparation not needed' | Out-File -FilePath $SetupLog -Encoding utf8
    }
    Set-Location $Root

    # Directly via the venv interpreter, not uv run: we re-sync nothing.
    $proc = Start-Process -FilePath $Py `
        -ArgumentList @('scripts\console.py', '--no-browser', '--port', $Port) `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $Log -RedirectStandardError $ErrLog
    Set-Content -Path $PidFile -Value $proc.Id

    # Wait up to 90s for the port to come up (cold first start on Windows: Defender scans
    # fresh .pyc/.dll files).
    for ($i = 0; $i -lt 180; $i++) {
        if (Test-Up) {
            # The waiting page navigates to the console itself - no second tab needed.
            if (-not $PreparingOpened) { Open-Url $Url }
            exit 0
        }
        Start-Sleep -Milliseconds 500
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $PidFile
    throw "Консоль не поднялась за 90 секунд. Последние строки лога ($ErrLog):`n`n$(Get-Tail $ErrLog)"
} catch {
    Show-Error $_.Exception.Message
    exit 1
}
