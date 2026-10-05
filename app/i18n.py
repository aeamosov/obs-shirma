"""Язык интерфейса: русский на русской Windows или macOS, иначе английский."""
import ctypes
import os
import subprocess
import sys

_LANG_RUSSIAN = 0x19


def _detect():
    forced = os.environ.get("SHIRMA_LANG", "").lower()
    if forced in ("ru", "en"):
        return forced
    if sys.platform == "darwin":
        # Первый язык из «Язык и регион»: строки вида    "ru-RU",
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return "en"
        langs = [x.strip(' ",()') for x in out.splitlines() if x.strip(' ",()')]
        return "ru" if langs and langs[0].lower().startswith("ru") else "en"
    try:
        return "ru" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == _LANG_RUSSIAN else "en"
    except (AttributeError, OSError):
        return "en"


LANG = _detect()


def t(ru: str, en: str) -> str:
    return ru if LANG == "ru" else en
