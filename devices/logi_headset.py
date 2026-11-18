"""Logitech PRO X 2 LIGHTSPEED headset (046D:0AF7) - "Centurion" protocol over its USB dongle.

Ported from HeadsetControl (https://github.com/Sapd/HeadsetControl, GPL-3.0):
  lib/devices/protocols/logitech_centurion_protocol.hpp
  lib/devices/logitech_gpro_x2_lightspeed.hpp

Framing (64-byte reports on usage page 0xFFA0, interface 3):
  frame   = [0x51, len(payload)+1, flags, payload..., 0-padding]
  direct  payload = [feature_index, fn|sw, params...]            (talks to the dongle)
  bridged payload = [bridge_idx, 0x10|sw, size_hi, size_lo,       (forwarded to the headset;
                     0x00, sub_feature_index, fn|sw, params...]    size = len(0x00.. params))
  bridged reply   = [bridge_idx, 0x10 (event, sw 0), ?, ?, 0x00, sub_feature_index, fn|sw, data...]
  A reply with sw == our id on the bridge is only the dongle's acknowledgement.
Large bridged messages are split: first fragment carries the 3-byte bridge header + 56 bytes,
continuations carry 60 bytes; flags = (fragment_index << 1) | more_follow.
"""
from __future__ import annotations

import math
import time

import hid

VID, PID = 0x046D, 0x0AF7
REPORT_ID = 0x51
FRAME = 64
SW = 0x01

F_ROOT, F_BRIDGE = 0x0000, 0x0003
F_BATTERY, F_AUTOSLEEP, F_SIDETONE, F_EQ = 0x0104, 0x0108, 0x0604, 0x0636

DEBUG = False
SIDETONE_MAX = 100
EQ_PRESETS = {   # G HUB's 5-band presets at 80, 240, 750, 2200, 6600 Hz (from HeadsetControl)
    "Flat": [0, 0, 0, 0, 0],
    "Bass Boost": [4, 2, 0, 0, 0],
    "Team Chat": [-1, 2, 1, 3, 3],
    "Shooter": [-1, -1, 4, 3, 2],
    "MOBA": [0, 1, 1, 2, 4],
}


class HeadsetError(Exception):
    pass


class Offline(HeadsetError):
    pass


def _frame(payload: bytes, flags: int = 0) -> bytes:
    f = bytearray(FRAME)
    f[0], f[1], f[2] = REPORT_ID, len(payload) + 1, flags
    f[3:3 + len(payload)] = payload
    return bytes(f)


def _payload(frame) -> bytes | None:
    if len(frame) < 4 or frame[0] != REPORT_ID:
        return None
    n = frame[1]
    if n <= 1 or n + 2 > len(frame):
        return None
    return bytes(frame[3:2 + n])


