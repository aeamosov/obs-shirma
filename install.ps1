<#
  Shirma — виртуальный фон для звонков на базе OBS.
  Установка одной командой (PowerShell, без прав администратора):

    irm https://raw.githubusercontent.com/aeamosov/obs-shirma/main/install.ps1 | iex

  Что делает: ставит OBS (если нет), плагин obs-backgroundremoval, Python (если нет),
  копирует Shirma в %LOCALAPPDATA%\Shirma, настраивает отдельные профиль и сцену OBS
  «Shirma» и создаёт ярлык Shirma. Ваши профили и сцены OBS не трогает.

  Строки в скрипте — только ASCII: Windows PowerShell 5.1 читает файл без BOM
  в ANSI-кодировке, и байты кириллицы превращаются в «умные» кавычки, ломая разбор.
  BOM тоже нельзя — с ним падает `irm | iex`. Комментарии на русском разбору не мешают.
#>
param(
    [string]$Camera = "",      # номер или часть имени камеры; пусто — спросит, если камер несколько
    [string]$Repo = "aeamosov/obs-shirma",
    [string]$Branch = "main",
    [switch]$Update            # запуск из меню трея «Обновить»: согласие уже дали там, OBS закрываем без вопроса
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Data = Join-Path $env:LOCALAPPDATA "Shirma"
$PluginRoot = Join-Path $env:ProgramData "obs-studio\plugins"
$PluginDll = Join-Path $PluginRoot "obs-backgroundremoval\bin\64bit\obs-backgroundremoval.dll"

# Язык сообщений: русский на русской Windows, иначе английский (SHIRMA_LANG=ru|en — принудительно).
# Русские строки записаны кодами \uXXXX — в .ps1 допустим только ASCII; перевод — в комментарии рядом.
$RU = [Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq "ru"
if ($env:SHIRMA_LANG) { $RU = $env:SHIRMA_LANG -eq "ru" }
function L([string]$en, [string]$ru) { if ($RU) { [regex]::Unescape($ru) } else { $en } }

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Refresh-ShortcutIcons {
    # Optional Windows utility: missing or blocked icon refresh must not stop installation.
    try {
        $refresh = Join-Path $env:SystemRoot "System32\ie4uinit.exe"
        if (Test-Path -LiteralPath $refresh -PathType Leaf) {
            & $refresh -show 2>$null | Out-Null
        }
    } catch {
        Write-Verbose "Skipping optional shortcut icon refresh: $_"
    }
}

function Find-Obs {
    $dirs = @()
    foreach ($key in "HKLM:\SOFTWARE\OBS Studio", "HKLM:\SOFTWARE\WOW6432Node\OBS Studio") {
        $v = (Get-ItemProperty $key -ErrorAction SilentlyContinue).'(default)'
        if ($v) { $dirs += $v }
    }
    $dirs += "$env:ProgramFiles\obs-studio", "${env:ProgramFiles(x86)}\obs-studio"
    foreach ($d in $dirs) {
        $exe = Join-Path $d "bin\64bit\obs64.exe"
        if (Test-Path $exe) { return (Resolve-Path $exe).Path }
    }
    return $null
}

# OBS может работать невидимым (Shirma убирает его значок из трея), поэтому закрываем сами:
# сначала штатно, а если не послушался (например, закрылось только окно превью) — принудительно.
function Close-Obs([switch]$Ask) {
    if (-not (Get-Process obs64 -ErrorAction SilentlyContinue)) { return }
    if ($Ask -and $Update) { # Закрываю OBS для обновления
Write-Host (L "    Closing OBS for the update" '    \u0417\u0430\u043a\u0440\u044b\u0432\u0430\u044e OBS \u0434\u043b\u044f \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f') }
    elseif ($Ask) { # OBS запущен. Нажмите Enter, чтобы закрыть его и продолжить
Read-Host (L "    OBS is running. Press Enter to close it and continue" '    OBS \u0437\u0430\u043f\u0443\u0449\u0435\u043d. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 Enter, \u0447\u0442\u043e\u0431\u044b \u0437\u0430\u043a\u0440\u044b\u0442\u044c \u0435\u0433\u043e \u0438 \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c') | Out-Null }
    else { # OBS снова запущен - закрываю
Write-Host (L "    OBS was started again - closing it" '    OBS \u0441\u043d\u043e\u0432\u0430 \u0437\u0430\u043f\u0443\u0449\u0435\u043d - \u0437\u0430\u043a\u0440\u044b\u0432\u0430\u044e') }
    taskkill /IM obs64.exe 2>&1 | Out-Null
    for ($i = 0; $i -lt 10 -and (Get-Process obs64 -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
    if (Get-Process obs64 -ErrorAction SilentlyContinue) { taskkill /F /IM obs64.exe 2>&1 | Out-Null; Start-Sleep 1 }
}

# Распаковка zip через .NET, а не Expand-Archive: если PowerShell запущен не из
# PowerShell 7, а, например, из трея, ему достаётся PSModulePath седьмой версии,
# Windows PowerShell находит там её модуль Archive, а политика выполнения не даёт
# его загрузить (так падало «Обновить Shirma…»). Папка назначения должна быть новой.
function Expand-Zip([string]$zip, [string]$dest) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $dest)
}

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Find-Python {
    # py-лаунчер надёжнее: «python» в PATH часто оказывается заглушкой Microsoft Store
    $candidates = @()
    if (Have "py") { $candidates += ,@("py", "-3") }
    if (Have "python") { $candidates += ,@("python") }
    foreach ($c in $candidates) {
        try {
            $exe = & $c[0] $c[1..9] -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe -and $exe -notmatch "WindowsApps") { return $exe.Trim() }
        } catch {}
    }
    return $null
}

# --- 1. OBS ------------------------------------------------------------------
Step "OBS Studio"
$Obs = Find-Obs
if (-not $Obs) {
    if (-not (Have "winget")) { # OBS не найден, а winget недоступен. Установите OBS с https://obsproject.com и запустите установку снова.
throw (L "OBS not found and winget is unavailable. Install OBS from https://obsproject.com and run the installer again." 'OBS \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d, \u0430 winget \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d. \u0423\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u0435 OBS \u0441 https://obsproject.com \u0438 \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0443 \u0441\u043d\u043e\u0432\u0430.') }
    winget install --id OBSProject.OBSStudio -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $Obs = Find-Obs
    if (-not $Obs) { # OBS не установился. Установите его с https://obsproject.com и запустите установку снова.
throw (L "OBS failed to install. Install it from https://obsproject.com and run the installer again." 'OBS \u043d\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u043b\u0441\u044f. \u0423\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u0435 \u0435\u0433\u043e \u0441 https://obsproject.com \u0438 \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0443 \u0441\u043d\u043e\u0432\u0430.') }
}
Write-Host "    $Obs"
Close-Obs -Ask

# --- 2. Плагин удаления фона ---------------------------------------------------
# Плагин obs-backgroundremoval
Step (L "obs-backgroundremoval plugin" '\u041f\u043b\u0430\u0433\u0438\u043d obs-backgroundremoval')
if (Test-Path $PluginDll) {
    # уже установлен
    Write-Host (L "    already installed" '    \u0443\u0436\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d')
} else {
    $rel = Invoke-RestMethod "https://api.github.com/repos/royshil/obs-backgroundremoval/releases/latest"
    $asset = $rel.assets | Where-Object { $_.name -like "*windows-x64.zip" } | Select-Object -First 1
    if (-not $asset) { # В релизе {0} нет сборки плагина для Windows.
throw ((L "No Windows build of the plugin in release {0}." '\u0412 \u0440\u0435\u043b\u0438\u0437\u0435 {0} \u043d\u0435\u0442 \u0441\u0431\u043e\u0440\u043a\u0438 \u043f\u043b\u0430\u0433\u0438\u043d\u0430 \u0434\u043b\u044f Windows.') -f $rel.tag_name) }
    $tmp = Join-Path $env:TEMP "shirma-plugin"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory $tmp | Out-Null
    Invoke-WebRequest $asset.browser_download_url -OutFile "$tmp\plugin.zip"
    Expand-Zip "$tmp\plugin.zip" "$tmp\x"
    New-Item -ItemType Directory $PluginRoot -Force | Out-Null
    Copy-Item "$tmp\x\obs-backgroundremoval" $PluginRoot -Recurse -Force
    Remove-Item $tmp -Recurse -Force
    Write-Host "    $($rel.tag_name)"
}

# --- 3. Python ---------------------------------------------------------------
Step "Python"
$Py = Find-Python
if (-not $Py) {
    if (-not (Have "winget")) { # Нужен Python 3.10+: https://www.python.org/downloads/
throw (L "Python 3.10+ is required: https://www.python.org/downloads/" '\u041d\u0443\u0436\u0435\u043d Python 3.10+: https://www.python.org/downloads/') }
    winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
    Refresh-Path
    $Py = Find-Python
    if (-not $Py) { # Python не установился. Поставьте Python 3.10+ и запустите установку снова.
throw (L "Python failed to install. Install Python 3.10+ and run the installer again." 'Python \u043d\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u043b\u0441\u044f. \u041f\u043e\u0441\u0442\u0430\u0432\u044c\u0442\u0435 Python 3.10+ \u0438 \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0443 \u0441\u043d\u043e\u0432\u0430.') }
}
Write-Host "    $Py"

# --- 4. Файлы Shirma ---------------------------------------------------------
# Файлы Shirma -> {0}
Step ((L "Shirma files -> {0}" '\u0424\u0430\u0439\u043b\u044b Shirma -> {0}') -f $Data)
$Src = $null
$Version = ""  # sha коммита — по нему пункт трея «Обновить» понимает, есть ли новая версия
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "app\shirma.py"))) {
    $Src = $PSScriptRoot  # запуск из клона репозитория
    if (Have "git") { try { $Version = "$(git -C $Src rev-parse HEAD 2>$null)".Trim() } catch {} }
} else {
    $tmp = Join-Path $env:TEMP "shirma-src"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory $tmp | Out-Null
    # Архив качаем по sha, а не по ветке: тогда записанная версия точно совпадает с файлами
    $zipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
    try {
        $Version = (Invoke-RestMethod "https://api.github.com/repos/$Repo/commits/$Branch").sha
        if ($Version) { $zipUrl = "https://github.com/$Repo/archive/$Version.zip" }
    } catch { $Version = "" }
    Invoke-WebRequest $zipUrl -OutFile "$tmp\src.zip"
    Expand-Zip "$tmp\src.zip" $tmp
    $Src = (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName
}
New-Item -ItemType Directory "$Data\app", "$Data\backgrounds" -Force | Out-Null
Copy-Item "$Src\app\*" "$Data\app" -Recurse -Force
@{ sha = $Version; repo = $Repo; branch = $Branch } | ConvertTo-Json | Set-Content "$Data\version.json" -Encoding ASCII
# Фоны раскладывает setup_obs.py (шаг 6): имена русские, а здесь допустим только ASCII

# --- 5. Окружение Python -----------------------------------------------------
# Зависимости
Step (L "Dependencies" '\u0417\u0430\u0432\u0438\u0441\u0438\u043c\u043e\u0441\u0442\u0438')
$VenvPy = "$Data\venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { & $Py -m venv "$Data\venv" }
& $VenvPy -m pip install --disable-pip-version-check -q -r "$Data\app\requirements.txt"
if ($LASTEXITCODE -ne 0) { # pip не смог поставить зависимости.
throw (L "pip failed to install dependencies." 'pip \u043d\u0435 \u0441\u043c\u043e\u0433 \u043f\u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u0432\u0438\u0441\u0438\u043c\u043e\u0441\u0442\u0438.') }

# --- 6. Настройка OBS --------------------------------------------------------
# Сцена OBS
Step (L "OBS scene" '\u0421\u0446\u0435\u043d\u0430 OBS')
# Пока ставились зависимости, OBS могли запустить с ярлыка: при выходе он перезапишет
# сцену своим состоянием, поэтому закрываем ещё раз (согласие уже получено выше)
Close-Obs
$setupArgs = @("$Data\app\setup_obs.py", "--obs", $Obs, "--data", $Data, "--bundled", "$Src\backgrounds")
if ($Camera) { $setupArgs += @("--camera", $Camera) }
& $VenvPy @setupArgs
if ($LASTEXITCODE -ne 0) { # Настройка OBS не удалась (см. сообщение выше).
throw (L "OBS setup failed (see the message above)." '\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430 OBS \u043d\u0435 \u0443\u0434\u0430\u043b\u0430\u0441\u044c (\u0441\u043c. \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u0432\u044b\u0448\u0435).') }

# --- 7. Ярлыки ---------------------------------------------------------------
# Ярлык ведёт прямо на obs64.exe: если запускать OBS из скрипта, корпоративный
# антивирус может не дать ему камеру. Значок в трее OBS поднимет сам.
# Ярлыки
Step (L "Shortcuts" '\u042f\u0440\u043b\u044b\u043a\u0438')
$sh = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $lnk = $sh.CreateShortcut((Join-Path $dir "Shirma.lnk"))
    $lnk.TargetPath = $Obs
    $lnk.Arguments = "--startvirtualcam --minimize-to-tray --disable-updater --disable-shutdown-check --profile Shirma --collection Shirma"
    $lnk.WorkingDirectory = Split-Path $Obs
    $lnk.IconLocation = "$Data\icon.ico,0"
    $lnk.Description = "Shirma - virtual background for calls"
    $lnk.WindowStyle = 7
    $lnk.Save()
}
# Ярлыки сразу с новой иконкой: просим Windows обновить кэш значков
Refresh-ShortcutIcons

