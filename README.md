# AirPodsTray

A small Windows tray app that brings iPhone-style AirPods features to the PC — similar to MagicPods.

- **Battery** for the left bud, right bud and case (tray tooltip, menu and pop-up)
- **Pop-up** in the corner when you open the case near the PC, with a **Connect** button
- **Auto-connect** when the case opens (optional)
- **Auto-pause** when you take a bud out, resume when it goes back in
- **Pause on disconnect** so videos don't carry on through the PC speakers
- **Low-battery** notifications
- **Start with Windows** (optional)

Tested with AirPods Pro 3 on Windows 11. Other AirPods models should work for battery and in-ear detection; model names are listed in `airpods_scanner.py`.

## Requirements

- Windows 10/11 with Bluetooth
- AirPods **paired with Windows once** (hold the case button until the light flashes white, then *Settings → Bluetooth & devices → Add device*)
- Python 3.10+ (only if running from source)

## Run

Download `AirPodsTray.exe` from the Releases page and double-click it, or run from source:

```bash
pip install -r requirements.txt
python airpods_tray.py
```

Right-click the tray icon for battery, Connect / Disconnect and settings. Settings are stored in `%APPDATA%\AirPodsTray\settings.json`.

## Build the .exe

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name AirPodsTray --icon icon.ico --collect-submodules winrt --collect-submodules bleak --hidden-import pystray._win32 airpods_tray.py
```

The result is `dist\AirPodsTray.exe`.

## Files

| File | What it does |
|---|---|
| `airpods_tray.py` | The tray app — combines everything below |
| `airpods_scanner.py` | Decodes AirPods Bluetooth LE advertisements (also runs as a console scanner) |
| `auto_connect.py` | Finds the paired AirPods and connects / disconnects them (also runs standalone) |
| `popup.py` | The battery pop-up window (also runs standalone, `--demo` for a preview) |

## How it works

**Battery, in-ear and lid state** come from the "proximity pairing" advertisement AirPods broadcast over Bluetooth LE (Apple company ID `0x004C`, type `0x07`). No pairing or connection is needed to read it. The status byte layout, worked out from captures:

| Bit | Meaning |
|---|---|
| `0x02` | primary bud in ear |
| `0x04` | primary bud in case |
| `0x08` | secondary bud in ear |
| `0x10` | secondary bud in case |
| `0x20` | the sending bud is the left one |
| `0x40` | the sending bud is the secondary |

Battery nibbles (0–10 → 0–100 %) are relative to the sending bud; the lid byte's low 3 bits count lid openings. AirPods rotate their Bluetooth address when the lid opens, so a new address showing the case battery is treated as "lid opened".

**Connecting** uses the Windows Bluetooth audio driver's own one-shot reconnect property (`KSPROPSETID_BtAudio` / `KSPROPERTY_ONESHOT_RECONNECT`) — the same thing the *Connect* button in Windows settings does.

**Media control** uses the Windows global media transport controls, so it works with browsers, Spotify and anything else that shows in the Windows media flyout.

## Limitations

- The advertisements don't say *whose* AirPods they are. The app filters by model and signal strength, but someone else's identical AirPods right next to your PC could confuse it. Telling them apart needs the encryption key, which is only available over Apple's private AAP protocol.
- Noise-control switching, exact 1 % battery and other AAP features aren't implemented — Windows has no public API for the L2CAP channel AAP uses.

## Disclaimer

Not affiliated with or endorsed by Apple. AirPods is a trademark of Apple Inc.
