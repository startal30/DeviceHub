"""Device Hub - one tray app for the AULA F99 Pro keyboard, MX Master 3S mouse and PRO X 2 headset.

  pythonw device_hub.py          run the tray app (click the icon to open the panel)
  python  device_hub.py --once   print every device's battery and exit
"""
from __future__ import annotations

import argparse
import copy
import ctypes
import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler

from PIL import Image, ImageDraw, ImageFont

from devices import aula, aula_lighting, logi_audiofx, logi_headset, logi_mouse, win_mic
from estimate import RATED_IS_USE_TIME, Estimator, fmt_duration
import ghub_import

APP_NAME = "DeviceHub"
TITLE = "Device Hub"
OLD_AUTOSTART_NAME = "AulaBattery"        # the earlier keyboard-only app this replaces
POLL_SECONDS = 60
FAST_POLL_SECONDS = 15                    # while the panel is open or something is charging
STALE_SECONDS = 15 * 60                   # keep showing the last % this long while a device sleeps
LOW_WARN = (20, 10)

DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "settings.json")
HISTORY_PATH = os.path.join(DATA_DIR, "battery_history.json")
log = logging.getLogger(APP_NAME)


# =============================================================================== saved settings

def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# =============================================================================== device state

@dataclass
class DevState:
    key: str
    name: str
    kind: str                       # "Keyboard", "Mouse", "Headset"
    online: bool = False
    battery: int | None = None
    charging: bool = False
    detail: str = "Looking for device..."
    last_ok: float = 0.0
    settings: dict = field(default_factory=dict)
    notice: str = ""                # last settings error, shown in the panel
    time_text: str = ""             # "~3 days left", "Charging · full in ~40 min", ...


