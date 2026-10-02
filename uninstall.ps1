<#
  Удаляет Shirma: ярлыки, автозапуск, %LOCALAPPDATA%\Shirma, профиль и сцену OBS «Shirma».
  OBS и плагин obs-backgroundremoval остаются (ими могут пользоваться и без Shirma).
  С ключом -RemovePlugin удаляет и плагин.

    irm https://raw.githubusercontent.com/aeamosov/obs-shirma/main/uninstall.ps1 | iex

  Строки — только ASCII, см. объяснение в install.ps1.
#>
param([switch]$RemovePlugin)

$ErrorActionPreference = "Stop"
# Язык сообщений: русский на русской Windows, иначе английский (SHIRMA_LANG=ru|en — принудительно).
# Русские строки записаны кодами \uXXXX — в .ps1 допустим только ASCII; перевод — в комментарии рядом.
$RU = [Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq "ru"
if ($env:SHIRMA_LANG) { $RU = $env:SHIRMA_LANG -eq "ru" }
function L([string]$en, [string]$ru) { if ($RU) { [regex]::Unescape($ru) } else { $en } }

$Data = Join-Path $env:LOCALAPPDATA "Shirma"
$ObsCfg = Join-Path $env:APPDATA "obs-studio\basic"

# OBS может работать невидимым (Shirma убирает его значок), поэтому закрываем сами
if (Get-Process obs64 -ErrorAction SilentlyContinue) {
    # OBS запущен. Нажмите Enter, чтобы закрыть его и продолжить
    Read-Host (L "OBS is running. Press Enter to close it and continue" 'OBS \u0437\u0430\u043f\u0443\u0449\u0435\u043d. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 Enter, \u0447\u0442\u043e\u0431\u044b \u0437\u0430\u043a\u0440\u044b\u0442\u044c \u0435\u0433\u043e \u0438 \u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c') | Out-Null
    taskkill /IM obs64.exe 2>&1 | Out-Null
    for ($i = 0; $i -lt 10 -and (Get-Process obs64 -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
    if (Get-Process obs64 -ErrorAction SilentlyContinue) { taskkill /F /IM obs64.exe 2>&1 | Out-Null; Start-Sleep 1 }
}
# Значок в трее (pythonw из venv Shirma)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*Shirma*shirma.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"),
                   [Environment]::GetFolderPath("Startup"))) {
    Remove-Item (Join-Path $dir "Shirma.lnk") -Force -ErrorAction SilentlyContinue
}
Remove-Item (Join-Path $ObsCfg "scenes\Shirma.json"), (Join-Path $ObsCfg "scenes\Shirma.json.bak") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $ObsCfg "profiles\Shirma") -Recurse -Force -ErrorAction SilentlyContinue

$bg = Join-Path $Data "backgrounds"
if (Test-Path $bg) {
    $keep = Join-Path ([Environment]::GetFolderPath("MyPictures")) "Shirma backgrounds"
    Copy-Item $bg $keep -Recurse -Force
    # Фоны сохранены в {0}
    Write-Host ((L "Backgrounds saved to {0}" '\u0424\u043e\u043d\u044b \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u044b \u0432 {0}') -f $keep)
}
Remove-Item $Data -Recurse -Force -ErrorAction SilentlyContinue

if ($RemovePlugin) {
    Remove-Item (Join-Path $env:ProgramData "obs-studio\plugins\obs-backgroundremoval") -Recurse -Force -ErrorAction SilentlyContinue
}
# Shirma удалена.
Write-Host (L "Shirma has been removed." 'Shirma \u0443\u0434\u0430\u043b\u0435\u043d\u0430.') -ForegroundColor Green
