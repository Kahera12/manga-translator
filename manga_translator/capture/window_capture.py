"""Optional "Select Window" support: list top-level windows and turn a window
into a capture region that can follow the window when it moves or resizes.

Region mode is what the MVP requires; window mode is a convenience on top of it
(Chrome/Edge, manga readers, image viewers...). Window capture itself is still a
region grab of the window's frame bounds, so it works with any app and needs no
WGC / DXGI integration.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import List, Optional

from ..core import dpi
from ..core.logging_setup import get_logger
from ..core.models import Rect, ScreenRegion

log = get_logger("window_capture")

IS_WINDOWS = sys.platform == "win32"
_USER32 = ctypes.windll.user32 if IS_WINDOWS else None
_DWMAPI = ctypes.windll.dwmapi if IS_WINDOWS else None

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

_ENUMPROC = (
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if IS_WINDOWS else None
)

if IS_WINDOWS:  # pragma: no cover - needs a desktop
    _USER32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _USER32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _USER32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _USER32.IsWindowVisible.argtypes = [wintypes.HWND]
    _USER32.IsIconic.argtypes = [wintypes.HWND]
    _USER32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _USER32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _USER32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _USER32.EnumWindows.argtypes = [_ENUMPROC, wintypes.LPARAM]


@dataclass
class WindowInfo:
    handle: int
    title: str
    rect: Rect          # physical px
    process_id: int = 0

    def label(self) -> str:
        return f"{self.title[:70]}  [{self.rect.w:.0f}x{self.rect.h:.0f}]"


def _window_frame_rect(hwnd) -> Optional[Rect]:
    """Physical bounds, excluding the invisible DWM drop shadow border."""
    if _DWMAPI is not None:
        rect = wintypes.RECT()
        try:
            result = _DWMAPI.DwmGetWindowAttribute(
                wintypes.HWND(hwnd),
                ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
                ctypes.byref(rect),
                ctypes.sizeof(rect),
            )
            if result == 0 and rect.right > rect.left and rect.bottom > rect.top:
                return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
        except OSError:  # pragma: no cover
            pass
    rect = wintypes.RECT()
    if _USER32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    return None


def _is_cloaked(hwnd) -> bool:
    if _DWMAPI is None:
        return False
    value = ctypes.c_int(0)
    try:
        result = _DWMAPI.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_CLOAKED), ctypes.byref(value), ctypes.sizeof(value)
        )
        return result == 0 and value.value != 0
    except OSError:  # pragma: no cover
        return False


def list_windows(min_size: int = 200, exclude_pid: Optional[int] = None) -> List[WindowInfo]:
    """Visible top-level windows that look like readers/browsers/viewers."""
    if not IS_WINDOWS:
        return []
    found: List[WindowInfo] = []

    def _callback(hwnd, _lparam):  # pragma: no cover - needs a desktop
        try:
            if not _USER32.IsWindowVisible(hwnd) or _USER32.IsIconic(hwnd):
                return True
            length = _USER32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            _USER32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title or _is_cloaked(hwnd):
                return True
            style = _USER32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            if style & WS_EX_TOOLWINDOW and not style & WS_EX_APPWINDOW:
                return True
            pid = wintypes.DWORD()
            _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if exclude_pid and pid.value == exclude_pid:
                return True
            rect = _window_frame_rect(hwnd)
            if rect is None or rect.w < min_size or rect.h < min_size:
                return True
            found.append(WindowInfo(handle=int(hwnd), title=title, rect=rect, process_id=int(pid.value)))
        except OSError:  # pragma: no cover
            return True
        return True

    try:
        _USER32.EnumWindows(_ENUMPROC(_callback), 0)
    except OSError as exc:  # pragma: no cover
        log.warning("window enumeration failed: %s", exc)
    return found


def window_region(info: WindowInfo, coordmap) -> ScreenRegion:
    monitor = coordmap.monitor_for_rect(info.rect)
    return ScreenRegion(
        x=int(info.rect.x),
        y=int(info.rect.y),
        width=int(info.rect.w),
        height=int(info.rect.h),
        monitor=monitor.name,
        dpi_scale=monitor.scale,
    )


def window_rect(handle: int) -> Optional[Rect]:
    if not IS_WINDOWS:
        return None
    rect = _window_frame_rect(handle)
    return rect


def is_window_alive(handle: int) -> bool:
    if not IS_WINDOWS:
        return False
    return bool(_USER32.IsWindow(wintypes.HWND(handle)))


def current_dpi_snapshot() -> dict:
    return dpi.current_desktop_mode()