class Controller:
    """Runs only on the worker thread."""
    key = name = kind = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = DevState(self.key, self.name, self.kind)
        self.was_online = False

    def saved(self) -> dict:
        return self.cfg.setdefault(self.key, {})

    def mark_ok(self, pct: int, charging: bool, detail: str):
        s = self.state
        s.online, s.battery, s.charging, s.detail, s.last_ok = True, pct, charging, detail, time.time()

    def mark_away(self, why: str):
        s = self.state
        s.online = False
        if s.battery is not None and time.time() - s.last_ok < STALE_SECONDS:
            mins = int((time.time() - s.last_ok) // 60)
            s.detail = f"{why} · {mins} min ago" if mins else why
        else:
            s.battery, s.charging, s.detail = None, False, why

    def refresh(self):
        raise NotImplementedError

    def apply(self, name: str, value):
        raise NotImplementedError

    def close(self):
        pass


class KeyboardCtl(Controller):
    key, name, kind = "keyboard", "AULA F99 Pro", "Keyboard"

    def refresh(self):
        r, msg = aula.read_battery()
        if r:
            self.mark_ok(r.percent, r.charging, r.source)
            self._read_lighting()
        else:
            self.mark_away("Asleep" if "didn't answer" in msg else "Not connected")

    def _read_lighting(self):
        st = self.state.settings
        try:
            _, light, _ = aula_lighting.read()
            st["lighting"] = vars(light)
            st.pop("lighting_note", None)
        except aula_lighting.BluetoothOnly as e:
            st.pop("lighting", None)
            st["lighting_note"] = str(e)
        except Exception as e:
            log.warning("keyboard lighting read failed: %s", e)
            st.pop("lighting", None)
            st["lighting_note"] = f"Couldn't read lighting: {e}"

    def apply(self, name, value):
        """Lighting lives in the keyboard itself, so nothing is saved or re-applied here."""
        light = aula_lighting.write(name, value)
        self.state.settings["lighting"] = vars(light)


class MouseCtl(Controller):
    key, name, kind = "mouse", "MX Master 3S", "Mouse"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.mouse: logi_mouse.Mouse | None = None
        self.dpi_range: tuple[int, int] | None = None

    def close(self):
        if self.mouse:
            self.mouse.rx.close()
        self.mouse = None

    def _connect(self) -> bool:
        mice = logi_mouse.find_mice()
        if not mice:
            return False
        mice.sort(key=lambda m: "MX" not in m.name)       # prefer the MX Master if several
        self.mouse = mice[0]
        for other in mice[1:]:
            if other.rx is not self.mouse.rx:
                other.rx.close()
        self.state.name = self.mouse.name or self.name
        return True

    def _read_settings(self):
        m, st = self.mouse, self.state.settings
        if self.dpi_range is None:
            choices = m.dpi_choices()
            self.dpi_range = (min(choices), max(choices)) if choices else (200, 8000)
        st["dpi_range"] = self.dpi_range
        st["dpi"] = m.get_dpi()
        try:
            st["smartshift"] = m.get_smartshift()[1]
        except logi_mouse.HidppError:
            st.pop("smartshift", None)
        st["wheel"] = m.get_wheel()

    def _reapply(self):
        """Mice forget host-set DPI etc. after power cycles - put back what the user chose."""
        st = self.state.settings
        for name, value in self.saved().items():
            if st.get(name) != value:
                log.info("re-applying mouse %s=%s", name, value)
                self._write(name, value)

    def _write(self, name, value):
        m = self.mouse
        if name == "dpi":
            m.set_dpi(int(value))
        elif name == "smartshift":
            m.set_smartshift(int(value))
        elif name == "wheel":
            m.set_wheel(int(value))

    def refresh(self):
        try:
            if self.mouse is None and not self._connect():
                self.mark_away("Asleep or off" if logi_mouse.find_receivers() else "Receiver not plugged in")
                self.was_online = False
                return
            pct, status = self.mouse.battery()
            self._read_settings()
            if not self.was_online:
                self._reapply()
                self._read_settings()
            self.was_online = True
            self.mark_ok(pct, status in ("charging", "full"), "Bolt receiver")
        except logi_mouse.Offline:
            self.was_online = False
            self.mark_away("Asleep")
        except Exception as e:
            log.warning("mouse refresh failed: %s", e)
            self.close()
            self.was_online = False
            self.mark_away("Not connected")

    def apply(self, name, value):
        if self.mouse is None or not self.state.online:
            raise RuntimeError("mouse is asleep - move it and try again")
        self._write(name, value)
        self.saved()[name] = value
        self._read_settings()


class HeadsetCtl(Controller):
    key, name, kind = "headset", "PRO X 2 LIGHTSPEED", "Headset"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.hs: logi_headset.Headset | None = None
        self.state.settings["custom_presets"] = dict(cfg.get("headset_presets", {}))

    def import_ghub(self) -> str:
        """Pull EQ presets and sidetone from G HUB. Returns a short summary for the panel."""
        got = ghub_import.read_headset_settings()
        if got is None:
            return "G HUB settings not found"
        presets = self.cfg.setdefault("headset_presets", {})
        presets.update(got["presets"])
        self.state.settings["custom_presets"] = dict(presets)
        if got["sidetone"] is not None:
            self.saved()["sidetone"] = got["sidetone"]
        self.cfg["ghub_imported"] = time.time()
        log.info("imported from G HUB: %s", got)
        voices = self.import_voice_presets()
        return (f"Imported {len(got['presets'])} EQ presets" + (", sidetone" if got["sidetone"] is not None else "")
                + f" and {voices} Blue VO!CE presets")

    def import_voice_presets(self) -> int:
        """Copy G HUB's Blue VO!CE presets into settings.json, converted to the mic properties they set."""
        got = ghub_import.read_voice_presets()
        if not got:
            return 0
        self.cfg["voice_presets"] = [
            {"name": p["name"], "builtin": p["builtin"],
             "values": {str(pid): v for pid, v in logi_audiofx.voice_values(p["settings"]).items()},
             "mic_level": logi_audiofx.voice_mic_level(p["settings"])}
            for p in got]
        log.info("imported %d Blue VO!CE presets from G HUB", len(got))
        return len(got)

    def _voice_presets(self) -> dict:
        return {p["name"]: p["values"] for p in self.cfg.get("voice_presets", [])}

    def close(self):
        if self.hs:
            self.hs.close()
        self.hs = None

    def _read_settings(self):
        h, st = self.hs, self.state.settings
        st["sidetone"] = h.get_sidetone()
        st["autosleep"] = h.get_autosleep()
        eq = h.get_eq()
        st["eq"] = [g for _, g, _ in eq]
        st["eq_freqs"] = [f for f, _, _ in eq]
        st["custom_presets"] = dict(self.cfg.get("headset_presets", {}))
        try:
            st["noise_reduction"] = h.get_noise_reduction()
        except logi_headset.HeadsetError:
            st.pop("noise_reduction", None)
        try:
            mic = win_mic.get_mic()
        except Exception as e:
            log.warning("windows mic read failed: %s", e)
            mic = None
        if bool(mic) != getattr(self, "_mic_seen", None):
            self._mic_seen = bool(mic)
            log.info("windows mic %s", f"found: level {mic[0]}%, muted={mic[1]}" if mic else "not found")
        if mic:
            st["mic_level"], st["mic_mute"] = mic
        else:
            st.pop("mic_level", None)
            st.pop("mic_mute", None)
        self._read_fx()

    FX_NAMES = ("surround", "room", "stereo", "bluevoice")

    def _read_fx(self):
        st = self.state.settings
        try:
            fx = logi_audiofx.read_state()
        except Exception as e:
            log.warning("audio effects read failed: %s", e)
            fx = {}
        for name in self.FX_NAMES:
            if name in fx:
                st[name] = fx[name]
            else:
                st.pop(name, None)
        try:
            st["surround_no_prompt"] = logi_audiofx.surround_writable()
        except Exception:
            st["surround_no_prompt"] = False
        st["voice_presets"] = [(p["name"], p["builtin"]) for p in self.cfg.get("voice_presets", [])]
        try:
            current = logi_audiofx.read_voice() if "bluevoice" in st else {}
        except Exception as e:
            log.warning("Blue VO!CE settings read failed: %s", e)
            current = {}
        st["voice_preset"] = logi_audiofx.match_voice_preset(current, self._voice_presets())

    def _write(self, name, value):
        if name == "sidetone":
            self.hs.set_sidetone(int(value))
        elif name == "autosleep":
            self.hs.set_autosleep(int(value))
        elif name == "eq":
            self.hs.set_eq([int(v) for v in value])
        elif name == "noise_reduction":
            self.hs.set_noise_reduction(bool(value))
        elif name == "mic_level":
            win_mic.set_mic_level(int(value))
        elif name == "mic_mute":
            win_mic.set_mic_mute(bool(value))

    def _reapply(self):
        st = self.state.settings
        for name, value in self.saved().items():
            if st.get(name) != value:
                log.info("re-applying headset %s=%s", name, value)
                self._write(name, value)

    def refresh(self):
        try:
            if self.hs is None:
                self.hs = logi_headset.Headset()
            pct, status = self.hs.battery()
            self._read_settings()
            if not self.was_online:
                self._reapply()
                self._read_settings()
                # Remember what's on the headset now (set by G HUB) so it can be put back after a reset.
                for name in ("eq", "sidetone"):
                    self.saved().setdefault(name, self.state.settings[name])
                save_config(self.cfg)
            self.was_online = True
            self.mark_ok(pct, status == "charging", "LIGHTSPEED")
        except logi_headset.Offline:
            self.was_online = False
            self.mark_away("Turned off")
        except logi_headset.HeadsetError as e:
            self.close()
            self.was_online = False
            self.mark_away("Dongle not plugged in" if "not plugged" in str(e) else "Not connected")
        except Exception as e:
            log.warning("headset refresh failed: %s", e)
            self.close()
            self.was_online = False
            self.mark_away("Not connected")

    def apply(self, name, value):
        if name == "voice_preset":
            # A whole Blue VO!CE preset in one elevated write (one permission prompt).
            preset = next((p for p in self.cfg.get("voice_presets", []) if p["name"] == value), None)
            if preset is None:
                raise RuntimeError(f"unknown Blue VO!CE preset {value!r}")
            logi_audiofx.apply_elevated("voice", preset["values"])
            # The preset's input gain is the Windows recording level (that's how G HUB applies it).
            level = preset.get("mic_level")
            if level is not None and self.state.settings.get("mic_level") is not None:
                win_mic.set_mic_level(level)
                self.saved()["mic_level"] = level
            if self.hs is not None and self.state.online:
                self._read_settings()
            else:
                self._read_fx()
            if self.state.settings.get("voice_preset") != value:
                raise RuntimeError("the microphone didn't take the preset")
            log.info("Blue VO!CE preset applied: %s (mic level %s%%)", value, level)
            return
        if name in self.FX_NAMES:
            # Windows audio effects: needs admin, so a UAC prompt appears. Not saved for re-applying,
            # because re-applying would mean surprise permission prompts; the setting persists anyway.
            if not logi_audiofx.write_without_prompt(name, int(value)):
                logi_audiofx.apply_elevated(name, int(value))
            self._read_fx()
            return
        if self.hs is None or not self.state.online:
            raise RuntimeError("headset is off - turn it on and try again")
        self._write(name, value)
        self.saved()[name] = value
        self._read_settings()


class Hub:
    """Owns the devices. All device I/O happens on one worker thread, so requests never overlap."""

    def __init__(self, on_change):
        self.cfg = load_config()
        self.ctls = [KeyboardCtl(self.cfg), MouseCtl(self.cfg), HeadsetCtl(self.cfg)]
        self.by_key = {c.key: c for c in self.ctls}
        self.jobs: queue.Queue = queue.Queue()
        self.on_change = on_change
        self.stop = threading.Event()
        self.fast = False                     # set by the UI while the panel is open
        self.est = Estimator(HISTORY_PATH)
        if "ghub_imported" not in self.cfg:
            try:
                self.by_key["headset"].import_ghub()
                save_config(self.cfg)
            except Exception:
                log.exception("G HUB import failed")
        elif "voice_presets" not in self.cfg or any("mic_level" not in p for p in self.cfg["voice_presets"]):
            # installs from before Blue VO!CE presets (or their mic level) existed
            try:
                self.by_key["headset"].import_voice_presets()
                save_config(self.cfg)
            except Exception:
                log.exception("Blue VO!CE preset import failed")

    def import_ghub(self):
        self.jobs.put(("import", "headset", None, None))

    def poll_interval(self) -> int:
        charging = any(c.state.charging and c.state.online for c in self.ctls)
        return FAST_POLL_SECONDS if (self.fast or charging) else POLL_SECONDS

    def _update_estimate(self, c: Controller):
        s = c.state
        if not s.online or s.battery is None:
            s.time_text = ""
            return
        self.est.update(c.key, s.battery, s.charging)
        if s.charging and s.battery >= 100:
            s.time_text = "Fully charged"
            return
        hours, basis = self.est.estimate(c.key, s.battery, s.charging)
        typical = self.est.accuracy(c.key, s.charging)[0] if basis == "usage" else None
        acc = f" · ±{max(5, round(typical * 20) * 5)}%" if typical is not None else ""   # nearest 5%
        if s.charging:
            s.time_text = f"Charging · full in ~{fmt_duration(hours)}{acc}" if hours else "Charging"
        elif hours is None:
            s.time_text = "Measuring battery life…"
        elif basis == "rated":
            use = " of use" if RATED_IS_USE_TIME.get(c.key) else ""
            s.time_text = f"~{fmt_duration(hours)}{use} left · rated"
        else:
            s.time_text = f"~{fmt_duration(hours)} of use left{acc}"

    def battery_report(self):
        self.jobs.put(("report", None, None, None))

    def _write_report(self):
        text = self.est.report({c.key: f"{c.kind} · {c.name}" for c in self.ctls})
        path = os.path.join(os.path.dirname(HISTORY_PATH), "battery_report.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.startfile(path)

    def snapshot(self) -> list[DevState]:
        return [copy.deepcopy(c.state) for c in self.ctls]

    def refresh_now(self):
        self.jobs.put(("refresh", None, None, None))

    def set(self, key: str, name: str, value):
        self.jobs.put(("set", key, name, value))

    def _poll(self):
        for c in self.ctls:
            try:
                c.refresh()
                self._update_estimate(c)
            except Exception:
                log.exception("%s refresh crashed", c.key)
            self.on_change(self.snapshot())     # show each device as soon as it's read

    def run(self):
        next_poll = 0.0
        while not self.stop.is_set():
            try:
                job = self.jobs.get(timeout=max(0.0, next_poll - time.time()))
            except queue.Empty:
                job = ("refresh", None, None, None)
            if self.stop.is_set():
                break
            kind, key, name, value = job
            if kind == "refresh":
                self._poll()
                next_poll = time.time() + self.poll_interval()
                continue
            if kind == "report":
                try:
                    self._write_report()
                except Exception:
                    log.exception("battery report failed")
                continue
            ctl = self.by_key[key]
            ctl.state.notice = ""
            if kind == "import":
                try:
                    ctl.state.notice = ctl.import_ghub()
                    save_config(self.cfg)
                    if ctl.state.online and "sidetone" in ctl.saved():
                        ctl.apply("sidetone", ctl.saved()["sidetone"])
                except Exception as e:
                    ctl.state.notice = f"Import failed: {e}"
                self.on_change(self.snapshot())
                continue
            try:
                ctl.apply(name, value)
                save_config(self.cfg)
            except Exception as e:
                log.warning("apply %s.%s=%s failed: %s", key, name, value, e)
                ctl.state.notice = f"Couldn't apply: {e}"
            self.on_change(self.snapshot())
        for c in self.ctls:
            c.close()


# =============================================================================== tray icon image

def _font(size: int):
    for f in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def battery_color(pct: int | None, charging: bool = False) -> str:
    if pct is None:
        return "#6b6f76"
    if charging:
        return "#4f7cff"
    return "#2ea043" if pct > 40 else "#d4a017" if pct > 20 else "#e5484d"


def lowest(states: list[DevState]) -> DevState | None:
    known = [s for s in states if s.battery is not None]
    return min(known, key=lambda s: s.battery) if known else None


def make_icon(states: list[DevState], bolt: bool = False) -> Image.Image:
    """Shows the LOWEST battery among the devices, so the icon warns about whichever needs charging.
    bolt=True draws a lightning bolt instead of the number (alternated to animate charging)."""
    low = lowest(states)
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = battery_color(low.battery if low else None, low.charging if low else False)
    if bolt:
        d.rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=color)
        d.polygon([(36, 4), (14, 36), (30, 36), (24, 60), (50, 24), (34, 24), (42, 4)], fill="white")
        return img
    text = "--" if low is None else ("F" if low.battery >= 100 else str(low.battery))
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=color)
    font = _font(46 if len(text) == 1 else 40)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text, font=font, fill="white")
    return img


