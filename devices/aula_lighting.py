"""AULA F99 Pro lighting and sleep timer (the keyboard's 128-byte "config page").

Ported from aula-f99-pro-hub (MIT): core/aula.py, core/misc.py and docs/PROTOCOL.md section 4.
  0x09 custom-colour flag (1 = per-key colour table)   0x0A effect number (0 = off, 1..19)
  0x18 sleep timer step                                 0x3A.. 2 bytes per effect:
                                                             [brightness 0..9, speed<<4 | colour]
  0x75 brightness for the custom effect                 0x7E 0x7F end marker 5A A5
Read: wired cmd 0x84 / wireless cmd 0x44.   Write: cmd 0x04.
Writing the page works over the dongle and Bluetooth too; per-key colours, key remapping and
macros need the USB cable.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import hid

from devices.aula import (PID_BLUETOOTH, PID_WIRED, PID_WIRELESS, VID_WIRED, VID_WIRELESS, WIRED_HEADER,
                          WIRED_LEN, WIRELESS_LEN, ReadError)

PAGE_LEN = 128
OFF_CUSTOM, OFF_EFFECT, OFF_SLEEP, OFF_TABLE, OFF_CUSTOM_BRIGHT = 0x09, 0x0A, 0x18, 0x3A, 0x75
BRIGHT_MAX, SPEED_MAX = 9, 4
EFFECT_CUSTOM = 19

# Translated from AULA's own effect list (tc_kb_led1..19); key = value stored at 0x0A
EFFECT_NAMES = {
    0: "Off", 1: "Static", 2: "Breathing", 3: "Rainbow", 4: "Reactive", 5: "Raindrops",
    6: "Wheel", 7: "Ripple", 8: "Starlight", 9: "Snow trail", 10: "Stream",
    11: "Drift", 12: "Shadow trail", 13: "Sine wave", 14: "Scan", 15: "Windmill",
    16: "Waterfall", 17: "Blossom", 18: "Storm", 19: "Custom",
}
COLOR_NAMES = ["Red", "Green", "Blue", "Yellow", "Magenta", "Cyan", "White", "Random"]
COLOR_HEX = ["#ff3b30", "#34c759", "#0a84ff", "#ffd60a", "#ff2d92", "#32d5f5", "#f2f5fa", ""]
SLEEP_SECONDS = [60, 90, 120, 180, 240, 300, 600, 900, 1200]
SLEEP_HW = [2, 3, 4, 6, 8, 10, 20, 30, 40]          # the only values the firmware accepts


def page_ok(page) -> bool:
    return len(page) >= PAGE_LEN and page[PAGE_LEN - 2] == 0x5A and page[PAGE_LEN - 1] == 0xA5


class WirelessLink:
    """Dongle or Bluetooth: 20-byte reports [0x13, cmd, total, index, op<<4|len, data(14)..., checksum]."""

    def __init__(self, path):
        self.dev = hid.device()
        self.dev.open_path(path)
        self.dev.set_nonblocking(1)

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass

    @staticmethod
    def packet(cmd, op, data, total, index) -> bytes:
        p = bytearray(WIRELESS_LEN)
        p[0], p[1], p[2], p[3], p[4] = 0x13, cmd, total, index, (op << 4) | len(data)
        p[5:5 + len(data)] = data
        p[19] = sum(p[:19]) & 0xFF
        return bytes(p)

    def _drain(self):
        while self.dev.read(64, timeout_ms=30):
            pass

    def read(self, cmd: int, timeout: float = 1.8) -> bytes:
        self._drain()
        self.dev.write(self.packet(cmd, 0, b"", 1, 0))
        deadline, chunks, total = time.time() + timeout, {}, 0
        while time.time() < deadline:
            r = self.dev.read(64, timeout_ms=300)
            if not r or len(r) < WIRELESS_LEN or r[0] != 0x13:
                continue
            if (r[1] & 0x7F) != (cmd & 0x7F) or r[19] != (sum(r[:19]) & 0xFF):
                continue
            if r[1] & 0x80:
                raise ReadError("keyboard rejected the request")
            total = r[2] or total
            chunks[r[3]] = bytes(r[5:5 + (r[4] & 0x0F)])
            if total and len(chunks) == total:
                return b"".join(chunks[i] for i in range(total))
        if chunks:
            # Never hand back a page with holes in it - writing one back could scramble settings.
            missing = sorted(set(range(total)) - set(chunks)) if total else "?"
            raise ReadError(f"incomplete reply from the keyboard (missing packets {missing})")
        raise ReadError("keyboard didn't answer (asleep?)")

    def write(self, cmd: int, data: bytes):
        self._drain()
        total = max(1, (len(data) + 13) // 14)
        for i in range(total):
            self.dev.write(self.packet(cmd, 1, data[i * 14:(i + 1) * 14], total, i))
            time.sleep(0.02)
        time.sleep(0.05)

    def read_page(self) -> bytes:
        return self.read(0x44)[:PAGE_LEN]

    def write_page(self, page: bytes):
        self.write(0x04, page)


class WiredLink:
    """USB cable: 520-byte feature report id 6: [6, cmd, param, 0, total, index, len_lo, len_hi, data...]."""

    def __init__(self, path):
        self.dev = hid.device()
        self.dev.open_path(path)

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass

    @staticmethod
    def frame(code, param, data=b"", want=0) -> bytes:
        buf = bytearray(WIRED_LEN)
        n = len(data) or want
        buf[0:8] = bytes([6, code, param, 0, 1, 0, n & 0xFF, (n >> 8) & 0xFF])
        buf[8:8 + len(data)] = data
        return bytes(buf)

    def read_page(self) -> bytes:
        for _ in range(3):
            try:
                time.sleep(0.015)
                self.dev.send_feature_report(self.frame(0x84, 0, want=PAGE_LEN))
                time.sleep(0.03)
                rep = self.dev.get_feature_report(6, WIRED_LEN)
                if rep and len(rep) > WIRED_HEADER and rep[1] == 0x84:
                    return bytes(rep[WIRED_HEADER:WIRED_HEADER + PAGE_LEN])
            except (OSError, ValueError):
                pass
            time.sleep(0.07)
        raise ReadError("couldn't read the keyboard's settings over the cable")

    def write_page(self, page: bytes):
        time.sleep(0.015)
        self.dev.send_feature_report(self.frame(0x04, 0, data=page))
        time.sleep(0.05)


class BluetoothOnly(ReadError):
    """Over Bluetooth the keyboard always drops packets 1, 4 and 7 of the settings page
    (verified on a real F99 Pro), so lighting can only be read and changed via dongle or cable."""


def open_link():
    """(link, source) for a connection that can carry lighting settings: cable first (the dongle goes
    silent while the cable is in), then the dongle. Raises BluetoothOnly if only Bluetooth is live."""
    for d in hid.enumerate(VID_WIRED, PID_WIRED):
        if d.get("usage_page") == 0xFF00 and d.get("usage") == 0x0001:
            link = WiredLink(d["path"])
            try:
                if page_ok(link.read_page()):
                    return link, "USB"
            except Exception:
                pass
            link.close()
    for d in hid.enumerate(VID_WIRELESS, PID_WIRELESS):
        if d.get("usage_page") == 0xFF02 and d.get("usage") == 0x0002:
            return WirelessLink(d["path"]), "2.4G"
    if any(d.get("usage_page") == 0xFF02 for d in hid.enumerate(VID_WIRELESS, PID_BLUETOOTH)):
        raise BluetoothOnly("Lighting can't be changed over Bluetooth. Use the 2.4GHz dongle or USB cable.")
    return None, ""


@dataclass
class Lighting:
    effect: int                  # 0 = off, 1..19
    brightness: int              # 0..9
    speed: int                   # 0..4
    color: int                   # 0..7 (7 = random)
    sleep_seconds: int | None    # keyboard sleep timer


def _slot(page) -> int:
    return OFF_TABLE + max(0, min(18, (page[OFF_EFFECT] or 1) - 1)) * 2


def parse(page: bytes) -> Lighting:
    if page[OFF_CUSTOM] == 1:
        bright, speed, color = page[OFF_CUSTOM_BRIGHT], SPEED_MAX, 7
    else:
        s = _slot(page)
        bright, speed, color = page[s], (page[s + 1] >> 4) & 0x0F, page[s + 1] & 0x0F
    hw = page[OFF_SLEEP]
    sleep = SLEEP_SECONDS[SLEEP_HW.index(hw)] if hw in SLEEP_HW else None
    return Lighting(page[OFF_EFFECT], min(BRIGHT_MAX, bright), min(SPEED_MAX, speed), min(7, color), sleep)


def patch(page: bytes, name: str, value) -> bytes:
    """A copy of the config page with one setting changed."""
    p = bytearray(page)
    s = _slot(p)
    if name == "effect":
        value = int(value)
        if not 0 <= value <= 19:
            raise ValueError("effect must be 0..19")
        p[OFF_CUSTOM] = 1 if value == EFFECT_CUSTOM else 0
        p[OFF_EFFECT] = value
    elif name == "brightness":
        level = max(0, min(BRIGHT_MAX, int(value)))
        if p[OFF_CUSTOM] == 1:
            p[OFF_CUSTOM_BRIGHT] = level
        else:
            p[s] = level
    elif name == "speed":
        p[s + 1] = (max(0, min(SPEED_MAX, int(value))) << 4) | (p[s + 1] & 0x0F)
    elif name == "color":
        p[s + 1] = (p[s + 1] & 0xF0) | max(0, min(7, int(value)))
    elif name == "sleep_seconds":
        p[OFF_SLEEP] = SLEEP_HW[SLEEP_SECONDS.index(int(value))]
    else:
        raise ValueError(f"unknown keyboard setting {name}")
    if not page_ok(p):
        raise ReadError("settings page lost its end marker")
    return bytes(p)


def read() -> tuple[bytes, Lighting, str]:
    link, source = open_link()
    if link is None:
        raise ReadError("keyboard not connected")
    try:
        page = link.read_page()
    finally:
        link.close()
    if not page_ok(page):
        raise ReadError("keyboard sent an invalid settings page")
    return page, parse(page), source


def write(name: str, value) -> Lighting:
    """Read the page, change one setting, write it, then read back to confirm the keyboard took it."""
    link, _ = open_link()
    if link is None:
        raise ReadError("keyboard not connected")
    try:
        page = link.read_page()
        if not page_ok(page):
            raise ReadError("keyboard sent an invalid settings page")
        new = patch(page, name, value)
        if new != page:
            link.write_page(new)
            back = link.read_page()
            if back != new:
                raise ReadError("keyboard didn't accept the change")
            page = back
        return parse(page)
    finally:
        link.close()


if __name__ == "__main__":
    page, light, src = read()
    print("via", src, "|", light, "->", EFFECT_NAMES.get(light.effect), COLOR_NAMES[light.color])
    print(page.hex(" "))
