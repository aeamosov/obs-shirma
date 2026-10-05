#!/bin/bash
# Shirma для macOS — ЭКСПЕРИМЕНТАЛЬНО: собрано по документации OBS и плагина, на живом Mac
# ещё не проверялось. Установка одной командой (Терминал, без прав администратора):
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/aeamosov/obs-shirma/main/install-mac.sh)"
#
# Именно bash -c "$(curl …)", а не curl | bash: так у установщика остаётся терминал
# для вопросов (например, какую камеру взять, если их несколько).
#
# Что делает: ставит OBS (если нет), плагин obs-backgroundremoval в домашнюю папку,
# копирует Shirma в ~/Library/Application Support/Shirma, настраивает отдельные профиль
# и сцену OBS «Shirma» и создаёт приложение Shirma в ~/Applications. Ваши профили и
# сцены OBS не трогает.
set -euo pipefail

REPO="aeamosov/obs-shirma"
BRANCH="main"
CAMERA=""
UPDATE=0  # --update: запуск из меню «Обновить Shirma…», согласие уже дали там
while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --camera) CAMERA="$2"; shift 2 ;;
        --update) UPDATE=1; shift ;;
        *) shift ;;
    esac
done

DATA="$HOME/Library/Application Support/Shirma"
OBS_CFG="$HOME/Library/Application Support/obs-studio"
PLUGINS="$OBS_CFG/plugins"
OBS_APP="/Applications/OBS.app"
LAUNCHER="$HOME/Applications/Shirma.app"
OBS_ARGS=(--startvirtualcam --minimize-to-tray --disable-updater --disable-shutdown-check --profile Shirma --collection Shirma)

# Язык сообщений: первый язык системы (SHIRMA_LANG=ru|en — принудительно)
RU=0
if [ -n "${SHIRMA_LANG:-}" ]; then
    [ "$SHIRMA_LANG" = "ru" ] && RU=1
