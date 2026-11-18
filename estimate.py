"""Battery time-left / time-to-full estimates, and how accurate they have been.

No device reports time remaining, so it's measured. Every reading is fed in; the moments the percentage
*changes* while the device is being watched are step boundaries, and the time between two consecutive
boundaries is exactly how long one step took. Only those intervals are used:
  - Time the device was off, asleep or not watched (no reading for MAX_GAP_S) never counts, so a
    discharge estimate means "hours of use", like the headset's rating.
  - Changes in the first SETTLE_S after a device is first seen, comes back, or is plugged in / unplugged
    are ignored: the reading jumps several % in a few minutes there before it settles.
The rate used is a mix of the recent pace (last RECENT_H of use) and the long-term one (this discharge
plus earlier ones), so it follows how the device is being used now without swinging on one step.

Validation: every time the % changes, the predicted rate is stored with it. When the battery has since
moved another HORIZON_PCT, the predicted time for that is compared with the real time, and the error is
kept. report() prints all of it.
Until there is enough data, the manufacturer's rated battery life is used (and labelled as such).
"""
from __future__ import annotations

import json
import os
import statistics
import time

VERSION = 2
MAX_GAP_S = 150            # no reading for longer than this = device was off / asleep / not watched
SETTLE_S = 10 * 60         # ignore changes this long after (re)connecting or plugging in / unplugging
RECENT_H = 1.0             # "recent pace" window, in hours of use
RECENT_W = 0.6             # weight of the recent pace vs the long-term rate
MIN_SPAN_H = 5 / 60        # a rate needs at least this much measured time...
MIN_STEP = 2               # ...and at least this many % of change
EMA = 0.35                 # weight of a finished discharge/charge in the remembered rate
HORIZON_PCT = 10           # accuracy check: predicted vs real time to move this many %
MAX_POINTS = 400
MAX_CHECKS = 50

# Manufacturer ratings (full battery -> empty). None = unknown.
RATED_HOURS = {
    "mouse": 70 * 24,      # MX Master 3S: "up to 70 days on full charge" (Logitech spec)
    "headset": 50,         # PRO X 2 LIGHTSPEED: 50 h of use (Logitech support article)
    "keyboard": None,
}
RATED_IS_USE_TIME = {"headset": True}   # rated in hours of use, not calendar time

# A point: [time, pct, ok, rate, checked]
#   ok      1 = the interval since the previous point is one measured step (both ends are boundaries)
#   rate    the %/h the estimate used right after this change (None = no estimate then)
#   checked 1 = its prediction has been compared with what happened


def _new(charging: bool) -> dict:
    return {"charging": charging, "points": [], "rate_down": None, "rate_up": None,
            "boundary": False, "settle_until": 0.0, "seen": 0.0, "checks": {"down": [], "up": []}}


