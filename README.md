# Device Hub (Windows)

One tray app for three devices, replacing AULA's software, Logitech G HUB and Logi Options+:

| Device | Battery | Settings |
|---|---|---|
| AULA F99 Pro keyboard (2.4GHz dongle, Bluetooth, USB cable) | ✅ | Lighting effect, brightness, speed, colour, sleep timer (dongle or cable only) |
| Logitech MX Master 3S (Bolt receiver) | ✅ | Pointer speed (DPI), SmartShift, scroll direction, smooth scrolling |
| Logitech PRO X 2 LIGHTSPEED headset | ✅ | DTS surround (+ modes), Blue VO!CE, sidetone, mic level & mute, mic noise reduction, auto-off timer, 5-band EQ + G HUB's presets |

The tray icon shows the **lowest** battery of the three (green > 40%, yellow 21–40%, red ≤ 20%,
blue = charging, `F` = full, `--` = nothing found). Hover it to see all three; click it to open the panel.
You get a Windows notification when any device drops to 20% and again at 10%.

**Charging:** the battery bar animates, a ⚡ appears next to the %, and the tray icon blinks with a
lightning bolt while the device it shows is charging.

**Time left:** no device reports this, so the app measures how fast each battery drains (or charges)
on your own usage and remembers it across restarts (`%LOCALAPPDATA%\DeviceHub\battery_history.json`).
It needs two changes in % at least 5 minutes apart. Until then it shows the maker's rating, marked
"rated" (MX Master 3S: 70 days per charge; PRO X 2: 50 h of use). The keyboard says "Measuring battery
life…" until it has data, because AULA publishes no rating.

## Install / update
Double-click `build.bat`. It runs the tests, builds the app and installs it to
`%LOCALAPPDATA%\Programs\DeviceHub\DeviceHub.exe`, which doesn't need Python or this folder.
It also puts a **Device Hub** icon on your desktop. Start the app once; "Start with Windows" then
points at the installed exe automatically. Run `build.bat` again after changing the code, and it
replaces the installed copy.

When Windows starts it, it stays quietly in the tray. Opening it from the desktop icon shows the panel,
and if it's already running, the icon just opens the running copy's panel.

For development:
```
pip install --user -r requirements.txt
pythonw device_hub.py                 # run from source
python device_hub.py --once           # print every device's battery and settings
python -m unittest discover tests     # offline tests (no devices needed)
```
Right-click the tray icon for: Open · Refresh now · Import headset settings from G HUB · Start with Windows · Exit.

The app checks devices every 60 s, or every 15 s while the panel is open or something is charging.
You get a notification when a device reaches 100% on the charger.

## Importing from G HUB
On first start, Device Hub reads G HUB's saved settings (read-only, from a copy of
`%LOCALAPPDATA%\LGHUB\settings.db`) and imports:
- your own EQ presets, shown under "Your presets (from G HUB)";
- the headset's sidetone level;
- all Blue VO!CE presets (built-in and yours).

The EQ, sidetone and auto-off that G HUB last set are stored **in the headset itself**, so they keep
working without G HUB. Device Hub saves them and puts them back if the headset ever resets.
Run it again any time from the tray menu: **Import headset settings from G HUB**.

**Mic noise reduction** is a setting stored in the headset (feature `0x0602`), so Device Hub controls it.
The PRO X 2 has **no mic-gain setting of its own**, so the panel's mic level is the Windows recording level
for "Microphone (PRO X 2 LIGHTSPEED)", the same as the slider in Windows Sound settings.

## DTS surround and Blue VO!CE
Neither runs inside G HUB. They're Windows audio effects from Logitech's driver packages, attached to
the PRO X 2 endpoints and run by the Windows audio engine:
- speakers: "Logitech HX2E Surround Sound Effect" (DTS Headphone:X 2.0)
- microphone: "Logitech Blue VO!CE 2.0 Effect" (DTS ClearVoice 2)

