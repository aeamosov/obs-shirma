"""Настройки камеры (яркость, контраст, зум, фокус…) через DirectShow.

Читает и записывает свойства IAMVideoProcAmp и IAMCameraControl устройства —
это управление драйвером камеры, кадры при этом не захватываются. Нужен, чтобы
Shirma запоминала настройки и возвращала их, когда камера включается по
требованию: часть камер сбрасывает значения при каждом открытии.
"""
import ctypes
from ctypes import HRESULT, POINTER, byref, c_long, c_void_p
from ctypes.wintypes import DWORD, LPCOLESTR, ULONG

import comtypes
import comtypes.client
from comtypes import COMMETHOD, GUID, IUnknown
from comtypes.automation import VARIANT

CLSID_SystemDeviceEnum = GUID("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")
CLSID_VideoInputDeviceCategory = GUID("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")

VIDEO_PROC_AMP = ["brightness", "contrast", "hue", "saturation", "sharpness", "gamma",
                  "color_enable", "white_balance", "backlight_compensation", "gain"]
CAMERA_CONTROL = ["pan", "tilt", "roll", "zoom", "exposure", "iris", "focus"]
FLAG_AUTO, FLAG_MANUAL = 1, 2


class IPropertyBag(IUnknown):
    _iid_ = GUID("{55272A00-42CB-11CE-8135-00AA004BB851}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Read", (["in"], LPCOLESTR, "name"), (["in", "out"], POINTER(VARIANT), "value"),
                  (["in"], c_void_p, "error_log")),
        COMMETHOD([], HRESULT, "Write", (["in"], LPCOLESTR, "name"), (["in"], POINTER(VARIANT), "value")),
    ]


class IMoniker(IUnknown):
    # Полная таблица методов: IPersist, IPersistStream, затем IMoniker — порядок важен
    _iid_ = GUID("{0000000F-0000-0000-C000-000000000046}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetClassID", (["out"], POINTER(GUID), "clsid")),
        COMMETHOD([], HRESULT, "IsDirty"),
        COMMETHOD([], HRESULT, "Load", (["in"], c_void_p, "stm")),
        COMMETHOD([], HRESULT, "Save", (["in"], c_void_p, "stm"), (["in"], ctypes.c_int, "clear_dirty")),
        COMMETHOD([], HRESULT, "GetSizeMax", (["out"], POINTER(ctypes.c_ulonglong), "size")),
        COMMETHOD([], HRESULT, "BindToObject", (["in"], c_void_p, "bc"), (["in"], c_void_p, "mk_to_left"),
                  (["in"], POINTER(GUID), "riid"), (["out"], POINTER(POINTER(IUnknown)), "result")),
        COMMETHOD([], HRESULT, "BindToStorage", (["in"], c_void_p, "bc"), (["in"], c_void_p, "mk_to_left"),
                  (["in"], POINTER(GUID), "riid"), (["out"], POINTER(POINTER(IUnknown)), "result")),
    ]


class IEnumMoniker(IUnknown):
    _iid_ = GUID("{00000102-0000-0000-C000-000000000046}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Next", (["in"], ULONG, "celt"), (["out"], POINTER(POINTER(IMoniker)), "moniker"),
                  (["out"], POINTER(ULONG), "fetched")),
    ]


class ICreateDevEnum(IUnknown):
    _iid_ = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
    _methods_ = [
        COMMETHOD([], HRESULT, "CreateClassEnumerator", (["in"], POINTER(GUID), "category"),
                  (["out"], POINTER(POINTER(IEnumMoniker)), "enum"), (["in"], DWORD, "flags")),
    ]


