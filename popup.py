"""
AirPods pop-up for Windows.

When you open the AirPods case near the PC, a card appears in the bottom-right
corner with left / right / case battery and a Connect button.

Usage:
  python popup.py          # run in the background, pop up when the lid opens
  python popup.py --demo   # show the pop-up right away with sample data
"""

import argparse
import asyncio
import ctypes
import time
import tkinter as tk

from bleak import BleakScanner

from airpods_scanner import APPLE_COMPANY_ID, AirPodsState, decode
from auto_connect import PairedAirPods

BG = "#1c1c1e"
TRACK = "#3a3a3c"
TEXT = "#ffffff"
SUBTLE = "#8e8e93"
GREEN = "#30d158"
RED = "#ff453a"
BLUE = "#0a84ff"
BUTTON_GREY = "#2c2c2e"
FONT = "Segoe UI"

MIN_WIDTH = 380
AUTO_HIDE_SECONDS = 20
REOPEN_COOLDOWN = 8


class BatteryGauge(tk.Frame):
    def __init__(self, parent, label: str):
        super().__init__(parent, bg=BG)
        tk.Label(self, text=label, bg=BG, fg=SUBTLE, font=(FONT, 10)).pack()
        self.bar = tk.Canvas(self, width=80, height=12, bg=BG, highlightthickness=0)
        self.bar.pack(pady=6)
        self.value = tk.Label(self, text="--", bg=BG, fg=TEXT, font=(FONT, 15, "bold"))
        self.value.pack()

    def show(self, percent: int | None, charging: bool) -> None:
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, 80, 12, fill=TRACK, outline="")
        if percent is None:
            self.value.config(text="--")
            return
        colour = RED if percent <= 20 and not charging else GREEN
        self.bar.create_rectangle(0, 0, max(4, 80 * percent // 100), 12, fill=colour, outline="")
        self.value.config(text=f"{'⚡' if charging else ''}{percent}%")


class FlatButton(tk.Label):
    def __init__(self, parent, text: str, bg: str, command):
        super().__init__(parent, text=text, bg=bg, fg=TEXT, font=(FONT, 11, "bold"),
                         padx=18, pady=8, cursor="hand2")
        self.command = command
        self.enabled = True
        self.bind("<Button-1>", lambda _: self.enabled and self.command())


class Popup(tk.Toplevel):
    def __init__(self, root, title: str, on_connect, on_close, on_disconnect=None):
        super().__init__(root, bg=BG)
        self.on_connect, self.on_disconnect = on_connect, on_disconnect
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=20, pady=(16, 0))
        tk.Label(header, text=title, bg=BG, fg=TEXT, font=(FONT, 14, "bold")).pack(side="left")
        close = tk.Label(header, text="✕", bg=BG, fg=SUBTLE, font=(FONT, 12), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _: on_close())

        self.status = tk.Label(self, text="", bg=BG, fg=SUBTLE, font=(FONT, 10), anchor="w")
        self.status.pack(fill="x", padx=20)

        gauges = tk.Frame(self, bg=BG)
        gauges.pack(pady=14)
        self.left = BatteryGauge(gauges, "Left")
        self.right = BatteryGauge(gauges, "Right")
        self.case = BatteryGauge(gauges, "Case")
        for g in (self.left, self.right, self.case):
            g.pack(side="left", padx=16)

        buttons = tk.Frame(self, bg=BG)
        buttons.pack(pady=(4, 16))
        # One action button (Connect / Disconnect); the ✕ in the corner closes the pop-up.
        self.connect_button = FlatButton(buttons, "Connect", BLUE, on_connect)
        self.connect_button.pack()

        # Size to the content (so display scaling can't clip it) and sit above the taskbar.
        self.update_idletasks()
        scale = self.winfo_fpixels("1i") / 96
        width = max(self.winfo_reqwidth(), int(MIN_WIDTH * scale))
        height = self.winfo_reqheight()
        x = self.winfo_screenwidth() - width - int(24 * scale)
        y = self.winfo_screenheight() - height - int(64 * scale)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def show_state(self, s: AirPodsState) -> None:
        self.left.show(s.left, s.left_charging)
        self.right.show(s.right, s.right_charging)
        self.case.show(s.case, s.case_charging)

    def show_connection(self, text: str, can_connect: bool, done: bool = False,
                        can_disconnect: bool = True) -> None:
        """done=True means connected: the left button becomes Disconnect (if supported)."""
        self.status.config(text=text)
        if done and self.on_disconnect:
            self.connect_button.config(text="Disconnect", bg=BUTTON_GREY)
            self.connect_button.command, self.connect_button.enabled = self.on_disconnect, can_disconnect
        elif done:
            self.connect_button.pack_forget()
        else:
            self.connect_button.config(text="Connect", bg=BLUE if can_connect else BUTTON_GREY)
            self.connect_button.command, self.connect_button.enabled = self.on_connect, can_connect


class App:
    """Runs on a single thread: an asyncio loop drives Bluetooth and pumps the Tk
    window. (Running Bleak on a second thread next to Tk made Windows deliver
    almost no advertisements.)"""

    def __init__(self, min_rssi: int, demo: bool, verbose: bool = False):
        self.min_rssi = min_rssi
        self.demo = demo
        self.verbose = verbose
        self.popup: Popup | None = None
        self.closed_at = 0.0
        self.shown_at = 0.0
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self.airpods: PairedAirPods | None = PairedAirPods()
        except RuntimeError:
            self.airpods = None

    # ---- Bluetooth -------------------------------------------------------------

    def _advert_handler(self):
        previous: dict[str, int] = {}  # BLE address -> lid counter
        started = time.monotonic()

        seen: dict[str, tuple] = {}  # for --verbose: last printed state per address

        def on_advert(device, adv):
            data = adv.manufacturer_data.get(APPLE_COMPANY_ID)
            if not data:
                return
            s = decode(bytes(data))
            if s is None:
                return
            if self.verbose:
                key = (adv.rssi // 5, s.lid_counter, s.case, s.left_in_ear, s.right_in_ear)
                if seen.get(device.address) != key:
                    seen[device.address] = key
                    print(f"{time.strftime('%X')} {device.address[-5:]} {adv.rssi} dBm {s.model} lid#{s.lid_counter} "
                          f"L {s.left} R {s.right} case {s.case}"
                          f"{'  (too far, ignored)' if adv.rssi < self.min_rssi else ''}", flush=True)
            if adv.rssi < self.min_rssi:
                return
            # Ignore pairs with the lid closed and no bud in an ear (e.g. a neighbour's).
            if s.case is None and not (s.left_in_ear or s.right_in_ear):
                return
            before = previous.get(device.address)
            previous[device.address] = s.lid_counter
            # A new address with the case open, or a changed lid counter, means the lid just opened.
            lid_opened = (before is None or before != s.lid_counter) and s.case is not None
            warming_up = time.monotonic() - started < 3
            if self.verbose and lid_opened:
                print(f"{time.strftime('%X')}   lid opened{' (ignored: still starting up)' if warming_up else ''}",
                      flush=True)
            self._on_state(s, lid_opened and not warming_up)

        return on_advert

    async def _check_connected(self) -> None:
        connected = await self.airpods.is_connected()
        self._on_connection("already" if connected else "idle")

    async def _connect(self) -> None:
        try:
            ok = await self.airpods.connect()
        except OSError:
            ok = False
        self._on_connection("connected" if ok else "failed")

    # ---- UI --------------------------------------------------------------------

    def _on_state(self, s: AirPodsState, lid_opened: bool) -> None:
        if self.popup:
            self.popup.show_state(s)
        elif lid_opened and time.monotonic() - self.closed_at > REOPEN_COOLDOWN:
            self.open(s)

    def _on_connection(self, status: str) -> None:
        if not self.popup:
            return
        if status == "already":
            self.popup.show_connection("Connected to this PC", False, done=True)
        elif status == "idle":
            self.popup.show_connection("Not connected to this PC", True)
        elif status == "connected":
            self.popup.show_connection("Connected ✓", False, done=True)
            self.shown_at = time.monotonic() - AUTO_HIDE_SECONDS + 3  # close in 3 s
        elif status == "failed":
            self.popup.show_connection("Couldn't connect - are they in use by your phone?", True)

    def open(self, s: AirPodsState) -> None:
        if self.verbose:
            print(f"{time.strftime('%X')}   showing pop-up", flush=True)
        self.popup = Popup(self.root, s.model, self.connect, self.close)
        self.popup.show_state(s)
        self.shown_at = time.monotonic()
        if self.airpods:
            self.popup.show_connection("Checking…", False)
            asyncio.get_running_loop().create_task(self._check_connected())
        else:
            self.popup.show_connection("Not paired with this PC - pair in Bluetooth settings", False)

    def connect(self) -> None:
        if not self.popup or not self.airpods:
            return
        self.popup.show_connection("Connecting…", False)
        self.shown_at = time.monotonic()
        asyncio.get_running_loop().create_task(self._connect())

    def close(self) -> None:
        if self.popup:
            self.popup.destroy()
            self.popup = None
            self.closed_at = time.monotonic()

    async def main(self) -> None:
        print("Waiting for the AirPods case to open near this PC. Ctrl+C to stop.")
        if self.demo:
            self.open(AirPodsState("AirPods Pro 3", 70, 80, 85, False, False, True,
                                   False, False, True, True, 1, 0))
        async with BleakScanner(detection_callback=self._advert_handler()):
            while True:
                try:
                    self.root.update()
                except tk.TclError:  # root window destroyed
                    break
                if self.popup and time.monotonic() - self.shown_at > AUTO_HIDE_SECONDS:
                    self.close()
                await asyncio.sleep(0.05)

    def run(self) -> None:
        asyncio.run(self.main())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AirPods pop-up")
    parser.add_argument("--demo", action="store_true", help="show the pop-up now with sample data")
    parser.add_argument("--rssi", type=int, default=-60, help="minimum signal strength (default -60)")
    parser.add_argument("--verbose", action="store_true", help="print every AirPods update")
    args = parser.parse_args()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
    except (AttributeError, OSError):
        pass
    try:
        App(args.rssi, args.demo, args.verbose).run()
    except KeyboardInterrupt:
        pass
