# AirPodsTray

A small Windows tray app that brings iPhone-style AirPods features to the PC — similar to MagicPods.

- **Battery** for the left bud, right bud and case (tray tooltip, menu and pop-up)
- **Pop-up** in the corner when you open the case near the PC, with a **Connect** / **Disconnect** button
- **Auto-connect** when the case opens or a bud goes in (optional, off by default)
- **Disconnect when both buds come out**, so audio goes back to the PC speakers straight away
- **Auto-pause** when you take a bud out, resume when it goes back in — including media the AirPods paused themselves
- **Pause on disconnect** so videos don't carry on through the PC speakers
- **Low-battery** notifications
- **Start with Windows** (optional)
- **Self-healing**: restarts the Bluetooth scan (or the whole app) if Windows stops it, e.g. after sleep

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

The app keeps a log of what it sees and does (bud changes, every raw AirPods broadcast change, play/pause, connects): next to the .exe as `AirPodsTray.log`, or in `%APPDATA%\AirPodsTray\log.txt` when run from source.

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

The app scans **passively** (it only needs the advertisement, not a scan response); while audio was streaming, Windows delivered about 3× more AirPods advertisements that way.

**Connecting** uses the Windows Bluetooth audio driver's own one-shot reconnect property (`KSPROPSETID_BtAudio` / `KSPROPERTY_ONESHOT_RECONNECT`) — the same thing the *Connect* button in Windows settings does.

**"Connected"** means the AirPods are the audio output: the app reads the `DeviceState` of their *Headphones* audio endpoint (`HKLM\...\MMDevices\Audio\Render`). The Bluetooth link's own status is not used, because the link often stays up after the audio has disconnected.

**Media control** uses the Windows global media transport controls, so it works with browsers, Spotify and anything else that shows in the Windows media flyout.

## Limitations

- **Left-bud ear detection is unreliable.** While worn, usually only one bud broadcasts, and in testing the AirPods often didn't update their advertisement when the other bud came out. The AirPods' own pause/resume sometimes covers it.
- The advertisements don't say *whose* AirPods they are. The app locks onto the strongest signal of your model and ignores clearly weaker ones, but someone else's identical AirPods right next to your PC could still confuse it. Telling them apart needs the encryption key, which is only available over Apple's private AAP protocol.
- Noise-control switching, exact 1 % battery and reliable ear detection need AAP, which runs over an L2CAP channel. Windows only lets kernel-mode profile drivers open L2CAP channels, so this would need a driver (like MagicPods' MagicAAP, which requires Test Mode) — not implemented.

## Disclaimer

Not affiliated with or endorsed by Apple. AirPods is a trademark of Apple Inc.
