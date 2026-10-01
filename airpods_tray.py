"""
AirPods tray app for Windows - scanner, pop-up, auto-connect and auto-pause in one.

- Tray icon: hover for battery, right-click for the menu, left-click for the pop-up
- Pop-up when you open the case near the PC
- Auto-connect when the case opens (optional)
- Pause media when you take a bud out, resume when it goes back in
- Low-battery notifications
- Start with Windows (optional)

Usage:
  python airpods_tray.py     # with log output in the console
  pythonw airpods_tray.py    # no console window
"""

import ctypes
import sys


def already_running() -> bool:
    """Hold a named mutex so a second copy (e.g. a double double-click) exits straight away."""
    global _instance_mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\AirPodsTray")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


# Checked before the slow imports below so a quick double-click can't start two copies.
if __name__ == "__main__" and already_running():
    sys.exit(0)

import asyncio
import dataclasses
import json
import os
import time
import tkinter as tk
import winreg
from pathlib import Path

import pystray
from bleak import BleakScanner
from PIL import Image, ImageDraw
from winrt.windows.devices.bluetooth import BluetoothConnectionStatus, BluetoothDevice
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)

from airpods_scanner import APPLE_COMPANY_ID, AirPodsState, decode
from auto_connect import PairedAirPods
from popup import AUTO_HIDE_SECONDS, Popup

APP_NAME = "AirPodsTray"
SETTINGS_FILE = Path(os.environ["APPDATA"]) / APP_NAME / "settings.json"
DEFAULTS = {"show_popup": True, "auto_connect": False, "auto_pause": True, "pause_on_disconnect": True,
            "min_rssi": -60}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

LID_COOLDOWN = 15     # seconds to ignore further "lid opened" signals (each bud announces it)
EAR_DEBOUNCE = 1.0    # an in-ear change must hold this long before pausing / resuming
EAR_RSSI = -70        # ear tracking works from a bit further away than the pop-up
LOW_BATTERY = 20


def log(*parts) -> None:
    if sys.stdout:  # pythonw has no console
        print(time.strftime("%X"), *parts, flush=True)


# ---- settings & start-up ----------------------------------------------------------

def load_settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            if getattr(sys, "frozen", False):  # running as the PyInstaller .exe
                command = f'"{sys.executable}"'
            else:
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                command = f'"{pythonw}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def show_in_taskbar_corner() -> None:
    """Windows 11 puts new tray icons under the ^ arrow. Promote ours to the taskbar once;
    if the user hides it again later, leave their choice alone."""
    if not getattr(sys, "frozen", False):
        return  # as a script we run as python.exe, whose tray entry other scripts share
    exe = os.path.normcase(sys.executable)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\NotifyIconSettings") as icons:
            for i in range(winreg.QueryInfoKey(icons)[0]):
                with winreg.OpenKey(icons, winreg.EnumKey(icons, i), 0,
                                    winreg.KEY_READ | winreg.KEY_SET_VALUE) as entry:
                    try:
                        if os.path.normcase(winreg.QueryValueEx(entry, "ExecutablePath")[0]) != exe:
                            continue
                    except FileNotFoundError:
                        continue
                    try:
                        winreg.QueryValueEx(entry, "IsPromoted")  # already decided
                    except FileNotFoundError:
                        winreg.SetValueEx(entry, "IsPromoted", 0, winreg.REG_DWORD, 1)
                        log("tray icon moved onto the taskbar")
    except OSError:
        pass


# ---- media control ------------------------------------------------------------------

async def pause_media() -> bool:
    """Pause whatever is playing. Returns True if something was actually paused."""
    session = (await MediaManager.request_async()).get_current_session()
    if session and session.get_playback_info().playback_status == PlaybackStatus.PLAYING:
        return await session.try_pause_async()
    return False


async def resume_media() -> None:
    session = (await MediaManager.request_async()).get_current_session()
    if session:
        await session.try_play_async()


# ---- tray icon ----------------------------------------------------------------------

