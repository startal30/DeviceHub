"""Change audio endpoint properties without administrator rights, the way Windows' own Sound settings do.

Windows' IPolicyConfig interface (CPolicyConfigClient) passes property writes to the Windows audio service,
which stores them and notifies the audio engine. Opening the endpoint's property store for writing
(IPropertyStore) needs administrator rights instead. The interface is undocumented but has kept this
layout since Windows 7; Windows 10's Settings handler (AudioHandlers.dll) calls SetPropertyValue at
vtable slot 12 with (endpoint id, 0 = Properties / 1 = FxProperties, key, value) for its spatial sound
writes (found by disassembling it; see devices/win_spatial.py).
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import HRESULT, POINTER, c_int, c_void_p, c_wchar_p

VT_I2, VT_I4, VT_R4, VT_BLOB = 2, 3, 4, 0x41


def _com():
    import comtypes
    try:
        comtypes.CoInitialize()
    except OSError:
        pass


_iface = None


def _interface():
    global _iface
    if _iface is None:
        from comtypes import COMMETHOD, GUID, IUnknown
        from pycaw.api.mmdeviceapi.depend import PROPERTYKEY, PROPVARIANT

        class IPolicyConfig(IUnknown):
            _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
            _methods_ = (
                COMMETHOD([], HRESULT, "GetMixFormat", (["in"], c_wchar_p), (["in"], c_void_p)),
                COMMETHOD([], HRESULT, "GetDeviceFormat", (["in"], c_wchar_p), (["in"], c_int), (["in"], c_void_p)),
                COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], c_wchar_p)),
                COMMETHOD([], HRESULT, "SetDeviceFormat", (["in"], c_wchar_p), (["in"], c_void_p), (["in"], c_void_p)),
                COMMETHOD([], HRESULT, "GetProcessingPeriod", (["in"], c_wchar_p), (["in"], c_int),
                          (["in"], c_void_p), (["in"], c_void_p)),
                COMMETHOD([], HRESULT, "SetProcessingPeriod", (["in"], c_wchar_p), (["in"], c_void_p)),
                COMMETHOD([], HRESULT, "GetShareMode", (["in"], c_wchar_p), (["in"], c_void_p)),
                COMMETHOD([], HRESULT, "SetShareMode", (["in"], c_wchar_p), (["in"], c_void_p)),
                COMMETHOD([], HRESULT, "GetPropertyValue", (["in"], c_wchar_p), (["in"], c_int),
                          (["in"], POINTER(PROPERTYKEY)), (["in"], POINTER(PROPVARIANT))),
                COMMETHOD([], HRESULT, "SetPropertyValue", (["in"], c_wchar_p), (["in"], c_int),
                          (["in"], POINTER(PROPERTYKEY)), (["in"], POINTER(PROPVARIANT))),
            )
        _iface = IPolicyConfig
    return _iface


def _client():
    import comtypes
    from comtypes import GUID
    _com()
    return comtypes.CoCreateInstance(GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"), _interface(),
                                     comtypes.CLSCTX_ALL)


def _key(fmtid: str, pid: int):
    from comtypes import GUID
    from pycaw.api.mmdeviceapi.depend import PROPERTYKEY
    k = PROPERTYKEY()
    k.fmtid = GUID(fmtid)
    k.pid = pid
    return k


def _variant(vt: int, value):
    """A PROPVARIANT, plus the buffer a VT_BLOB points at (keep it alive while the variant is used)."""
    from pycaw.api.mmdeviceapi.depend import PROPVARIANT
    pv = PROPVARIANT()
    base = ctypes.addressof(pv)
    ctypes.memset(base, 0, ctypes.sizeof(pv))
    ctypes.c_ushort.from_address(base).value = vt
    buf = None
    if vt == VT_BLOB:
        data = bytes(value)
        buf = ctypes.create_string_buffer(data, max(1, len(data)))
        ctypes.c_ulong.from_address(base + 8).value = len(data)
        ctypes.c_void_p.from_address(base + 16).value = ctypes.addressof(buf)
    elif vt == VT_I2:
        ctypes.c_short.from_address(base + 8).value = int(value)
    elif vt == VT_I4:
        ctypes.c_long.from_address(base + 8).value = int(value)
    elif vt == VT_R4:
        ctypes.c_float.from_address(base + 8).value = float(value)
    else:
        raise ValueError(f"unsupported VARTYPE {vt}")
    return pv, buf


def set_values(dev_id: str, values: list[tuple[str, int, int, object]], fx: bool = False):
    """Write [(fmtid, pid, VARTYPE, value), ...] to one endpoint, in order. Raises on the first failure."""
    client = _client()
    for fmtid, pid, vt, value in values:
        pv, buf = _variant(vt, value)
        client.SetPropertyValue(dev_id, int(fx), ctypes.byref(_key(fmtid, pid)), ctypes.byref(pv))
        del buf


def get_value(dev_id: str, fmtid: str, pid: int, fx: bool = False):
    """(VARTYPE, value) of one endpoint property; value is None for a type this module doesn't decode."""
    from pycaw.api.mmdeviceapi.depend import PROPVARIANT
    pv = PROPVARIANT()
    _client().GetPropertyValue(dev_id, int(fx), ctypes.byref(_key(fmtid, pid)), ctypes.byref(pv))
    try:
        raw = ctypes.string_at(ctypes.addressof(pv), ctypes.sizeof(pv))
        vt = struct.unpack_from("<H", raw)[0]
        if vt == VT_I2:
            return vt, struct.unpack_from("<h", raw, 8)[0]
        if vt == VT_I4:
            return vt, struct.unpack_from("<i", raw, 8)[0]
        if vt == VT_R4:
            return vt, struct.unpack_from("<f", raw, 8)[0]
        if vt == VT_BLOB:
            size = struct.unpack_from("<I", raw, 8)[0]
            ptr = struct.unpack_from("<Q", raw, 16)[0]
            return vt, ctypes.string_at(ptr, size) if ptr and size else b""
        return vt, None
    finally:
        ctypes.windll.ole32.PropVariantClear(ctypes.byref(pv))
