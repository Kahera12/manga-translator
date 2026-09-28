"""Headless test of browser mode: attach, collect panels, OCR+translate, map to screen.

    python tools/browser_mode_test.py [--steps 20] [--panels 3]
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ARTIFACTS = ROOT / "runtime" / "artifacts"
ARTIFACTS.mkdir(parents=True, exist_ok=True)
# Uses the real application config (provider, key, cache) unless overridden.
if os.environ.get("MT_DATA_DIR_OVERRIDE"):
    os.environ["MT_DATA_DIR"] = os.environ["MT_DATA_DIR_OVERRIDE"]

from manga_translator.browser.cdp import CdpConnection, discover_port, list_tabs  # noqa: E402
from manga_translator.browser.reader import collect_chapter, decode_image, read_state  # noqa: E402
from manga_translator.browser.service import PanelTranslator  # noqa: E402
from manga_translator.cache.page_cache import PageCache  # noqa: E402
from manga_translator.core.config import Config  # noqa: E402
from manga_translator.core.paths import get_paths  # noqa: E402
from manga_translator.glossary.glossary import Glossary  # noqa: E402
from manga_translator.ocr import create_engine  # noqa: E402
from manga_translator.translation import create_backend  # noqa: E402

user32 = ctypes.windll.user32
user32.EnumWindows.argtypes = [
    ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM
]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]


def browser_client_rect(substring: str = "") -> tuple:
    best = None

    def callback(hwnd, _lparam):
        nonlocal best
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
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w > 400 and h > 400:
            best = (origin.x, origin.y, w, h, buffer.value)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(callback), 0)
    return best


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=str(ROOT / "runtime" / "browser-profile"))
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--panels", type=int, default=3)
    args = parser.parse_args()

    port = discover_port(Path(args.profile))
    if port is None:
        print("!! no browser with remote debugging found; run the app's browser mode first")
        return 2
    tabs = [t for t in list_tabs(port) if "kuaikan" in t.url or "comic" in t.url]
    if not tabs:
        tabs = list_tabs(port)
    print(f"port {port}, tab: {tabs[0].title[:60]!r}")

    conn = CdpConnection(tabs[0].ws_url)
    state = read_state(conn)
    print(f"viewport {state.viewport_w:.0f}x{state.viewport_h:.0f} dpr={state.dpr} "
          f"doc={state.doc_h:.0f}px panels(loaded)={len(state.panels)}")

    started = time.time()
    panels = collect_chapter(conn, max_steps=args.steps, settle_s=0.3,
                             progress=lambda step, y, doc, found: print(
                                 f"  scroll step {step}: y={y} doc={doc} panels={found}"
                             ) if step % 5 == 0 else None)
    print(f"collected {len(panels)} panels in {time.time() - started:.1f}s")

    cfg = Config.load(get_paths())
    cache = PageCache(get_paths(), max_mb=cfg.cache.max_mb,
                      hamming_threshold=cfg.cache.hamming_threshold)
    engine, _ = create_engine(cfg.ocr)
    backend, warning = create_backend(cfg.translation, cfg.api_key())
    print(f"provider: {cfg.translation.active_provider()}/{cfg.translation.text_model} "
          f"(warning={warning})")
    translator = PanelTranslator(cfg, engine, backend, cache,
                                 Glossary(get_paths().glossary).load(create_sample=True))

    import httpx
    import numpy as np
    from PIL import Image

    http = httpx.Client(timeout=40, headers={"User-Agent": "Mozilla/5.0 Chrome/154"},
                        follow_redirects=True)
    for panel in panels[: args.panels]:
        started = time.time()
        response = http.get(panel.src)
        image = decode_image(response.content)
        result = translator.process(panel, image)
        print(f"\n--- panel #{panel.index} {panel.nat_w}x{panel.nat_h} "
              f"({panel.view_w:.0f}x{panel.view_h:.0f} on screen) "
              f"in {(time.time() - started) * 1000:.0f} ms "
              f"cache={result.from_cache} blocks={len(result.blocks)}")
        for block in result.blocks:
            mark = "SFX" if block.is_sfx else ("OK " if block.translation else "---")
            print(f"   [{mark}] {block.text[:30]!r:<34} -> {(block.translation or '')[:48]!r}")
        Image.fromarray(np.asarray(image)[:, :, ::-1]).save(
            str(ARTIFACTS / f"panel_{panel.index:03d}.jpg"), quality=80
        )

    # screen mapping check
    client = browser_client_rect("快看")
    if client:
        cx, cy, cw, ch, title = client
        print(f"\nbrowser client rect: {cw}x{ch} at ({cx},{cy}) {title[:40]!r}")
        state = read_state(conn)
        dpr = state.dpr or 1.0
        print(f"scroll_y={state.scroll_y:.0f} dpr={dpr}")
        for panel in state.visible()[:4]:
            px = (panel.doc_x - state.scroll_x) * dpr
            py = (panel.doc_y - state.scroll_y) * dpr
            inside = 0 <= py <= ch and 0 <= px <= cw
            print(f"   panel {panel.nat_w}x{panel.nat_h}: screen offset within window "
                  f"({px:.0f},{py:.0f}) size {(panel.view_w * dpr):.0f}x{(panel.view_h * dpr):.0f} "
                  f"visible={inside}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
