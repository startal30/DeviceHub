"""Windows microphone level and mute for a headset (Windows Core Audio, via pycaw).

The PRO X 2 has no mic-gain feature of its own (its features are 0000 0001 0100 0101 0104 010A 0108
0604 0602 0636 - no 0611 HEADSET_MIC_GAIN), so mic level is the Windows recording level, the same
slider as in Windows Sound settings.
"""
from __future__ import annotations

MIC_NAME = "PRO X 2"


def _endpoint_volume(name_part: str = MIC_NAME):
    import comtypes
    from pycaw.pycaw import AudioUtilities
    try:
        comtypes.CoInitialize()          # safe to call more than once per thread
    except OSError:
        pass
    for d in AudioUtilities.GetAllDevices():
        if (d.state.name == "Active" and name_part in (d.FriendlyName or "")
                and (d.FriendlyName or "").lower().startswith("microphone")):
            return d.EndpointVolume
    return None


def get_mic() -> tuple[int, bool] | None:
    """(level 0-100, muted) or None if the headset's microphone isn't present in Windows."""
    ev = _endpoint_volume()
    if ev is None:
        return None
    return round(ev.GetMasterVolumeLevelScalar() * 100), bool(ev.GetMute())


def set_mic_level(level: int):
    ev = _endpoint_volume()
    if ev is None:
        raise RuntimeError("headset microphone not found in Windows")
    ev.SetMasterVolumeLevelScalar(max(0, min(100, int(level))) / 100, None)


def set_mic_mute(muted: bool):
    ev = _endpoint_volume()
    if ev is None:
        raise RuntimeError("headset microphone not found in Windows")
    ev.SetMute(1 if muted else 0, None)


if __name__ == "__main__":
    print(get_mic())
