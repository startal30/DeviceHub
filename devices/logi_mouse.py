"""Logitech HID++ 2.0 mice (MX Master 3S) through a Bolt / Unifying receiver.

Protocol reference: Solaar (https://github.com/pwr-Solaar/Solaar), lib/logitech_receiver.
  Long report  0x11, 20 bytes: [0x11, device, feature_index, function|sw_id, params...]
  HID++ 2.0 error:            [0x11, device, 0xFF, feature_index, function|sw_id, error]
  HID++ 1.0 error (short):    [0x10, device, 0x8F, ...]   e.g. device asleep / not paired
Features used:
  0x0000 ROOT           fn 0x00 getFeature(id) -> [index]
  0x0005 DEVICE_NAME    fn 0x00 count, fn 0x10 name chunk
  0x1004 UNIFIED_BATTERY fn 0x10 -> [percent, level, status, ...]   status 1,2,4 = charging, 3 = full
  0x2201 ADJUSTABLE_DPI  fn 0x10 list, 0x20 get, 0x30 set
  0x2111 SMART_SHIFT_ENHANCED fn 0x10 get [mode, threshold], 0x20 set [0, threshold]
  0x2121 HIRES_WHEEL     fn 0x10 get flags, 0x20 set flags (bit1 hi-res, bit2 invert)
"""
from __future__ import annotations

import time

import hid

VID_LOGITECH = 0x046D
RECEIVER_PIDS = {0xC548: "Bolt", 0xC52B: "Unifying", 0xC547: "Bolt", 0xC539: "Lightspeed"}
SW_ID = 0x0B

F_ROOT, F_NAME, F_BATTERY = 0x0000, 0x0005, 0x1004
F_DPI, F_SMARTSHIFT, F_SMARTSHIFT_CLASSIC, F_HIRES = 0x2201, 0x2111, 0x2110, 0x2121

CHARGING_STATES = {1: "charging", 2: "charging", 3: "full", 4: "charging"}


class HidppError(Exception):
    pass


class Offline(HidppError):
    pass


class Receiver:
    """Talks HID++ through one receiver. Opens the short (0x10) and long (0x11) report collections."""

    def __init__(self, pid: int):
        self.pid = pid
        paths = {d["usage"]: d["path"] for d in hid.enumerate(VID_LOGITECH, pid) if d["usage_page"] == 0xFF00}
        if 2 not in paths:
            raise HidppError("receiver's HID++ interface not found")
        self.long = hid.device()
        self.long.open_path(paths[2])
        self.long.set_nonblocking(1)
        self.short = None
        if 1 in paths:
            self.short = hid.device()
            self.short.open_path(paths[1])
            self.short.set_nonblocking(1)

    def close(self):
        for h in (self.long, self.short):
            try:
                h and h.close()
            except Exception:
                pass

    def _drain(self):
        for h in (self.long, self.short):
            while h and h.read(64):
                pass

    def request(self, dev: int, feat_idx: int, fn: int, *params: int, timeout: float = 1.0) -> bytes:
        self._drain()
        pkt = bytearray(20)
        pkt[0], pkt[1], pkt[2], pkt[3] = 0x11, dev, feat_idx, (fn & 0xF0) | SW_ID
        pkt[4:4 + len(params)] = bytes(params)
        self.long.write(bytes(pkt))
        deadline = time.time() + timeout
        while time.time() < deadline:
            for h in (self.long, self.short):
                if not h:
                    continue
                r = h.read(64)
                if not r or r[1] != dev:
                    continue
                if r[0] == 0x10 and r[2] == 0x8F:
                    raise Offline("device is asleep or turned off")
                if r[0] != 0x11:
                    continue
                if r[2] == 0xFF and r[3] == feat_idx and r[4] == pkt[3]:
                    raise HidppError(f"device returned error {r[5]}")
                if r[2] == feat_idx and r[3] == pkt[3]:
                    return bytes(r[4:20])
            time.sleep(0.005)
        raise Offline("no answer (device asleep?)")


