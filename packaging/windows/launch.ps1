# Запуск консоли TG Newspaper на Windows (аналог scripts/launch.sh в релизном режиме).
# Лежит в каталоге установки рядом с исходниками проекта (pyproject.toml, src\, scripts\);
# данные пользователя, venv и логи — в %LOCALAPPDATA%\TG Newspaper, чтобы
# переустановка/обновление программы их не трогали.
#
# Переменные окружения (для проверки, в обычной работе не задаются):
#   TG_NEWSPAPER_PORT     - порт консоли (по умолчанию 8420)
#   TG_NEWSPAPER_NO_OPEN  - не открывать браузер
#   TG_NEWSPAPER_HOME     - каталог данных пользователя (по умолчанию %LOCALAPPDATA%\TG Newspaper)

# Continue, не Stop: в Windows PowerShell 5.1 при Stop любая строка stderr нативной
# программы (uv, playwright пишут туда прогресс) превращается в исключение.
# Ошибки проверяем явно по $LASTEXITCODE и try/catch.
$ErrorActionPreference = 'Continue'

# Без этого stdout процесса в файл идёт в cp1251, и кириллица/эмодзи в логах роняют консоль.
$env:PYTHONUTF8 = '1'

Add-Type -AssemblyName System.Windows.Forms

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Port = if ($env:TG_NEWSPAPER_PORT) { $env:TG_NEWSPAPER_PORT } else { '8420' }
$Url = "http://localhost:$Port/"
$HomeDir = if ($env:TG_NEWSPAPER_HOME) { $env:TG_NEWSPAPER_HOME } else { Join-Path $env:LOCALAPPDATA 'TG Newspaper' }
$NoOpen = [bool]$env:TG_NEWSPAPER_NO_OPEN

# Релизный режим включается так же, как в launch.sh: консоль и uv видят эти переменные.
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
    # В тестах/CI окно некому закрыть - оно бы зависло навсегда, поэтому только stderr.
    if ($NoOpen -or $env:CI) { [Console]::Error.WriteLine($Message); return }
    [void][System.Windows.Forms.MessageBox]::Show($Message, 'TG Newspaper',
        [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Warning)
}

function Get-Tail([string]$Path) {
    if (Test-Path $Path) { (Get-Content -Path $Path -Tail 15 -ErrorAction SilentlyContinue) -join "`n" } else { '' }
}

function Test-Up {
    try {
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -MaximumRedirection 0 -ErrorAction Stop | Out-Null
        return $true
    } catch {
        # 303 на /setup на свежей установке PowerShell считает ошибкой, но это
        # ответ сервера, то есть консоль жива; а вот "нет соединения" ответа не имеет.
        return ($null -ne $_.Exception.Response)
    }
}

function Open-Url([string]$Target) {
    if ($NoOpen) { Write-Output "open $Target" } else { Start-Process $Target }
}

# Повторный клик по ярлыку: консоль уже работает - просто открываем вкладку.
if (Test-Up) {
    Open-Url $Url
    exit 0
}

# Ищем uv: PATH, обычное место установщика uv, наш собственный каталог.
function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($c in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'), (Join-Path $HomeDir 'uv\uv.exe'))) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

# Подготовка нужна, если нет окружения, сборка другая (BUILD_ID не совпал со
# штампом в venv - например, после обновления; нет BUILD_ID - всегда) или нет
# Chromium. Иначе стартуем сразу, без uv и сети.
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
    # UTF-8 без BOM явно: Set-Content в Windows PowerShell 5.1 пишет в ANSI и ломает кириллицу.
    [System.IO.File]::WriteAllText($PrepPage, $tpl.Replace('__PORT__', $Port), (New-Object System.Text.UTF8Encoding($false)))
    Start-Process $PrepPage
    $PreparingOpened = $true
}

try {
    if ($NeedPrepare) {
        Set-Content -Path $SetupLog -Value '' -Encoding utf8
        $Uv = Find-Uv
        # Только для проверки ветки установки uv.
        if ($env:TG_NEWSPAPER_FORCE_UV_INSTALL) { $Uv = $null }
        if (-not $Uv) {
            Add-Content -Path $SetupLog -Value '== установка uv =='
            # Ставим uv в свой каталог, не трогая PATH пользователя.
            $env:UV_INSTALL_DIR = Join-Path $HomeDir 'uv'
            $env:UV_NO_MODIFY_PATH = '1'
            try {
                Invoke-RestMethod https://astral.sh/uv/install.ps1 -ErrorAction Stop | Invoke-Expression *>&1 | Out-File -FilePath $SetupLog -Append -Encoding utf8
            } catch {
                throw "Не удалось установить uv (менеджер Python). Проверьте интернет и запустите TG Newspaper снова.`n`n$($_.Exception.Message)"
            }
            $Uv = Find-Uv
            if (-not $Uv) { throw 'uv установлен, но не найден. Запустите TG Newspaper снова.' }
        }

        Set-Location $Root
        # --no-editable: путь к программе может меняться при обновлении; --reinstall-package:
        # после обновления в venv должен оказаться новый код при той же версии пакета.
        Add-Content -Path $SetupLog -Value "== uv sync ($Uv) =="
        & $Uv sync --frozen --no-editable --reinstall-package tg-newspaper --quiet *>>$SetupLog
        if ($LASTEXITCODE -ne 0) {
            throw "Не удалось установить зависимости (uv sync):`n`n$(Get-Tail $SetupLog)"
        }
        # Идемпотентно: при уже установленном Chromium возвращается сразу.
        Add-Content -Path $SetupLog -Value '== playwright install chromium =='
        & $Py -m playwright install chromium *>>$SetupLog
        if ($LASTEXITCODE -ne 0) {
            throw "Не удалось установить Chromium для вёрстки (playwright install):`n`n$(Get-Tail $SetupLog)"
        }
        if ($BuildId) { Set-Content -Path $Stamp -Value $BuildId -Encoding ascii }
    } else {
        'подготовка не нужна' | Out-File -FilePath $SetupLog -Encoding utf8
    }
    Set-Location $Root

    # Напрямую интерпретатором venv, не uv run: ничего не пере-синхронизируем.
    $proc = Start-Process -FilePath $Py `
        -ArgumentList @('scripts\console.py', '--no-browser', '--port', $Port) `
        -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $Log -RedirectStandardError $ErrLog
    Set-Content -Path $PidFile -Value $proc.Id

    # Ждём готовности порта до 30 с.
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-Up) {
            # Страница ожидания сама переходит на консоль - вторая вкладка не нужна.
            if (-not $PreparingOpened) { Open-Url $Url }
            exit 0
        }
        Start-Sleep -Milliseconds 500
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $PidFile
    throw "Консоль не поднялась за 30 секунд. Последние строки лога ($ErrLog):`n`n$(Get-Tail $ErrLog)"
} catch {
    Show-Error $_.Exception.Message
    exit 1
}
