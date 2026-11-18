# Device Hub (Windows)

One tray app for three devices, replacing AULA's software, Logitech G HUB and Logi Options+:

| Device | Battery | Settings |
|---|---|---|
| AULA F99 Pro keyboard (2.4GHz dongle, Bluetooth, USB cable) | ✅ | – |
| Logitech MX Master 3S (Bolt receiver) | ✅ | Pointer speed (DPI), SmartShift, scroll direction, smooth scrolling |
| Logitech PRO X 2 LIGHTSPEED headset | ✅ | Sidetone, auto-off timer, 5-band EQ + G HUB's presets |

The tray icon shows the **lowest** battery of the three (green > 40%, yellow 21–40%, red ≤ 20%,
blue = charging, `F` = full, `--` = nothing found). Hover it to see all three; click it to open the panel.
You get a Windows notification when any device drops to 20% and again at 10%.

**Charging:** the battery bar animates, a ⚡ appears next to the %, and the tray icon blinks with a
lightning bolt while the device it shows is charging.

**Time left:** no device reports this, so the app measures how fast each battery drains (or charges)
on your own usage and remembers it across restarts (`%LOCALAPPDATA%\DeviceHub\battery_history.json`).
It needs at least two 1% changes over 15+ minutes. Until then it shows the maker's rating, marked
"rated" (MX Master 3S: 70 days per charge; PRO X 2: 50 h of use). The keyboard says "Measuring battery
life…" until it has data, because AULA publishes no rating.

## Install / update
Double-click `build.bat`. It runs the tests, builds the app and installs it to
`%LOCALAPPDATA%\Programs\DeviceHub\DeviceHub.exe`, which doesn't need Python or this folder.
Start that exe once; "Start with Windows" then points at it automatically. Run `build.bat` again
after changing the code, and it replaces the installed copy.

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

These **can't** be imported, because they're audio processing that runs inside G HUB on the PC, not
settings in the headset: **DTS surround, Blue VO!CE, noise reduction and mic gain**. Quitting G HUB
turns them off. Windows' own sound settings can set mic level, and Windows Sonic can stand in for surround.

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
- `devices/logi_mouse.py`: Logitech HID++ 2.0 (battery, DPI, SmartShift, scroll wheel)
- `devices/logi_headset.py`: PRO X 2 "Centurion" protocol (battery, sidetone, auto-off, EQ)

## Credits
Protocols come from these open-source projects:
- [xx12ek/aula-f99-pro-hub](https://github.com/xx12ek/aula-f99-pro-hub) (MIT): AULA keyboard
- [Solaar](https://github.com/pwr-Solaar/Solaar) (GPL-2.0): Logitech HID++
- [HeadsetControl](https://github.com/Sapd/HeadsetControl) (GPL-3.0): PRO X 2 headset.
  `devices/logi_headset.py` is ported from it, so that file is GPL-3.0.

The PRO X 2's sidetone byte order was worked out on real hardware and differs from HeadsetControl's.
