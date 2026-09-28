"""End-to-end test against the real manga page in a real browser window.

Uses the real provider/key from the config, the real OCR, the real overlay, and
prints what the user would see: blocks, translations, timings, card count.

    python tools/site_live.py --title-substr 快看 --rounds 3
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ARTIFACTS = ROOT / "runtime" / "artifacts"
ARTIFACTS.mkdir(parents=True, exist_ok=True)

user32 = ctypes.windll.user32
user32.EnumWindows.argtypes = [
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM
]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]


def find_window(substring: str) -> dict | None:
    found = []

    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0 or not user32.IsWindowVisible(hwnd):
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if substring and substring.lower() not in buffer.value.lower():
            return True
        rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width > 400 and height > 400:
            found.append({"hwnd": int(hwnd), "title": buffer.value,
                          "rect": (origin.x, origin.y, width, height)})
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback), 0)
    return found[0] if found else None


def scroll(window: dict, clicks: int = -8) -> None:
    user32.SetCursorPos(window["rect"][0] + window["rect"][2] // 2,
                        window["rect"][1] + window["rect"][3] // 2)
    time.sleep(0.2)
    for _ in range(abs(clicks)):
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, -120 if clicks < 0 else 120, 0)
        time.sleep(0.08)
    time.sleep(1.5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title-substr", default="快看")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--scroll", type=int, default=-8)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    window = find_window(args.title_substr)
    if window is None:
        print(f"!! no window matching {args.title_substr!r}")
        return 2
    print(f"page window: {window['title'][:70]!r} client={window['rect'][2]}x{window['rect'][3]}"
          f" at ({window['rect'][0]},{window['rect'][1]})")


    from manga_translator.app import MangaTranslatorApp
    from manga_translator.core.models import ScreenRegion

    app = MangaTranslatorApp([])
    qt = app.qt
    print(f"provider: {app.config.translation.active_provider()} / "
          f"{app.config.translation.text_model} · key: {app.config.api_key_status()}")

    region = ScreenRegion(*window["rect"], monitor="TEST", dpi_scale=1.0)
    app.config.set_region(region)
    app.pipeline.set_region(region)
    app.overlay.follow_region(region)

    pages: list = []
    errors: list = []
    statuses: list = []
    app.bridge.pageReady.connect(lambda result, job: pages.append((result, job)))
    app.bridge.failed.connect(lambda message, kind: errors.append((kind, message)))
    app.bridge.statusChanged.connect(lambda message, level: statuses.append((level, message)))

    app.set_manga_mode(True)

    def pump(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            qt.processEvents()
            time.sleep(0.03)

    def wait_for_page(count: int, timeout: float) -> tuple | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            qt.processEvents()
            if len(pages) >= count:
                return pages[count - 1]
            time.sleep(0.05)
        return None

    def report(index: int, entry, elapsed: float) -> None:
        result, job = entry
        if result is None:
            print(f"\n--- round {index}: no result within {args.timeout:.0f}s")
            return
        cards = app.overlay.cards
        print(f"\n--- round {index}: gen={result.generation} kind={job.kind} "
              f"waited={elapsed:.1f}s total={result.total_ms:.0f}ms "
              f"ocr={result.ocr_ms:.0f}ms mt={result.translate_ms:.0f}ms "
              f"cache={result.from_cache} cards={len(cards)}")
        for block in result.blocks:
            mark = "SFX" if block.is_sfx else ("OK " if block.translation else "---")
            print(f"   [{mark}] {block.text[:34]!r:<38} -> {(block.translation or '')[:60]!r}")
        if cards:
            path = ARTIFACTS / f"site_round{index}_cards.png"
            app.overlay.grab().save(str(path))
            frame, _ = app.pipeline.latest_frame()
            if frame is not None:
                import numpy as np
                from PIL import Image

                Image.fromarray(np.asarray(frame)[:, :, ::-1]).save(
                    str(ARTIFACTS / f"site_round{index}_frame.png")
                )
            print(f"   artifacts: site_round{index}_frame.png / site_round{index}_cards.png")

    try:
        for index in range(1, args.rounds + 1):
            started = time.time()
            entry = wait_for_page(index, args.timeout)
            report(index, entry, time.time() - started)
            if index < args.rounds:
                print(f"   … scrolling ({abs(args.scroll)} notches)")
                scroll(window, args.scroll)
                pump(1.0)
        pump(3.0)
        stats = app.pipeline.stats.as_dict()
        print(f"\nstats: {stats}")
        print(f"translation memory: {app.cache.translation_memory_stats()}")
        print(f"detector: {app.pipeline.detector_status()}")
        if errors:
            print("errors:")
            for kind, message in errors[-6:]:
                print(f"   [{kind}] {message}")
        print("status messages:")
        for level, message in statuses[-10:]:
            print(f"   [{level}] {message}")
    finally:
        app.set_manga_mode(False)
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
