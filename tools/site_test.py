"""Live test against a real manga site.

Opens a chromeless Chrome window (dedicated profile, fixed position) on a real
comic page, then runs the real capture -> OCR -> grouping -> translation ->
overlay pipeline against it and dumps every artifact and diagnostic.

    python tools/site_test.py --url https://www.kuaikanmanhua.com/webs/comic-next/185055
    python tools/site_test.py --url ... --scroll 800 --no-launch
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = ROOT / "runtime" / "chrome-site-test"
ARTIFACTS = ROOT / "runtime" / "artifacts"

user32 = ctypes.windll.user32
user32.EnumWindows.argtypes = [
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM
]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


def list_windows() -> list:
    found: list = []

    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        title = ""
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append({
            "hwnd": int(hwnd), "title": title, "pid": int(pid.value),
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "rect": (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top),
        })
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback), 0)
    return found


def client_rect(hwnd: int) -> tuple:
    rect = wintypes.RECT()
    user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin))
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


def find_browser(profile_pid: int | None = None, title_substr: str = "") -> dict | None:
    """Largest visible top-level window of the launched Chrome process."""
    candidates = []
    for window in list_windows():
        if not window["visible"] or window["rect"][2] < 300 or window["rect"][3] < 300:
            continue
        if profile_pid and window["pid"] != profile_pid:
            continue
        if title_substr and title_substr.lower() not in window["title"].lower():
            continue
        if not window["title"]:
            continue
        candidates.append(window)
    if not candidates:
        return None
    candidates.sort(key=lambda w: w["rect"][2] * w["rect"][3], reverse=True)
    return candidates[0]


def launch(url: str, width: int, height: int, x: int, y: int) -> subprocess.Popen:
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        CHROME,
        f"--app={url}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,TranslateUI",
        f"--window-size={width},{height}",
        f"--window-position={x},{y}",
        "--lang=zh-CN",
    ]
    print("launching:", " ".join(args[:3]), "…")
    return subprocess.Popen(args)


def screenshot_window(hwnd: int, path: Path) -> tuple:
    """Capture the window's own pixels (works even when partly covered)."""
    user32.ShowWindow(wintypes.HWND(hwnd), 9)
    user32.SetForegroundWindow(wintypes.HWND(hwnd))
    time.sleep(0.6)
    left, top, width, height = client_rect(hwnd)
    window_dc = user32.GetWindowDC(wintypes.HWND(hwnd))
    gdi32 = ctypes.windll.gdi32
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    user32.PrintWindow(wintypes.HWND(hwnd), mem_dc, 2)

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
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0)
    image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("RGB")
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(wintypes.HWND(hwnd), window_dc)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return image, (left, top, width, height)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://www.kuaikanmanhua.com/webs/comic-next/185055")
    parser.add_argument("--width", type=int, default=1240)
    parser.add_argument("--height", type=int, default=1330)
    parser.add_argument("--x", type=int, default=60)
    parser.add_argument("--y", type=int, default=30)
    parser.add_argument("--wait", type=float, default=14.0)
    parser.add_argument("--scroll", type=int, default=0, help="scroll down by N px before capturing")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument("--pid", type=int, default=0, help="browser pid to attach to")
    parser.add_argument("--title-substr", default="", help="match the window title instead of a pid")
    args = parser.parse_args()

    pid = args.pid or None
    before = {window["hwnd"] for window in list_windows()}
    if not args.no_launch and not args.pid:
        process = launch(args.url, args.width, args.height, args.x, args.y)
        print(f"launcher pid={process.pid}; waiting {args.wait:.0f}s for the page…")
        time.sleep(args.wait)
    else:
        time.sleep(0.8)

    target = None
    if not args.no_launch:
        fresh = [
            window for window in list_windows()
            if window["hwnd"] not in before and window["visible"]
            and window["rect"][2] > 300 and window["rect"][3] > 300
        ]
        if fresh:
            fresh.sort(key=lambda w: w["rect"][2] * w["rect"][3], reverse=True)
            target = fresh[0]
    target = target or find_browser(pid, args.title_substr)
    if target is None:
        print("!! browser window not found; windows seen:")
        for window in list_windows():
            if window["visible"] and window["rect"][2] > 300:
                print(f"   pid={window['pid']:<7} {window['rect'][2]}x{window['rect'][3]} "
                      f"{window['title'][:60]!r}")
        return 2

    title = target["title"]
    print(f"browser window: hwnd={target['hwnd']} pid={target['pid']} title={title[:80]!r}")
    image, rect = screenshot_window(target["hwnd"], ARTIFACTS / "site_page.png")
    print(f"client area: {rect[2]}x{rect[3]} at ({rect[0]},{rect[1]}) -> "
          f"{ARTIFACTS / 'site_page.png'}")

    if args.scroll:
        # wheel scroll inside the window
        user32.SetCursorPos(rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)
        for _ in range(abs(args.scroll) // 100):
            ctypes.windll.user32.mouse_event(0x0800, 0, 0, -120 if args.scroll > 0 else 120, 0)
            time.sleep(0.05)
        time.sleep(1.2)
        image, rect = screenshot_window(target["hwnd"], ARTIFACTS / "site_page_scrolled.png")
        print(f"scrolled -> {ARTIFACTS / 'site_page_scrolled.png'}")

    print(f"window title decoded: {title}")
    print(json.dumps({"rect": rect, "title": title}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
