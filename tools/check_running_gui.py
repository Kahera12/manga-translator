"""Capture the app window itself (PrintWindow) so overlapping windows don't hide it."""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image

OUT = Path(r"C:\Users\neuro\.dsh\translator\runtime\artifacts\gui_running.png")
TITLES = ("control panel", "панель управления")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.EnumWindows.argtypes = [
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM
]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint]
user32.GetWindowDC.argtypes = [wintypes.HWND]

found: list = []


def callback(hwnd, _lparam):
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0 or not user32.IsWindowVisible(hwnd):
        return True
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    found.append({
        "hwnd": int(hwnd),
        "title": buffer.value,
        "rect": (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
    })
    return True


user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback), 0)
panels = [w for w in found
          if any(part in w["title"].lower() for part in TITLES) and w["rect"][2] > 400]
for window in found:
    if any(part in window["title"].lower() for part in TITLES):
        print(f"  hwnd={window['hwnd']} {window['rect'][2]}x{window['rect'][3]} "
              f"at {window['rect'][0]},{window['rect'][1]}  {window['title']}")
if not panels:
    print("RESULT: control panel window NOT FOUND")
    sys.exit(1)

window = panels[0]
hwnd = wintypes.HWND(window["hwnd"])
left, top, width, height = window["rect"]
user32.ShowWindow(hwnd, 9)
user32.SetForegroundWindow(hwnd)
time.sleep(0.7)

window_dc = user32.GetWindowDC(hwnd)
mem_dc = gdi32.CreateCompatibleDC(window_dc)
bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
gdi32.SelectObject(mem_dc, bitmap)
# PW_RENDERFULLCONTENT (2) also renders DWM-composited content
ok = user32.PrintWindow(hwnd, mem_dc, 2)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


info = BITMAPINFO()
info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
info.bmiHeader.biWidth = width
info.bmiHeader.biHeight = -height          # top-down
info.bmiHeader.biPlanes = 1
info.bmiHeader.biBitCount = 32
info.bmiHeader.biCompression = 0           # BI_RGB

buffer = ctypes.create_string_buffer(width * height * 4)
gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0)
image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("RGB")
gdi32.DeleteObject(bitmap)
gdi32.DeleteDC(mem_dc)
user32.ReleaseDC(hwnd, window_dc)

OUT.parent.mkdir(parents=True, exist_ok=True)
image.save(OUT)
print(f"RESULT: PrintWindow ok={bool(ok)} size={width}x{height} -> {OUT}")

bands = {
    "toolbar": (0, 0, width, 80),
    "preview (left)": (0, 100, int(width * 0.55), height - 240),
    "status (right)": (int(width * 0.55), 100, width, 560),
    "tabs (bottom)": (0, height - 240, width, height),
}
for name, box in bands.items():
    region = image.crop(box)
    colors = region.getcolors(maxcolors=1000000) or []
    light = sum(count for count, color in colors if min(color) > 200)
    dark = sum(count for count, color in colors if max(color) < 60)
    print(f"  {name:<16} distinct={len(colors):<6} light(text)={light:<7} dark(theme)={dark}")
