"""PRO X 2 DTS Headphone:X 2.0 surround and Blue VO!CE, via Logitech's own installed audio effects.

The effects are Windows audio processing objects (APOs) from Logitech's driver packages, run by the
Windows audio engine, not by G HUB:
  speakers   "Logitech HX2E Surround Sound Effect"  logi_audio_hx2e_render_apo.dll
  microphone "Logitech Blue VO!CE 2.0 Effect"        logi_audio_dts_clearvoice_2_capture_apo.dll
G HUB only stores settings where the effects read them. Found by recording what G HUB changes when
each switch is flipped (on a real PRO X 2, G HUB 2026.7):
  Surround   HKLM\\SOFTWARE\\Logitech\\GHUB\\Audio Devices\\<speaker endpoint id>
               DTSon = 1 / 0, SelectedSurround = 0 (DTS) / 2 (off), DTSroomModel, DTSstereoModel
             then speaker endpoint property {d57f93ae-4210-46b3-acfc-90437b666152},1 is written
             (value 0), which notifies the effect to re-read its settings.
  Blue VO!CE microphone endpoint property {b4d50347-1450-477f-b173-56b14770589a},1 = 1 / 0 (VT_I4);
             the other properties in that set are the voice chain settings (VT_R4). Applying a G HUB
             preset writes 40 of them (VOICE_FIELDS). Checked on a real PRO X 2: after G HUB applied
             two different presets, all 40 matched the preset's values, nothing else in the set
             changed, and a field missing from a preset was written as 0. The preset's "inputGain"
             is never written by G HUB, so it isn't here either.
Reading needs no special rights. Writing needs administrator rights (both the HKLM key and endpoint
property stores are admin-only), so writes go through apply_elevated(), which shows a UAC prompt.
No Logitech or DTS code is copied: Device Hub only flips the same switches G HUB does.
"""
from __future__ import annotations

import ctypes
import math
import os
import struct
import sys
import winreg

DEVICE_NAME = "PRO X 2"
GHUB_KEY = r"SOFTWARE\Logitech\GHUB\Audio Devices"
PK_BLUEVOICE_FMTID, PK_BLUEVOICE_PID = "{b4d50347-1450-477f-b173-56b14770589a}", 1
PK_SURROUND_FMTID, PK_SURROUND_PID = "{d57f93ae-4210-46b3-acfc-90437b666152}", 1
ROOM_MODELS = {0: "Entertainment", 1: "Gaming", 2: "Sports"}          # G HUB "Multi-channel Surround Mode"
STEREO_MODELS = {0: "Off", 1: "Front", 2: "Wide", 3: "Traditional"}   # G HUB "DTS Super Stereo Mode"

# G HUB Blue VO!CE preset field -> property id in the {b4d50347-...} set of the microphone endpoint.
VOICE_FIELDS = {
    "volume": 3,
    "highPassFilter.frequency": 101,
    "noiseReduction.release": 200, "noiseReduction.sensitivity": 201, "noiseReduction.bias": 202,
    "noiseReduction.maxAttenuation": 203,
    "gatingCompressor.noiseGateThreshold": 300, "gatingCompressor.noiseGateMaxAttenuation": 301,
    "gatingCompressor.noiseGateAttack": 302, "gatingCompressor.noiseGateRelease": 303,
    "gatingCompressor.noiseGateHoldTime": 304, "gatingCompressor.compressorThreshold": 305,
    "gatingCompressor.compressorRatio": 306, "gatingCompressor.compressorPreGain": 307,
    "gatingCompressor.compressorPostGain": 308, "gatingCompressor.compressorAttack": 309,
    "gatingCompressor.compressorRelease": 310,
    "multiBandEq.lowBandFilter.frequency": 404, "multiBandEq.lowBandFilter.q": 405,
    "multiBandEq.lowBandFilter.gain": 406,
    "multiBandEq.midBandFilter.frequency": 407, "multiBandEq.midBandFilter.q": 408,
    "multiBandEq.midBandFilter.gain": 409,
    "multiBandEq.highBandFilter.frequency": 410, "multiBandEq.highBandFilter.q": 411,
    "multiBandEq.highBandFilter.gain": 412,
    "dePopper.threshold": 500, "dePopper.frequency": 502, "dePopper.width": 503,
    "dePopper.maxAttenuation": 504, "dePopper.release": 506,
    "deEsser.threshold": 501, "deEsser.frequency": 508, "deEsser.width": 509,
    "deEsser.maxAttenuation": 510, "deEsser.attack": 511, "deEsser.release": 512,
    "hardLimiter.attack": 900, "hardLimiter.release": 901, "hardLimiter.boost": 902,
}
VOICE_PIDS = frozenset(VOICE_FIELDS.values())


