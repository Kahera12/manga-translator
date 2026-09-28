"""DPI awareness + physical/logical pixel conversion (Windows, multi-monitor).

Coordinate spaces used by the app:

* **physical** – real device pixels. `mss` grabs and Win32 monitor rects use this
  space. All capture geometry, page hashes and OCR boxes are anchored here
  (OCR boxes are *region-relative* physical pixels).
* **logical** – device-independent pixels used by Qt (`QScreen.geometry()`,
  `QWidget.setGeometry()`). On a 150% display 1 logical px = 1.5 physical px.

The app declares itself Per-Monitor-V2 DPI aware *before* Qt creates any
window, which is what makes `mss` coordinates equal to real pixels.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from .logging_setup import get_logger
from .models import Rect

log = get_logger("dpi")

IS_WINDOWS = sys.platform == "win32"
DPI_BASE = 96.0

_USER32 = ctypes.windll.user32 if IS_WINDOWS else None
_SHCORE = ctypes.windll.shcore if IS_WINDOWS else None

# DPI_AWARENESS_CONTEXT values (opaque handles expressed as -N in the SDK).
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = ctypes.c_void_p(-3)
_MDT_EFFECTIVE_DPI = 0
_MONITOR_DEFAULT_TO_PRIMARY = 0
_MONITOR_NEAREST_WINDOW = 2


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


# BOOL CALLBACK MonitorEnumProc(HMONITOR, HDC, LPRECT, LPARAM)
_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)

if IS_WINDOWS:  # declare signatures so 64-bit handles are not truncated
    _USER32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    _USER32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    _USER32.SetProcessDPIAware.restype = wintypes.BOOL
    _USER32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    _USER32.MonitorFromPoint.restype = wintypes.HMONITOR
    _USER32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfoEx)]
    _USER32.GetMonitorInfoW.restype = wintypes.BOOL
    _USER32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        _MONITORENUMPROC,
        wintypes.LPARAM,
    ]
    _USER32.EnumDisplayMonitors.restype = wintypes.BOOL
    _USER32.GetDpiForSystem.restype = wintypes.UINT
    try:
        _SHCORE.GetDpiForMonitor.argtypes = [
            wintypes.HMONITOR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        _SHCORE.GetDpiForMonitor.restype = ctypes.c_long
    except AttributeError:  # pragma: no cover - very old Windows
        _SHCORE = None


def enable_per_monitor_dpi_awareness() -> str:
    """Must run before any window/GUI object is created. Returns mode reached."""
    if not IS_WINDOWS:
        return "none"
    try:
        if _USER32.SetProcessDpiAwarenessContext(
            _DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return "per-monitor-v2"
    except OSError:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:  # PER_MONITOR_AWARE
            return "per-monitor"
    except OSError:
        pass
    try:
        if _USER32.SetProcessDPIAware():
            return "system"
    except OSError:
        pass
    return "unaware"


def list_monitors() -> list:
    """Physical monitor rectangles + device names (Windows only, [] elsewhere).

    Returns dicts: {"name", "physical": Rect, "work": Rect}.
    """
    if not IS_WINDOWS:
        return []

    monitors: list = []

    def _callback(hmon, _hdc, _lprect, _data):  # pragma: no cover - needs a desktop
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(_MonitorInfoEx)
        if not _USER32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return True
        r, w = info.rcMonitor, info.rcWork
        monitors.append(
            {
                "name": info.szDevice,
                "physical": Rect(r.left, r.top, r.right - r.left, r.bottom - r.top),
                "work": Rect(w.left, w.top, w.right - w.left, w.bottom - w.top),
            }
        )
        return True

    _USER32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_callback), 0)
    return monitors


def get_monitor_scale(x: int, y: int) -> float:
    """DPI scale (1.0 / 1.25 / 1.5 / 1.75 ...) of the monitor containing a point."""
    if not IS_WINDOWS or _SHCORE is None:
        return 1.0
    try:
        hmon = _USER32.MonitorFromPoint(
            wintypes.POINT(int(x), int(y)), _MONITOR_DEFAULT_TO_PRIMARY
        )
        dpi_x = wintypes.UINT()
        dpi_y = wintypes.UINT()
        if _SHCORE.GetDpiForMonitor(hmon, _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
            return max(0.5, round(dpi_x.value / DPI_BASE, 4))
    except (OSError, AttributeError):  # pragma: no cover
        pass
    return get_system_scale()


def get_monitor_name(x: int, y: int) -> str:
    if not IS_WINDOWS:
        return "display"
    try:
        hmon = _USER32.MonitorFromPoint(
            wintypes.POINT(int(x), int(y)), _MONITOR_DEFAULT_TO_PRIMARY
        )
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(_MonitorInfoEx)
        if _USER32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return info.szDevice
    except OSError:  # pragma: no cover
        pass
    return "display"


def get_system_scale() -> float:
    if not IS_WINDOWS:
        return 1.0
    try:
        return max(0.5, round(_USER32.GetDpiForSystem() / DPI_BASE, 4))
    except OSError:  # pragma: no cover
        return 1.0


# --- pure conversions (unit tested) ---------------------------------------


def physical_to_logical(value: float, scale: float) -> float:
    return value / (scale or 1.0)


def logical_to_physical(value: float, scale: float) -> float:
    return value * (scale or 1.0)


def rect_physical_to_logical(rect: Rect, scale: float) -> Rect:
    """Convert a rect whose *origin* is already monitor-relative logical space."""
    scale = scale or 1.0
    return Rect(rect.x / scale, rect.y / scale, rect.w / scale, rect.h / scale)


def rect_logical_to_physical(rect: Rect, scale: float) -> Rect:
    scale = scale or 1.0
    return Rect(rect.x * scale, rect.y * scale, rect.w * scale, rect.h * scale)


def current_desktop_mode() -> dict:
    """Diagnostics for the settings dialog / logs."""
    if not IS_WINDOWS:
        return {"awareness": "none", "scale": 1.0, "monitors": []}
    mode = "unknown"
    try:
        ctx = _USER32.GetThreadDpiAwarenessContext()
        value = _USER32.GetAwarenessFromDpiAwarenessContext(ctx)
        mode = {0: "unaware", 1: "system", 2: "per-monitor", 3: "per-monitor-v2"}.get(
            int(value), str(value)
        )
    except (OSError, AttributeError):  # pragma: no cover
        pass
    return {
        "awareness": mode,
        "scale": get_system_scale(),
        "monitors": [m["physical"].as_tuple() for m in list_monitors()],
    }
