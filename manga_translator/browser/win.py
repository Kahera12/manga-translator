"""Find the browser window that owns a DevTools port (for screen placement).

The overlay has to cover exactly the browser *client area*, so we need the window
of the process that listens on the debugging port — not just any Chrome window
(the user may have several).
"""
from __future__ import annotations

import ctypes
import re
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import List, Optional

from ..core.logging_setup import get_logger

log = get_logger("browser.win")

IS_WINDOWS = sys.platform == "win32"
_USER32 = ctypes.windll.user32 if IS_WINDOWS else None

_ENUMPROC = (
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if IS_WINDOWS else None
)

if IS_WINDOWS:  # pragma: no cover - needs a desktop
    _USER32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _USER32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _USER32.IsWindowVisible.argtypes = [wintypes.HWND]
    _USER32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _USER32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _USER32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _USER32.EnumWindows.argtypes = [_ENUMPROC, wintypes.LPARAM]


@dataclass
class BrowserWindow:
    hwnd: int
    pid: int
    title: str
    client: tuple          # (x, y, width, height) in physical pixels

    def label(self) -> str:
        return f"{self.title[:60]} [{self.client[2]}x{self.client[3]}]"


def _windows() -> List[dict]:
    if not IS_WINDOWS:
        return []
    found: List[dict] = []

    def callback(hwnd, _lparam):  # pragma: no cover - needs a desktop
        if not _USER32.IsWindowVisible(hwnd):
            return True
        length = _USER32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        _USER32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        _USER32.GetClientRect(hwnd, ctypes.byref(rect))
        origin = wintypes.POINT(0, 0)
        _USER32.ClientToScreen(hwnd, ctypes.byref(origin))
        found.append({
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "title": buffer.value,
            "client": (origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top),
        })
        return True

    _USER32.EnumWindows(_ENUMPROC(callback), 0)
    return found


def pid_listening_on(port: int) -> Optional[int]:
    """PID that owns a local listening TCP port (netstat, no extra dependency)."""
    if not IS_WINDOWS:
        return None
    try:
        output = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        log.debug("netstat failed: %s", exc)
        return None
    pattern = re.compile(rf"^\s*TCP\s+\S+:{port}\s+\S+\s+LISTENING\s+(\d+)\s*$", re.MULTILINE)
    match = pattern.search(output or "")
    return int(match.group(1)) if match else None


def window_for_port(port: int, title_hint: str = "") -> Optional[BrowserWindow]:
    """Largest visible window of the process that listens on the debug port."""
    pid = pid_listening_on(port)
    candidates = []
    for window in _windows():
        if pid is not None and window["pid"] != pid:
            continue
        if window["client"][2] < 400 or window["client"][3] < 300:
            continue
        if not window["title"]:
            continue
        candidates.append(window)
    if not candidates and title_hint:
        # fall back to a title match (e.g. the port belongs to a child process)
        for window in _windows():
            if title_hint.lower() in window["title"].lower() and window["client"][2] > 400:
                candidates.append(window)
    if not candidates:
        return None
    candidates.sort(key=lambda w: w["client"][2] * w["client"][3], reverse=True)
    best = candidates[0]
    return BrowserWindow(hwnd=best["hwnd"], pid=best["pid"], title=best["title"],
                         client=best["client"])


def client_rect_for_hwnd(hwnd: int) -> tuple:
    if not IS_WINDOWS:
        return (0, 0, 0, 0)
    rect = wintypes.RECT()
    _USER32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    _USER32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin))
    return (origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top)