class FxError(Exception):
    pass


def _com():
    import comtypes
    try:
        comtypes.CoInitialize()
    except OSError:
        pass


def endpoint_id(kind: str) -> str | None:
    """kind = 'Speakers' or 'Microphone' -> '{0.0.x.00000000}.{guid}' of the PRO X 2 endpoint."""
    _com()
    from pycaw.pycaw import AudioUtilities
    for d in AudioUtilities.GetAllDevices():
        name = d.FriendlyName or ""
        if d.state.name == "Active" and DEVICE_NAME in name and name.lower().startswith(kind.lower()):
            return d.id
    return None


def _store(dev_id: str, write: bool):
    _com()
    from pycaw.pycaw import AudioUtilities
    dev = AudioUtilities.GetDeviceEnumerator().GetDevice(dev_id)
    return dev.OpenPropertyStore(2 if write else 0)       # STGM_READWRITE / STGM_READ


def _key(fmtid: str, pid: int):
    from comtypes import GUID
    from pycaw.api.mmdeviceapi.depend import PROPERTYKEY
    k = PROPERTYKEY()
    k.fmtid = GUID(fmtid)
    k.pid = pid
    return k


def _get_int(dev_id: str, fmtid: str, pid: int) -> int | None:
    pv = _store(dev_id, False).GetValue(ctypes.byref(_key(fmtid, pid)))
    if pv.vt in (3, 19):                                    # VT_I4 / VT_UI4
        return int(pv.union.lVal)
    return None


def _set_int(dev_id: str, fmtid: str, pid: int, value: int):
    from pycaw.api.mmdeviceapi.depend import PROPVARIANT
    pv = PROPVARIANT()
    pv.vt = 3                                               # VT_I4
    pv.union.lVal = int(value)
    store = _store(dev_id, True)
    store.SetValue(ctypes.byref(_key(fmtid, pid)), ctypes.byref(pv))
    store.Commit()


# pycaw's PROPVARIANT union has no float member, so VT_R4 values travel as the bits of lVal.
def _f32_to_bits(v: float) -> int:
    return struct.unpack("<i", struct.pack("<f", v))[0]