def make_icon(connected: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(28, 28, 30, 255))
    for x in (22, 42):  # two earbuds: head + stem
        d.ellipse((x - 8, 12, x + 8, 28), fill="white")
        d.rounded_rectangle((x - 3, 22, x + 3, 50), radius=3, fill="white")
    if connected:
        d.ellipse((42, 42, 62, 62), fill=(48, 209, 88, 255), outline=(28, 28, 30, 255), width=3)
    return img


# ---- the app ------------------------------------------------------------------------

class TrayApp:
    def __init__(self):
        self.settings = load_settings()
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self.airpods: PairedAirPods | None = PairedAirPods()
        except RuntimeError:
            self.airpods = None

        self.loop: asyncio.AbstractEventLoop | None = None
        self.quitting = False
        self.state: AirPodsState | None = None
        self.battery: dict[str, tuple[int | None, bool]] = {}  # last known value per part
        self.connected = False
        self.connecting = False
        self.connect_failed = False
        self.popup: Popup | None = None
        self.popup_shown_at = 0.0

        self.lid_counters: dict[str, int] = {}
        self.last_lid_open = 0.0
        self.started = time.monotonic()

        self.ears_seen: int | None = None       # latest in-ear count from adverts
        self.ears_stable: int | None = None     # debounced in-ear count
        self.ears_pending: int | None = None
        self.ears_pending_since = 0.0
        self.paused_by_us = False
        self.ears_before_pause = 0
        self.low_warned: set[str] = set()

        self.icon = pystray.Icon(APP_NAME, make_icon(False), "AirPods - waiting for case", self._menu())

    # ---- tray menu (callbacks arrive on pystray's thread) ----

    def _on_loop(self, fn, *args):
        return lambda icon, item: self.loop.call_soon_threadsafe(fn, *args)

    def _menu(self) -> pystray.Menu:
        def line(part):
            return lambda item: f"{part}: {self._fmt(part)}"

        def setting(key):
            return pystray.MenuItem(
                {"show_popup": "Show pop-up when case opens",
                 "auto_connect": "Auto-connect when case opens",
                 "auto_pause": "Pause media when a bud is removed",
                 "pause_on_disconnect": "Pause media when AirPods disconnect"}[key],
                self._on_loop(self.toggle, key), checked=lambda item: self.settings[key])

        return pystray.Menu(
            pystray.MenuItem(line("Left"), None, enabled=False),
            pystray.MenuItem(line("Right"), None, enabled=False),
            pystray.MenuItem(line("Case"), None, enabled=False),
            pystray.MenuItem(lambda item: "Connected to this PC" if self.connected else "Not connected",
                             None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Show battery", self._on_loop(self.open_popup), default=True),
            pystray.MenuItem("Connect", self._on_loop(self.connect),
                             enabled=lambda item: bool(self.airpods) and not self.connected),
            pystray.MenuItem("Disconnect", self._on_loop(self.disconnect),
                             enabled=lambda item: self.connected),
            pystray.Menu.SEPARATOR,
            setting("show_popup"),
            setting("auto_connect"),
            setting("auto_pause"),
            setting("pause_on_disconnect"),
            pystray.MenuItem("Start with Windows", self._on_loop(self.toggle_startup),
                             checked=lambda item: startup_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_loop(self.quit)),
        )

    def toggle(self, key: str) -> None:
        self.settings[key] = not self.settings[key]
        save_settings(self.settings)
        log(f"{key} -> {self.settings[key]}")
        self.icon.update_menu()

    def toggle_startup(self) -> None:
        set_startup(not startup_enabled())
        log("start with Windows ->", startup_enabled())
        self.icon.update_menu()

    def quit(self) -> None:
        self.quitting = True

    # ---- state ----

    def _fmt(self, part: str) -> str:
        value, charging = self.battery.get(part, (None, False))
        return "--" if value is None else f"{value}%{' (charging)' if charging else ''}"

    def _merged_state(self) -> AirPodsState:
        b = self.battery
        return dataclasses.replace(
            self.state,
            left=b.get("Left", (None, False))[0], left_charging=b.get("Left", (None, False))[1],
            right=b.get("Right", (None, False))[0], right_charging=b.get("Right", (None, False))[1],
            case=b.get("Case", (None, False))[0], case_charging=b.get("Case", (None, False))[1])

    def _changed(self) -> None:
        """Push the current state to the tray icon, its menu and the pop-up."""
        model = self.state.model if self.state else "AirPods"
        title = f"{model}\nL {self._fmt('Left')}  R {self._fmt('Right')}  Case {self._fmt('Case')}"[:127]
        signature = (title, self.connected, self.connecting)
        if signature != getattr(self, "_last_signature", None):  # adverts arrive many times a second
            self._last_signature = signature
            self.icon.title = title
            self.icon.icon = make_icon(self.connected)
            self.icon.update_menu()
        self._refresh_popup()

    def _check_low_battery(self) -> None:
        for part in ("Left", "Right", "Case"):
            value, charging = self.battery.get(part, (None, False))
            if value is None:
                continue
            if value <= LOW_BATTERY and not charging and part not in self.low_warned:
                self.low_warned.add(part)
                name = "Case" if part == "Case" else f"{part} AirPod"
                self.icon.notify(f"{name} at {value}%", "AirPods battery low")
            elif value > LOW_BATTERY + 10 or charging:
                self.low_warned.discard(part)

    # ---- Bluetooth adverts ----

    def on_advert(self, device, adv) -> None:
        data = adv.manufacturer_data.get(APPLE_COMPANY_ID)
        if not data:
            return
        data = bytes(data)
        s = decode(data)
        if s is None or adv.rssi < EAR_RSSI:
            return
        if self.airpods and ((data[3] << 8) | data[4]) != self.airpods.model_id:
            return  # a different AirPods model - not ours
        nothing_known = s.case is None and not (s.left_in_ear or s.right_in_ear
                                                or s.left_in_case or s.right_in_case)
        if nothing_known:
            return  # lid closed / buds in a pocket - also how a neighbour's pair usually looks

        self.state = s
        for part, value, charging in (("Left", s.left, s.left_charging), ("Right", s.right, s.right_charging),
                                      ("Case", s.case, s.case_charging)):
            if value is not None:
                self.battery[part] = (value, charging)
        self.ears_seen = int(s.left_in_ear) + int(s.right_in_ear)

        # Lid opened: a fresh address with the case open, or a changed lid counter.
        before = self.lid_counters.get(device.address)
        self.lid_counters[device.address] = s.lid_counter
        now = time.monotonic()
        if (s.case is not None and before != s.lid_counter and adv.rssi >= self.settings["min_rssi"]
                and now - self.started > 3 and now - self.last_lid_open > LID_COOLDOWN):
            self.last_lid_open = now
            self.on_lid_opened()

        self._check_low_battery()
        self._changed()

    def on_lid_opened(self) -> None:
        log("lid opened")
        if self.settings["show_popup"]:
            self.open_popup()
        if self.settings["auto_connect"] and not self.connected:
            self.connect()

    # ---- connection ----

    async def _watch_connection(self) -> None:
        if not self.airpods:
            return
        # Windows tells us the moment the link drops; the 3 s poll is only a fallback.
        self.bt_device = await BluetoothDevice.from_bluetooth_address_async(self.airpods.mac)
        if self.bt_device:
            self.bt_device.add_connection_status_changed(
                lambda device, _: self.loop.call_soon_threadsafe(
                    self._set_connected, device.connection_status == BluetoothConnectionStatus.CONNECTED))
        while not self.quitting:
            if not self.connecting:
                self._set_connected(await self.airpods.is_connected())
            await asyncio.sleep(3)

    def _set_connected(self, connected: bool) -> None:
        if connected == self.connected:
            return
        was_connected, self.connected = self.connected, connected
        log("connected" if connected else "disconnected")
        if was_connected and not connected and self.settings["pause_on_disconnect"]:
            # Stop YouTube & co. from carrying on through the PC speakers.
            self.paused_by_us = False
            self.loop.create_task(self._pause_after_disconnect())
        self._changed()

    async def _pause_after_disconnect(self) -> None:
        if await pause_media():
            log("paused media (AirPods disconnected)")

    def connect(self) -> None:
        if self.connecting or not self.airpods:
            return
        self.connecting, self.connect_failed = True, False
        self._changed()
        self.loop.create_task(self._connect())

    async def _connect(self) -> None:
        log("connecting...")
        try:
            ok = await self.airpods.connect()
        except OSError:
            ok = False
        log("connect", "ok" if ok else "failed")
        self.connecting, self.connected, self.connect_failed = False, ok, not ok
        if ok and self.popup:
            self.popup_shown_at = time.monotonic() - AUTO_HIDE_SECONDS + 3  # close in 3 s
        self._changed()

    def disconnect(self) -> None:
        if self.airpods:
            self.airpods.disconnect()
            log("disconnect sent")

    # ---- auto-pause ----

    def _check_ears(self) -> None:
        count = self.ears_seen
        if count is None or count == self.ears_stable:
            self.ears_pending = None
            return
        now = time.monotonic()
        if self.ears_pending != count:
            self.ears_pending, self.ears_pending_since = count, now
            return
        if now - self.ears_pending_since < EAR_DEBOUNCE:
            return
        previous, self.ears_stable, self.ears_pending = self.ears_stable, count, None
        log(f"buds in ears: {previous} -> {count}")
        if previous is None or not (self.settings["auto_pause"] and self.connected):
            return
        if count < previous and not self.paused_by_us:
            self.loop.create_task(self._pause(previous))
        elif count > previous and self.paused_by_us and count >= self.ears_before_pause:
            self.loop.create_task(self._resume())

    async def _pause(self, ears_before: int) -> None:
        if await pause_media():
            self.paused_by_us, self.ears_before_pause = True, ears_before
            log("paused media")

    async def _resume(self) -> None:
        self.paused_by_us = False
        await resume_media()
        log("resumed media")

    # ---- pop-up ----

    def open_popup(self) -> None:
        if self.popup or not self.state:
            return
        self.popup = Popup(self.root, self.state.model, self.connect, self.close_popup)
        self.popup_shown_at = time.monotonic()
        self._refresh_popup()

    def _refresh_popup(self) -> None:
        if not self.popup or not self.state:
            return
        self.popup.show_state(self._merged_state())
        if not self.airpods:
            self.popup.show_connection("Not paired with this PC - pair in Bluetooth settings", False)
        elif self.connecting:
            self.popup.show_connection("Connecting…", False)
        elif self.connected:
            self.popup.show_connection("Connected to this PC", False, done=True)
        elif self.connect_failed:
            self.popup.show_connection("Couldn't connect - are they in use by your phone?", True)
        else:
            self.popup.show_connection("Not connected to this PC", True)

    def close_popup(self) -> None:
        if self.popup:
            self.popup.destroy()
            self.popup = None

    # ---- main loop: one thread drives Bluetooth and Tk; pystray runs its own ----

    async def main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.icon.run_detached()
        log("running - look for the AirPods icon in the system tray")
        self.loop.call_later(5, show_in_taskbar_corner)  # Windows registers the icon a moment after it appears
        watcher = self.loop.create_task(self._watch_connection())
        async with BleakScanner(detection_callback=self.on_advert):
            while not self.quitting:
                self.root.update()
                self._check_ears()
                if self.popup and time.monotonic() - self.popup_shown_at > AUTO_HIDE_SECONDS:
                    self.close_popup()
                await asyncio.sleep(0.05)
        watcher.cancel()
        self.close_popup()
        self.icon.stop()


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    try:
        asyncio.run(TrayApp().main())
    except KeyboardInterrupt:
        pass