def tooltip(states: list[DevState]) -> str:
    parts = [(f"{s.kind} {s.battery}%{' ⚡' if s.charging else ''}" if s.battery is not None else f"{s.kind} --")
             for s in states]
    return " · ".join(parts)[:127]


# =============================================================================== autostart

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_cmd() -> str:
    """--tray: when Windows starts it, stay in the tray instead of opening the panel."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" "{os.path.abspath(__file__)}" --tray'


def _run_value(name: str) -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def autostart_enabled() -> bool:
    return _run_value(APP_NAME) is not None


def set_autostart(on: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_cmd())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def migrate_old_autostart():
    """The keyboard-only AulaBattery app is replaced by this one: move its 'start with Windows' over."""
    if _run_value(OLD_AUTOSTART_NAME) is None:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.DeleteValue(k, OLD_AUTOSTART_NAME)
    set_autostart(True)
    log.info("moved autostart from %s to %s", OLD_AUTOSTART_NAME, APP_NAME)


# =============================================================================== panel UI

C = {
    "bg": "#17181b", "card": "#222428", "line": "#2e3136", "text": "#f2f3f5", "muted": "#9aa0a8",
    "accent": "#4f7cff", "accent_text": "#ffffff", "btn": "#2e3136", "error": "#ff6b6b",
    "charge": "#7fa2ff", "charge_wave": "#2f4a8f", "charge_glint": "#b9cbff",
}
AUTOSLEEP_CHOICES = [(0, "Never"), (10, "10 min"), (20, "20 min"), (30, "30 min"), (60, "1 hr")]


class Slider:
    """A themed slider that snaps to `step` and shows its value as text (beside it, or above if vertical)."""

    def __init__(self, panel, parent, key, frm, to, step, orient, length, fmt):
        import tkinter as tk
        from tkinter import ttk
        self.step, self.fmt, self.lo, self.hi = step, fmt, min(frm, to), max(frm, to)
        self.frame = tk.Frame(parent, bg=C["card"])
        self.value = tk.Label(self.frame, bg=C["card"], fg=C["text"], font=("Segoe UI", 9),
                              width=8 if orient == "horizontal" else 5,
                              anchor="e" if orient == "horizontal" else "center")
        self.scale = ttk.Scale(self.frame, from_=frm, to=to, orient=orient, length=length,
                               style=f"Hub.{orient.capitalize()}.TScale", command=self._moved)
        if orient == "horizontal":
            self.scale.pack(side="left", fill="x", expand=True)
            self.value.pack(side="right")
        else:
            self.value.pack()
            self.scale.pack()
        self.scale.bind("<ButtonPress-1>", lambda e: panel.dragging.add(key), add="+")

    def _snap(self, v) -> int:
        v = round(float(v) / self.step) * self.step
        return int(max(self.lo, min(self.hi, v)))

    def _moved(self, v):
        self.value.configure(text=self.fmt.format(self._snap(v)))

    def get(self) -> int:
        return self._snap(self.scale.get())

    def set(self, v):
        self.scale.set(v)
        self._moved(v)

    def configure(self, **kw):
        if "from_" in kw or "to" in kw:
            self.lo, self.hi = kw.get("from_", self.lo), kw.get("to", self.hi)
        if "state" in kw:
            self.scale.state(["disabled"] if kw.pop("state") == "disabled" else ["!disabled"])
            self.value.configure(fg=C["muted"] if self.scale.instate(["disabled"]) else C["text"])
        if kw:
            self.scale.configure(**kw)

    def bind(self, *a, **kw):
        self.scale.bind(*a, **kw)

    def pack(self, **kw):
        self.frame.pack(**kw)


def _style_sliders(root):
    from tkinter import ttk
    s = ttk.Style(root)
    s.theme_use("clam")
    for orient in ("Horizontal", "Vertical"):
        s.configure(f"Hub.{orient}.TScale", background=C["accent"], troughcolor=C["line"],
                    bordercolor=C["line"], lightcolor=C["accent"], darkcolor=C["accent"],
                    gripcount=0, sliderthickness=14, troughrelief="flat")
        s.map(f"Hub.{orient}.TScale", background=[("disabled", C["muted"]), ("active", "#6d93ff")])


class Panel:
    def __init__(self, root, hub: Hub):
        import tkinter as tk
        self.tk = tk
        self.root = root
        _style_sliders(root)
        self.hub = hub
        self.scale_px = root.winfo_fpixels("1i") / 96.0
        self.dragging: set[str] = set()
        self.cards: dict[str, dict] = {}
        self.expanded = {"keyboard": False, "mouse": False, "headset": False}
        self.states: list[DevState] = []

        root.title(TITLE)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=C["line"])
        self.body = tk.Frame(root, bg=C["bg"], padx=self.px(14), pady=self.px(12))
        self.body.pack(fill="both", expand=True, padx=1, pady=1)

        head = tk.Frame(self.body, bg=C["bg"])
        head.pack(fill="x", pady=(0, self.px(8)))
        tk.Label(head, text=TITLE, bg=C["bg"], fg=C["text"], font=("Segoe UI Semibold", 12)).pack(side="left")
        self._flat_button(head, "✕", self.hide).pack(side="right")
        self._flat_button(head, "⟳", hub.refresh_now).pack(side="right", padx=(0, self.px(4)))

        for key in ("keyboard", "mouse", "headset"):
            self._build_card(key)
        root.bind("<Escape>", lambda e: self.hide())
        root.bind("<FocusOut>", lambda e: root.after(200, self._hide_if_unfocused))
        root.withdraw()

    # ------------------------------------------------------------------ helpers
    def px(self, v: float) -> int:
        return int(round(v * self.scale_px))

    def _flat_button(self, parent, text, cmd, active=False, small=False):
        b = self.tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, cursor="hand2",
                           font=("Segoe UI", 8 if small else 10),
                           padx=self.px(8 if small else 6), pady=self.px(2),
                           activebackground=C["accent"], activeforeground=C["accent_text"])
        self._paint_button(b, active)
        return b

    def _paint_button(self, b, active):
        b.configure(bg=C["accent"] if active else C["btn"], fg=C["accent_text"] if active else C["text"])

    def _label(self, parent, text="", muted=False, size=9, bold=False, **kw):
        return self.tk.Label(parent, text=text, bg=kw.pop("bg", C["card"]), fg=C["muted"] if muted else C["text"],
                             font=("Segoe UI Semibold" if bold else "Segoe UI", size), **kw)

    def _scale(self, parent, key, frm, to, step, orient="horizontal", length=220, fmt="{}"):
        return Slider(self, parent, key, frm, to, step, orient, self.px(length), fmt)

    def _on_release(self, scale, key, devkey, name, transform=lambda v: v):
        def handler(_e):
            self.dragging.discard(key)
            self.hub.set(devkey, name, transform(scale.get()))
        scale.bind("<ButtonRelease-1>", handler)

    # ------------------------------------------------------------------ cards
    def _build_card(self, key):
        tk = self.tk
        card = tk.Frame(self.body, bg=C["card"], padx=self.px(12), pady=self.px(10))
        card.pack(fill="x", pady=(0, self.px(8)))
        top = tk.Frame(card, bg=C["card"])
        top.pack(fill="x")
        left = tk.Frame(top, bg=C["card"])
        left.pack(side="left", fill="x", expand=True)
        name = self._label(left, size=10, bold=True, anchor="w")
        name.pack(fill="x")
        detail = self._label(left, muted=True, anchor="w")
        detail.pack(fill="x")
        when = self._label(left, anchor="w")
        when.pack(fill="x")
        right = tk.Frame(top, bg=C["card"])
        right.pack(side="right")
        pct = self._label(right, size=14, bold=True, anchor="e")
        pct.pack(anchor="e")
        bar = tk.Canvas(right, width=self.px(90), height=self.px(6), bg=C["line"], highlightthickness=0, bd=0)
        bar.pack(anchor="e", pady=(self.px(2), 0))
        parts = {"card": card, "name": name, "detail": detail, "time": when, "pct": pct, "bar": bar}
        toggle = self._flat_button(card, "Settings ▾", lambda k=key: self.toggle_settings(k), small=True)
        toggle.pack(anchor="w", pady=(self.px(8), 0))
        box = tk.Frame(card, bg=C["card"])
        notice = self._label(card, anchor="w", wraplength=self.px(320))
        notice.configure(fg=C["error"])
        parts.update(toggle=toggle, box=box, notice=notice)
        {"keyboard": self._build_keyboard_settings, "mouse": self._build_mouse_settings,
         "headset": self._build_headset_settings}[key](box, parts)
        self.cards[key] = parts

    def _build_keyboard_settings(self, box, parts):
        tk = self.tk
        note = self._label(box, muted=True, anchor="w", justify="left", wraplength=self.px(320))
        controls = tk.Frame(box, bg=C["card"])

        self._label(controls, "Lighting effect", muted=True, anchor="w").pack(fill="x", pady=(self.px(8), self.px(2)))
        grid = tk.Frame(controls, bg=C["card"])
        grid.pack(fill="x")
        effect_btns = {}
        order = [0] + list(range(1, 20))
        for i, eff in enumerate(order):
            b = self._flat_button(grid, aula_lighting.EFFECT_NAMES[eff],
                                  lambda e=eff: self.hub.set("keyboard", "effect", e), small=True)
            b.grid(row=i // 4, column=i % 4, sticky="ew", padx=(0, self.px(3)), pady=(0, self.px(3)))
            effect_btns[eff] = b
        for c in range(4):
            grid.columnconfigure(c, weight=1, uniform="fx")

        self._label(controls, "Brightness", muted=True, anchor="w").pack(fill="x", pady=(self.px(6), 0))
        bright = self._scale(controls, "kb_brightness", 0, aula_lighting.BRIGHT_MAX, 1)
        bright.pack(fill="x")
        self._on_release(bright, "kb_brightness", "keyboard", "brightness", int)
        self._label(controls, "Speed", muted=True, anchor="w").pack(fill="x", pady=(self.px(6), 0))
        speed = self._scale(controls, "kb_speed", 0, aula_lighting.SPEED_MAX, 1)
        speed.pack(fill="x")
        self._on_release(speed, "kb_speed", "keyboard", "speed", int)

        self._label(controls, "Colour", muted=True, anchor="w").pack(fill="x", pady=(self.px(6), self.px(2)))
        crow = tk.Frame(controls, bg=C["card"])
        crow.pack(fill="x")
        color_btns = {}
        for idx, name in enumerate(aula_lighting.COLOR_NAMES):
            hexc = aula_lighting.COLOR_HEX[idx]
            b = tk.Button(crow, text="" if hexc else "Random", width=3 if hexc else 7, relief="flat", bd=0,
                          cursor="hand2", bg=hexc or C["btn"], fg=C["text"], activebackground=hexc or C["accent"],
                          font=("Segoe UI", 8), highlightthickness=2, highlightbackground=C["card"],
                          command=lambda i=idx: self.hub.set("keyboard", "color", i))
            b.pack(side="left", padx=(0, self.px(4)))
            color_btns[idx] = b

        self._label(controls, "Keyboard sleeps after", muted=True, anchor="w").pack(fill="x", pady=(self.px(6), self.px(2)))
        srow = tk.Frame(controls, bg=C["card"])
        srow.pack(fill="x")
        sleep_btns = {}
        for i, secs in enumerate(aula_lighting.SLEEP_SECONDS):
            text = f"{secs // 60} min" if secs % 60 == 0 else f"{secs / 60:g} min"
            b = self._flat_button(srow, text, lambda s=secs: self.hub.set("keyboard", "sleep_seconds", s), small=True)
            b.grid(row=i // 5, column=i % 5, sticky="ew", padx=(0, self.px(3)), pady=(0, self.px(3)))
            sleep_btns[secs] = b
        for c in range(5):
            srow.columnconfigure(c, weight=1, uniform="sl")
        parts.update(kb_note=note, kb_controls=controls, effect_btns=effect_btns, kb_bright=bright,
                     kb_speed=speed, color_btns=color_btns, kb_sleep_btns=sleep_btns)

    def _update_keyboard(self, p, s):
        light = s.settings.get("lighting")
        note = s.settings.get("lighting_note") or ("Keyboard not connected." if not s.online else "Reading lighting…")
        if light and s.online:
            p["kb_note"].pack_forget()
            p["kb_controls"].pack(fill="x")
            for eff, b in p["effect_btns"].items():
                self._paint_button(b, light["effect"] == eff)
            self._set_scale("kb_brightness", p["kb_bright"], light["brightness"])
            self._set_scale("kb_speed", p["kb_speed"], light["speed"])
            for idx, b in p["color_btns"].items():
                on = light["color"] == idx
                if aula_lighting.COLOR_HEX[idx]:
                    b.configure(text="✓" if on else "", fg="#111111")
                else:
                    self._paint_button(b, on)
            for secs, b in p["kb_sleep_btns"].items():
                self._paint_button(b, light["sleep_seconds"] == secs)
        else:
            p["kb_controls"].pack_forget()
            p["kb_note"].configure(text=note)
            p["kb_note"].pack(fill="x", pady=(self.px(8), 0))

    def _build_mouse_settings(self, box, parts):
        tk = self.tk
        self._label(box, "Pointer speed (DPI)", muted=True, anchor="w").pack(fill="x", pady=(self.px(8), 0))
        dpi = self._scale(box, "dpi", 200, 8000, 50, fmt="{} DPI")
        dpi.pack(fill="x")
        self._on_release(dpi, "dpi", "mouse", "dpi", int)

        self._label(box, "SmartShift: wheel switches to free-spin above this speed", muted=True,
                    anchor="w").pack(fill="x", pady=(self.px(8), 0))
        ss = self._scale(box, "smartshift", 1, 50, 1)
        ss.pack(fill="x")
        self._on_release(ss, "smartshift", "mouse", "smartshift", int)
        always = tk.BooleanVar()
        cb_always = tk.Checkbutton(box, text="SmartShift off (always ratchet)", variable=always,
                                   command=lambda: self.hub.set("mouse", "smartshift",
                                                                255 if always.get() else int(ss.get())),
                                   **self._check_style())
        cb_always.pack(anchor="w")

        invert = tk.BooleanVar()
        hires = tk.BooleanVar()
        self._label(box, "Scroll wheel", muted=True, anchor="w").pack(fill="x", pady=(self.px(8), 0))
        tk.Checkbutton(box, text="Reverse scroll direction", variable=invert,
                       command=lambda: self._set_wheel_bit(0x04, invert.get()), **self._check_style()).pack(anchor="w")
        tk.Checkbutton(box, text="High-resolution (smooth) scrolling", variable=hires,
                       command=lambda: self._set_wheel_bit(0x02, hires.get()), **self._check_style()).pack(anchor="w")
        parts.update(dpi=dpi, ss=ss, always=always, cb_always=cb_always, invert=invert, hires=hires)

    def _set_wheel_bit(self, bit, on):
        cur = self._state("mouse").settings.get("wheel", 0)
        self.hub.set("mouse", "wheel", (cur | bit) if on else (cur & ~bit))

    def _build_headset_settings(self, box, parts):
        tk = self.tk
        # Three tabs keep the panel short enough for any screen.
        tabbar = tk.Frame(box, bg=C["card"])
        tabbar.pack(fill="x", pady=(self.px(8), 0))
        pages = {name: tk.Frame(box, bg=C["card"]) for name in ("Sound", "Mic", "Power")}
        tab_btns = {}

        def show(name):
            for n, f in pages.items():
                f.pack_forget()
                self._paint_button(tab_btns[n], n == name)
            pages[name].pack(fill="x")
            parts["tab"] = name
            self.place()

        for name in pages:
            b = self._flat_button(tabbar, name, lambda n=name: show(n), small=True)
            b.pack(side="left", padx=(0, self.px(4)))
            tab_btns[name] = b
        parts.update(tab_btns=tab_btns, tab="Sound")

        # ---- Sound: EQ + DTS surround
        snd = pages["Sound"]
        self._label(snd, "Equalizer", muted=True, anchor="w").pack(fill="x", pady=(self.px(8), self.px(2)))
        prow = tk.Frame(snd, bg=C["card"])
        prow.pack(fill="x")
        preset_btns = {}
        for name, gains in logi_headset.EQ_PRESETS.items():
            b = self._flat_button(prow, name, lambda g=gains: self.hub.set("headset", "eq", list(g)), small=True)
            b.pack(side="left", padx=(0, self.px(4)))
            preset_btns[name] = b
        custom = tk.Frame(snd, bg=C["card"])
        custom.pack(fill="x")
        bands = tk.Frame(snd, bg=C["card"])
        bands.pack(fill="x", pady=(self.px(4), 0))
        eq_scales, eq_labels = [], []
        for i in range(5):
            col = tk.Frame(bands, bg=C["card"])
            col.pack(side="left", expand=True)
            s = self._scale(col, f"eq{i}", 12, -12, 1, orient="vertical", length=110, fmt="{:+d}")
            s.pack()
            s.bind("<ButtonRelease-1>", lambda e, i=i: self._eq_release(i))
            lab = self._label(col, "", muted=True, size=8)
            lab.pack()
            eq_scales.append(s)
            eq_labels.append(lab)

        self._label(snd, "Surround sound (DTS Headphone:X 2.0)", muted=True, anchor="w").pack(
            fill="x", pady=(self.px(8), 0))
        surround = tk.BooleanVar()
        cb_surround = tk.Checkbutton(snd, text="Enable surround sound", variable=surround,
                                     command=lambda: self.hub.set("headset", "surround", surround.get()),
                                     **self._check_style())
        cb_surround.pack(anchor="w")
        room_row = tk.Frame(snd, bg=C["card"])
        room_row.pack(fill="x", pady=(self.px(2), 0))
        room_btns = {}
        for val, text in logi_audiofx.ROOM_MODELS.items():
            b = self._flat_button(room_row, text, lambda v=val: self.hub.set("headset", "room", v), small=True)
            b.pack(side="left", padx=(0, self.px(4)))
            room_btns[val] = b
        self._label(snd, "Super Stereo (for stereo sources)", muted=True, anchor="w").pack(
            fill="x", pady=(self.px(4), self.px(2)))
        stereo_row = tk.Frame(snd, bg=C["card"])
        stereo_row.pack(fill="x")
        stereo_btns = {}
        for val, text in logi_audiofx.STEREO_MODELS.items():
            b = self._flat_button(stereo_row, text, lambda v=val: self.hub.set("headset", "stereo", v), small=True)
            b.pack(side="left", padx=(0, self.px(4)))
            stereo_btns[val] = b
        fx_note = self._label(snd, "", muted=True, size=8, anchor="w", justify="left", wraplength=self.px(280))
        fx_note.pack(fill="x", pady=(self.px(4), 0))
        parts["fx_note"] = fx_note

        # ---- Mic: level, mute, noise reduction, Blue VO!CE, sidetone
        micp = pages["Mic"]
        self._label(micp, "Microphone level", muted=True, anchor="w").pack(fill="x", pady=(self.px(8), 0))
        mic = self._scale(micp, "mic_level", 0, 100, 1, fmt="{}%")
        mic.pack(fill="x")
        self._on_release(mic, "mic_level", "headset", "mic_level", int)
        mic_mute, nr, bluevoice = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()
        tk.Checkbutton(micp, text="Mute microphone", variable=mic_mute,
                       command=lambda: self.hub.set("headset", "mic_mute", mic_mute.get()),
                       **self._check_style()).pack(anchor="w")
        cb_nr = tk.Checkbutton(micp, text="Noise reduction (in the headset)", variable=nr,
                               command=lambda: self.hub.set("headset", "noise_reduction", nr.get()),
                               **self._check_style())
        cb_nr.pack(anchor="w")
        cb_bluevoice = tk.Checkbutton(micp, text="Blue VO!CE voice processing", variable=bluevoice,
                                      command=lambda: self.hub.set("headset", "bluevoice", bluevoice.get()),
                                      **self._check_style())
        cb_bluevoice.pack(anchor="w")
        vrow = tk.Frame(micp, bg=C["card"])
        vrow.pack(fill="x", pady=(self.px(2), 0))
        self._label(vrow, "Preset", muted=True).pack(side="left", padx=(self.px(2), self.px(6)))
        voice_toggle = self._flat_button(vrow, "…", lambda: self._toggle_voice_list(), small=True)
        voice_toggle.pack(side="left")
        voice_list = tk.Frame(micp, bg=C["card"])            # packed under vrow while open
        self._label(micp, "Changing Blue VO!CE or its preset asks Windows for permission.", muted=True,
                    size=8, anchor="w", justify="left", wraplength=self.px(280)).pack(fill="x")
        parts.update(vrow=vrow, voice_toggle=voice_toggle, voice_list=voice_list, voice_open=False,
                     voice_btns={}, voice_names=None)
        self._label(micp, "Sidetone (hear your own voice)", muted=True, anchor="w").pack(
            fill="x", pady=(self.px(8), 0))
        st = self._scale(micp, "sidetone", 0, 100, 1, fmt="{}%")
        st.pack(fill="x")
        self._on_release(st, "sidetone", "headset", "sidetone", int)

        # ---- Power: auto-off
        pwr = pages["Power"]
        self._label(pwr, "Turn off when idle", muted=True, anchor="w").pack(fill="x", pady=(self.px(8), self.px(2)))
        row = tk.Frame(pwr, bg=C["card"])
        row.pack(fill="x")
        sleep_btns = {}
        for minutes, text in AUTOSLEEP_CHOICES:
            b = self._flat_button(row, text, lambda m=minutes: self.hub.set("headset", "autosleep", m), small=True)
            b.pack(side="left", padx=(0, self.px(4)))
            sleep_btns[minutes] = b

        parts.update(sidetone=st, sleep_btns=sleep_btns, preset_btns=preset_btns, eq_scales=eq_scales,
                     eq_labels=eq_labels, custom=custom, custom_btns={}, custom_names=None,
                     mic=mic, mic_mute=mic_mute, nr=nr, cb_nr=cb_nr, surround=surround, cb_surround=cb_surround,
                     room_btns=room_btns, stereo_btns=stereo_btns, bluevoice=bluevoice, cb_bluevoice=cb_bluevoice)
        show("Sound")

    def _toggle_voice_list(self):
        p = self.cards["headset"]
        p["voice_open"] = not p["voice_open"]
        if p["voice_open"]:
            p["voice_list"].pack(fill="x", pady=(self.px(4), self.px(2)), after=p["vrow"])
        else:
            p["voice_list"].pack_forget()
        self._paint_voice(p, p.get("voice_current"))
        self.place()

    def _pick_voice(self, name):
        self._toggle_voice_list()
        self.hub.set("headset", "voice_preset", name)

    def _build_voice_presets(self, p, presets: list):
        """Two columns of buttons: your presets first, then G HUB's built-in ones."""
        tk = self.tk
        box = p["voice_list"]
        for w in box.winfo_children():
            w.destroy()
        p["voice_btns"] = {}
        row = 0
        for title, group in (("Your presets", [n for n, b in presets if not b]),
                             ("G HUB presets", [n for n, b in presets if b])):
            if not group:
                continue
            self._label(box, title, muted=True, size=8, anchor="w").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(self.px(2), self.px(1)))
            row += 1
            for i, name in enumerate(group):
                text = name if len(name) <= 22 else name[:21].rstrip() + "…"
                b = self._flat_button(box, text, lambda n=name: self._pick_voice(n), small=True)
                b.grid(row=row + i // 2, column=i % 2, sticky="ew", padx=(0, self.px(4)), pady=(0, self.px(3)))
                p["voice_btns"][name] = b
            row += (len(group) + 1) // 2
        box.columnconfigure(0, weight=1, uniform="v")
        box.columnconfigure(1, weight=1, uniform="v")

    def _paint_voice(self, p, current):
        p["voice_current"] = current
        if not p["voice_names"]:
            label = "Import from G HUB (tray menu)"
        else:
            label = current or "Custom settings"
            if len(label) > 30:
                label = label[:29].rstrip() + "…"
        p["voice_toggle"].configure(text=f"{label}  {'▴' if p['voice_open'] else '▾'}")
        for name, b in p["voice_btns"].items():
            self._paint_button(b, name == current)

    def _build_custom_presets(self, p, presets: dict):
        for w in p["custom"].winfo_children():
            w.destroy()
        p["custom_btns"] = {}
        if presets:
            self._label(p["custom"], "Your presets (from G HUB)", muted=True, anchor="w").pack(
                fill="x", pady=(self.px(6), self.px(2)))
        for name, gains in presets.items():
            short = name if len(name) <= 40 else name[:38] + "…"
            b = self._flat_button(p["custom"], short, lambda g=gains: self.hub.set("headset", "eq", list(g)), small=True)
            b.configure(anchor="w")
            b.pack(fill="x", pady=(0, self.px(3)))
            p["custom_btns"][name] = (b, gains)
        p["custom_names"] = list(presets)

    def _eq_release(self, i):
        self.dragging.discard(f"eq{i}")
        gains = [int(s.get()) for s in self.cards["headset"]["eq_scales"]]
        self.hub.set("headset", "eq", gains)

    def _check_style(self):
        return dict(bg=C["card"], fg=C["text"], selectcolor=C["line"], activebackground=C["card"],
                    activeforeground=C["text"], highlightthickness=0, bd=0, font=("Segoe UI", 9), anchor="w")

    def toggle_settings(self, key):
        """Only one settings section open at a time, so the panel always fits on screen."""
        opening = not self.expanded[key]
        for k in self.expanded:
            self.expanded[k] = opening and k == key
            p = self.cards[k]
            if self.expanded[k]:
                p["box"].pack(fill="x")
                p["toggle"].configure(text="Settings ▴")
            else:
                p["box"].pack_forget()
                p["toggle"].configure(text="Settings ▾")
        self.place()

    # ------------------------------------------------------------------ updates
    def _state(self, key) -> DevState:
        return next(s for s in self.states if s.key == key)

    def update(self, states: list[DevState]):
        self.states = states
        for s in states:
            p = self.cards[s.key]
            p["name"].configure(text=f"{s.kind} · {s.name}")
            p["detail"].configure(text=s.detail)
            p["time"].configure(text=s.time_text, fg=C["charge"] if s.charging else C["text"])
            if s.time_text:
                p["time"].pack(fill="x")
            else:
                p["time"].pack_forget()
            p["pct"].configure(text="--" if s.battery is None else f"{'⚡' if s.charging else ''}{s.battery}%",
                               fg=C["text"] if s.online else C["muted"])
            self._draw_bar(p, s)
            if "notice" in p:
                if s.notice:
                    bad = s.notice.startswith(("Couldn't", "Import failed", "G HUB settings not found"))
                    p["notice"].configure(text=s.notice, fg=C["error"] if bad else C["charge"])
                    p["notice"].pack(fill="x", pady=(self.px(4), 0))
                else:
                    p["notice"].pack_forget()
            if s.key == "mouse":
                self._update_mouse(p, s)
            elif s.key == "headset":
                self._update_headset(p, s)
            elif s.key == "keyboard":
                self._update_keyboard(p, s)
        self.place()

    def _set_scale(self, key, scale, value):
        if key not in self.dragging and value is not None and scale.get() != value:
            scale.set(value)

    def _update_mouse(self, p, s):
        st = s.settings
        if "dpi_range" in st:
            lo, hi = st["dpi_range"]
            p["dpi"].configure(from_=lo, to=hi)
        self._set_scale("dpi", p["dpi"], st.get("dpi"))
        ss = st.get("smartshift")
        if ss is not None:
            off = ss > 50
            p["always"].set(off)
            p["ss"].configure(state="disabled" if off else "normal")
            if not off:
                self._set_scale("smartshift", p["ss"], ss)
        wheel = st.get("wheel", 0)
        p["invert"].set(bool(wheel & 0x04))
        p["hires"].set(bool(wheel & 0x02))

    def _update_headset(self, p, s):
        st = s.settings
        self._set_scale("sidetone", p["sidetone"], st.get("sidetone"))
        self._set_scale("mic_level", p["mic"], st.get("mic_level"))
        p["mic_mute"].set(bool(st.get("mic_mute")))
        p["nr"].set(bool(st.get("noise_reduction")))
        p["cb_nr"].configure(state="normal" if "noise_reduction" in st else "disabled")
        # DTS surround / Blue VO!CE (Logitech's installed audio effects)
        p["surround"].set(bool(st.get("surround")))
        p["cb_surround"].configure(state="normal" if "surround" in st else "disabled")
        for val, b in p["room_btns"].items():
            self._paint_button(b, st.get("surround") and st.get("room") == val)
        for val, b in p["stereo_btns"].items():
            self._paint_button(b, st.get("stereo") == val)
        p["bluevoice"].set(bool(st.get("bluevoice")))
        p["cb_bluevoice"].configure(state="normal" if "bluevoice" in st else "disabled")
        voices = st.get("voice_presets", [])
        if p["voice_names"] != voices:
            p["voice_names"] = voices
            self._build_voice_presets(p, voices)
        p["voice_toggle"].configure(state="normal" if voices and "bluevoice" in st else "disabled")
        self._paint_voice(p, st.get("voice_preset"))
        if "surround" not in st:
            p["fx_note"].configure(text="Surround isn't available: Logitech's DTS audio effect isn't installed.")
        elif st.get("surround_no_prompt"):
            p["fx_note"].configure(text="")
        else:
            p["fx_note"].configure(text="Windows asks for permission for each surround change. "
                                        "Run tools\\allow-surround-without-prompt.ps1 once to stop that.")
        for minutes, b in p["sleep_btns"].items():
            self._paint_button(b, st.get("autosleep") == minutes)
        gains = st.get("eq")
        custom = st.get("custom_presets", {})
        if p["custom_names"] != list(custom):
            self._build_custom_presets(p, custom)
        for name, b in p["preset_btns"].items():
            self._paint_button(b, gains == logi_headset.EQ_PRESETS[name])
        for b, g in p["custom_btns"].values():
            self._paint_button(b, gains == g)
        if gains:
            for i, (sc, g) in enumerate(zip(p["eq_scales"], gains)):
                self._set_scale(f"eq{i}", sc, g)
            for lab, f in zip(p["eq_labels"], st.get("eq_freqs", [])):
                lab.configure(text=f"{f / 1000:g}k" if f >= 1000 else str(f))

    # ------------------------------------------------------------------ show / hide
    def place(self):
        if self.root.state() == "withdrawn":
            return
        self.root.update_idletasks()
        w, h = self.px(360), self.root.winfo_reqheight()
        left, top, right, bottom = work_area()
        margin = self.px(12)
        self.root.geometry(f"{w}x{h}+{right - w - margin}+{max(top, bottom - h - margin)}")

    # ------------------------------------------------------------------ battery bar + charging animation
    def _draw_bar(self, p, s: DevState):
        bar = p["bar"]
        bar.delete("all")
        if s.battery is None:
            return
        full_w, h = bar.winfo_reqwidth(), self.px(6)
        w = int(full_w * s.battery / 100)
        bar.create_rectangle(0, 0, w, h, width=0, fill=battery_color(s.battery, s.charging))
        if s.charging and s.online:
            # A light "wave" that keeps filling from the current level up to full, like a phone.
            phase = (time.time() % 1.6) / 1.6
            ease = 1 - (1 - phase) ** 2
            if s.battery < 100:
                bar.create_rectangle(w, 0, w + int((full_w - w) * ease), h, width=0, fill=C["charge_wave"])
            # and a bright glint running across the filled part
            x = int((full_w + 30) * phase) - 15
            bar.create_rectangle(max(0, x - 6), 0, min(w, x + 6), h, width=0, fill=C["charge_glint"])

    def _animate(self):
        if self.root.state() != "withdrawn":
            for s in self.states:
                if s.charging and s.online:
                    self._draw_bar(self.cards[s.key], s)
        self.root.after(40, self._animate)

    def show(self):
        self.root.deiconify()
        self.place()
        self.root.lift()
        self.root.focus_force()
        self.hub.fast = True
        self.hub.refresh_now()

    def hide(self):
        self.hub.fast = False
        self.dragging.clear()
        self.root.withdraw()

    def toggle(self):
        self.hide() if self.root.state() != "withdrawn" else self.show()

    def _hide_if_unfocused(self):
        try:
            if self.root.focus_displayof() is None:
                self.hide()
        except Exception:
            pass


def work_area() -> tuple[int, int, int, int]:
    """Screen area not covered by the taskbar."""
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
    r = RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)   # SPI_GETWORKAREA
    return r.l, r.t, r.r, r.b