def _bits_to_f32(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<i", bits))[0]


def _get_floats(dev_id: str, fmtid: str, pids) -> dict[int, float]:
    store = _store(dev_id, False)
    out = {}
    for pid in pids:
        pv = store.GetValue(ctypes.byref(_key(fmtid, pid)))
        if pv.vt == 4:                                      # VT_R4
            out[pid] = _bits_to_f32(pv.union.lVal)
    return out


def _set_floats(dev_id: str, fmtid: str, values: dict[int, float]):
    from pycaw.api.mmdeviceapi.depend import PROPVARIANT
    store = _store(dev_id, True)
    for pid, v in sorted(values.items()):
        pv = PROPVARIANT()
        pv.vt = 4                                           # VT_R4
        pv.union.lVal = _f32_to_bits(v)
        store.SetValue(ctypes.byref(_key(fmtid, pid)), ctypes.byref(pv))
    store.Commit()


# ---------------------------------------------------------------------------------------------- Blue VO!CE presets

def voice_values(settings: dict) -> dict[int, float]:
    """A G HUB Blue VO!CE preset ("blueVoice2Settings.settings") -> {property id: value}.
    A field the preset leaves out is written as 0, as G HUB does."""
    out = {}
    for path, pid in VOICE_FIELDS.items():
        v = settings
        for part in path.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        out[pid] = float(v) if ok else 0.0
    return out


def voice_mic_level(settings: dict) -> int:
    """A preset's "inputGain" (0.0 - 1.0) as a Windows recording level in %. G HUB doesn't send it to the
    effect: it sets the mic endpoint's level (seen on a real PRO X 2: inputGain 0.5 -> level 50%)."""
    g = settings.get("inputGain")
    if not isinstance(g, (int, float)) or isinstance(g, bool) or not math.isfinite(g):
        return 100
    return max(0, min(100, round(g * 100)))


def _check_voice(values: dict) -> dict[int, float]:
    vals = {int(p): float(v) for p, v in values.items()}
    if set(vals) != VOICE_PIDS or not all(math.isfinite(v) and abs(v) < 1e6 for v in vals.values()):
        raise FxError("bad Blue VO!CE preset")
    return vals


def read_voice() -> dict[int, float]:
    """The mic's current Blue VO!CE chain settings {property id: value}; {} if not available."""
    mic = endpoint_id("Microphone")
    if not mic:
        return {}
    return _get_floats(mic, PK_BLUEVOICE_FMTID, sorted(VOICE_PIDS))


def match_voice_preset(current: dict, presets: dict) -> str | None:
    """Name of the preset ({name: {pid: value}}) the mic is set to now, if any."""
    if not current:
        return None
    for name, vals in presets.items():
        vals = {int(p): v for p, v in vals.items()}
        if vals and all(p in current and abs(current[p] - v) < 1e-3 * max(1.0, abs(v)) for p, v in vals.items()):
            return name
    return None


def _encode_voice(values: dict) -> str:
    return ",".join(f"{p}:{float(v)!r}" for p, v in sorted(_check_voice(values).items()))


def _decode_voice(text: str) -> dict[int, float]:
    return _check_voice(dict((int(p), float(v)) for p, v in (item.split(":") for item in text.split(","))))


# ---------------------------------------------------------------------------------------------- read

def read_state() -> dict:
    """{'surround': bool, 'room': int, 'stereo': int, 'bluevoice': bool}; missing keys = not available."""
    out: dict = {}
    spk = endpoint_id("Speakers")
    if spk:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, GHUB_KEY + "\\" + spk) as k:
                out["surround"] = bool(winreg.QueryValueEx(k, "DTSon")[0])
                for name, field in (("DTSroomModel", "room"), ("DTSstereoModel", "stereo")):
                    try:
                        out[field] = int(winreg.QueryValueEx(k, name)[0])
                    except OSError:
                        pass
        except OSError:
            pass
    mic = endpoint_id("Microphone")
    if mic:
        v = _get_int(mic, PK_BLUEVOICE_FMTID, PK_BLUEVOICE_PID)
        if v is not None:
            out["bluevoice"] = bool(v)
    return out


def effects_installed() -> dict:
    """Which Logitech effects Windows has attached to the PRO X 2 endpoints."""
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
    found = {}
    for kind, flow, label in (("Speakers", "Render", "surround"), ("Microphone", "Capture", "bluevoice")):
        ep = endpoint_id(kind)
        if not ep:
            continue
        guid = ep.split(".")[-1]
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{flow}\\{guid}\\FxProperties") as k:
                name = winreg.QueryValueEx(k, "{b725f130-47ef-101a-a5f1-02608c9eebac},10")[0]
                found[label] = name
        except OSError:
            pass
    return found


# ---------------------------------------------------------------------------------------------- write (admin)

