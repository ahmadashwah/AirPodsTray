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


QUIT_EVENT = "Local\\AirPodsTray-quit"


def quit_event():
    """Named event a running copy watches; `AirPodsTray.exe --quit` sets it so the app exits cleanly
    (a force-kill skips PyInstaller's cleanup and leaves its unpacked _MEI folder in %TEMP%)."""
    return ctypes.WinDLL("kernel32").CreateEventW(None, False, False, QUIT_EVENT)


if __name__ == "__main__" and "--quit" in sys.argv:
    ctypes.windll.kernel32.SetEvent(quit_event())
    sys.exit(0)

# Checked before the slow imports below so a quick double-click can't start two copies.
if __name__ == "__main__" and already_running():
    sys.exit(0)

import asyncio
import dataclasses
import json
import os
import subprocess
import time
import tkinter as tk
import winreg
from pathlib import Path

import pystray
from bleak import BleakScanner
from bleak.backends.winrt.util import allow_sta
from PIL import Image, ImageDraw
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
            "disconnect_on_remove": True, "min_rssi": -60}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

LID_COOLDOWN = 15     # seconds to ignore further "lid opened" signals (each bud announces it)
EAR_DEBOUNCE = 0.1    # an in-ear change must hold this long before acting on it (each change
                      # arrives as one clean advert, so this only guards against stray ones)
EAR_RSSI = -85        # buds in your ears are weak (your head is in the way), so track them from far
LOW_BATTERY = 20
OTHER_PAIR_MARGIN = 8       # dB weaker than the strongest same-model AirPods = someone else's
PAIR_MEMORY = 15            # seconds an address counts as "recently seen" for that comparison
SCAN_SILENCE_RESTART = 30   # restart the Bluetooth scan if nothing at all is heard for this long
MAX_SCAN_FAILURES = 5       # failed scan restarts in a row before the app relaunches itself
HEARTBEAT_SECONDS = 600     # write an "alive" line to the log this often
# The .exe logs next to itself: AppData is redirected per app package on Windows, so a log there
# can look different (or stale) depending on which app opens it.
LOG_FILE = (Path(sys.executable).with_name("AirPodsTray.log") if getattr(sys, "frozen", False)
            else SETTINGS_FILE.with_name("log.txt"))


def log(*parts) -> None:
    line = " ".join([time.strftime("%Y-%m-%d %X"), *map(str, parts)])
    if sys.stdout:  # pythonw / the .exe have no console
        print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".old.txt"))
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


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

async def pause_media() -> list[str]:
    """Pause every session that is playing (not just Windows' "current" one, which may be
    a different app). Returns the ids of the apps that were paused."""
    paused = []
    for session in (await MediaManager.request_async()).get_sessions():
        if session.get_playback_info().playback_status == PlaybackStatus.PLAYING:
            if await session.try_pause_async():
                paused.append(session.source_app_user_model_id)
    log("paused:", paused or "nothing was playing")
    return paused


async def resume_media(app_ids: list[str]) -> None:
    """Resume these apps - only if they're still paused (not if the user stopped or switched)."""
    resumed = []
    for session in (await MediaManager.request_async()).get_sessions():
        if (session.source_app_user_model_id in app_ids
                and session.get_playback_info().playback_status == PlaybackStatus.PAUSED):
            if await session.try_play_async():
                resumed.append(session.source_app_user_model_id)
    log("resumed:", resumed or f"nothing ({app_ids} no longer paused)")