class Estimator:
    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        if self.data.get("_v") != VERSION:
            self._migrate()

    def _migrate(self):
        """Version 1 measured wall-clock time including time devices were off, so its discharge rates
        are wrong; charge rates were measured while plugged in and are kept."""
        old, self.data = self.data, {"_v": VERSION}
        for key, rec in old.items():
            if isinstance(rec, dict) and "points" in rec:
                new = _new(bool(rec.get("charging")))
                new["rate_up"] = rec.get("rate_up")
                self.data[key] = new

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ measuring
    @staticmethod
    def _intervals(points) -> list[tuple[float, int]]:
        """(hours, % moved) of each measured step."""
        return [((points[i][0] - points[i - 1][0]) / 3600, abs(points[i - 1][1] - points[i][1]))
                for i in range(1, len(points)) if points[i][2]]

    @staticmethod
    def _rate_of(intervals) -> float | None:
        hours = sum(h for h, _ in intervals)
        moved = sum(p for _, p in intervals)
        if hours < MIN_SPAN_H or moved < MIN_STEP:
            return None
        return moved / hours

    @classmethod
    def _recent(cls, intervals) -> float | None:
        picked, hours = [], 0.0
        for h, p in reversed(intervals):
            picked.append((h, p))
            hours += h
            if hours >= RECENT_H:
                break
        return cls._rate_of(picked)

    def rates(self, key: str) -> dict:
        """{'recent', 'segment', 'remembered', 'used'} in %/hour for the current discharge or charge."""
        rec = self.data.get(key)
        if not rec:
            return {}
        iv = self._intervals(rec["points"])
        recent, segment = self._recent(iv), self._rate_of(iv)
        remembered = rec.get("rate_up" if rec["charging"] else "rate_down")
        if segment and remembered:
            long_term = remembered * (1 - EMA) + segment * EMA
        else:
            long_term = segment or remembered
        if recent and long_term:
            used = recent * RECENT_W + long_term * (1 - RECENT_W)
        else:
            used = recent or long_term
        return {"recent": recent, "segment": segment, "remembered": remembered, "used": used,
                "measured_h": sum(h for h, _ in iv)}

    def update(self, key: str, pct: int, charging: bool, now: float | None = None):
        """Feed every successful reading (only readings: when a device is away, don't call this)."""
        now = now or time.time()
        rec = self.data.get(key)
        if rec is None:
            rec = self.data[key] = _new(charging)
        resumed = now - rec.get("seen", 0.0) > MAX_GAP_S
        rec["seen"] = now
        pts = rec["points"]
        last = pts[-1][1] if pts else None
        # A new discharge/charge starts when charging starts/stops, or the % jumped the "wrong" way
        # (charged while we weren't watching, or recalibration).
        wrong_way = last is not None and ((not charging and pct > last + 1) or (charging and pct < last - 1))
        if rec["charging"] != charging or wrong_way or not pts:
            self._finish(rec)
            rec.update(charging=charging, points=[[now, pct, 0, None, 1]], boundary=False,
                       settle_until=now + SETTLE_S)
            self._save()
            return
        if resumed:
            rec["boundary"] = False
            rec["settle_until"] = now + SETTLE_S
        if pct == last:
            return
        # This change is a step boundary if we were watching continuously and the reading has settled.
        is_boundary = not resumed and now >= rec["settle_until"]
        pts.append([now, pct, 1 if (rec["boundary"] and is_boundary) else 0, None, 0])
        rec["boundary"] = is_boundary
        pts[-1][3] = self.rates(key).get("used")
        self._check(rec)
        del pts[:-MAX_POINTS]
        self._save()

    def _finish(self, rec):
        rate = self._rate_of(self._intervals(rec["points"]))
        if rate and sum(h for h, _ in self._intervals(rec["points"])) >= 0.5:
            k = "rate_up" if rec["charging"] else "rate_down"
            rec[k] = rate if not rec.get(k) else rec[k] * (1 - EMA) + rate * EMA

    def _check(self, rec):
        """Compare earlier predictions with what really happened, once the battery moved HORIZON_PCT."""
        pts = rec["points"]
        checks = rec.setdefault("checks", {"down": [], "up": []})["up" if rec["charging"] else "down"]
        for i, (t, pct, _ok, rate, checked) in enumerate(pts):
            if checked or not rate:
                continue
            hours = moved = 0.0
            for h, p in self._intervals(pts[i:]):
                hours, moved = hours + h, moved + p
            if moved < HORIZON_PCT:
                break                       # later points have moved even less
            predicted = moved / rate
            checks.append([t, pct, round(predicted, 3), round(hours, 3)])
            pts[i][4] = 1
        del checks[:-MAX_CHECKS]

    # ------------------------------------------------------------------ results
    def estimate(self, key: str, pct: int, charging: bool) -> tuple[float | None, str]:
        """(hours, basis) - basis is 'usage' (measured), 'rated', or '' when unknown."""
        rec = self.data.get(key)
        rate = self.rates(key).get("used") if rec and rec["charging"] == charging else None
        need = (100 - pct) if charging else pct
        if rate:
            return need / rate, "usage"
        if not charging and RATED_HOURS.get(key):
            return RATED_HOURS[key] * pct / 100, "rated"
        return None, ""

    def accuracy(self, key: str, charging: bool) -> tuple[float | None, float | None, int]:
        """(typical error, average bias) as fractions of the real time, and how many checks.
        Bias > 0: the estimate said longer than it really took."""
        rec = self.data.get(key) or {}
        checks = (rec.get("checks") or {}).get("up" if charging else "down", [])
        errs = [(pred - real) / real for _t, _p, pred, real in checks if real > 0]
        if len(errs) < 3:
            return None, None, len(errs)
        return statistics.median(abs(e) for e in errs), statistics.fmean(errs), len(errs)

    def report(self, names: dict[str, str], now: float | None = None) -> str:
        now = now or time.time()
        out = [f"Battery estimate report - {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}", "",
               "Accuracy = predicted vs real time for the battery to move "
               f"{HORIZON_PCT}%, counting only time the device was on.", ""]
        for key, name in names.items():
            rec = self.data.get(key)
            if not rec or not rec["points"]:
                out += [f"{name}: no readings yet", ""]
                continue
            pts, charging = rec["points"], rec["charging"]
            r = self.rates(key)
            f = lambda v: f"{v:.2f} %/h" if v else "-"
            out.append(f"{name}: {'charging' if charging else 'on battery'}, now {pts[-1][1]}%")
            out.append(f"  measured {r['measured_h']:.1f} h in {len(self._intervals(pts))} steps since "
                       f"{time.strftime('%d %b %H:%M', time.localtime(pts[0][0]))}")
            out.append(f"  recent pace {f(r['recent'])}, this {'charge' if charging else 'discharge'} "
                       f"{f(r['segment'])}, earlier ones {f(r['remembered'])}, used {f(r['used'])}")
            hours, basis = self.estimate(key, pts[-1][1], charging)
            if hours:
                out.append(f"  estimate now: {fmt_duration(hours)} ({basis})")
            for mode in ("down", "up"):
                checks = (rec.get("checks") or {}).get(mode, [])
                if not checks:
                    continue
                typ, bias, n = self.accuracy(key, mode == "up")
                label = "Charging" if mode == "up" else "Battery life"
                if typ is not None:
                    side = "longer" if bias > 0 else "shorter"
                    out.append(f"  {label} accuracy: {n} checks, typically off by {typ:.0%}; on average "
                               f"it said {abs(bias):.0%} {side} than reality")
                else:
                    out.append(f"  {label} accuracy: {n} check(s) so far (need 3)")
                for t, p, pred, real in checks[-8:]:
                    out.append(f"    {time.strftime('%d %b %H:%M', time.localtime(t))} at {p}%: predicted "
                               f"{fmt_duration(pred)}, really {fmt_duration(real)} ({(pred - real) / real:+.0%})")
            out.append("")
        return "\n".join(out)


def fmt_duration(hours: float) -> str:
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 10:
        h, m = int(hours), round((hours % 1) * 60 / 5) * 5
        if m == 60:
            h, m = h + 1, 0
        return f"{h} h {m} min" if m else f"{h} h"
    if hours < 48:
        return f"{round(hours)} h"
    return f"{round(hours / 24)} days"
