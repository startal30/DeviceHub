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
             the ~70 other properties in that set are the voice chain settings and are left alone.
Reading needs no special rights. Writing needs administrator rights (both the HKLM key and endpoint
property stores are admin-only), so writes go through apply_elevated(), which shows a UAC prompt.
No Logitech or DTS code is copied: Device Hub only flips the same switches G HUB does.
"""
from __future__ import annotations

import ctypes
import os
import sys
import winreg

DEVICE_NAME = "PRO X 2"
GHUB_KEY = r"SOFTWARE\Logitech\GHUB\Audio Devices"
PK_BLUEVOICE_FMTID, PK_BLUEVOICE_PID = "{b4d50347-1450-477f-b173-56b14770589a}", 1
PK_SURROUND_FMTID, PK_SURROUND_PID = "{d57f93ae-4210-46b3-acfc-90437b666152}", 1
ROOM_MODELS = {0: "Entertainment", 1: "Gaming", 2: "Sports"}          # G HUB "Multi-channel Surround Mode"
STEREO_MODELS = {0: "Off", 1: "Front", 2: "Wide", 3: "Traditional"}   # G HUB "DTS Super Stereo Mode"


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


def apply_elevated(name: str, value: int, timeout_ms: int = 60_000):
    """Run this program elevated (UAC prompt) to write one setting, and wait for it to finish."""
    if is_admin():
        _write(name, value)
        return
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, f"--audiofx {name}={int(value)}"
    else:
        exe = sys.executable.replace("pythonw.exe", "python.exe")
        main = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "device_hub.py")
        args = f'"{main}" --audiofx {name}={int(value)}'

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
        _write(name, int(value))
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    print("effects attached:", effects_installed())
    print("state:", read_state())
