# Device Hub (Windows)

One tray app for three devices, replacing AULA's software, Logitech G HUB and Logi Options+:

| Device | Battery | Settings |
|---|---|---|
| AULA F99 Pro keyboard (2.4GHz dongle, Bluetooth, USB cable) | ✅ | Lighting effect, brightness, speed, colour, sleep timer (dongle or cable only) |
| Logitech MX Master 3S (Bolt receiver) | ✅ | Pointer speed (DPI), SmartShift, scroll direction, smooth scrolling |
| Logitech PRO X 2 LIGHTSPEED headset | ✅ | Sidetone, mic level & mute, mic noise reduction, auto-off timer, 5-band EQ + G HUB's presets |

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
- the headset's sidetone level.

The EQ, sidetone and auto-off that G HUB last set are stored **in the headset itself**, so they keep
working without G HUB. Device Hub saves them and puts them back if the headset ever resets.
Run it again any time from the tray menu: **Import headset settings from G HUB**.

**Mic noise reduction** is a setting stored in the headset (feature `0x0602`), so Device Hub controls it.
The PRO X 2 has **no mic-gain setting of its own**, so the panel's mic level is the Windows recording level
for "Microphone (PRO X 2 LIGHTSPEED)", the same as the slider in Windows Sound settings.

These **can't** be replaced, because they're audio processing that runs inside G HUB on the PC:
**DTS surround** and **Blue VO!CE**. Without G HUB they're off. Windows Sonic
(Settings → Sound → speakers → Spatial audio) can stand in for surround.

## Keyboard lighting
Lighting settings live in the keyboard's 128-byte settings page (protocol from aula-f99-pro-hub).
They can be changed over the **2.4GHz dongle or the USB cable**, but **not over Bluetooth**: there the
keyboard always drops 3 of the page's 10 reply packets, so the page can't be read safely, and writing a
page that wasn't fully read could scramble the keyboard's settings. Every change is read back to confirm
the keyboard accepted it.

## Removing G HUB, AULA's app and OpenLogi
Device Hub covers everything they did for these three devices **except**:
- **DTS surround and Blue VO!CE** (see above).
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
- `device_hub.py`: the tray app and panel
- `estimate.py`: time-left / time-to-full from measured battery history
- `ghub_import.py`: reads headset EQ presets and sidetone from G HUB's settings database
- `tests/test_core.py`: offline tests with simulated devices
- `build.bat`: test + build + install
- `devices/aula.py`: AULA F99 Pro battery
- `devices/aula_lighting.py`: AULA lighting effects and sleep timer
- `devices/win_mic.py`: Windows mic level and mute
- `devices/logi_mouse.py`: Logitech HID++ 2.0 (battery, DPI, SmartShift, scroll wheel)
- `devices/logi_headset.py`: PRO X 2 "Centurion" protocol (battery, sidetone, auto-off, EQ)

## Credits
Protocols come from these open-source projects:
- [xx12ek/aula-f99-pro-hub](https://github.com/xx12ek/aula-f99-pro-hub) (MIT): AULA keyboard
- [Solaar](https://github.com/pwr-Solaar/Solaar) (GPL-2.0): Logitech HID++
- [HeadsetControl](https://github.com/Sapd/HeadsetControl) (GPL-3.0): PRO X 2 headset.
  `devices/logi_headset.py` is ported from it, so that file is GPL-3.0.

The PRO X 2's sidetone byte order was worked out on real hardware and differs from HeadsetControl's.