G HUB only stores their settings. Recording what G HUB changes for each switch showed:
- **Surround:** `HKLM\SOFTWARE\Logitech\GHUB\Audio Devices\<speaker endpoint>` → `DTSon` (1/0),
  `SelectedSurround` (0 = DTS, 2 = off), `DTSroomModel` (Entertainment/Gaming/Sports = 0/1/2),
  `DTSstereoModel` (Off/Front/Wide/Traditional = 0/1/2/3). Writing speaker property
  `{d57f93ae-4210-46b3-acfc-90437b666152},1` then tells the effect to re-read them.
- **Blue VO!CE:** microphone property `{b4d50347-1450-477f-b173-56b14770589a},1` = 1/0.
- **Blue VO!CE presets:** a preset is 40 float properties in that same set (noise reduction, gate,
  compressor, 3-band EQ, de-popper, de-esser, limiter, volume). The preset's input gain isn't sent to the
  effect: G HUB sets the Windows recording level to it (0.5 → 50%). Two presets applied by G HUB were
  recorded, and all 40 values matched; nothing else changed.

**Blue VO!CE presets in Device Hub:** Mic tab → **Preset ▾** lists G HUB's 12 built-in presets and your
own, imported from G HUB (tray menu → Import headset settings from G HUB re-reads them). Picking one
writes all 40 values in one go (one permission prompt) and sets the mic level to the preset's input gain.
The button shows which preset the mic is on now, read back from the mic.

Device Hub flips the same switches; no Logitech or DTS code is copied. Both live in admin-only places,
so by default **Windows asks for permission** for each change, and a small helper
(`DeviceHub.exe --audiofx name=value`) applies just that change.

**Surround without prompts:** the DTS effect watches its registry key and applies changes live, so the
admin-only notify write isn't needed. Run `tools\allow-surround-without-prompt.ps1` once from an
administrator PowerShell to give your account write access to that one key; after that, surround
on/off and its modes change with no prompt. `tools\undo-allow-surround-without-prompt.ps1` reverses it.
The trade-off: any program running as you could change your surround settings.
**Blue VO!CE** always needs the prompt: its switch is an endpoint property, which only admins can write.

These settings aren't saved for re-applying, so reconnecting a device never causes a surprise prompt;
they persist on their own.

Verified on a real PRO X 2 with G HUB closed: surround off/on was audible immediately; Super Stereo
Wide → Off via the no-prompt registry write was audible immediately; Blue VO!CE's live meters stopped
when it was turned off and resumed when it was turned on.

## Keyboard lighting
Lighting settings live in the keyboard's 128-byte settings page (protocol from aula-f99-pro-hub).
They can be changed over the **2.4GHz dongle or the USB cable**, but **not over Bluetooth**: there the
keyboard always drops 3 of the page's 10 reply packets, so the page can't be read safely, and writing a
page that wasn't fully read could scramble the keyboard's settings. Every change is read back to confirm
the keyboard accepted it.

## Removing G HUB, AULA's app and OpenLogi
Device Hub covers everything they did for these three devices **except**:
- **Possibly DTS surround and Blue VO!CE.** The effects are separate Logitech driver packages, but they
  were probably installed by G HUB, so its uninstaller may remove them. Check after uninstalling: if the
  surround and Blue VO!CE switches in Device Hub turn grey, the effects are gone.
- **Firmware updates.** G HUB is the only way to update the headset and its dongle, and AULA's app
  updates the keyboard. To update firmware later, reinstall the app, update, and uninstall it again.
- **Keyboard per-key colours, key remapping and macros** (lighting effects are covered). They're stored
  in the keyboard, so your current setup keeps working; you just can't change it without AULA's app.
- **Mouse buttons and gestures.** No Logitech mouse app is installed (no Options+), so they already run
  on the mouse's defaults.

Uninstalling G HUB also removes its background updater service (`LGHUBUpdaterService`).

