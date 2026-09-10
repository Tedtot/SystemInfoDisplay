"""
Requirements:
    pip install pythonnet pystray pillow
    (tkinter and ctypes ship with Python already)
    LibreHardwareMonitorLib.dll + HidSharp.dll (net472 build) in this folder

Run as Administrator for full sensor access.
"""

import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import pythonnet
pythonnet.load("netfx")  # LibreHardwareMonitorLib.dll (net472) needs classic .NET Framework

import clr  # from pythonnet
import System

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DLL_PATH = os.path.join(BASE_DIR, "DLLs", "LibreHardwareMonitorLib.dll")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# pythonnet's netfx host doesn't automatically discover "python.exe.config"
# the way a normal .NET executable would, so assembly binding redirects
# (needed for System.Numerics.Vectors, System.Memory, etc.) never get
# applied. Pointing the AppDomain at the config file explicitly, before any
# relevant assembly gets loaded, fixes this without touching the global
# Python install. This should be the same LibreHardwareMonitor.exe.config
# that ships in the release zip -- just copy it into this folder too.
_BINDING_CONFIG = os.path.join(BASE_DIR, "LibreHardwareMonitor.exe.config")
if os.path.exists(_BINDING_CONFIG):
    System.AppDomain.CurrentDomain.SetData("APP_CONFIG_FILE", _BINDING_CONFIG)

if not os.path.exists(DLL_PATH):
    sys.exit("LibreHardwareMonitorLib.dll not found next to this script.")

if not os.path.exists(CONFIG_PATH):
    sys.exit("config.json not found next to this script.")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

clr.AddReference(DLL_PATH)

from System import Activator  # noqa: E402
from System.Reflection import Assembly  # noqa: E402

from pystray import Icon as TrayIcon, Menu, MenuItem  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

# ---------------------------------------------------------------------------
# Hardware setup (via reflection, so we don't depend on pythonnet's
# namespace-import mechanism matching this particular DLL build)
# ---------------------------------------------------------------------------

_assembly = Assembly.LoadFrom(DLL_PATH)

try:
    _all_types = list(_assembly.GetTypes())
except Exception as ex:
    _all_types = [t for t in getattr(ex, "Types", []) if t is not None]


def _find_type(name_suffix):
    for t in _all_types:
        if t.FullName and t.FullName.endswith(name_suffix):
            return t
    return None


ComputerType = _find_type(".Computer")
if ComputerType is None:
    sys.exit("Could not find the Computer type in LibreHardwareMonitorLib.dll.")

computer = Activator.CreateInstance(ComputerType)
computer.IsCpuEnabled = True
computer.IsGpuEnabled = False  # LibreHardwareMonitorLib's NVIDIA GPU init crashes under
                                 # pythonnet (Vector<T> type-load issue) -- read GPU via
                                 # nvidia-smi instead, see get_nvidia_smi_text() below.
computer.IsMotherboardEnabled = True
computer.IsMemoryEnabled = True
computer.IsStorageEnabled = True
computer.Open()


def iter_hardware(hardware_list):
    for hw in hardware_list:
        yield hw
        sub = getattr(hw, "SubHardware", None)
        if sub:
            yield from iter_hardware(sub)


def update_all_hardware():
    for hw in iter_hardware(computer.Hardware):
        try:
            hw.Update()
        except Exception:
            pass


def get_metric_value(hw_names, sensor_type_name, match):
    match = (match or "").lower()
    best = None
    for hardware in iter_hardware(computer.Hardware):
        if str(hardware.HardwareType) not in hw_names:
            continue
        for sensor in hardware.Sensors:
            if str(sensor.SensorType) != sensor_type_name:
                continue
            if sensor.Value is None:
                continue
            name_lower = sensor.Name.lower()
            if match and match in name_lower:
                return sensor.Value
            if best is None:
                best = sensor.Value
    return best


def get_metric_values(hw_names, sensor_type_name, match):
    """Like get_metric_value, but returns every matching sensor's value
    instead of just the first."""
    match = (match or "").lower()
    values = []
    for hardware in iter_hardware(computer.Hardware):
        if str(hardware.HardwareType) not in hw_names:
            continue
        for sensor in hardware.Sensors:
            if str(sensor.SensorType) != sensor_type_name:
                continue
            if sensor.Value is None:
                continue
            name_lower = sensor.Name.lower()
            if match and match in name_lower:
                values.append(sensor.Value)
    return values