def _write(name: str, value: int):
    if name == "surround":
        spk = endpoint_id("Speakers")
        if not spk:
            raise FxError("PRO X 2 speakers not found")
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, GHUB_KEY + "\\" + spk, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, "DTSon", 0, winreg.REG_DWORD, 1 if value else 0)
            winreg.SetValueEx(k, "SelectedSurround", 0, winreg.REG_DWORD, 0 if value else 2)
        _set_int(spk, PK_SURROUND_FMTID, PK_SURROUND_PID, 0)        # notify the effect, as G HUB does
    elif name in ("room", "stereo"):
        table = ROOM_MODELS if name == "room" else STEREO_MODELS
        if int(value) not in table:
            raise FxError(f"bad {name} value {value}")
        spk = endpoint_id("Speakers")
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, GHUB_KEY + "\\" + spk, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, "DTSroomModel" if name == "room" else "DTSstereoModel", 0,
                              winreg.REG_DWORD, int(value))
        _set_int(spk, PK_SURROUND_FMTID, PK_SURROUND_PID, 0)
    elif name == "bluevoice":
        mic = endpoint_id("Microphone")
        if not mic:
            raise FxError("PRO X 2 microphone not found")
        _set_int(mic, PK_BLUEVOICE_FMTID, PK_BLUEVOICE_PID, 1 if value else 0)
    elif name == "voice":                                   # value = {property id: value}, a whole preset
        mic = endpoint_id("Microphone")
        if not mic:
            raise FxError("PRO X 2 microphone not found")
        _set_floats(mic, PK_BLUEVOICE_FMTID, _check_voice(value))
    else:
        raise FxError(f"unknown setting {name}")


SURROUND_VALUES = {"surround": None, "room": ROOM_MODELS, "stereo": STEREO_MODELS}


def _surround_values(name: str, value: int) -> dict:
    if name == "surround":
        return {"DTSon": 1 if value else 0, "SelectedSurround": 0 if value else 2}
    table = SURROUND_VALUES[name]
    if int(value) not in table:
        raise FxError(f"bad {name} value {value}")
    return {"DTSroomModel" if name == "room" else "DTSstereoModel": int(value)}


def surround_writable() -> bool:
    """True if tools/allow-surround-without-prompt.ps1 was run, so surround changes need no prompt."""
    spk = endpoint_id("Speakers")
    if not spk:
        return False
    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, GHUB_KEY + "\\" + spk, 0,
                       winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY).Close()
        return True
    except OSError:
        return False


def write_without_prompt(name: str, value: int) -> bool:
    """Surround settings with a plain registry write. The DTS effect watches its key and applies changes
    live (verified on a real PRO X 2: Super Stereo Wide -> Off was audible immediately, no notify write).
    Returns False if this account isn't allowed to write the key (then use apply_elevated)."""
    if name not in SURROUND_VALUES:
        return False
    spk = endpoint_id("Speakers")
    if not spk:
        raise FxError("PRO X 2 speakers not found")
    values = _surround_values(name, value)
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, GHUB_KEY + "\\" + spk, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            for reg_name, v in values.items():
                winreg.SetValueEx(k, reg_name, 0, winreg.REG_DWORD, v)
        return True
    except PermissionError:
        return False


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def helper_arg(name: str, value) -> str:
    """The NAME=VALUE argument for the elevated helper. A Blue VO!CE preset goes as 'voice=pid:value,...'."""
    return f"voice={_encode_voice(value)}" if name == "voice" else f"{name}={int(value)}"


def apply_elevated(name: str, value, timeout_ms: int = 60_000):
    """Run this program elevated (UAC prompt) to write one setting, and wait for it to finish."""
    if is_admin():
        _write(name, value)
        return
    arg = helper_arg(name, value)
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, f"--audiofx {arg}"
    else:
        exe = sys.executable.replace("pythonw.exe", "python.exe")
        main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "device_hub.py")
        args = f'"{main}" --audiofx {arg}'

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                    ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                    ("dwHotKey", ctypes.c_ulong), ("hIcon", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040                                  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile, info.lpParameters, info.nShow = "runas", exe, args, 0
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise FxError("administrator permission was not given")
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject(ctypes.c_void_p(info.hProcess), timeout_ms)
    code = ctypes.c_ulong(1)
    k32.GetExitCodeProcess(ctypes.c_void_p(info.hProcess), ctypes.byref(code))
    k32.CloseHandle(ctypes.c_void_p(info.hProcess))
    if code.value != 0:
        raise FxError(f"helper failed (exit code {code.value})")


def run_helper(arg: str) -> int:
    """Entry point for the elevated helper: '--audiofx name=value'. Returns a process exit code."""
    try:
        name, value = arg.split("=", 1)
        _write(name, _decode_voice(value) if name == "voice" else int(value))
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    print("effects attached:", effects_installed())
    print("state:", read_state())
