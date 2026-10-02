"""Язык интерфейса: русский на русской Windows, иначе английский."""
import ctypes
import os

_LANG_RUSSIAN = 0x19


def _detect():
    forced = os.environ.get("SHIRMA_LANG", "").lower()
    if forced in ("ru", "en"):
        return forced
    try:
        return "ru" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == _LANG_RUSSIAN else "en"
    except (AttributeError, OSError):
        return "en"


LANG = _detect()


def t(ru: str, en: str) -> str:
    return ru if LANG == "ru" else en
