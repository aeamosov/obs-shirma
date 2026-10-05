"""Камеры на macOS — без открытия устройства (экспериментально).

Список даёт system_profiler: имя и уникальный ID устройства — тот же, что
AVCaptureDevice.uniqueID, по которому выбирает камеру источник OBS «macos-avcapture».
system_profiler отвечает около секунды, а трей сверяет список каждые 2 с — поэтому
результат кешируем.
"""
import json
import subprocess
import time
from dataclasses import dataclass

_CACHE_SECONDS = 15
_cache = (0.0, [])


@dataclass
class Camera:
    name: str
    uid: str  # AVCaptureDevice.uniqueID

    @property
    def obs_id(self) -> str:
        return self.uid  # значение «device» источника macos-avcapture

    @property
    def path(self) -> str:
        return self.uid

    @property
    def is_usb(self) -> bool:
        """«Внешняя» камера — всё, кроме встроенной FaceTime (подпись «встроенная» в меню)."""
        n = self.name.lower()
        return not ("facetime" in n or "built-in" in n or "macbook" in n)


def list_cameras():
    global _cache
    if time.time() - _cache[0] < _CACHE_SECONDS:
        return list(_cache[1])
    try:
        out = subprocess.run(["system_profiler", "SPCameraDataType", "-json"],
                             capture_output=True, timeout=30).stdout
        items = json.loads(out or b"{}").get("SPCameraDataType", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        items = []
    cams = [Camera(name=d.get("_name", "Camera"), uid=d["spcamera_unique-id"])
            for d in items if d.get("spcamera_unique-id")]
    # Внешние первыми — как на Windows: если подключили камеру, скорее всего, её и хотят
    cams.sort(key=lambda c: (not c.is_usb, c.name.lower()))
    _cache = (time.time(), cams)
    return list(cams)