class PlayingTracker:
    """Remembers which apps were playing recently. AirPods send their own "pause" when a bud
    comes out, which can beat us to it - so "was playing a moment ago" decides what to resume."""

    def __init__(self):
        self.last_playing: dict[str, float] = {}

    async def run(self) -> None:
        manager = await MediaManager.request_async()
        statuses: dict[str, str] = {}
        while True:
            now = time.monotonic()
            for session in manager.get_sessions():
                app, status = session.source_app_user_model_id, session.get_playback_info().playback_status
                if status == PlaybackStatus.PLAYING:
                    self.last_playing[app] = now
                name = PlaybackStatus(status).name
                if statuses.get(app) != name:  # log every play/pause, whoever caused it
                    if app in statuses:
                        log(f"  media: {app} {statuses[app]} -> {name}")
                    statuses[app] = name
            await asyncio.sleep(0.2)

    def playing_within(self, seconds: float) -> list[str]:
        now = time.monotonic()
        return [app for app, t in self.last_playing.items() if now - t <= seconds]


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
        self.disconnecting = False
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
        self.paused_apps: list[str] = []
        self.playing = PlayingTracker()
        self.ears_rssi = 0
        self.low_warned: set[str] = set()
        self._errors_logged: dict[str, float] = {}
        self._raw_seen: dict[str, tuple] = {}
        self._signal: dict[str, tuple[float, float]] = {}  # address -> (smoothed dBm, last seen)
        self.last_advert_at = time.monotonic()
        self.advert_count = 0

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
                 "auto_connect": "Auto-connect (case opens or a bud goes in)",
                 "auto_pause": "Pause media when a bud is removed",
                 "pause_on_disconnect": "Pause media when AirPods disconnect",
                 "disconnect_on_remove": "Disconnect when both buds come out"}[key],
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
            setting("disconnect_on_remove"),
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
            try:
                self.icon.title = title
                self.icon.icon = make_icon(self.connected)
                self.icon.update_menu()
                self._last_signature = signature
            except Exception as e:  # a tray hiccup must never stop scanning or connection tracking
                self._log_error("tray update failed", e)
        try:
            self._refresh_popup()
        except Exception as e:
            self._log_error("pop-up update failed", e)

    def _log_error(self, what: str, error: BaseException) -> None:
        """Log an error, but each distinct error only once a minute (adverts arrive constantly)."""
        key, now = f"{what}: {error!r}", time.monotonic()
        if now - self._errors_logged.get(key, -1e9) > 60:
            self._errors_logged[key] = now
            log(key)

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
        self.last_advert_at = time.monotonic()
        self.advert_count += 1
        try:
            self._handle_advert(device, adv)
        except Exception as e:
            self._log_error("advert handling failed", e)

    def _log_raw_advert(self, address: str, rssi: int, data: bytes, s: AirPodsState, ignored: bool,
                        other_pair: bool = False) -> None:
        """Log each change in what a bud broadcasts (status bits + decoded result), to debug ear detection."""
        where = lambda ear, case: "ear" if ear else "case" if case else "out"
        key = (data[5], s.left_in_ear, s.right_in_ear, s.left_in_case, s.right_in_case)
        if self._raw_seen.get(address) == key:
            return
        self._raw_seen[address] = key
        sender = "L" if data[5] & 0x20 else "R"
        note = ("  (ignored: too far)" if rssi < EAR_RSSI else "  (ignored: no state)" if ignored
                else "  (ignored: someone else's AirPods)" if other_pair else "")
        log(f"  advert from {sender} bud {address[-5:]} {rssi} dBm status={data[5]:08b} -> "
            f"L {where(s.left_in_ear, s.left_in_case)}, R {where(s.right_in_ear, s.right_in_case)}{note}")

    def _is_other_pair(self, address: str, rssi: int) -> bool:
        """The broadcasts don't say whose AirPods they are, so lock onto the strongest signal of our
        model (ours sit next to the PC) and ignore addresses clearly weaker than it. Each address keeps
        a smoothed signal level so one strong or weak reading doesn't flip the choice."""
        now = time.monotonic()
        level = self._signal.get(address, (rssi, now))[0]
        self._signal[address] = (0.7 * level + 0.3 * rssi, now)
        recent = {a: lvl for a, (lvl, seen) in self._signal.items() if now - seen < PAIR_MEMORY}
        return self._signal[address][0] < max(recent.values()) - OTHER_PAIR_MARGIN

    def _handle_advert(self, device, adv) -> None:
        data = adv.manufacturer_data.get(APPLE_COMPANY_ID)
        if not data:
            return
        data = bytes(data)
        s = decode(data)
        if s is None:
            return
        if self.airpods and ((data[3] << 8) | data[4]) != self.airpods.model_id:
            return  # a different AirPods model - not ours
        nothing_known = s.case is None and not (s.left_in_ear or s.right_in_ear
                                                or s.left_in_case or s.right_in_case)
        other_pair = not nothing_known and adv.rssi >= EAR_RSSI and self._is_other_pair(device.address, adv.rssi)
        self._log_raw_advert(device.address, adv.rssi, data, s, nothing_known, other_pair)
        if adv.rssi < EAR_RSSI or nothing_known or other_pair:
            return  # too far / lid closed and buds in a pocket / someone else's AirPods

        self.state = s
        for part, value, charging in (("Left", s.left, s.left_charging), ("Right", s.right, s.right_charging),
                                      ("Case", s.case, s.case_charging)):
            if value is not None:
                self.battery[part] = (value, charging)
        self.ears_seen = int(s.left_in_ear) + int(s.right_in_ear)
        self.ears_rssi = adv.rssi

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
        # "Connected" = the AirPods are the audio output (a cheap registry read), checked every
        # 0.25 s so a disconnect pauses media quickly. The Bluetooth link's own status/event is
        # no use here: it often stays up after the audio has gone.
        while not self.quitting:
            if not self.connecting:
                try:
                    self._set_connected(await self.airpods.is_connected())
                except OSError as e:
                    log("connection check failed:", e)
            await asyncio.sleep(0.25)

    def _set_connected(self, connected: bool) -> None:
        if connected == self.connected:
            return
        was_connected, self.connected = self.connected, connected
        self.disconnecting = False
        log("connected" if connected else "disconnected")
        if was_connected and not connected and self.settings["pause_on_disconnect"]:
            # Stop YouTube & co. from carrying on through the PC speakers.
            self.paused_by_us = False
            self.loop.create_task(self._pause_after_disconnect())
        self._changed()

    async def _pause_after_disconnect(self) -> None:
        log("AirPods disconnected - pausing media")
        await pause_media()

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
            if self.connected:
                self.disconnecting = True
                self._refresh_popup()

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
        log(f"buds in ears: {previous} -> {count}  ({self.ears_rssi} dBm, connected={self.connected})")
        if previous is None:
            return
        # Connect the moment a bud goes in, disconnect the moment the last one comes out, instead of
        # waiting for the AirPods/Windows to switch on their own (which takes until the lid closes).
        if count > previous and not self.connected and self.settings["auto_connect"]:
            log("  bud put in -> connecting")
            self.connect()
            return
        if count == 0 and self.connected and self.settings["disconnect_on_remove"]:
            log("  both buds out -> disconnecting")
            self.disconnect()  # pause-on-disconnect takes care of the media
            return
        if not self.settings["auto_pause"]:
            log("  (auto-pause is turned off)")
            return
        if not self.connected:
            log("  (not acting: AirPods aren't connected to this PC)")
            return
        if count < previous and not self.paused_by_us:
            self.loop.create_task(self._pause(previous))
        elif count > previous and self.paused_by_us and count >= self.ears_before_pause:
            self.loop.create_task(self._resume())

    async def _pause(self, ears_before: int) -> None:
        # Include apps the AirPods already paused themselves in the last few seconds.
        recently_playing = self.playing.playing_within(EAR_DEBOUNCE + 3)
        paused_now = await pause_media()
        self.paused_apps = sorted(set(recently_playing) | set(paused_now))
        if self.paused_apps:
            self.paused_by_us, self.ears_before_pause = True, ears_before
            log("will resume when the bud goes back in:", self.paused_apps)

    async def _resume(self) -> None:
        self.paused_by_us = False
        await resume_media(self.paused_apps)

    # ---- pop-up ----

    def open_popup(self) -> None:
        if self.popup or not self.state:
            return
        self.popup = Popup(self.root, self.state.model, self.connect, self.close_popup, self.disconnect)
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
        elif self.disconnecting:
            self.popup.show_connection("Disconnecting…", False, done=True, can_disconnect=False)
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
        self.loop.set_exception_handler(lambda loop, ctx: log("error:", ctx.get("exception") or ctx["message"]))
        # The tray icon / Tk switch this thread to a GUI (STA) COM apartment once running. WinRT
        # callbacks then need Windows messages pumped - which _pump_ui does, all the time (also while
        # a scan is starting) - so tell Bleak not to refuse to start on an STA thread.
        allow_sta()
        jobs = [self.loop.create_task(self._pump_ui()),
                self.loop.create_task(self._keep_running(self._watch_connection, "connection watcher")),
                self.loop.create_task(self._keep_running(self.playing.run, "media tracker"))]
        failures = 0
        while not self.quitting:
            try:
                await self._scan()
                failures = 0
            except Exception as e:
                failures += 1
                log(f"Bluetooth scan stopped ({failures} in a row): {e!r} - restarting")
                if failures >= MAX_SCAN_FAILURES:
                    self.relaunch()  # a fresh process has always been able to scan, even after sleep
                    break
                await asyncio.sleep(2)
        for job in jobs:
            job.cancel()
        self.close_popup()
        self.icon.stop()

    def relaunch(self) -> None:
        """Start a fresh copy of the app; this one then quits normally (so PyInstaller cleans up)."""
        log("scan keeps failing - relaunching the app")
        mutex = globals().get("_instance_mutex")
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)  # let the new copy pass the single-instance check
        command = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
        subprocess.Popen(command, close_fds=True)
        self.quitting = True

    async def _pump_ui(self) -> None:
        """Keep the Tk window (and with it this thread's Windows messages) serviced continuously,
        and watch for `--quit` from another process."""
        quit_requested = quit_event()
        while not self.quitting:
            if ctypes.windll.kernel32.WaitForSingleObject(quit_requested, 0) == 0:  # WAIT_OBJECT_0
                log("quit requested")
                self.quitting = True
                break
            try:
                self.root.update()
                self._check_ears()
                if self.popup and time.monotonic() - self.popup_shown_at > AUTO_HIDE_SECONDS:
                    self.close_popup()
            except Exception as e:
                self._log_error("UI update failed", e)
            await asyncio.sleep(0.05)

    async def _keep_running(self, job, name: str) -> None:
        """Run a background job forever: if it crashes, log why and start it again."""
        while not self.quitting:
            try:
                await job()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"{name} crashed: {e!r} - restarting in 5 s")
                await asyncio.sleep(5)

    async def _scan(self) -> None:
        """Scan for adverts. Returns (so the caller restarts the scan) if adverts stop arriving -
        there are always other Bluetooth devices around, so total silence means Windows stopped
        the scan (e.g. the PC slept)."""
        self.last_advert_at = time.monotonic()
        last_heartbeat = time.monotonic()
        # Passive scanning: we only need the advertisement itself, never the scan response, and in
        # testing Windows delivered ~3x more AirPods adverts this way while audio was streaming.
        async with BleakScanner(detection_callback=lambda device, adv: self.on_advert(device, adv),
                                scanning_mode="passive"):
            while not self.quitting:
                now = time.monotonic()
                if now - self.last_advert_at > SCAN_SILENCE_RESTART:
                    log(f"no Bluetooth adverts for {SCAN_SILENCE_RESTART} s - restarting the scan")
                    return
                if now - last_heartbeat > HEARTBEAT_SECONDS:
                    log(f"alive: {self.advert_count} adverts in the last {HEARTBEAT_SECONDS // 60} min, "
                        f"connected={self.connected}")
                    self.advert_count, last_heartbeat = 0, now
                await asyncio.sleep(0.25)


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    try:
        asyncio.run(TrayApp().main())
    except KeyboardInterrupt:
        pass
