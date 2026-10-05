#!/bin/bash
# Удаляет Shirma на macOS: приложение Shirma, автозапуск, ~/Library/Application Support/Shirma,
# профиль и сцену OBS «Shirma». OBS и плагин obs-backgroundremoval остаются (ими могут
# пользоваться и без Shirma); с ключом --remove-plugin удаляет и плагин.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/aeamosov/obs-shirma/main/uninstall-mac.sh)"
set -euo pipefail

REMOVE_PLUGIN=0
[ "${1:-}" = "--remove-plugin" ] && REMOVE_PLUGIN=1

DATA="$HOME/Library/Application Support/Shirma"
OBS_CFG="$HOME/Library/Application Support/obs-studio"

RU=0
if [ -n "${SHIRMA_LANG:-}" ]; then
    [ "$SHIRMA_LANG" = "ru" ] && RU=1
elif [ "$(defaults read -g AppleLanguages 2>/dev/null | sed -n 2p | tr -d ' ",' | cut -c1-2)" = "ru" ]; then
    RU=1
fi
L() { if [ "$RU" = 1 ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }

if pgrep -x OBS >/dev/null 2>&1; then
    read -r -p "$(L 'OBS is running. Press Enter to close it and continue' 'OBS запущен. Нажмите Enter, чтобы закрыть его и продолжить') " _ </dev/tty
    osascript -e 'tell application "OBS" to quit' >/dev/null 2>&1 || true
    for _ in $(seq 10); do pgrep -x OBS >/dev/null 2>&1 || break; sleep 1; done
    pkill -x OBS || true
fi
pkill -f "Shirma/app/shirma.py" || true  # значок в строке меню

rm -rf "$HOME/Applications/Shirma.app"
rm -f "$HOME/Library/LaunchAgents/com.github.aeamosov.shirma.plist"
rm -f "$OBS_CFG/basic/scenes/Shirma.json" "$OBS_CFG/basic/scenes/Shirma.json.bak"
rm -rf "$OBS_CFG/basic/profiles/Shirma"

if [ -d "$DATA/backgrounds" ]; then
    keep="$HOME/Pictures/Shirma backgrounds"
    mkdir -p "$keep"
    cp -R "$DATA/backgrounds/." "$keep/"
    echo "$(L 'Backgrounds saved to' 'Фоны сохранены в') $keep"
fi
rm -rf "$DATA"

[ "$REMOVE_PLUGIN" = 1 ] && rm -rf "$OBS_CFG/plugins/obs-backgroundremoval.plugin"
printf '\033[32m%s\033[0m\n' "$(L 'Shirma has been removed.' 'Shirma удалена.')"
