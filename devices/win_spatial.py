"""Windows Sonic for Headphones (Windows' built-in virtual surround) on/off for an audio output, without
administrator rights and without anything installed.

Windows' public API for this (Windows.Media.Audio.SpatialAudioDeviceConfiguration.
SetDefaultSpatialAudioFormatAsync) answers AccessDenied to every app except the one that owns the format,
even when run as administrator. Windows' Settings app instead does this itself (AudioHandlers.dll,
SpatialAudioDevicePropertyWriter / SpatialAudioStateIO, found by disassembling it on Windows 10 22H2):
it writes three endpoint properties through IPolicyConfig and then a change signal that makes the
audio engine reload them:
  {fd8a7b27-...},2  spatial encoder description (SetDeviceProperties)
  {908dba32-...},2  spatial device settings     (SetDeviceSettings)
  {8a845654-...},2  selected format             (SpatialAudioStateIO)
  {1e94c58f-...},2  change signal, VT_I2 0      (SignalChange)
The values below are what Settings wrote when Windows Sonic was switched on and off on a stereo output.
Writing them the same way switches Windows Sonic, which was confirmed with Windows' own read API
(ActiveSpatialAudioFormat) and the spatial engine (ISpatialAudioClient). set_sonic() verifies every
change the same way and raises if Windows didn't take it.

Windows only offers spatial sound on stereo outputs. The PRO X 2 with Logitech's audio driver is a 7.1
output (for DTS), so this applies to it only without Logitech's driver, where it's a stereo headset.
"""
from __future__ import annotations

import base64
import ctypes
import time
from ctypes import HRESULT, POINTER, c_uint32, c_void_p

from devices import win_policy

SONIC = "{b53d940c-b846-4831-9f76-d102b9b725a0}"
K_ENCODER = ("{fd8a7b27-0b18-4025-ab1c-bdd6b32e1604}", 2)
K_SETTINGS = ("{908dba32-edff-4c28-8e45-c918561f6748}", 2)
K_SELECTED = ("{8a845654-d6c3-4cd7-b4eb-243d4bd99032}", 2)
K_SIGNAL = ("{1e94c58f-3e40-4ddb-b10c-a86d8b870a31}", 2)

_SONIC_GUID_BYTES = bytes.fromhex("0c943db546b831489f76d102b9b725a0")
SONIC_ON = {
    K_ENCODER: base64.b64decode(
        "AQAAWgyUPbVGuDFIn3bRArm3JaD+/wMAAQAAAG8AAACgQAAAoEAAAKBAAAAAAAAAAAAAAAAAAADwQQAAtEIAAAdDAAA0QqRwX0Ku"
        "R/hCAACAPwAAgD8AAIA/AACAPwAAgD8AAIA/AACAPwAAgD+WQzs/vw4cPwAAgD+/Dhw/05/9PtOf/T4AAIA/AAAAAAAAAAA="),
    K_SETTINGS: base64.b64decode(
        "AQAAWgEAAAABAAAAAQAAAAyUPbVGuDFIn3bRArm3JaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAACAAAAAAQAAAA=="),
    K_SELECTED: bytes(8) + _SONIC_GUID_BYTES,
}
SONIC_OFF = {
    K_ENCODER: b"\x00",
    K_SETTINGS: base64.b64decode(
        "AQAAWgAAAAABAAAAAQAAAAyUPbVGuDFIn3bRArm3JaAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAAAAAAAAA=="),
    K_SELECTED: bytes(24),
}


class SpatialError(Exception):
    pass


def _device(dev_id: str):
    win_policy._com()
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetDeviceEnumerator().GetDevice(dev_id)


_ISpatialAudioClient = None


def _spatial_client(dev_id: str):
    global _ISpatialAudioClient
    from comtypes import CLSCTX_ALL, COMMETHOD, GUID, IUnknown
    if _ISpatialAudioClient is None:
        class ISpatialAudioClient(IUnknown):            # first methods only; enough for the count
            _iid_ = GUID("{bbf8e066-aaaa-49be-9a4d-fd2a858ea27f}")
            _methods_ = (
                COMMETHOD([], HRESULT, "GetStaticObjectPosition", (["in"], c_uint32), (["in"], c_void_p),
                          (["in"], c_void_p), (["in"], c_void_p)),
                COMMETHOD([], HRESULT, "GetNativeStaticObjectTypeMask", (["out"], POINTER(c_uint32))),
                COMMETHOD([], HRESULT, "GetMaxDynamicObjectCount", (["out"], POINTER(c_uint32))),
            )
        _ISpatialAudioClient = ISpatialAudioClient
    iface = _ISpatialAudioClient
    return _device(dev_id).Activate(iface._iid_, CLSCTX_ALL, None).QueryInterface(iface)


def supported(dev_id: str) -> bool:
    """Spatial sound is offered on stereo outputs only (what the apps' shared mix format says)."""
    from comtypes import CLSCTX_ALL
    from pycaw.api.audioclient import IAudioClient
    client = _device(dev_id).Activate(IAudioClient._iid_, CLSCTX_ALL, None).QueryInterface(IAudioClient)
    wf = client.GetMixFormat()
    try:
        return wf.contents.nChannels == 2
    finally:
        ctypes.windll.ole32.CoTaskMemFree(wf)


def is_on(dev_id: str) -> bool:
    """True while a spatial renderer (Windows Sonic) is active: it then offers dynamic sound objects."""
    return _spatial_client(dev_id).GetMaxDynamicObjectCount() > 0


def set_sonic(dev_id: str, on: bool, timeout_s: float = 3.0):
    values = SONIC_ON if on else SONIC_OFF
    writes = [(fmtid, pid, win_policy.VT_BLOB, data) for (fmtid, pid), data in values.items()]
    writes.append((*K_SIGNAL, win_policy.VT_I2, 0))
    win_policy.set_values(dev_id, writes)
    end = time.time() + timeout_s
    while time.time() < end:
        if is_on(dev_id) == bool(on):
            return
        time.sleep(0.2)
    raise SpatialError("Windows didn't switch Windows Sonic " + ("on" if on else "off"))