def _amp_methods():
    return [
        COMMETHOD([], HRESULT, "GetRange", (["in"], c_long, "prop"), (["out"], POINTER(c_long), "min"),
                  (["out"], POINTER(c_long), "max"), (["out"], POINTER(c_long), "step"),
                  (["out"], POINTER(c_long), "default"), (["out"], POINTER(c_long), "caps")),
        COMMETHOD([], HRESULT, "Set", (["in"], c_long, "prop"), (["in"], c_long, "value"), (["in"], c_long, "flags")),
        COMMETHOD([], HRESULT, "Get", (["in"], c_long, "prop"), (["out"], POINTER(c_long), "value"),
                  (["out"], POINTER(c_long), "flags")),
    ]


class IAMVideoProcAmp(IUnknown):
    _iid_ = GUID("{C6E13360-30AC-11d0-A18C-00A0C9118956}")
    _methods_ = _amp_methods()


class IAMCameraControl(IUnknown):
    _iid_ = GUID("{C6E13370-30AC-11d0-A18C-00A0C9118956}")
    _methods_ = _amp_methods()


IBASEFILTER_IID = GUID("{56a86895-0ad4-11ce-b03a-0020af0ba770}")


def _devices():
    """(имя, путь устройства, IMoniker) для всех видеоустройств DirectShow."""
    comtypes.CoInitialize()
    dev_enum = comtypes.client.CreateObject(CLSID_SystemDeviceEnum, interface=ICreateDevEnum)
    enum = dev_enum.CreateClassEnumerator(CLSID_VideoInputDeviceCategory, 0)
    if not enum:
        return
    while True:
        try:
            moniker, fetched = enum.Next(1)
        except comtypes.COMError:
            return
        if not fetched:
            return
        bag = moniker.BindToStorage(None, None, IPropertyBag._iid_).QueryInterface(IPropertyBag)

        def read(name):
            try:
                return bag.Read(name, VARIANT(), None)
            except comtypes.COMError:
                return None
        yield read("FriendlyName"), (read("DevicePath") or ""), moniker


def _controls(path):
    for name, dev_path, moniker in _devices():
        if dev_path and dev_path.lower() == path.lower():
            flt = moniker.BindToObject(None, None, IBASEFILTER_IID)
            out = []
            for iface, names in ((IAMVideoProcAmp, VIDEO_PROC_AMP), (IAMCameraControl, CAMERA_CONTROL)):
                try:
                    out.append((flt.QueryInterface(iface), names))
                except comtypes.COMError:
                    pass
            return out
    return []


def read_settings(path):
    """{свойство: {"value", "auto", "min", "max", "default"}} — только поддержанные камерой."""
    result = {}
    for ctl, names in _controls(path):
        for i, name in enumerate(names):
            try:
                lo, hi, _step, default, caps = ctl.GetRange(i)
                value, flags = ctl.Get(i)
            except comtypes.COMError:
                continue  # камера не поддерживает это свойство
            result[name] = {"value": value, "auto": bool(flags & FLAG_AUTO), "min": lo, "max": hi,
                            "default": default, "can_auto": bool(caps & FLAG_AUTO)}
    return result


def apply_settings(path, saved):
    """Вернуть сохранённые значения. Отличающиеся от текущих — только их, вне диапазона — пропускаем."""
    changed = []
    for ctl, names in _controls(path):
        for i, name in enumerate(names):
            want = saved.get(name)
            if not want:
                continue
            try:
                lo, hi, _step, _default, _caps = ctl.GetRange(i)
                value, flags = ctl.Get(i)
                auto = bool(flags & FLAG_AUTO)
                if want.get("auto"):
                    if not auto:
                        ctl.Set(i, value, FLAG_AUTO)
                        changed.append(name)
                elif lo <= want["value"] <= hi and (auto or value != want["value"]):
                    ctl.Set(i, want["value"], FLAG_MANUAL)
                    changed.append(name)
            except comtypes.COMError:
                continue
    return changed


if __name__ == "__main__":
    for name, path, _m in _devices():
        print(name, "|", path)
        if path:
            for k, v in read_settings(path).items():
                print(f"   {k:24s} {v}")