# =============================================================================== app

class App:
    def __init__(self, show_panel: bool = False):
        import tkinter as tk
        import pystray
        self.show_panel = show_panel
        self.ui_queue: queue.Queue = queue.Queue()
        self.hub = Hub(on_change=lambda states: self.ui_queue.put(("states", states)))
        self.root = tk.Tk()
        self.panel = Panel(self.root, self.hub)
        self.warned: dict[str, set[int]] = {}
        self.states: list[DevState] = []
        self.blink_on = False
        self.full_notified: set[str] = set()
        item = pystray.MenuItem
        self.icon = pystray.Icon(APP_NAME, make_icon([]), f"{TITLE}: starting...", menu=pystray.Menu(
            item("Open", lambda i, it: self.ui_queue.put(("toggle", None)), default=True),
            item("Refresh now", lambda i, it: self.hub.refresh_now()),
            item("Import headset settings from G HUB", lambda i, it: self.hub.import_ghub()),
            item("Battery estimate report", lambda i, it: self.hub.battery_report()),
            item("Start with Windows", lambda i, it: set_autostart(not autostart_enabled()),
                 checked=lambda it: autostart_enabled()),
            item("Exit", lambda i, it: self.ui_queue.put(("quit", None))),
        ))

    def _check_full(self, s: DevState):
        """One notification per charge when a device reaches 100% on the charger."""
        if not (s.online and s.charging):
            self.full_notified.discard(s.key)
            return
        if s.battery is not None and s.battery >= 100 and s.key not in self.full_notified:
            self.full_notified.add(s.key)
            try:
                self.icon.notify(f"{s.name} is fully charged - you can unplug it.", f"{s.kind} charged")
            except Exception:
                log.exception("notify failed")

    def _check_low(self, s: DevState):
        warned = self.warned.setdefault(s.key, set())
        if s.battery is None or not s.online:
            return
        if s.charging or s.battery > max(LOW_WARN) + 5:
            warned.clear()
            return
        for level in sorted(LOW_WARN):
            if s.battery <= level and level not in warned:
                warned.update(l for l in LOW_WARN if l >= level)
                try:
                    self.icon.notify(f"{s.name} is at {s.battery}% - time to charge it.", f"{s.kind} battery low")
                except Exception:
                    log.exception("notify failed")
                break

    def _pump(self):
        try:
            while True:
                kind, data = self.ui_queue.get_nowait()
                if kind == "states":
                    self.panel.update(data)
                    for s in data:
                        self._check_low(s)
                        self._check_full(s)
                    self.states = data
                    self.icon.icon = make_icon(data)
                    self.icon.title = tooltip(data)
                elif kind == "toggle":
                    self.panel.toggle()
                elif kind == "show":
                    self.panel.show()
                elif kind == "quit":
                    self.hub.stop.set()
                    self.hub.refresh_now()
                    self.icon.stop()
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(150, self._pump)

    def _blink(self):
        """While the device shown in the tray is charging, alternate its % with a lightning bolt."""
        low = lowest(self.states)
        if low and low.charging and low.online:
            self.blink_on = not self.blink_on
            try:
                self.icon.icon = make_icon(self.states, bolt=self.blink_on)
            except Exception:
                pass
        elif self.blink_on:
            self.blink_on = False
            self.icon.icon = make_icon(self.states)
        self.root.after(900, self._blink)

    def run(self):
        threading.Thread(target=self.hub.run, daemon=True, name="devices").start()
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()
        self.root.after(150, self._pump)
        self.root.after(40, self.panel._animate)
        self.root.after(900, self._blink)
        threading.Thread(target=_watch_show_requests, args=(lambda: self.ui_queue.put(("show", None)),),
                         daemon=True, name="show-requests").start()
        if self.show_panel:
            self.ui_queue.put(("show", None))
        self.root.mainloop()