# --- 8. Первый запуск -----------------------------------------------------------
# Обычный запуск, без обходных путей. Некоторые корпоративные антивирусы не дают камеру
# процессам, запущенным из скриптов, — тогда в этом сеансе в звонке будет только фон,
# и достаточно один раз перезапустить Shirma с ярлыка; об этом говорим ниже.
# Запуск Shirma
Step (L "Starting Shirma" '\u0417\u0430\u043f\u0443\u0441\u043a Shirma')
# После падения OBS предлагает «безопасный режим», а в нём отключены скрипты — то есть
# вся Shirma. Мы только что всё перенастроили, поэтому метку сбоя сбрасываем.
Remove-Item (Join-Path $env:APPDATA "obs-studio\.sentinel\*") -Force -ErrorAction SilentlyContinue
Start-Process -FilePath $Obs -WorkingDirectory (Split-Path $Obs) -WindowStyle Hidden -ArgumentList "--startvirtualcam", "--minimize-to-tray", "--disable-updater", "--disable-shutdown-check", "--profile", "Shirma", "--collection", "Shirma"

Write-Host ""
# Готово. Shirma запущена - её значок в трее (клик по нему - превью).
Write-Host (L "Done. Shirma is running - its icon is in the tray (click it for a 1:1 preview)." '\u0413\u043e\u0442\u043e\u0432\u043e. Shirma \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u0430 - \u0435\u0451 \u0437\u043d\u0430\u0447\u043e\u043a \u0432 \u0442\u0440\u0435\u0435 (\u043a\u043b\u0438\u043a \u043f\u043e \u043d\u0435\u043c\u0443 - \u043f\u0440\u0435\u0432\u044c\u044e).') -ForegroundColor Green
# Первый запуск может занять до минуты: OBS загружает модель и камеру. Значок появится в трее, когда всё будет готово.
Write-Host (L "The first start can take up to a minute while OBS loads the model and the camera. The icon appears in the tray when it is ready." '\u041f\u0435\u0440\u0432\u044b\u0439 \u0437\u0430\u043f\u0443\u0441\u043a \u043c\u043e\u0436\u0435\u0442 \u0437\u0430\u043d\u044f\u0442\u044c \u0434\u043e \u043c\u0438\u043d\u0443\u0442\u044b: OBS \u0437\u0430\u0433\u0440\u0443\u0436\u0430\u0435\u0442 \u043c\u043e\u0434\u0435\u043b\u044c \u0438 \u043a\u0430\u043c\u0435\u0440\u0443. \u0417\u043d\u0430\u0447\u043e\u043a \u043f\u043e\u044f\u0432\u0438\u0442\u0441\u044f \u0432 \u0442\u0440\u0435\u0435, \u043a\u043e\u0433\u0434\u0430 \u0432\u0441\u0451 \u0431\u0443\u0434\u0435\u0442 \u0433\u043e\u0442\u043e\u0432\u043e.') -ForegroundColor Yellow
# 1. В программе для звонков выберите камеру «OBS Virtual Camera».
Write-Host (L "1. In your call app, pick the 'OBS Virtual Camera' camera." '1. \u0412 \u043f\u0440\u043e\u0433\u0440\u0430\u043c\u043c\u0435 \u0434\u043b\u044f \u0437\u0432\u043e\u043d\u043a\u043e\u0432 \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u0430\u043c\u0435\u0440\u0443 \u00abOBS Virtual Camera\u00bb.')
# 2. Фон, камера и качество переключаются в меню значка.
Write-Host (L "2. Switch backgrounds, camera and quality from the tray menu." '2. \u0424\u043e\u043d, \u043a\u0430\u043c\u0435\u0440\u0430 \u0438 \u043a\u0430\u0447\u0435\u0441\u0442\u0432\u043e \u043f\u0435\u0440\u0435\u043a\u043b\u044e\u0447\u0430\u044e\u0442\u0441\u044f \u0432 \u043c\u0435\u043d\u044e \u0437\u043d\u0430\u0447\u043a\u0430.')
# 3. Свои картинки кладите в {0}\backgrounds (папки станут подменю).
Write-Host ((L "3. Your own images go to {0}\backgrounds (subfolders become submenus)." '3. \u0421\u0432\u043e\u0438 \u043a\u0430\u0440\u0442\u0438\u043d\u043a\u0438 \u043a\u043b\u0430\u0434\u0438\u0442\u0435 \u0432 {0}\\backgrounds (\u043f\u0430\u043f\u043a\u0438 \u0441\u0442\u0430\u043d\u0443\u0442 \u043f\u043e\u0434\u043c\u0435\u043d\u044e).') -f $Data)
# В следующий раз запускайте её ярлыком «Shirma» на рабочем столе.
Write-Host (L "Next time start it from the 'Shirma' desktop shortcut." '\u0412 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0440\u0430\u0437 \u0437\u0430\u043f\u0443\u0441\u043a\u0430\u0439\u0442\u0435 \u0435\u0451 \u044f\u0440\u043b\u044b\u043a\u043e\u043c \u00abShirma\u00bb \u043d\u0430 \u0440\u0430\u0431\u043e\u0447\u0435\u043c \u0441\u0442\u043e\u043b\u0435.')
# Если в звонке виден только фон без вас - антивирус не даёт камеру программам,
Write-Host (L "If the call shows only the background without you, your antivirus blocks the camera for apps" '\u0415\u0441\u043b\u0438 \u0432 \u0437\u0432\u043e\u043d\u043a\u0435 \u0432\u0438\u0434\u0435\u043d \u0442\u043e\u043b\u044c\u043a\u043e \u0444\u043e\u043d \u0431\u0435\u0437 \u0432\u0430\u0441 - \u0430\u043d\u0442\u0438\u0432\u0438\u0440\u0443\u0441 \u043d\u0435 \u0434\u0430\u0451\u0442 \u043a\u0430\u043c\u0435\u0440\u0443 \u043f\u0440\u043e\u0433\u0440\u0430\u043c\u043c\u0430\u043c,')
# запущенным из скриптов: выключите Shirma в меню значка и запустите её ярлыком.
Write-Host (L "started by scripts: exit Shirma from the tray menu and start it from the desktop shortcut." '\u0437\u0430\u043f\u0443\u0449\u0435\u043d\u043d\u044b\u043c \u0438\u0437 \u0441\u043a\u0440\u0438\u043f\u0442\u043e\u0432: \u0432\u044b\u043a\u043b\u044e\u0447\u0438\u0442\u0435 Shirma \u0432 \u043c\u0435\u043d\u044e \u0437\u043d\u0430\u0447\u043a\u0430 \u0438 \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u0435\u0451 \u044f\u0440\u043b\u044b\u043a\u043e\u043c.')
if ($Update) { # Обновление завершено - это окно можно закрыть.
Write-Host ""
Write-Host (L "Update finished - you can close this window." '\u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u043e - \u044d\u0442\u043e \u043e\u043a\u043d\u043e \u043c\u043e\u0436\u043d\u043e \u0437\u0430\u043a\u0440\u044b\u0442\u044c.') -ForegroundColor Green }
