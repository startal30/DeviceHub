"""Import PRO X 2 headset settings from Logitech G HUB's local settings database.

G HUB keeps everything in %LOCALAPPDATA%\\LGHUB\\settings.db: a SQLite file whose `data` table holds one
JSON document. The parts used here:
  cards[] with attribute CENTURION_PRO_EQ   -> EQ presets {"name", "gains": [5 ints]}; user-made ones
                                               are the cards without "readOnly": true
  cards[] with attribute ACOUSTICS and a PRO X 2 deviceSignature
          -> acoustics.sidetone.gain (0.0 - 1.0)
The database is copied to a temp folder first and only ever read, so G HUB's own file is never touched.
DTS surround, Blue VO!CE, noise reduction and mic gain are processed by G HUB on the PC, not stored in the
headset, so there is nothing to import for them.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile

GHUB_DB = os.path.join(os.environ.get("LOCALAPPDATA", ""), "LGHUB", "settings.db")
HEADSET_SIGNATURE = "pro_x2_gaming_wireless_headset"


def _load_ghub_json(db_path: str = GHUB_DB) -> dict | None:
    if not os.path.exists(db_path):
        return None
    tmp = tempfile.mkdtemp(prefix="ghub_")
    try:
        for suffix in ("", "-wal", "-shm"):      # G HUB uses WAL mode: recent changes live in -wal
            if os.path.exists(db_path + suffix):
                shutil.copy2(db_path + suffix, os.path.join(tmp, "settings.db" + suffix))
        con = sqlite3.connect(os.path.join(tmp, "settings.db"))
        try:
            row = con.execute("SELECT file FROM data ORDER BY _id DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if not row:
            return None
        blob = row[0]
        return json.loads(blob.decode("utf-8") if isinstance(blob, bytes) else blob)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _cards(js: dict) -> list[dict]:
    cards = js.get("cards", [])
    if isinstance(cards, dict):
        cards = cards.get("cards", list(cards.values()))
    return [c for c in cards if isinstance(c, dict)]


def read_headset_settings(db_path: str = GHUB_DB) -> dict | None:
    """{"presets": {name: [5 gains]}, "sidetone": int | None} or None if G HUB isn't installed."""
    js = _load_ghub_json(db_path)
    if js is None:
        return None
    presets: dict[str, list[int]] = {}
    sidetone = None
    for c in _cards(js):
        attr = c.get("attribute")
        if attr == "CENTURION_PRO_EQ" and not c.get("readOnly"):
            eq = c.get("centurionProEqualizer") or {}
            gains = eq.get("gains")
            name = (c.get("name") or eq.get("name") or "").strip()
            if name and isinstance(gains, list) and len(gains) == 5:
                presets[name] = [max(-12, min(12, int(round(g)))) for g in gains]
        elif attr == "ACOUSTICS" and HEADSET_SIGNATURE in str(c.get("deviceSignature", "")):
            gain = ((c.get("acoustics") or {}).get("sidetone") or {}).get("gain")
            if isinstance(gain, (int, float)) and sidetone is None:
                sidetone = max(0, min(100, int(round(gain * 100))))
    return {"presets": presets, "sidetone": sidetone}


if __name__ == "__main__":
    print(json.dumps(read_headset_settings(), indent=2))
