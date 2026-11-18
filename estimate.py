"""Battery time-left / time-to-full estimates.

No device reports time remaining, so we measure it: every reading is compared with the previous one,
and the moments the percentage *changes* are recorded. The rate between two such changes is exact
(no guessing when the first drop happened). Rates are kept per device and averaged over time, so
estimates survive restarts and get better the longer the app runs. Until there is enough data, the
manufacturer's rated battery life is used (and labelled as such).
"""
from __future__ import annotations

import json
import os
import time

MIN_SPAN_H = 5 / 60        # need >= 2 changes at least 5 min apart (the MX Master steps in 5% jumps)
MAX_POINTS = 400
EMA = 0.35                 # weight of a newly finished segment in the remembered rate

# Manufacturer ratings (full battery -> empty). None = unknown.
RATED_HOURS = {
    "mouse": 70 * 24,      # MX Master 3S: "up to 70 days on full charge" (Logitech spec)
    "headset": 50,         # PRO X 2 LIGHTSPEED: 50 h of use (Logitech support article)
    "keyboard": None,
}
RATED_IS_USE_TIME = {"headset": True}   # rated in hours of use, not calendar time


class Estimator:
    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    @staticmethod
    def _seg_rate(points) -> float | None:
        """%/hour between the first and last *change* in a segment (points[0] is just 'first seen')."""
        if len(points) < 3:
            return None
        (t1, p1), (tn, pn) = points[1], points[-1]
        hours = (tn - t1) / 3600
        if hours < MIN_SPAN_H or p1 == pn:
            return None
        return abs(p1 - pn) / hours

    def update(self, key: str, pct: int, charging: bool, now: float | None = None):
        now = now or time.time()
        rec = self.data.setdefault(key, {"charging": charging, "points": [], "rate_down": None, "rate_up": None})
        pts = rec["points"]
        last = pts[-1][1] if pts else None
        # A new segment starts when charging starts/stops, or the % jumped the "wrong" way
        # (charged while we weren't watching, or recalibration).
        wrong_way = last is not None and ((not charging and pct > last + 1) or (charging and pct < last - 1))
        if rec["charging"] != charging or wrong_way or not pts:
            self._finish(rec)
            rec["charging"] = charging
            rec["points"] = [[now, pct]]
            self._save()
        elif pct != last:
            pts.append([now, pct])
            del pts[:-MAX_POINTS]
            self._save()

    def _finish(self, rec):
        rate = self._seg_rate(rec["points"])
        if rate:
            k = "rate_up" if rec["charging"] else "rate_down"
            rec[k] = rate if not rec.get(k) else rec[k] * (1 - EMA) + rate * EMA

    def estimate(self, key: str, pct: int, charging: bool) -> tuple[float | None, str]:
        """(hours, basis) - basis is 'usage' (measured), 'rated', or '' when unknown."""
        rec = self.data.get(key)
        rate, basis = None, ""
        if rec and rec.get("charging") == charging:
            seg = self._seg_rate(rec["points"])
            remembered = rec.get("rate_up" if charging else "rate_down")
            if seg and remembered:
                rate = remembered * (1 - EMA) + seg * EMA
            else:
                rate = seg or remembered
            basis = "usage" if rate else ""
        need = (100 - pct) if charging else pct
        if rate:
            return need / rate, basis
        if not charging and RATED_HOURS.get(key):
            return RATED_HOURS[key] * pct / 100, "rated"
        return None, ""


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
