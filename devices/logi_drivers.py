"""Keep Logitech's audio effect drivers (DTS surround, Blue VO!CE) after G HUB is uninstalled.

The effects come from 7 Logitech driver packages (logi_audio*.inf). They are installed by G HUB's
installer, not by Windows Update, and G HUB's uninstaller deletes them (both observed on a real PC).
Device Hub therefore:
  - backs them up automatically from Windows' driver store into its own folder whenever it finds them
    installed (reading the driver store needs no administrator rights);
  - restores them on request with Windows' own pnputil, run elevated (one permission prompt). pnputil
    only installs packages whose catalog signature checks out, so a modified file is refused.
The packages stay on the user's own PC; nothing is redistributed.
"""
from __future__ import annotations

import ctypes
import glob
import os
import shutil
import tempfile

STORE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "DriverStore", "FileRepository")
PATTERN = "logi_audio*.inf_amd64_*"
BACKUP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "DeviceHub", "logitech-audio-drivers")


class DriverError(Exception):
    pass


def _packages(root: str) -> list[str]:
    """Package folders under root that hold a complete package (an .inf and its signed .cat)."""
    out = []
    for d in sorted(glob.glob(os.path.join(root, PATTERN))):
        files = {f.lower() for f in os.listdir(d)} if os.path.isdir(d) else set()
        if any(f.endswith(".inf") for f in files) and any(f.endswith(".cat") for f in files):
            out.append(d)
    return out


def installed() -> list[str]:
    """Names of the Logitech audio packages in Windows' driver store."""
    return [os.path.basename(d) for d in _packages(STORE)]


def backed_up() -> list[str]:
    return [os.path.basename(d) for d in _packages(BACKUP_DIR)]


def backup() -> int:
    """Copy the installed packages into BACKUP_DIR (replacing an older backup). Returns how many."""
    pkgs = _packages(STORE)
    if not pkgs:
        return 0
    os.makedirs(os.path.dirname(BACKUP_DIR), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="logi-audio-", dir=os.path.dirname(BACKUP_DIR))
    try:
        for d in pkgs:
            shutil.copytree(d, os.path.join(tmp, os.path.basename(d)))
        if os.path.isdir(BACKUP_DIR):
            shutil.rmtree(BACKUP_DIR)
        os.replace(tmp, BACKUP_DIR)
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    return len(pkgs)


def needs_backup() -> bool:
    """Installed packages that the backup doesn't have yet (e.g. first run, or G HUB updated them)."""
    have = set(backed_up())
    return any(p not in have for p in installed())


def restore_command() -> tuple[str, str]:
    """(program, arguments) that reinstall the backed-up packages: Windows' pnputil, one .inf at a time."""
    infs = []
    for d in _packages(BACKUP_DIR):
        infs += [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".inf")]
    if not infs:
        raise DriverError("no backup of Logitech's audio drivers on this PC")
    pnputil = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "pnputil.exe")
    cmds = " & ".join(f'"{pnputil}" /add-driver "{inf}" /install' for inf in sorted(infs))
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), f'/c "{cmds}"'


def restore(timeout_ms: int = 180_000):
    """Reinstall the backup with pnputil, elevated (UAC prompt). Raises DriverError if it didn't work."""
    from devices.logi_audiofx import run_elevated
    exe, args = restore_command()
    run_elevated(exe, args, timeout_ms)
    missing = [p for p in backed_up() if p not in installed()]
    if missing:
        raise DriverError(f"Windows didn't install {len(missing)} of the {len(backed_up())} driver packages")
