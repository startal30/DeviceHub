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

    FEATURES = {H.F_BATTERY: 4, H.F_AUTOSLEEP: 6, H.F_SIDETONE: 7, H.F_MIC_NR: 8, H.F_EQ: 9}

    def __init__(self):
        self.out, self.partial, self.writes = [], b"", []
        self.sidetone = 47
        self.nr = 1
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
        if sub == 8:                                         # mic noise reduction: read fn 0, write fn 0x10
            if fn == 0x10:
                self.nr = params[0]
                return b""
            return bytes([self.nr])
        if sub == 9:
            if fn == 0x10:
                bands = b"".join(bytes([f >> 8, f & 0xFF, g & 0xFF, 2])
                                 for f, g in zip((80, 240, 750, 2200, 6600), self.eq))
                return bytes([0x80, 5]) + bands               # real headset reports slot 0x80
            if fn == 0x20:
                self.writes.append(("eq", bytes(params)))
                if params[0] == 0x80:                         # only that slot updates what's read back
                    self.eq = [b - 256 if b > 127 else b for b in params[4:2 + 5 * 4:4]]
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

    def test_noise_reduction(self):
        h = make_headset(FakeHeadset())
        self.assertTrue(h.get_noise_reduction())
        h.set_noise_reduction(False)
        self.assertFalse(h.get_noise_reduction())

    def test_eq_write_is_fragmented(self):
        fake = FakeHeadset()
        h = make_headset(fake)
        h.set_eq([4, 2, 0, 0, 0])
        name, payload = fake.writes[-1]
        self.assertEqual(name, "eq")
        self.assertEqual(payload, H._eq_payload(0x80, [(80, 4, 2), (240, 2, 2), (750, 0, 2), (2200, 0, 2), (6600, 0, 2)]))
        self.assertEqual([g for _, g, _ in h.get_eq()], [4, 2, 0, 0, 0])    # read-back shows the new EQ
        h.set_eq([-1, -1, 4, 3, 2])
        self.assertEqual([g for _, g, _ in h.get_eq()], [-1, -1, 4, 3, 2])  # negative gains round-trip
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

    def feed(self, key, start_t, start_pct, end_pct, step_s, charging=False, poll=60, settle_drop=0):
        """Simulate polling every `poll` s while the battery moves 1% every `step_s` s.
        settle_drop: extra % lost in the first minutes (the reading settling). Returns the end time."""
        e, t, pct = self.e, start_t, start_pct
        e.update(key, pct, charging, t)
        for _ in range(settle_drop):
            t += poll
            pct -= 1
            e.update(key, pct, charging, t)
        sign = 1 if charging else -1
        next_step = t + step_s
        while pct != end_pct:
            t += poll
            if t >= next_step:
                pct += sign
                next_step += step_s
            e.update(key, pct, charging, t)
        return t

    def test_no_data_then_measured(self):
        e = self.e
        e.update("keyboard", 83, False, 1000)
        self.assertEqual(e.estimate("keyboard", 83, False), (None, ""))
        self.feed("keyboard", 1060, 83, 77, 1200)            # 1% every 20 min = 3 %/h
        hours, basis = e.estimate("keyboard", 77, False)
        self.assertEqual(basis, "usage")
        self.assertAlmostEqual(hours, 77 / 3, delta=0.5)

    def test_settling_drop_is_ignored(self):
        e = self.e
        self.feed("headset", 1000, 78, 70, 1200, settle_drop=3)   # 78 -> 75 in 3 min, then 3 %/h
        self.assertAlmostEqual(e.rates("headset")["used"], 3.0, delta=0.15)

    def test_time_switched_off_does_not_count(self):
        e = self.e
        t = self.feed("headset", 1000, 80, 76, 1200)            # 3 %/h while on
        e.update("headset", 76, False, t + 10 * 3600)            # off for 10 h, same %
        self.feed("headset", t + 10 * 3600 + 60, 76, 72, 1200)
        self.assertAlmostEqual(e.rates("headset")["used"], 3.0, delta=0.15)

    def test_recent_pace_counts_more(self):
        e = self.e
        t = self.feed("keyboard", 1000, 90, 75, 1800)           # 2 %/h for 7.5 h
        self.feed("keyboard", t + 60, 75, 65, 450)               # then 8 %/h for 1.25 h
        r = e.rates("keyboard")
        self.assertAlmostEqual(r["recent"], 8.0, delta=0.4)
        self.assertGreater(r["used"], 5.0)                       # follows the new pace...
        self.assertLess(r["used"], 8.0)                          # ...without jumping all the way

    def test_accuracy_is_checked(self):
        e = self.e
        self.feed("keyboard", 1000, 90, 50, 1200)                # steady 3 %/h: predictions are right
        typical, bias, n = e.accuracy("keyboard", False)
        self.assertGreaterEqual(n, 3)
        self.assertLess(typical, 0.1)
        self.assertIn("typically off by", e.report({"keyboard": "Keyboard"}))

    def test_old_history_is_migrated(self):
        path = os.path.join(tempfile.mkdtemp(), "h.json")
        with open(path, "w") as f:
            json.dump({"headset": {"charging": False, "points": [[1, 78]], "rate_down": 0.85, "rate_up": 20}}, f)
        e = estimate.Estimator(path)
        self.assertIsNone(e.data["headset"]["rate_down"])      # measured with off-time: dropped
        self.assertEqual(e.data["headset"]["rate_up"], 20)

    def test_charge_rate_remembered(self):
        e = self.e
        t = self.feed("mouse", 1000, 15, 45, 180, charging=True, poll=15)   # 20 %/h
        self.assertAlmostEqual(e.estimate("mouse", 45, True)[0], 55 / 20, delta=0.2)
        e.update("mouse", 45, False, t + 15)                   # unplugged: rated life until measured
        self.assertEqual(e.estimate("mouse", 45, False), (70 * 24 * 0.45, "rated"))
        e.update("mouse", 44, True, t + 4000)                  # plugged in again: remembered rate
        self.assertAlmostEqual(e.estimate("mouse", 44, True)[0], 56 / 20, delta=0.2)

    def test_charged_while_away_restarts_segment(self):
        e = self.e
        e.update("keyboard", 60, False, 1000)
        e.update("keyboard", 70, False, 4600)
        self.assertEqual([p[:2] for p in e.data["keyboard"]["points"]], [[4600, 70]])

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
            {"attribute": "CENTURION_PRO_EQ", "name": "My EQ",
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
        self.assertEqual(got["presets"], {"My EQ": [5, 3, 1, 2, 3], "Loud": [12, 0, 0, 0, -12]})
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


# ----------------------------------------------------------------------------- AULA lighting

from devices import aula_lighting as AL   # noqa: E402


def sample_page() -> bytearray:
    p = bytearray(AL.PAGE_LEN)
    p[AL.OFF_EFFECT] = 2                                 # Breathing
    p[AL.OFF_SLEEP] = 10                                 # 300 s
    for i in range(19):
        p[AL.OFF_TABLE + i * 2] = 7                      # brightness 7
        p[AL.OFF_TABLE + i * 2 + 1] = (3 << 4) | 2       # speed 3, blue
    p[AL.OFF_CUSTOM_BRIGHT] = 9
    p[126], p[127] = 0x5A, 0xA5
    return p


class FakeKeyboardDongle:
    """Keyboard behind the 2.4G dongle: answers 0x44 with the page in 14-byte chunks, accepts 0x04 writes.
    drop = packet numbers to lose on the way back (as Bluetooth does with 1, 4, 7)."""

    def __init__(self, drop=()):
        self.page, self.out, self.incoming, self.drop, self.writes = bytes(sample_page()), [], {}, set(drop), 0

    def close(self): pass

    def read(self, n, timeout_ms=0):
        return self.out.pop(0) if self.out else []

    def write(self, pkt):
        cmd, total, idx, op = pkt[1], pkt[2], pkt[3], pkt[4] >> 4
        if cmd == 0x44 and op == 0:
            chunks = [self.page[i:i + 14] for i in range(0, AL.PAGE_LEN, 14)]
            for i, c in enumerate(chunks):
                if i not in self.drop:
                    self.out.append(list(AL.WirelessLink.packet(0x44, 0, c, len(chunks), i)))
        elif cmd == 0x04 and op == 1:
            self.incoming[idx] = bytes(pkt[5:5 + (pkt[4] & 0x0F)])
            if len(self.incoming) == total:
                self.page = b"".join(self.incoming[i] for i in range(total))
                self.incoming, self.writes = {}, self.writes + 1


def fake_link(dev):
    link = AL.WirelessLink.__new__(AL.WirelessLink)
    link.dev = dev
    return link


class LightingTests(unittest.TestCase):
    def setUp(self):
        self._open = AL.open_link

    def tearDown(self):
        AL.open_link = self._open

    def test_parse(self):
        self.assertEqual(AL.parse(bytes(sample_page())), AL.Lighting(2, 7, 3, 2, 300))
        p = sample_page()
        p[AL.OFF_CUSTOM], p[AL.OFF_EFFECT] = 1, 19      # custom effect uses its own brightness byte
        self.assertEqual(AL.parse(bytes(p)).brightness, 9)

    def test_patch_only_touches_the_right_bytes(self):
        page = bytes(sample_page())
        for name, value, offset in (("brightness", 4, AL.OFF_TABLE + 2), ("speed", 1, AL.OFF_TABLE + 3),
                                    ("color", 5, AL.OFF_TABLE + 3), ("sleep_seconds", 600, AL.OFF_SLEEP)):
            new = AL.patch(page, name, value)
            diff = [i for i in range(AL.PAGE_LEN) if new[i] != page[i]]
            self.assertEqual(diff, [offset], name)
        self.assertEqual(AL.parse(AL.patch(page, "color", 5)).speed, 3)       # colour keeps speed
        off = AL.patch(page, "effect", 0)
        self.assertEqual((off[AL.OFF_CUSTOM], off[AL.OFF_EFFECT]), (0, 0))
        custom = AL.patch(page, "effect", 19)
        self.assertEqual((custom[AL.OFF_CUSTOM], custom[AL.OFF_EFFECT]), (1, 19))
        with self.assertRaises(ValueError):
            AL.patch(page, "sleep_seconds", 61)              # firmware only accepts 9 values

    def test_write_roundtrip_over_dongle(self):
        dev = FakeKeyboardDongle()
        AL.open_link = lambda: (fake_link(dev), "2.4G")
        light = AL.write("effect", 7)
        self.assertEqual((light.effect, dev.writes), (7, 1))
        self.assertEqual(AL.write("brightness", 3).brightness, 3)
        self.assertEqual(AL.read()[1].brightness, 3)
        AL.write("brightness", 3)                             # unchanged -> nothing written
        self.assertEqual(dev.writes, 2)

    def test_incomplete_page_is_refused(self):
        dev = FakeKeyboardDongle(drop={1, 4, 7})              # what Bluetooth does
        AL.open_link = lambda: (fake_link(dev), "2.4G")
        with self.assertRaises(AL.ReadError):
            AL.write("brightness", 1)
        self.assertEqual(dev.writes, 0)


# ----------------------------------------------------------------------------- DTS surround / Blue VO!CE

from devices import logi_audiofx as FX   # noqa: E402


class AudioFxTests(unittest.TestCase):
    def test_helper_rejects_bad_input(self):
        self.assertEqual(FX.run_helper("nonsense"), 2)
        self.assertEqual(FX.run_helper("unknown=1"), 2)

    def test_write_validates_names_and_modes(self):
        orig = FX.endpoint_id
        FX.endpoint_id = lambda kind: "{0.0.0.00000000}.{test}"
        try:
            with self.assertRaises(FX.FxError):
                FX._write("unknown", 1)
            with self.assertRaises(FX.FxError):
                FX._write("room", 7)                  # only 0..2 exist
            with self.assertRaises(FX.FxError):
                FX._write("stereo", -1)               # only 0..3 exist
        finally:
            FX.endpoint_id = orig

    def test_mode_tables_match_ghub(self):
        self.assertEqual(FX.ROOM_MODELS[1], "Gaming")           # DTSroomModel=1 while G HUB showed Gaming
        self.assertEqual(FX.STEREO_MODELS[0], "Off")            # DTSstereoModel=0 while G HUB showed Off


class SurroundNoPromptTests(unittest.TestCase):
    def test_registry_values_match_what_ghub_writes(self):
        self.assertEqual(FX._surround_values("surround", 1), {"DTSon": 1, "SelectedSurround": 0})
        self.assertEqual(FX._surround_values("surround", 0), {"DTSon": 0, "SelectedSurround": 2})
        self.assertEqual(FX._surround_values("stereo", 2), {"DTSstereoModel": 2})
        self.assertEqual(FX._surround_values("room", 0), {"DTSroomModel": 0})
        with self.assertRaises(FX.FxError):
            FX._surround_values("stereo", 9)

    def test_bluevoice_goes_through_policy_config_without_prompt(self):
        from devices import win_policy
        writes = []
        orig = (FX.endpoint_id, win_policy.set_values)
        FX.endpoint_id = lambda kind: "{0.0.1.00000000}.{mic}"
        win_policy.set_values = lambda dev, values, fx=False: writes.append((dev, values))
        try:
            self.assertTrue(FX.write_without_prompt("bluevoice", 1))
            self.assertEqual(writes[-1], ("{0.0.1.00000000}.{mic}",
                                          [(FX.PK_BLUEVOICE_FMTID, 1, win_policy.VT_I4, 1)]))
            self.assertTrue(FX.write_without_prompt("voice", FX.voice_values(BROADCASTER)))
            self.assertEqual(len(writes[-1][1]), 40)
            self.assertTrue(all(vt == win_policy.VT_R4 for _f, _p, vt, _v in writes[-1][1]))
            with self.assertRaises(FX.FxError):                  # validated before anything is written
                FX.write_without_prompt("voice", {101: 70.0})
            win_policy.set_values = lambda dev, values, fx=False: (_ for _ in ()).throw(OSError("denied"))
            self.assertFalse(FX.write_without_prompt("bluevoice", 0))   # -> falls back to the prompt
        finally:
            FX.endpoint_id, win_policy.set_values = orig


# Part of a user's G HUB preset (a copy of Broadcaster 1) and the mic values G HUB wrote for it (real PRO X 2).
BROADCASTER = {
    "highPassFilter": {"enabled": True, "frequency": 70},
    "noiseReduction": {"enabled": True, "release": 270, "sensitivity": 5, "bias": 2, "maxAttenuation": -10},
    "multiBandEq": {"enabled": True, "lowBandFilter": {"frequency": 150, "q": 0.5},
                    "highBandFilter": {"frequency": 6200, "q": 5, "gain": 9}},
    "dePopper": {"threshold": -15, "width": 1.5}, "deEsser": {"frequency": 5500},
    "hardLimiter": {"boost": 10}, "inputGain": 0.5,
}


class VoicePresetTests(unittest.TestCase):
    def test_field_map(self):
        self.assertEqual(len(FX.VOICE_FIELDS), 40)
        self.assertEqual(len(FX.VOICE_PIDS), 40)                 # no property used twice
        self.assertNotIn(1, FX.VOICE_PIDS)                       # the on/off switch isn't part of a preset

    def test_preset_to_mic_values(self):
        v = FX.voice_values(BROADCASTER)
        self.assertEqual(set(v), FX.VOICE_PIDS)
        self.assertEqual((v[101], v[200], v[203], v[405], v[410], v[412]), (70, 270, -10, 0.5, 6200, 9))
        self.assertEqual((v[500], v[503], v[508], v[902]), (-15, 1.5, 5500, 10))
        self.assertEqual(v[406], 0.0)                            # missing low-band gain: G HUB writes 0
        self.assertEqual(v[3], 0.0)                              # missing volume: G HUB writes 0
        self.assertEqual(FX.voice_values({"volume": True})[3], 0.0)   # a bool is not a number here
        self.assertEqual(FX.voice_mic_level(BROADCASTER), 50)     # inputGain 0.5 -> Windows mic level 50%
        self.assertEqual(FX.voice_mic_level({}), 100)
        self.assertEqual(FX.voice_mic_level({"inputGain": 3}), 100)

    def test_helper_argument_roundtrip(self):
        v = FX.voice_values(dict(BROADCASTER, volume=-5.490899085998535))
        arg = FX.helper_arg("voice", v)
        self.assertTrue(arg.startswith("voice=3:-5.490899085998535,101:70.0,"))
        self.assertEqual(FX._decode_voice(arg.split("=", 1)[1]), v)

    def test_helper_rejects_bad_presets(self):
        good = FX.voice_values(BROADCASTER)
        enc = FX._encode_voice(good)
        self.assertEqual(FX.run_helper("voice=" + enc.rsplit(",", 1)[0]), 2)      # a property missing
        self.assertEqual(FX.run_helper("voice=" + enc + ",1:0.0"), 2)             # the on/off switch
        self.assertEqual(FX.run_helper("voice=" + enc.replace("101:70.0", "101:nan")), 2)
        self.assertEqual(FX.run_helper("voice=" + enc.replace("101:70.0", "101:1e9")), 2)
        self.assertEqual(FX.run_helper("voice=garbage"), 2)

    def test_float_bits(self):
        for x in (0.0, 70.0, -5.490899085998535, 0.5, 6200.0):
            self.assertAlmostEqual(FX._bits_to_f32(FX._f32_to_bits(x)), x, places=5)
        self.assertEqual(FX._f32_to_bits(1.0), 0x3F800000)

    def test_match_current_preset(self):
        a = {str(p): x for p, x in FX.voice_values(dict(BROADCASTER, volume=-5.490899085998535)).items()}
        b = {str(p): x for p, x in FX.voice_values({}).items()}
        presets = {"Mine": a, "Flat": b}
        mic = {int(p): FX._bits_to_f32(FX._f32_to_bits(x)) for p, x in a.items()}   # as stored (float32)
        self.assertEqual(FX.match_voice_preset(mic, presets), "Mine")
        mic[902] = 11.0
        self.assertIsNone(FX.match_voice_preset(mic, presets))
        self.assertIsNone(FX.match_voice_preset({}, presets))

    def test_setup_constants_complete_the_set(self):
        # G HUB's Blue VO!CE set has 68 properties: the switch, 27 fixed values and 40 preset values.
        self.assertEqual(len(FX.VOICE_CONSTANTS), 27)
        self.assertFalse(set(FX.VOICE_CONSTANTS) & FX.VOICE_PIDS)
        self.assertNotIn(1, FX.VOICE_CONSTANTS)
        self.assertEqual(1 + len(FX.VOICE_CONSTANTS) + len(FX.VOICE_PIDS), 68)

    def test_setup_argument_roundtrip_and_validation(self):
        v = {"surround": 1, "room": 1, "stereo": 0, "bluevoice": 1, "voice": FX.voice_values(BROADCASTER)}
        arg = FX.helper_arg("setup", v)
        self.assertTrue(arg.startswith("setup=1,1,0,1/3:0.0,101:70.0,"))
        self.assertEqual(FX._decode_setup(arg.split("=", 1)[1]), v)
        surround_only = {"surround": 0, "room": 2, "stereo": 3}
        self.assertEqual(FX._decode_setup(FX._encode_setup(surround_only)), surround_only)
        for bad in ("1,9,0", "2,1,0", "1,1,0,1", "1,1,0/3:1.0", "x"):
            self.assertEqual(FX.run_helper("setup=" + bad), 2, bad)

    def test_windows_sonic_writes_what_settings_writes(self):
        from devices import win_policy, win_spatial as S
        writes, state = [], {"on": False}
        orig = (win_policy.set_values, S.is_on)
        win_policy.set_values = lambda dev, values, fx=False: (writes.append(values),
                                                               state.update(on=values[2][3] != bytes(24)))
        S.is_on = lambda dev: state["on"]
        try:
            S.set_sonic("{0.0.0.00000000}.{x}", True)
            keys = [(f, p) for f, p, _vt, _v in writes[-1]]
            self.assertEqual(keys, [S.K_ENCODER, S.K_SETTINGS, S.K_SELECTED, S.K_SIGNAL])   # signal last
            self.assertEqual(writes[-1][-1][2:], (win_policy.VT_I2, 0))
            self.assertEqual([len(v) for _f, _p, _vt, v in writes[-1][:3]], [146, 76, 24])
            self.assertEqual(writes[-1][2][3][8:], S._SONIC_GUID_BYTES)
            from comtypes import GUID
            self.assertEqual(bytes(GUID(S.SONIC)), S._SONIC_GUID_BYTES)
            S.set_sonic("{0.0.0.00000000}.{x}", False)
            self.assertEqual(writes[-1][2][3], bytes(24))
            S.is_on = lambda dev: True                        # Windows didn't take it -> error
            with self.assertRaises(S.SpatialError):
                S.set_sonic("{0.0.0.00000000}.{x}", False, timeout_s=0.3)
        finally:
            win_policy.set_values, S.is_on = orig

    def test_import_from_ghub(self):
        doc = {"cards": [
            {"attribute": "BLUE_VOICE_2_SETTINGS", "name": "Broadcaster 1", "readOnly": True,
             "blueVoice2Settings": {"description": "", "settings": BROADCASTER}},
            {"attribute": "BLUE_VOICE_2_SETTINGS", "name": "My Voice",
             "blueVoice2Settings": {"settings": {"volume": -5.49}}},
            {"attribute": "BLUE_VOICE_2_SETTINGS", "name": "broken", "blueVoice2Settings": {}},
            {"attribute": "CENTURION_PRO_EQ", "name": "My EQ", "centurionProEqualizer": {"gains": [0] * 5}},
        ]}
        path = os.path.join(tempfile.mkdtemp(), "settings.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE data (_id INTEGER PRIMARY KEY, _date_created TEXT, file BLOB)")
        con.execute("INSERT INTO data (file) VALUES (?)", (json.dumps(doc).encode(),))
        con.commit()
        con.close()
        got = ghub_import.read_voice_presets(path)
        self.assertEqual([(p["name"], p["builtin"]) for p in got],
                         [("Broadcaster 1", True), ("My Voice", False)])
        self.assertEqual(got[0]["settings"]["hardLimiter"]["boost"], 10)
        self.assertIsNone(ghub_import.read_voice_presets(os.path.join(tempfile.mkdtemp(), "nope.db")))