elif [ "$(defaults read -g AppleLanguages 2>/dev/null | sed -n 2p | tr -d ' ",' | cut -c1-2)" = "ru" ]; then
    RU=1
fi
L() { if [ "$RU" = 1 ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
die() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || die "$(L 'This installer is for macOS. On Windows use install.ps1.' 'Этот установщик — для macOS. На Windows — install.ps1.')"

obs_running() { pgrep -x OBS >/dev/null 2>&1; }

# Закрыть OBS: при выходе он перезаписывает сцену своим состоянием
close_obs() {
    obs_running || return 0
    if [ "$UPDATE" = 1 ] || [ "${1:-}" = "again" ]; then
        echo "    $(L 'Closing OBS' 'Закрываю OBS')"
    else
        read -r -p "    $(L 'OBS is running. Press Enter to close it and continue' 'OBS запущен. Нажмите Enter, чтобы закрыть его и продолжить') " _ </dev/tty
    fi
    osascript -e 'tell application "OBS" to quit' >/dev/null 2>&1 || true
    for _ in $(seq 10); do obs_running || return 0; sleep 1; done
    pkill -x OBS || true
    sleep 1
}

# Ссылка на файл последнего релиза GitHub по шаблону имени
release_asset() {
    curl -fsSL "https://api.github.com/repos/$1/releases/latest" \
        | grep -o '"browser_download_url": *"[^"]*"' | sed 's/.*"\(https[^"]*\)"/\1/' | grep -E "$2" | head -1 || true
}  # || true: пустой результат не должен ронять скрипт раньше понятного сообщения

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "$(L 'Shirma for macOS is experimental: please report what worked and what did not.' 'Shirma для macOS — экспериментальная версия: напишите, что сработало, а что нет.')"

# --- 1. OBS ------------------------------------------------------------------
step "OBS Studio"
if [ ! -d "$OBS_APP" ]; then
    if command -v brew >/dev/null 2>&1; then
        brew install --cask obs
    else
        arch="Intel"; [ "$(uname -m)" = "arm64" ] && arch="Apple"
        url="$(release_asset obsproject/obs-studio "macOS-$arch\\.dmg$")"
        [ -n "$url" ] || die "$(L 'Could not find the OBS download. Install OBS from https://obsproject.com and run the installer again.' 'Не нашёл загрузку OBS. Установите OBS с https://obsproject.com и запустите установку снова.')"
        curl -fL --progress-bar "$url" -o "$TMP/obs.dmg"
        mnt="$(hdiutil attach -nobrowse -readonly "$TMP/obs.dmg" | tail -1 | awk -F'\t' '{print $NF}')"
        cp -R "$mnt/OBS.app" /Applications/ || { hdiutil detach "$mnt" -quiet || true; die "$(L 'Could not copy OBS to /Applications.' 'Не удалось скопировать OBS в «Программы».')"; }
        hdiutil detach "$mnt" -quiet || true
    fi
fi
[ -d "$OBS_APP" ] || die "$(L 'OBS failed to install. Install it from https://obsproject.com and run the installer again.' 'OBS не установился. Установите его с https://obsproject.com и запустите установку снова.')"
echo "    $OBS_APP"
close_obs

# --- 2. Плагин удаления фона ---------------------------------------------------
# Под macOS это отдельная реализация плагина (CoreML), пакет рассчитан на установку
# в домашнюю папку — без прав администратора.
step "$(L 'obs-backgroundremoval plugin' 'Плагин obs-backgroundremoval')"
if [ ! -d "$PLUGINS/obs-backgroundremoval.plugin" ]; then
    url="$(release_asset royshil/obs-backgroundremoval 'macos-universal\.pkg$')"
    [ -n "$url" ] || die "$(L 'Could not find the plugin download.' 'Не нашёл загрузку плагина.')"
    curl -fL --progress-bar "$url" -o "$TMP/plugin.pkg"
    installer -pkg "$TMP/plugin.pkg" -target CurrentUserHomeDirectory >/dev/null 2>&1 || true
    if [ ! -d "$PLUGINS/obs-backgroundremoval.plugin" ]; then
        # Запасной путь: распаковать пакет и положить бандл плагина самим
        pkgutil --expand-full "$TMP/plugin.pkg" "$TMP/pkg"
        bundle="$(find "$TMP/pkg" -type d -name 'obs-backgroundremoval.plugin' -prune | head -1)"
        [ -n "$bundle" ] || die "$(L 'Could not unpack the plugin.' 'Не удалось распаковать плагин.')"
        mkdir -p "$PLUGINS"
        cp -R "$bundle" "$PLUGINS/"
    fi
fi
echo "    $PLUGINS/obs-backgroundremoval.plugin"

# --- 3. Python ---------------------------------------------------------------
step "Python"
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
    [ -x "$c" ] || continue
    # /usr/bin/python3 без Command Line Tools — заглушка, которая предлагает их установить
    if [ "$c" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
    if "$c" -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    if command -v brew >/dev/null 2>&1; then
        brew install python
        PY="$(brew --prefix)/bin/python3"
    else
        die "$(L 'Python 3.9+ is required: install it from https://www.python.org/downloads/macos/ and run the installer again.' 'Нужен Python 3.9+: установите его с https://www.python.org/downloads/macos/ и запустите установку снова.')"
    fi
fi
echo "    $PY"

# --- 4. Файлы Shirma ---------------------------------------------------------
step "$(L 'Shirma files' 'Файлы Shirma') -> $DATA"
VERSION=""  # sha коммита — по нему пункт меню «Обновить» понимает, есть ли новая версия
here=""  # при bash -c "$(curl …)" файла скрипта нет — качаем архив
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; fi
if [ -n "$here" ] && [ -f "$here/app/shirma.py" ]; then
    SRC="$here"  # запуск из клона репозитория
    VERSION="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)"
else
    VERSION="$(curl -fsSL "https://api.github.com/repos/$REPO/commits/$BRANCH" \
        | "$PY" -c 'import json, sys; print(json.load(sys.stdin)["sha"])' 2>/dev/null || true)"
    # Архив по sha, а не по ветке: записанная версия точно совпадает с файлами
    curl -fsSL "https://github.com/$REPO/archive/${VERSION:-$BRANCH}.tar.gz" | tar -xz -C "$TMP"
    SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d -name 'obs-shirma*' | head -1)"
    [ -n "$SRC" ] || SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d ! -name pkg | head -1)"
fi
mkdir -p "$DATA/app" "$DATA/backgrounds"
cp -R "$SRC/app/." "$DATA/app/"
printf '{"sha": "%s", "repo": "%s", "branch": "%s"}\n' "$VERSION" "$REPO" "$BRANCH" > "$DATA/version.json"

# --- 5. Окружение Python -----------------------------------------------------
step "$(L 'Dependencies' 'Зависимости')"
VPY="$DATA/venv/bin/python"
[ -x "$VPY" ] || "$PY" -m venv "$DATA/venv"
"$VPY" -m pip install --disable-pip-version-check -q -r "$DATA/app/requirements.txt" \
    || die "$(L 'pip failed to install dependencies.' 'pip не смог поставить зависимости.')"

# --- 6. Настройка OBS --------------------------------------------------------
step "$(L 'OBS scene' 'Сцена OBS')"
close_obs again  # OBS могли запустить, пока ставились зависимости
setup_args=("$DATA/app/setup_obs.py" --obs "$OBS_APP" --data "$DATA" --bundled "$SRC/backgrounds")
[ -n "$CAMERA" ] && setup_args+=(--camera "$CAMERA")
"$VPY" "${setup_args[@]}" || die "$(L 'OBS setup failed (see the message above).' 'Настройка OBS не удалась (см. сообщение выше).')"

# --- 7. Приложение Shirma ----------------------------------------------------
# Маленькое приложение-ярлык: открывает OBS с профилем и сценой Shirma.
step "$(L 'Shirma app' 'Приложение Shirma') -> $LAUNCHER"
rm -rf "$LAUNCHER"
mkdir -p "$LAUNCHER/Contents/MacOS" "$LAUNCHER/Contents/Resources"
{
    echo '#!/bin/bash'
    printf 'exec /usr/bin/open -a %q --args' "$OBS_APP"
    printf ' %q' "${OBS_ARGS[@]}"
    echo
} > "$LAUNCHER/Contents/MacOS/Shirma"
chmod +x "$LAUNCHER/Contents/MacOS/Shirma"
cat > "$LAUNCHER/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Shirma</string>
    <key>CFBundleDisplayName</key><string>Shirma</string>
    <key>CFBundleIdentifier</key><string>com.github.aeamosov.shirma.launcher</string>
    <key>CFBundleExecutable</key><string>Shirma</string>
    <key>CFBundleIconFile</key><string>icon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST
"$VPY" -c 'import sys; sys.path.insert(0, sys.argv[1]); import shirma; shirma.make_icon().save(sys.argv[2])' \
    "$DATA/app" "$LAUNCHER/Contents/Resources/icon.icns" 2>/dev/null || true

# --- 8. Первый запуск -----------------------------------------------------------
step "$(L 'Starting Shirma' 'Запуск Shirma')"
# После падения OBS предлагает «безопасный режим», а в нём отключены скрипты — то есть
# вся Shirma. Мы только что всё перенастроили, поэтому метку сбоя сбрасываем.
rm -f "$OBS_CFG/.sentinel/"* 2>/dev/null || true
open -a "$OBS_APP" --args "${OBS_ARGS[@]}"

echo
printf '\033[32m%s\033[0m\n' "$(L 'Done. Shirma is starting - its icon appears in the menu bar.' 'Готово. Shirma запускается - её значок появится в строке меню.')"
printf '\033[33m%s\033[0m\n' "$(L 'On the first start macOS asks two things - please allow both:' 'При первом запуске macOS спросит две вещи - разрешите обе:')"
echo "$(L '  - camera access for OBS;' '  - доступ OBS к камере;')"
echo "$(L '  - the OBS Virtual Camera system extension (System Settings -> General -> Login Items & Extensions, or Privacy & Security). After allowing it, quit Shirma from its menu and start it again.' '  - системное расширение OBS Virtual Camera (Системные настройки -> Основные -> Объекты входа и расширения или Конфиденциальность и безопасность). После разрешения выключите Shirma в её меню и запустите снова.')"
echo "$(L '1. In your call app, pick the "OBS Virtual Camera" camera.' '1. В программе для звонков выберите камеру «OBS Virtual Camera».')"
echo "$(L '2. Switch backgrounds and camera from the Shirma menu-bar icon.' '2. Фон и камера переключаются в меню значка Shirma в строке меню.')"
echo "$(L "3. Your own images go to $DATA/backgrounds (subfolders become submenus)." "3. Свои картинки кладите в $DATA/backgrounds (папки станут подменю).")"
echo "$(L "Next time start it with the Shirma app in ~/Applications (or Spotlight: Shirma)." "В следующий раз запускайте приложением Shirma в ~/Applications (или через Spotlight: Shirma).")"
if [ "$UPDATE" = 1 ]; then
    echo
    printf '\033[32m%s\033[0m\n' "$(L 'Update finished - you can close this window.' 'Обновление завершено - это окно можно закрыть.')"
fi
