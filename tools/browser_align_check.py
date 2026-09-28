"""Independent check: do the in-page cards sit on the bubbles they translate?

* card rectangles come from the DOM (viewport coordinates),
* the Chinese text position comes from OCR of an *actual screenshot*.

The screenshot is in screen coordinates, so the viewport origin is derived from
the window geometry (client rect minus the browser's own chrome height reported
by the page itself).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from manga_translator.browser.cdp import CdpConnection, discover_port, list_tabs  # noqa: E402
from manga_translator.browser.win import window_for_port  # noqa: E402
from manga_translator.core.config import Config  # noqa: E402
from manga_translator.core.models import ScreenRegion  # noqa: E402
from manga_translator.core.paths import get_paths  # noqa: E402
from manga_translator.capture.screen_capture import ScreenCapture  # noqa: E402
from manga_translator.ocr import create_engine  # noqa: E402

PROFILE = ROOT / "runtime" / "browser-profile"

JS_CARDS = """
(() => {
  const root = document.getElementById('__mt_overlay');
  const cards = [];
  if (root) {
    for (const card of root.children) {
      const r = card.getBoundingClientRect();
      cards.push({text: card.textContent, id: card.getAttribute('data-mt-block'),
                  x: r.x, y: r.y, w: r.width, h: r.height});
    }
  }
  return JSON.stringify({
    cards: cards,
    viewport: {w: innerWidth, h: innerHeight, ox: window.screenX, oy: window.screenY},
    outer: {w: window.outerWidth, h: window.outerHeight},
    dpr: window.devicePixelRatio,
  });
})()
"""


def main() -> int:
    port = discover_port(PROFILE)
    tab = next((t for t in list_tabs(port) if "kuaikan" in t.url), None) if port else None
    if tab is None:
        print("!! no kuaikan tab (start browser mode first)")
        return 2
    window = window_for_port(port, tab.title)
    client = window.client
    print(f"client {client[2]}x{client[3]} at ({client[0]},{client[1]})")

    conn = CdpConnection(tab.ws_url)
    data = json.loads(conn.evaluate(JS_CARDS))
    cards = data["cards"]
    viewport = data["viewport"]
    chrome_height = client[3] - viewport["h"]
    viewport_left = client[2] - viewport["w"]
    print(f"viewport {viewport['w']}x{viewport['h']} · browser chrome height {chrome_height}px "
          f"· side border {viewport_left}px")
    print(f"cards in the page: {len(cards)}")
    if not cards:
        print("!! no cards drawn yet — translate something first")
        conn.close()
        return 3

    cfg = Config.load(get_paths())
    engine, _ = create_engine(cfg.ocr)
    capture = ScreenCapture()
    region = ScreenRegion(int(client[0]), int(client[1]), int(client[2]), int(client[3]),
                          "browser", 1.0)
    screen = capture.grab(region)
    spans = engine.recognize(screen)
    # screenshot pixels -> viewport CSS pixels
    ox, oy = viewport_left, chrome_height
    print(f"\nOCR of the window: {len(spans)} spans (converted to viewport coordinates)")
    candidates = []
    for span in spans:
        text = span.text
        if not any("\u4e00" <= ch <= "\u9fff" for ch in text):
            continue
        box = (span.bbox.x - ox, span.bbox.y - oy, span.bbox.w, span.bbox.h)
        candidates.append((text, box))
        print(f"   {text[:22]!r:<26} viewport ({box[0]:.0f},{box[1]:.0f}) "
              f"{box[2]:.0f}x{box[3]:.0f}")

    good = 0
    for card in cards:
        best = None
        for text, box in candidates:
            common = len(set(card["text"]) & set(text))
            horizontal = not (box[0] > card["x"] + card["w"] or box[0] + box[2] < card["x"])
            gap = box[1] - (card["y"] + card["h"])
            if not horizontal or not (-60 <= gap <= 120):
                continue
            score = abs(gap) - common * 5
            if best is None or score < best[0]:
                best = (score, gap, text)
        if best is None:
            print(f"   card {card['id']} {card['text'][:24]!r}: no bubble text found nearby")
            continue
        _score, gap, text = best
        ok = 0 <= gap <= 60
        good += 1 if ok else 0
        print(f"   card {card['id']} {card['text'][:22]!r} at y={card['y']:.0f} "
              f"h={card['h']:.0f} → bubble {text[:16]!r} at y={card['y'] + card['h'] + gap:.0f} "
              f"gap {gap:+.0f}px {'OK' if ok else 'CHECK'}")
    print(f"\ncards standing right above their bubble: {good}/{len(cards)}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
