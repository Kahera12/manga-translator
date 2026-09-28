"""Verify browser-mode coordinate mapping against the real screen.

Takes a panel, computes where its blocks should be on screen, screenshots the
browser window and OCRs it, then compares positions of the *same text*.

    python tools/mapping_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from manga_translator.browser.cdp import CdpConnection, discover_port, list_tabs  # noqa: E402
from manga_translator.browser.reader import decode_image, read_state  # noqa: E402
from manga_translator.browser.win import window_for_port  # noqa: E402
from manga_translator.core.config import Config  # noqa: E402
from manga_translator.core.models import ScreenRegion  # noqa: E402
from manga_translator.core.paths import get_paths  # noqa: E402
from manga_translator.capture.screen_capture import ScreenCapture  # noqa: E402
from manga_translator.ocr import create_engine, filter_settings, filter_spans, group_settings, group_spans  # noqa: E402

PROFILE = ROOT / "runtime" / "browser-profile"


def main() -> int:
    port = discover_port(PROFILE)
    if port is None:
        print("!! no debugging browser")
        return 2
    tab = next((t for t in list_tabs(port) if "kuaikan" in t.url), None)
    if tab is None:
        print("!! no kuaikan tab")
        return 3
    window = window_for_port(port, tab.title)
    if window is None:
        print("!! browser window not found")
        return 4
    client_x, client_y, client_w, client_h = window.client
    print(f"client rect: {client_w}x{client_h} at ({client_x},{client_y})")

    conn = CdpConnection(tab.ws_url)
    state = read_state(conn)
    dpr = state.dpr or 1.0
    print(f"scroll={state.scroll_y:.0f} dpr={dpr} viewport={state.viewport_w:.0f}x"
          f"{state.viewport_h:.0f}")

    cfg = Config.load(get_paths())
    engine, _ = create_engine(cfg.ocr)
    engine.warmup()
    http = httpx.Client(timeout=40, headers={"User-Agent": "Mozilla/5.0 Chrome/154"},
                        follow_redirects=True)
    capture = ScreenCapture()
    region = ScreenRegion(int(client_x), int(client_y), int(client_w), int(client_h),
                          "browser", 1.0)

    expected = []          # (text, expected_x, expected_y, source)
    for panel in state.visible():
        if panel.nat_w < 800:
            continue
        x = (panel.doc_x - state.scroll_x) * dpr
        y = (panel.doc_y - state.scroll_y) * dpr
        scale = (panel.view_w / panel.nat_w) * dpr
        if y + panel.view_h < 0 or y > client_h:      # not actually on screen
            continue
        data = http.get(panel.src).content
        image = decode_image(data)
        spans = engine.recognize(image)
        filtered = filter_spans(spans, filter_settings(cfg.ocr))
        blocks = group_spans(filtered.accepted, group_settings(cfg.ocr),
                             page_area=float(image.shape[0] * image.shape[1]))
        print(f"\npanel #{panel.index} natural {panel.nat_w}x{panel.nat_h} "
              f"screen offset ({x:.0f},{y:.0f}) scale {scale:.3f}: {len(blocks)} block(s)")
        for block in blocks:
            ex = x + block.bbox.x * scale
            ey = y + block.bbox.y * scale
            print(f"   {block.text[:22]!r:<26} image box ({block.bbox.x:.0f},{block.bbox.y:.0f}) "
                  f"-> expected screen ({ex:.0f},{ey:.0f}) size "
                  f"{block.bbox.w * scale:.0f}x{block.bbox.h * scale:.0f}")
            expected.append((block.text, panel.key, block.bbox.x, block.bbox.y,
                             block.bbox.w, block.bbox.h, panel.index))

    if not expected:
        print("!! no text found on the visible panels")
        conn.close()
        return 5

    # Screenshot first, then re-read the layout: the reader's own widgets (and the
    # Chrome translate bar) resize the viewport, which shifts the images.
    screen = capture.grab(region)
    state2 = read_state(conn)
    by_key = {p.key: p for p in state2.panels}
    scale2 = {}
    for key, panel in by_key.items():
        scale2[key] = (panel.view_w / panel.nat_w) * (state2.dpr or 1.0)
    screen_spans = engine.recognize(screen)
    print(f"\nscreen OCR: {len(screen_spans)} span(s)")
    for span in screen_spans:
        print(f"   {span.text[:24]!r:<28} at ({span.bbox.x:.0f},{span.bbox.y:.0f}) "
              f"{span.bbox.w:.0f}x{span.bbox.h:.0f}")

    print("\nmatching:")
    matched = 0
    total = 0
    for text, key, bx, by, bw, bh, index in expected:
        panel = by_key.get(key)
        if panel is None or key not in scale2:
            continue
        total += 1
        ex = (panel.doc_x - state2.scroll_x) * (state2.dpr or 1.0) + bx * scale2[key]
        ey = (panel.doc_y - state2.scroll_y) * (state2.dpr or 1.0) + by * scale2[key]
        best = None
        for span in screen_spans:
            common = len(set(text) & set(span.text))
            if common < 2:
                continue
            distance = abs(span.bbox.x - ex) + abs(span.bbox.y - ey)
            if best is None or distance < best[0]:
                best = (distance, span)
        if best is None:
            print(f"   {text[:20]!r}: not found on screen")
            continue
        distance, span = best
        dx = span.bbox.x - ex
        dy = span.bbox.y - ey
        ok = abs(dx) <= 12 and abs(dy) <= 12
        matched += 1 if ok else 0
        print(f"   {text[:20]!r}: expected ({ex:.0f},{ey:.0f}) found "
              f"({span.bbox.x:.0f},{span.bbox.y:.0f}) delta ({dx:+.0f},{dy:+.0f}) "
              f"{'OK' if ok else 'MISMATCH'}")
    print(f"\n{matched}/{total} blocks map onto the right on-screen position")
    conn.close()
    return 0 if total and matched == total else 6


if __name__ == "__main__":
    sys.exit(main())