class Headset:
    def __init__(self):
        paths = [d["path"] for d in hid.enumerate(VID, PID) if d["usage_page"] == 0xFFA0]
        if not paths:
            raise HeadsetError("PRO X 2 dongle not plugged in")
        self.dev = hid.device()
        self.dev.open_path(paths[0])
        self.dev.set_nonblocking(1)
        self.bridge: int | None = None
        self.features: dict[int, int] = {}

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ transport
    def _drain(self):
        while self.dev.read(FRAME):
            pass

    def _read(self, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.dev.read(FRAME)
            if r:
                p = _payload(r)
                if p is not None:
                    yield p
            else:
                time.sleep(0.005)

    def _direct(self, feat_idx: int, fn: int, params: bytes = b"") -> bytes:
        self._drain()
        fsw = (fn & 0xF0) | SW
        self.dev.write(_frame(bytes([feat_idx, fsw]) + params))
        for p in self._read(1.0):
            if len(p) >= 2 and p[0] == feat_idx and p[1] == fsw:
                return p[2:]
        raise HeadsetError("dongle didn't answer")

    def _bridged(self, sub_idx: int, fn: int, params: bytes = b"") -> bytes:
        if self.bridge is None:
            r = self._direct(0x00, 0x00, bytes([F_BRIDGE >> 8, F_BRIDGE & 0xFF]))
            if not r or r[0] == 0:
                raise HeadsetError("dongle has no bridge feature")
            self.bridge = r[0]
        b = self.bridge
        msg = bytes([0x00, sub_idx, (fn & 0xF0) | SW]) + params
        head = bytes([b, 0x10 | SW, (len(msg) >> 8) & 0x0F, len(msg) & 0xFF])
        self._drain()
        if len(msg) <= 56:
            self.dev.write(_frame(head + msg))
        else:
            off, i = 0, 0
            while off < len(msg):
                size = min(56 if i == 0 else 60, len(msg) - off)
                more = off + size < len(msg)
                self.dev.write(_frame((head if i == 0 else b"") + msg[off:off + size], (i << 1) | int(more)))
                off += size
                i += 1
        acked = False
        for p in self._read(1.5):
            if len(p) >= 4 and p[0] == 0xFF and p[1] == b and p[2] == (0x10 | SW):
                raise Offline("headset is off or out of range")
            if len(p) < 2 or p[0] != b:
                continue
            if p[1] == 0x00 and len(p) >= 4:            # connection event
                if p[3] == 0x00:
                    raise Offline("headset is off or out of range")
                continue
            if p[1] >> 4 != 0x1:
                continue
            if p[1] & 0x0F == SW:
                acked = True
                continue
            if p[1] & 0x0F != 0 or len(p) < 7 or p[4] != 0x00:
                continue
            if DEBUG:
                print("   <", p.hex(" "))
            if p[5] == 0xFF and p[6] == sub_idx:
                raise HeadsetError("headset rejected the request")
            # Match feature AND function+software id, so a late reply to an earlier request
            # (or an unsolicited notification, sw id 0) isn't mistaken for this one.
            if p[5] == sub_idx and p[6] == ((fn & 0xF0) | SW):
                return p[7:]
        raise Offline("headset is off or out of range" if acked else "dongle didn't acknowledge")

    def call(self, fid: int, fn: int, params: bytes = b"") -> bytes:
        if fid not in self.features:
            r = self._bridged(0x00, 0x00, bytes([fid >> 8, fid & 0xFF]))
            if not r or r[0] == 0:
                raise HeadsetError(f"feature {fid:04X} not supported")
            self.features[fid] = r[0]
        return self._bridged(self.features[fid], fn, params)

    # ------------------------------------------------------------------ features
    def battery(self) -> tuple[int, str]:
        r = self.call(F_BATTERY, 0x00)
        pct = r[0]
        if pct > 100:
            raise HeadsetError("bad battery value")
        state = r[2] if len(r) > 2 else 0
        return pct, "charging" if state in (1, 2) else "discharging"

    # Sidetone byte layout, verified on a real PRO X 2 (differs from HeadsetControl's guess):
    #   read  fn 0x00 -> [mic id 1, enabled 1, level 0..100]
    #   write fn 0x10 <- [mic id 1, level 0..100, enabled 1]
    def get_sidetone(self) -> int:
        r = self.call(F_SIDETONE, 0x00)
        if len(r) < 3 or r[2] > SIDETONE_MAX:
            raise HeadsetError(f"unexpected sidetone reply {r.hex(' ')}")
        return r[2]

    def set_sidetone(self, level: int):
        level = max(0, min(SIDETONE_MAX, int(level)))
        self.call(F_SIDETONE, 0x10, bytes([0x01, level, 0x01]))

    def get_autosleep(self) -> int:
        return self.call(F_AUTOSLEEP, 0x00)[0]

    def set_autosleep(self, minutes: int):
        self.call(F_AUTOSLEEP, 0x10, bytes([max(0, min(255, minutes))]))

    def get_eq(self) -> list[tuple[int, int, int]]:
        """[(frequency Hz, gain dB, q)] for each playback band."""
        r = self.call(F_EQ, 0x10, b"\x00")
        n, out = r[1], []
        for i in range(n):
            o = 2 + i * 4
            if o + 3 >= len(r):
                break
            g = r[o + 2]
            out.append(((r[o] << 8) | r[o + 1], g - 256 if g > 127 else g, r[o + 3]))
        return out

    def set_eq(self, gains: list[int]):
        bands = self.get_eq()
        if len(gains) != len(bands):
            raise HeadsetError(f"need {len(bands)} gains")
        bands = [(f, max(-12, min(12, int(g))), q) for (f, _, q), g in zip(bands, gains)]
        self.call(F_EQ, 0x00)   # same order as HeadsetControl: info, params, write
        self.call(F_EQ, 0x20, _eq_payload(0, bands))


# ---------------------------------------------------------------------- EQ maths (from HeadsetControl)

def _biquad(freq, gain_db, q, fs):
    a = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * freq / fs
    cw, alpha = math.cos(w0), math.sin(w0) / (2 * q)
    a0 = 1 + alpha / a
    return [(1 + alpha * a) / a0, (-2 * cw) / a0, (1 - alpha * a) / a0, (-2 * cw) / a0, (1 - alpha / a) / a0]


def _clamp32(v):
    return max(-(1 << 31), min((1 << 31) - 1, v))


def _quantize(coeffs):
    scales = (2147483648.0, 1073741824.0, 2147483648.0, 1073741824.0, 2147483648.0)
    words = []
    for c, s in zip(coeffs, scales):
        q = _clamp32(_round_half_away(c * s)) & ~0xFF
        words += [(q >> 16) & 0xFFFF, q & 0xFFFF]
    return words


def _round_half_away(x):
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _section(bands, fs, kind):
    raw, max_b0 = [], 1.0
    for f, g, q in bands:
        c = _biquad(f, g, max(0.1, float(q)), fs)
        max_b0 = max(max_b0, abs(c[0]))
        raw.append(c)
    rescale = max(1.0, max_b0) * 1.19
    words = [len(bands)]
    for c in raw:
        words += _quantize([c[0] / rescale, c[1] / rescale, c[2] / rescale, c[3], c[4]])
    rq = _clamp32(_round_half_away(rescale * 67108864.0)) & 0xFFFFFF00
    words += [(rq >> 16) & 0xFFFF, rq & 0xFFFF]
    count = len(bands) * 10 + 3
    out = bytearray([kind, 0x00, count & 0xFF, (count >> 8) & 0xFF])
    for w in words:
        out += bytes([w & 0xFF, (w >> 8) & 0xFF])
    return bytes(out)


def _eq_payload(slot, bands) -> bytes:
    p = bytearray([slot, len(bands)])
    for f, g, q in bands:
        p += bytes([(f >> 8) & 0xFF, f & 0xFF, g & 0xFF, q])
    p += bytes([0x05, 0x5A, 0xE3, 0x00, 0x03, 0x0E, 0x00, 0x02, 0x00, 0x00, 0x00])
    p += _section(bands, 48000.0, 0x01)
    p += _section(bands, 16000.0, 0x02)
    return bytes(p)


if __name__ == "__main__":
    h = Headset()
    try:
        for label, fn in (("battery", h.battery), ("eq", h.get_eq), ("autosleep", h.get_autosleep)):
            try:
                print(f"{label}: {fn()}")
            except HeadsetError as e:
                print(f"{label}: ERROR {type(e).__name__}: {e}")
        try:
            print("sidetone raw:", h.call(F_SIDETONE, 0x00).hex(" "))
        except HeadsetError as e:
            print("sidetone: ERROR", e)
        print("features:", {f"{k:04X}": v for k, v in h.features.items()}, "bridge:", h.bridge)
    finally:
        h.close()