def get_component_text(component):
    if component.get("source") == "nvidia-smi":
        return get_nvidia_smi_text(component)

    hw_names = component["hardware"]
    if isinstance(hw_names, str):
        hw_names = [hw_names]

    parts = []
    for metric in component["metrics"]:
        unit = metric.get("unit", "")
        decimals = metric.get("decimals", 0)

        if metric.get("match_all"):
            values = get_metric_values(hw_names, metric["sensor_type"], metric.get("match", ""))
            if not values:
                parts.append("--")
            else:
                parts.extend(f"{v:.{decimals}f}{unit}" for v in values)
        else:
            value = get_metric_value(hw_names, metric["sensor_type"], metric.get("match", ""))
            parts.append(f"{value:.{decimals}f}{unit}" if value is not None else "--")

    return f"{component['label']} " + " ".join(parts)

def get_nvidia_smi_text(component):
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return f"{component['label']} --"

        temp_str, power_str = result.stdout.strip().splitlines()[0].split(",")
        temp = float(temp_str.strip())
        power = float(power_str.strip())

        return f"{component['label']} {temp:.0f}°C {power:.0f}W"

    except Exception:
        return f"{component['label']} --"

# ---------------------------------------------------------------------------
# Taskbar-aware positioning
# ---------------------------------------------------------------------------

_user32 = ctypes.windll.user32


def get_taskbar_rect():
    """Return the real screen rectangle of the Windows taskbar, or None."""
    hwnd = _user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect


# ---------------------------------------------------------------------------
# Outlined text helper (Label can't do text borders -- a Canvas can, by
# drawing the text several times offset in the outline color underneath
# the real text on top)
# ---------------------------------------------------------------------------

class OutlinedText:
    def __init__(self, parent, bg, fg, font, outline="#000000", offset=1, padx=8):
        self.font = font
        self.fg = fg
        self.outline = outline
        self.offset = offset
        self.padx = padx
        self.canvas = tk.Canvas(parent, bg=bg, highlightthickness=0, bd=0)
        self.text = ""
        self._redraw()

    def grid(self, **kwargs):
        self.canvas.grid(**kwargs)

    def bind(self, seq, func):
        self.canvas.bind(seq, func)

    def set_text(self, text):
        if text == self.text:
            return
        self.text = text
        self._redraw()

    def _redraw(self):
        self.canvas.delete("all")
        w = self.font.measure(self.text) + self.padx * 2 + self.offset * 2
        h = self.font.metrics("linespace") + self.offset * 2
        self.canvas.configure(width=w, height=h)

        cx = self.padx + self.offset
        cy = self.offset

        o = self.offset
        for dx, dy in ((-o, 0), (o, 0), (0, -o), (0, o)):
            self.canvas.create_text(
                cx + dx, cy + dy, text=self.text, font=self.font,
                fill=self.outline, anchor="nw",
            )
        self.canvas.create_text(cx, cy, text=self.text, font=self.font, fill=self.fg, anchor="nw")


# ---------------------------------------------------------------------------
# Overlay window
# ---------------------------------------------------------------------------