def _single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}Mutex")
    return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS


SHOW_EVENT = f"Local\\{APP_NAME}Show"


def _ask_running_copy_to_show():
    """Launched again (e.g. from the desktop icon) while already running: open the running copy's panel."""
    k32 = ctypes.windll.kernel32
    h = k32.OpenEventW(0x0002, False, SHOW_EVENT)          # EVENT_MODIFY_STATE
    if h:
        k32.SetEvent(h)
        k32.CloseHandle(h)


def _watch_show_requests(on_show):
    k32 = ctypes.windll.kernel32
    h = k32.CreateEventW(None, False, False, SHOW_EVENT)   # auto-reset event
    while True:
        if k32.WaitForSingleObject(h, 0xFFFFFFFF) == 0:   # WAIT_OBJECT_0
            on_show()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="print every device's battery and settings, then exit")
    ap.add_argument("--tray", action="store_true", help="start in the tray without opening the panel")
    ap.add_argument("--battery-report", action="store_true",
                    help="print the battery estimates and how accurate they have been, then exit")
    ap.add_argument("--audiofx", metavar="NAME=VALUE", help=argparse.SUPPRESS)   # elevated helper (UAC)
    args = ap.parse_args()
    if args.audiofx:
        from devices import logi_audiofx
        sys.exit(logi_audiofx.run_helper(args.audiofx))
    if args.battery_report:                           # read-only: safe while the app is running
        names = {c.key: f"{c.kind} · {c.name}" for c in (KeyboardCtl, MouseCtl, HeadsetCtl)}
        text = Estimator(HISTORY_PATH).report(names)
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
        print(text)
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        RotatingFileHandler(os.path.join(DATA_DIR, "log.txt"), maxBytes=300_000, backupCount=1, encoding="utf-8")])

    if args.once:
        try:
            sys.stdout.reconfigure(errors="replace")   # old consoles can't show ⚡ / …
        except Exception:
            pass
        hub = Hub(on_change=lambda s: None)
        hub._poll()
        for s in hub.snapshot():
            pct = "--" if s.battery is None else f"{s.battery}%"
            extra = {k: v for k, v in s.settings.items() if k not in ("dpi_range", "eq_freqs")}
            print(f"{s.kind:8} {s.name:20} {pct:>5}  {s.detail:14} {s.time_text:32} {extra or ''}")
        for c in hub.ctls:
            c.close()
        return

    if not _single_instance():
        _ask_running_copy_to_show()
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        migrate_old_autostart()
        if getattr(sys, "frozen", False) and autostart_enabled() and _run_value(APP_NAME) != _autostart_cmd():
            set_autostart(True)       # point "Start with Windows" at this installed exe
            log.info("autostart now runs %s", _autostart_cmd())
    except Exception:
        log.exception("autostart migration failed")
    App(show_panel=not args.tray).run()


if __name__ == "__main__":
    main()