class Mouse:
    def __init__(self, receiver: Receiver, index: int):
        self.rx = receiver
        self.index = index
        self.features: dict[int, int | None] = {}
        self.name = ""

    def feature(self, fid: int) -> int | None:
        if fid not in self.features:
            r = self.rx.request(self.index, 0x00, 0x00, fid >> 8, fid & 0xFF)
            self.features[fid] = r[0] or None
        return self.features[fid]

    def call(self, fid: int, fn: int, *params: int) -> bytes:
        idx = self.feature(fid)
        if idx is None:
            raise HidppError(f"feature {fid:04X} not supported")
        return self.rx.request(self.index, idx, fn, *params)

    def read_name(self) -> str:
        n = self.call(F_NAME, 0x00)[0]
        name = b""
        while len(name) < n:
            name += self.call(F_NAME, 0x10, len(name))[: n - len(name)]
        self.name = name.decode("utf-8", "replace").strip("\x00 ")
        return self.name

    # --- battery
    def battery(self) -> tuple[int, str]:
        r = self.call(F_BATTERY, 0x10)
        pct, level, status = r[0], r[1], r[2]
        if pct == 0:   # some devices only report coarse levels
            pct = {8: 100, 4: 50, 2: 20, 1: 5}.get(level, 0)
        return pct, CHARGING_STATES.get(status, "discharging")

    # --- DPI
    def dpi_choices(self) -> list[int]:
        raw = b""
        for i in range(16):
            raw += self.call(F_DPI, 0x10, 0, i)[1:]
            if raw[-2:] == b"\x00\x00" or len(raw) > 200:
                break
        out, i = [], 0
        while i + 1 < len(raw):
            val = int.from_bytes(raw[i:i + 2], "big")
            if val == 0:
                break
            if val >> 13 == 0b111 and out:
                step, last = val & 0x1FFF, int.from_bytes(raw[i + 2:i + 4], "big")
                out += range(out[-1] + step, last + 1, step)
                i += 4
            else:
                out.append(val)
                i += 2
        return out

    def get_dpi(self) -> int:
        r = self.call(F_DPI, 0x20, 0)
        dpi = int.from_bytes(r[1:3], "big")
        return dpi or int.from_bytes(r[3:5], "big")

    def set_dpi(self, dpi: int):
        self.call(F_DPI, 0x30, 0, dpi >> 8, dpi & 0xFF)

    # --- SmartShift: threshold 1..50 (wheel switches to free-spin when scrolled faster than this),
    #     255 = always ratchet. mode 1 = free-spin, 2 = ratchet.
    def _smartshift(self) -> tuple[int, int, int]:
        """(feature id, read fn, write fn): enhanced 0x2111 or classic 0x2110."""
        if self.feature(F_SMARTSHIFT) is not None:
            return F_SMARTSHIFT, 0x10, 0x20
        if self.feature(F_SMARTSHIFT_CLASSIC) is not None:
            return F_SMARTSHIFT_CLASSIC, 0x00, 0x10
        raise HidppError("SmartShift not supported")

    def get_smartshift(self) -> tuple[int, int]:
        fid, rd, _ = self._smartshift()
        r = self.call(fid, rd)
        return r[0], r[1]

    def set_smartshift(self, threshold: int):
        fid, _, wr = self._smartshift()
        self.call(fid, wr, 0, threshold)

    # --- scroll wheel flags
    def get_wheel(self) -> int:
        return self.call(F_HIRES, 0x10)[0]

    def set_wheel(self, flags: int):
        self.call(F_HIRES, 0x20, flags & 0x07)


def find_receivers() -> list[int]:
    seen = []
    for d in hid.enumerate(VID_LOGITECH, 0):
        if d["product_id"] in RECEIVER_PIDS and d["usage_page"] == 0xFF00 and d["product_id"] not in seen:
            seen.append(d["product_id"])
    return seen


def find_mice() -> list[Mouse]:
    """Every device on every receiver that answers and has a battery feature."""
    found = []
    for pid in find_receivers():
        try:
            rx = Receiver(pid)
        except Exception:
            continue
        before = len(found)
        for idx in range(1, 7):
            m = Mouse(rx, idx)
            try:
                if m.feature(F_BATTERY) is None:
                    continue
                m.read_name()
                found.append(m)
            except HidppError:
                continue
        if len(found) == before:
            rx.close()
    return found


if __name__ == "__main__":
    for m in find_mice():
        print(f"device {m.index}: {m.name!r}")
        for label, fn in (("battery", m.battery), ("dpi", m.get_dpi), ("dpi choices", m.dpi_choices),
                          ("smartshift", m.get_smartshift), ("wheel flags", m.get_wheel)):
            try:
                v = fn()
                if label == "dpi choices":
                    v = f"{len(v)} values, {v[:3]}...{v[-2:]}"
                print(f"  {label}: {v}")
            except HidppError as e:
                print(f"  {label}: ERROR {e}")