## Notes
- **Settings you change here are remembered** and put back automatically when a device reconnects
  (the MX Master forgets DPI etc. after it's switched off). Saved in `%LOCALAPPDATA%\DeviceHub\settings.json`.
- **Quit G HUB, Logi Options+ and AULA's app** once you're happy with this: if they keep running they
  can apply their own settings over yours, and two programs talking to a device can confuse each other.
- Sleeping devices can't answer. The panel keeps showing their last battery % for 15 minutes.
- Logs: `%LOCALAPPDATA%\DeviceHub\log.txt`

## Files
- `LICENSE` (GPL-3.0) and `THIRD_PARTY_NOTICES.md`
- `device_hub.py`: the tray app and panel
- `estimate.py`: time-left / time-to-full from measured battery history
- `ghub_import.py`: reads headset EQ presets and sidetone from G HUB's settings database
- `tests/test_core.py`: offline tests with simulated devices
- `build.bat`: test + build + install
- `devices/aula.py`: AULA F99 Pro battery
- `devices/aula_lighting.py`: AULA lighting effects and sleep timer
- `devices/win_mic.py`: Windows mic level and mute
- `devices/logi_audiofx.py`: DTS surround and Blue VO!CE (Logitech's installed audio effects)
- `devices/logi_mouse.py`: Logitech HID++ 2.0 (battery, DPI, SmartShift, scroll wheel)
- `devices/logi_headset.py`: PRO X 2 "Centurion" protocol (battery, sidetone, auto-off, EQ)

## Security and privacy
- **No network access.** Device Hub never connects to the internet. It talks to the three devices over USB
  HID (matched by their vendor and product IDs) and to Windows' audio and registry APIs.
- **G HUB's data is only read**, from a temporary copy of its database that's deleted straight after.
  Your settings stay on your PC in `%LOCALAPPDATA%\DeviceHub`.
- **Administrator rights are used only for DTS surround and Blue VO!CE**, via a UAC prompt you approve.
  The elevated helper (`DeviceHub.exe --audiofx NAME=VALUE`) accepts only the known setting names and
  validated values, and writes only to the PRO X 2's audio endpoints and G HUB's surround key.
- **Install location:** `build.bat` installs to `%LOCALAPPDATA%\Programs`, which your account can write.
  That's convenient (no admin needed to install or update), but it means a program already running as
  you could replace `DeviceHub.exe` and get its code run as administrator the next time you approve a
  Device Hub prompt. If that matters to you, copy `dist\DeviceHub` to `C:\Program Files\DeviceHub`
  instead and start it from there. Windows shows "Unknown publisher" because the exe isn't code-signed.
- **`tools\allow-surround-without-prompt.ps1`** lets your account write one registry key that the DTS
  audio effect reads (it can set values but not create subkeys). Any program running as you could then
  change, or feed unexpected values to, the surround settings. It's optional; undo it with
  `tools\undo-allow-surround-without-prompt.ps1`.
- **Keyboard lighting writes** are only sent after the whole settings page was read and checked, and
  are read back to confirm.

Found a security problem? Please open an issue (without exploit details) or contact the maintainer
through GitHub.

## Credits and license
Device Hub is licensed under the **GNU General Public License v3.0** (`LICENSE`), because
`devices/logi_headset.py` is ported from HeadsetControl (GPL-3.0). Protocols come from these projects
(details and the MIT notice in `THIRD_PARTY_NOTICES.md`):
- [xx12ek/aula-f99-pro-hub](https://github.com/xx12ek/aula-f99-pro-hub) (MIT): AULA keyboard
- [Solaar](https://github.com/pwr-Solaar/Solaar) (GPL-2.0-or-later): Logitech HID++
- [HeadsetControl](https://github.com/Sapd/HeadsetControl) (GPL-3.0): PRO X 2 headset

The PRO X 2's sidetone byte order was worked out on real hardware and differs from HeadsetControl's.
DTS surround and Blue VO!CE support was worked out by recording which settings G HUB changes; no
Logitech or DTS code or files are included.

Not affiliated with or endorsed by AULA, Logitech or DTS. All trademarks belong to their owners.
