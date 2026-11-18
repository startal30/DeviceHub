"""AULA F99 Pro keyboard battery: USB cable, Bluetooth and 2.4GHz dongle.

Protocol from https://github.com/xx12ek/aula-f99-pro-hub (MIT), docs/PROTOCOL.md:
  2.4G dongle  VID 3554 / PID FA09  vendor interface 0xFF02/0x02, 20-byte reports, id 0x13,
               cmd 0x4A = GET_BATTERY -> [percent, flags]
  Bluetooth    VID 3554 / PID FA08  same interface and protocol (verified on a real F99 Pro)
  USB cable    VID 258A / PID 010C  vendor interface 0xFF00/0x01, 520-byte feature report id 6,
               cmd 0x87 param 2 -> [percent, flags]
  flags & 0x10 -> powered by cable (charging)
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import hid

VID_WIRELESS, PID_WIRELESS = 0x3554, 0xFA09   # 2.4G dongle
PID_BLUETOOTH = 0xFA08                        # keyboard over Bluetooth (same VID)
VID_WIRED, PID_WIRED = 0x258A, 0x010C
WIRELESS_LEN = 20
WIRED_LEN = 520
WIRED_HEADER = 8


class ReadError(Exception):
    pass


@dataclass
class Reading:
    percent: int
    charging: bool
    source: str  # "2.4G", "USB", "Bluetooth"


# --------------------------------------------------------------------------- 2.4G dongle / Bluetooth

def _wireless_packet(cmd: int) -> bytes:
    p = bytearray(WIRELESS_LEN)
    p[0] = 0x13          # report id
    p[1] = cmd
    p[2] = 1             # total packets
    p[3] = 0             # packet index
    p[4] = 0             # (op << 4) | data length -> op 0 = read, no data
    p[19] = sum(p[:19]) & 0xFF
    return bytes(p)


def _read_vendor_battery(pid: int, source: str, no_answer: str) -> Reading | None:
    """Ask the keyboard for its battery over the 20-byte vendor protocol.

    Returns None if the device isn't present. Raises ReadError if it's present but doesn't answer.
    """
    paths = [d["path"] for d in hid.enumerate(VID_WIRELESS, pid)
             if d.get("usage_page") == 0xFF02 and d.get("usage") == 0x0002]
    if not paths:
        return None
    dev = hid.device()
    dev.open_path(paths[0])
    try:
        dev.set_nonblocking(1)
        while dev.read(64, timeout_ms=30):   # drop stale packets
            pass
        cmd = 0x4A   # GET_BATTERY
        for attempt in range(2):
            dev.write(_wireless_packet(cmd))
            deadline = time.time() + 1.5
            while time.time() < deadline:
                r = dev.read(64, timeout_ms=300)
                if not r or len(r) < WIRELESS_LEN or r[0] != 0x13:
                    continue
                if (r[1] & 0x7F) != cmd or r[19] != (sum(r[:19]) & 0xFF):
                    continue
                if r[1] & 0x80:
                    raise ReadError("keyboard rejected the battery request")
                n = r[4] & 0x0F
                data = bytes(r[5:5 + n])
                if len(data) >= 2 and 0 <= data[0] <= 100:
                    return Reading(data[0], bool(data[1] & 0x10), source)
        raise ReadError(no_answer)
    finally:
        dev.close()


def read_wireless() -> Reading | None:
    return _read_vendor_battery(PID_WIRELESS, "2.4G",
                                "dongle found, but the keyboard didn't answer (asleep, off, or in Bluetooth mode)")


def read_bluetooth() -> Reading | None:
    # Windows only lists this device while the keyboard is connected over Bluetooth.
    return _read_vendor_battery(PID_BLUETOOTH, "Bluetooth",
                                "Bluetooth keyboard found, but it didn't answer (asleep?)")


# --------------------------------------------------------------------------- USB cable

def read_wired() -> Reading | None:
    paths = [d["path"] for d in hid.enumerate(VID_WIRED, PID_WIRED)
             if d.get("usage_page") == 0xFF00 and d.get("usage") == 0x0001]
    if not paths:
        return None
    last_err = None
    for path in paths:
        dev = hid.device()
        try:
            dev.open_path(path)
            buf = bytearray(WIRED_LEN)
            buf[0] = 6        # report id
            buf[1] = 0x87     # GET_POWER
            buf[2] = 0x02
            buf[4] = 1        # total packets
            buf[5] = 0        # packet index
            buf[6] = 2        # wanted length (LE)
            for attempt in range(3):
                try:
                    dev.send_feature_report(bytes(buf))
                    time.sleep(0.04)
                    rep = dev.get_feature_report(6, WIRED_LEN)
                    if rep and len(rep) > WIRED_HEADER + 1 and rep[1] == 0x87:
                        pct, flag = rep[WIRED_HEADER], rep[WIRED_HEADER + 1]
                        if 0 <= pct <= 100:
                            return Reading(pct, bool(flag & 0x10), "USB")
                except (OSError, ValueError) as e:
                    last_err = e
                time.sleep(0.08)
        except (OSError, IOError) as e:
            last_err = e
        finally:
            dev.close()
    raise ReadError(f"cable connected, but battery read failed ({last_err or 'no valid answer'})")


# --------------------------------------------------------------------------- combined

READERS = (("USB cable", read_wired), ("Bluetooth", read_bluetooth), ("2.4G dongle", read_wireless))


def read_battery() -> tuple[Reading | None, str]:
    """Returns (reading, status message). Tries cable, then Bluetooth, then dongle.

    Bluetooth goes before the dongle: its device only exists while connected, so it fails fast,
    whereas a plugged-in dongle with the keyboard elsewhere waits ~3 s for an answer.
    """
    problems = []
    for name, fn in READERS:
        try:
            r = fn()
            if r:
                return r, ""
        except ReadError as e:
            problems.append(str(e))
        except Exception as e:  # hid errors, device unplugged mid-read, ...
            problems.append(f"{name}: {e}")
    return None, problems[0] if problems else "keyboard not found"
