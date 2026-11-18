"""Offline tests - no keyboard, mouse or headset needed. Run:  python -m unittest discover tests"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from devices import aula, logi_headset as H, logi_mouse as M   # noqa: E402
import estimate                                                # noqa: E402
import ghub_import                                             # noqa: E402


# ----------------------------------------------------------------------------- AULA keyboard

class FakeAulaWireless:
    """Answers GET_BATTERY (0x4A) like the real keyboard: 13 4A 01 00 02 <pct> <flags> ... <checksum>."""

    def __init__(self, pct=73, flags=0x01):
        self.q, self.pct, self.flags, self.sent = [], pct, flags, []

    def open_path(self, p): pass
    def set_nonblocking(self, x): pass
    def close(self): pass

    def write(self, pkt):
        self.sent.append(bytes(pkt))
        r = bytearray(20)
        r[0:7] = bytes([0x13, pkt[1], 1, 0, 2, self.pct, self.flags])
        r[19] = sum(r[:19]) & 0xFF
        self.q.append(list(r))

    def read(self, n, timeout_ms=0):
        return self.q.pop(0) if self.q else []


class AulaTests(unittest.TestCase):
    def setUp(self):
        self._enum, self._dev = aula.hid.enumerate, aula.hid.device

    def tearDown(self):
        aula.hid.enumerate, aula.hid.device = self._enum, self._dev

    def test_packet_checksum(self):
        p = aula._wireless_packet(0x4A)
        self.assertEqual(len(p), 20)
        self.assertEqual(p[:5], bytes([0x13, 0x4A, 1, 0, 0]))
        self.assertEqual(p[19], sum(p[:19]) & 0xFF)

    def _only(self, pid):
        def enum(vid, want_pid):
            if vid == aula.VID_WIRELESS and want_pid == pid:
                return [{"path": b"x", "usage_page": 0xFF02, "usage": 2}]
            return []
        return enum

    def test_bluetooth_reading(self):
        dev = FakeAulaWireless(pct=83)
        aula.hid.enumerate, aula.hid.device = self._only(aula.PID_BLUETOOTH), lambda: dev
        r, msg = aula.read_battery()
        self.assertEqual((r.percent, r.charging, r.source, msg), (83, False, "Bluetooth", ""))

    def test_dongle_charging_flag(self):
        dev = FakeAulaWireless(pct=90, flags=0x11)       # 0x10 = cable power
        aula.hid.enumerate, aula.hid.device = self._only(aula.PID_WIRELESS), lambda: dev
        r, _ = aula.read_battery()
        self.assertEqual((r.percent, r.charging, r.source), (90, True, "2.4G"))

    def test_nothing_connected(self):
        aula.hid.enumerate = lambda vid, pid: []
        self.assertEqual(aula.read_battery(), (None, "keyboard not found"))


# ----------------------------------------------------------------------------- PRO X 2 headset

class FakeHeadset:
    """Simulates the PRO X 2 dongle + headset: bridge at index 3, Centurion fragments, ack + reply."""

    FEATURES = {H.F_BATTERY: 4, H.F_AUTOSLEEP: 6, H.F_SIDETONE: 7, H.F_EQ: 9}

    def __init__(self):
        self.out, self.partial, self.writes = [], b"", []
        self.sidetone = 47
        self.eq = [5, 3, 1, 2, 3]
        self.frames = []

    def close(self): pass

    def read(self, n):
        return self.out.pop(0) if self.out else []

    def _send(self, payload):
        self.out.append(list(H._frame(payload)))

    def write(self, frame):
        self.frames.append(bytes(frame))
        payload, flags = H._payload(frame), frame[2]
        if flags == 0 and payload[0] == 0x00:                # direct: root lookup of the bridge
            self._send(bytes([0x00, payload[1], 0x03]))
            return
        if flags >> 1 == 0:                                  # first fragment: [bridge, 0x11, hi, lo, msg...]
            self.size = (payload[2] << 8) | payload[3]
            self.partial = payload[4:]
        else:
            self.partial += payload
        if flags & 1:                                        # more fragments follow
            return
        msg, self.partial = self.partial[:self.size], b""
        sub, fsw, params = msg[1], msg[2], msg[3:]
        self._send(bytes([0x03, 0x11, 0, 0]))                # dongle ack (sw id 1)
        self._send(bytes([0x03, 0x00, 0x00, 0x01]))          # unrelated "connected" event
        data = self._handle(sub, fsw & 0xF0, params)
        body = bytes([0x00, sub, fsw]) + data
        self._send(bytes([0x03, 0x10, len(body) >> 8, len(body) & 0xFF]) + body)

    def _handle(self, sub, fn, params):
        if sub == 0:                                         # root: feature lookup
            return bytes([self.FEATURES.get((params[0] << 8) | params[1], 0), 0, 0])
        if sub == 4:
            return bytes([78, 78, 0])
        if sub == 7:
            if fn == 0x10:
                self.writes.append(("sidetone", bytes(params)))
                self.sidetone = params[1]
                return b""
            return bytes([1, 1, self.sidetone])
        if sub == 6:
            return bytes([0])
        if sub == 9:
            if fn == 0x10:
                bands = b"".join(bytes([f >> 8, f & 0xFF, g & 0xFF, 2])
                                 for f, g in zip((80, 240, 750, 2200, 6600), self.eq))
                return bytes([0, 5]) + bands
            if fn == 0x20:
                self.writes.append(("eq", bytes(params)))
            return b"\x00" * 5
        return b""


def make_headset(fake) -> H.Headset:
    h = H.Headset.__new__(H.Headset)
    h.dev, h.bridge, h.features = fake, None, {}
    return h


class HeadsetTests(unittest.TestCase):
    def test_frame_roundtrip(self):
        f = H._frame(b"\x01\x02\x03")
        self.assertEqual(len(f), 64)
        self.assertEqual(f[:6], bytes([0x51, 4, 0, 1, 2, 3]))
        self.assertEqual(H._payload(f), b"\x01\x02\x03")

    def test_battery_and_settings(self):
        h = make_headset(FakeHeadset())
        self.assertEqual(h.battery(), (78, "discharging"))
        self.assertEqual(h.get_sidetone(), 47)
        self.assertEqual([g for _, g, _ in h.get_eq()], [5, 3, 1, 2, 3])
        self.assertEqual(h.get_autosleep(), 0)

    def test_sidetone_write_layout(self):
        """Verified on hardware: write is [mic 1, level, enabled 1] (read is [mic, enabled, level])."""
        fake = FakeHeadset()
        h = make_headset(fake)
        h.set_sidetone(40)
        self.assertEqual(fake.writes[-1], ("sidetone", bytes([1, 40, 1])))
        self.assertEqual(h.get_sidetone(), 40)

    def test_eq_write_is_fragmented(self):
        fake = FakeHeadset()
        h = make_headset(fake)
        h.set_eq([4, 2, 0, 0, 0])
        name, payload = fake.writes[-1]
        self.assertEqual(name, "eq")
        self.assertEqual(payload, H._eq_payload(0, [(80, 4, 2), (240, 2, 2), (750, 0, 2), (2200, 0, 2), (6600, 0, 2)]))
        eq_frames = [f for f in fake.frames if f[2] != 0]
        self.assertGreaterEqual(len(eq_frames), 4)            # 256-byte message -> several fragments
        self.assertEqual(eq_frames[-1][2] & 1, 0)             # last fragment: "more" bit clear

    def test_eq_payload_matches_headsetcontrol_test(self):
        p = H._eq_payload(0, [(80, 4, 1), (240, 2, 1), (750, 0, 1), (2200, 0, 1), (6600, 0, 1)])
        self.assertEqual(list(p[:6]), [0x00, 5, 0x00, 80, 4, 1])     # from HeadsetControl's unit test
        self.assertEqual(len(p), 2 + 5 * 4 + 11 + 2 * (4 + 2 * (1 + 50 + 2)))

    def test_quantize_negative(self):
        words = H._quantize([0.0, -100.0 / 1073741824.0, 0.0, 0.0, 0.0])   # HeadsetControl's unit test
        self.assertEqual(len(words), 10)
        self.assertEqual(words[2], 0xFFFF)
        self.assertEqual(words[3], 0xFF00)


# ----------------------------------------------------------------------------- MX Master mouse

class MouseTests(unittest.TestCase):
    def test_dpi_list_with_range(self):
        m = M.Mouse.__new__(M.Mouse)
        # 200, then "range step 50 up to 8000" (0xE032 = 0b111 << 13 | 50), then end
        raw = bytes([0, 0x00, 0xC8, 0xE0, 0x32, 0x1F, 0x40, 0, 0]) + bytes(7)
        m.call = lambda fid, fn, *p: raw
        dpis = m.dpi_choices()
        self.assertEqual((dpis[0], dpis[1], dpis[-1], len(dpis)), (200, 250, 8000, 157))

    def test_unified_battery_status(self):
        m = M.Mouse.__new__(M.Mouse)
        for raw, expect in ((bytes([15, 2, 1, 1]), (15, "charging")), (bytes([100, 8, 3, 1]), (100, "full")),
                            (bytes([0, 4, 0, 0]), (50, "discharging"))):
            m.call = lambda fid, fn, *p, r=raw: r
            self.assertEqual(m.battery(), expect)


# ----------------------------------------------------------------------------- time estimates

class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.e = estimate.Estimator(os.path.join(tempfile.mkdtemp(), "h.json"))

    def test_no_data_then_measured(self):
        e, t = self.e, 1000.0
        e.update("keyboard", 83, False, t)
        self.assertEqual(e.estimate("keyboard", 83, False), (None, ""))
        for i, pct in enumerate([82, 81, 80]):                  # 1% every 2 hours
            e.update("keyboard", pct, False, t + 3600 + i * 7200)
        hours, basis = e.estimate("keyboard", 80, False)
        self.assertAlmostEqual(hours, 160, places=3)
        self.assertEqual(basis, "usage")

    def test_charge_rate_remembered(self):
        e, t = self.e, 1000.0
        e.update("mouse", 15, True, t)
        for i, pct in enumerate(range(16, 26)):                 # 1% every 3 minutes
            e.update("mouse", pct, True, t + 60 + i * 180)
        self.assertAlmostEqual(e.estimate("mouse", 25, True)[0], 3.75, places=3)
        e.update("mouse", 25, False, t + 5000)                 # unplugged: rated life until measured
        self.assertEqual(e.estimate("mouse", 25, False), (70 * 24 * 0.25, "rated"))
        e.update("mouse", 24, True, t + 9000)                  # plugged in again: remembered rate
        self.assertAlmostEqual(e.estimate("mouse", 24, True)[0], 3.8, places=3)

    def test_charged_while_away_restarts_segment(self):
        e = self.e
        e.update("keyboard", 60, False, 1000)
        e.update("keyboard", 70, False, 4600)
        self.assertEqual(e.data["keyboard"]["points"], [[4600, 70]])

    def test_persists(self):
        self.e.update("headset", 78, False, 1000)
        self.assertIn("headset", estimate.Estimator(self.e.path).data)

    def test_format(self):
        cases = {0.3: "18 min", 1.5: "1 h 30 min", 5.97: "6 h", 23: "23 h", 160: "7 days"}
        for hours, text in cases.items():
            self.assertEqual(estimate.fmt_duration(hours), text)


# ----------------------------------------------------------------------------- G HUB import

class GhubImportTests(unittest.TestCase):
    def test_reads_custom_presets_and_sidetone(self):
        doc = {"cards": [
            {"attribute": "CENTURION_PRO_EQ", "name": "Perfect EQ",
             "centurionProEqualizer": {"gains": [5, 3, 1, 2, 3], "type": "ONBOARD"}},
            {"attribute": "CENTURION_PRO_EQ", "name": "EQUALIZER_FLAT", "readOnly": True,
             "centurionProEqualizer": {"gains": [0, 0, 0, 0, 0]}},
            {"attribute": "CENTURION_PRO_EQ", "name": "Loud", "centurionProEqualizer": {"gains": [20, 0, 0, 0, -20]}},
            {"attribute": "ACOUSTICS", "deviceSignature": "HEADSET.pro_x2_gaming_wireless_headset.0.x",
             "acoustics": {"sidetone": {"channel": "SIDETONE", "gain": 0.47}}},
        ]}
        path = os.path.join(tempfile.mkdtemp(), "settings.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE data (_id INTEGER PRIMARY KEY, _date_created TEXT, file BLOB)")
        con.execute("INSERT INTO data (file) VALUES (?)", (json.dumps(doc).encode(),))
        con.commit()
        con.close()
        got = ghub_import.read_headset_settings(path)
        self.assertEqual(got["presets"], {"Perfect EQ": [5, 3, 1, 2, 3], "Loud": [12, 0, 0, 0, -12]})
        self.assertEqual(got["sidetone"], 47)

    def test_missing_ghub(self):
        self.assertIsNone(ghub_import.read_headset_settings(os.path.join(tempfile.mkdtemp(), "nope.db")))


# ----------------------------------------------------------------------------- app logic

class AppLogicTests(unittest.TestCase):
    def test_poll_interval_and_tray(self):
        import device_hub as D
        hub = D.Hub.__new__(D.Hub)
        hub.fast = False
        kb, ms = D.KeyboardCtl.__new__(D.KeyboardCtl), D.MouseCtl.__new__(D.MouseCtl)
        kb.state = D.DevState("keyboard", "AULA", "Keyboard", online=True, battery=83)
        ms.state = D.DevState("mouse", "MX", "Mouse", online=True, battery=15)
        hub.ctls = [kb, ms]
        self.assertEqual(hub.poll_interval(), D.POLL_SECONDS)
        ms.state.charging = True
        self.assertEqual(hub.poll_interval(), D.FAST_POLL_SECONDS)
        ms.state.charging, hub.fast = False, True
        self.assertEqual(hub.poll_interval(), D.FAST_POLL_SECONDS)
        states = [kb.state, ms.state]
        self.assertIs(D.lowest(states), ms.state)
        self.assertEqual(D.make_icon(states).size, (64, 64))
        self.assertEqual(D.make_icon(states, bolt=True).size, (64, 64))
        self.assertEqual(D.tooltip(states), "Keyboard 83% · Mouse 15%")


if __name__ == "__main__":
    unittest.main()
