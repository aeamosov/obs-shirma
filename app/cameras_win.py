"""Поиск камер так, как их видит DirectShow (и OBS), — без открытия устройства.

Открывать камеру на этапе установки нельзя: корпоративные антивирусы
(например, Kaspersky Endpoint Security) отдают пустые кадры процессам,
запущенным из скриптов. Список подключённых устройств берём у cfgmgr32,
имена — из реестра; ни то ни другое камеру не трогает и прав администратора не требует.
"""
import ctypes
import uuid
import winreg
from ctypes import wintypes
from dataclasses import dataclass

_KSCATEGORY_CAPTURE = "{65e8773d-8f56-11d0-a3b9-00a0c9223196}"
_KSCATEGORY_VIDEO = "{6994ad05-93ef-11d0-a3cc-00a0c9223196}"
_CLASSES = r"SYSTEM\CurrentControlSet\Control\DeviceClasses"
_CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, s):
        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, 16)
        return g


def _present_interfaces(category):
    """Символьные ссылки подключённых сейчас интерфейсов категории."""
    cfg = ctypes.WinDLL("cfgmgr32")
    guid = _GUID.parse(category)
    size = wintypes.ULONG()
    if cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(guid), None,
                                               _CM_GET_DEVICE_INTERFACE_LIST_PRESENT):
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cfg.CM_Get_Device_Interface_ListW(ctypes.byref(guid), None, buf, size,
                                          _CM_GET_DEVICE_INTERFACE_LIST_PRESENT):
        return []
    return [s for s in ctypes.wstring_at(buf, size.value).split("\0") if s]


@dataclass
class Camera:
    name: str      # имя в DirectShow — именно его показывает OBS
    path: str      # \\?\usb#vid_...#{guid}\global
    instance: str  # USB#VID_...#...

    @property
    def obs_id(self) -> str:
        """Значение video_device_id для источника dshow_input.

        OBS кодирует «#» как «#22» и «:» как «#3A» в каждой части и соединяет
        их двоеточием. С сырым путём OBS считает устройство отсутствующим
        и не получает кадров.
        """
        enc = lambda s: s.replace("#", "#22").replace(":", "#3A")
        return f"{enc(self.name)}:{enc(self.path)}"

    @property
    def is_usb(self) -> bool:
        return self.instance.startswith("USB")


def _split(link):
    r"""\\?\USB#VID..#7&..#{guid}\GLOBAL -> ('USB#VID..#7&..', '{guid}', 'GLOBAL')."""
    body, _, ref = link[4:].partition("\\")
    inst, _, guid = body.rpartition("#")
    return inst.upper(), guid.lower(), ref


def _friendly_name(link):
    inst, guid, ref = _split(link)
    key = f"{_CLASSES}\\{guid}\\##?#{inst}#{guid}\\#{ref}\\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            return winreg.QueryValueEx(k, "FriendlyName")[0]
    except OSError:
        return None


def list_cameras():
    video = {_split(l)[0] for l in _present_interfaces(_KSCATEGORY_VIDEO)}
    cams = []
    for link in _present_interfaces(_KSCATEGORY_CAPTURE):
        inst = _split(link)[0]
        if inst not in video:
            continue  # микрофоны и прочие устройства захвата
        name = _friendly_name(link)
        if name:
            cams.append(Camera(name=name, path=link.lower(), instance=inst))
    # USB-камеры первыми: встроенные IR/IPU-сенсоры ноутбуков через DirectShow часто отдают чёрный кадр
    cams.sort(key=lambda c: (not c.is_usb, " ir" in f" {c.name.lower()}", c.name.lower()))
    return cams


if __name__ == "__main__":
    for c in list_cameras():
        print(f"{c.name}\t{c.instance}\t{c.obs_id}")