class Overlay:
    def __init__(self, config):
        self.config = config
        self.components = config["components"]
        self.locked = True

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)

        bg_config = config.get("background", "#1e1e1e")
        if bg_config in (None, "transparent", ""):
            # A color unlikely to appear in any text/theme; every pixel of
            # this exact color becomes see-through, while the text itself
            # (a different color) stays fully visible.
            bg = "#123456"
            self.root.configure(bg=bg)
            self.root.attributes("-transparentcolor", bg)
        else:
            bg = bg_config
            self.root.configure(bg=bg)
            self.root.attributes("-alpha", config.get("opacity", 0.9))

        self.frame = tk.Frame(self.root, bg=bg)
        self.frame.pack(padx=2, pady=2)

        font_family = config.get("font_family", "Segoe UI")
        font_size = config.get("font_size", 12)
        self.font = tkfont.Font(family=font_family, size=font_size, weight="bold")
        outline_color = config.get("outline_color", "#000000")
        outline_width = config.get("outline_width", 1)

        self.labels = {}
        for i, component in enumerate(self.components):
            fg = component.get("color", "#ffffff")
            otext = OutlinedText(
                self.frame, bg=bg, fg=fg, font=self.font,
                outline=outline_color, offset=outline_width,
            )
            otext.set_text(f"{component['label']} --")
            otext.grid(row=0, column=i)
            self.labels[component["id"]] = otext

            otext.bind("<Button-1>", self._start_move)
            otext.bind("<B1-Motion>", self._on_move)
            otext.bind("<ButtonRelease-1>", self._save_position)

        self.root.update_idletasks()
        self._position_window()

        # Light safety-net re-check, not an aggressive fight for z-order.
        self.root.after(5000, self._light_keep_on_top)

    def _position_window(self):
        pos = self.config.get("position", {})
        x, y = pos.get("x"), pos.get("y")
        if x is not None and y is not None:
            self.root.geometry(f"+{x}+{y}")
            return

        win_w = self.root.winfo_width()
        win_h = self.root.winfo_height()
        margin = 8

        taskbar = get_taskbar_rect()
        if taskbar is not None:
            # Bottom-right, sitting just above the taskbar's top edge.
            x = taskbar.right - win_w - margin
            y = taskbar.top - win_h - margin
        else:
            # Fallback if the taskbar window couldn't be found.
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            x = screen_w - win_w - margin
            y = screen_h - win_h - 56

        self.root.geometry(f"+{x}+{y}")

    def _start_move(self, event):
        if self.locked:
            return

        self._drag_x = event.x
        self._drag_y = event.y

    def _on_move(self, event):
        if self.locked:
            return

        x = self.root.winfo_pointerx() - self._drag_x
        y = self.root.winfo_pointery() - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _save_position(self, event):
        if self.locked:
            return

        self.config["position"] = {
            "x": self.root.winfo_x(),
            "y": self.root.winfo_y(),
        }

        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception:
            pass

    def _light_keep_on_top(self):
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        self.root.after(5000, self._light_keep_on_top)

    def set_component_text(self, component_id, text):
        self.labels[component_id].set_text(text)

    def show(self):
        self.root.deiconify()

    def hide(self):
        self.root.withdraw()

    def quit(self):
        try:
            computer.Close()
        finally:
            self.root.destroy()


def poll_loop(overlay):
    interval = CONFIG.get("refresh_seconds", 2)
    while True:
        try:
            update_all_hardware()
            for component in overlay.components:
                text = get_component_text(component)
                overlay.root.after(0, overlay.set_component_text, component["id"], text)
        except Exception:
            pass
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Tray icon (controls only -- no data displayed here)
# ---------------------------------------------------------------------------

def make_tray_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # simple thermometer glyph
    draw.rounded_rectangle((26, 8, 38, 42), radius=6, fill=(255, 255, 255, 255))
    draw.ellipse((20, 38, 44, 62), fill=(255, 90, 60, 255))
    draw.rectangle((29, 14, 35, 44), fill=(255, 90, 60, 255))
    return img


def run_tray(overlay):
    visible = {"state": True}
    locked = {"state": True}

    def on_toggle(icon, item):
        def _do():
            if visible["state"]:
                overlay.hide()
            else:
                overlay.show()
            visible["state"] = not visible["state"]
        overlay.root.after(0, _do)

    def on_lock(icon, item):
        def _do():
            if locked["state"]:
                overlay.locked = False
            else:
                overlay.locked = True
            locked["state"] = not locked["state"]

        overlay.root.after(0, _do)

    def on_restart(icon, item):
        def _do():
            icon.stop()
            overlay.root.destroy()

            os.execv(sys.executable, [sys.executable] + sys.argv)

        overlay.root.after(0, _do)

    def on_quit(icon, item):
        icon.stop()
        overlay.root.after(0, overlay.quit)

    menu = Menu(
        MenuItem(
            "Visible",
            on_toggle,
            checked=lambda item: overlay.root.winfo_viewable(),
        ),
        MenuItem(
            "Lock UI",
            on_lock,
            checked=lambda item: locked["state"],
        ),
        MenuItem("Restart", on_restart),
        MenuItem("Quit", on_quit),
    )

    tray = TrayIcon(
        "sensor_overlay_control",
        make_tray_icon_image(),
        "Sensor Overlay",
        menu,
    )
    tray.run()



def main():
    overlay = Overlay(CONFIG)

    threading.Thread(target=poll_loop, args=(overlay,), daemon=True).start()
    threading.Thread(target=run_tray, args=(overlay,), daemon=True).start()

    overlay.root.mainloop()


if __name__ == "__main__":
    main()