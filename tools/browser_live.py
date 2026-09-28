"""Live test of browser mode through the real application (overlay included).

    python tools/browser_live.py [--scrolls 3] [--port-from runtime\\browser-profile]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ARTIFACTS = ROOT / "runtime" / "artifacts"
ARTIFACTS.mkdir(parents=True, exist_ok=True)

from manga_translator.browser.cdp import discover_port, list_tabs  # noqa: E402
from manga_translator.browser.win import window_for_port  # noqa: E402



JS_CARD_COUNT = """
(() => {
  const root = document.getElementById('__mt_overlay');
  const out = [];
  if (root) for (const card of root.children) {
    const r = card.getBoundingClientRect();
    out.push({text: card.textContent.slice(0, 60), y: Math.round(r.y), h: Math.round(r.height)});
  }
  return JSON.stringify(out);
})()
"""


def page_cards(app):
    """Cards drawn inside the page (browser mode) as a list of dicts."""
    import json as _json

    try:
        raw = app.browser_mode.conn.evaluate(JS_CARD_COUNT)
        return _json.loads(raw or "[]")
    except Exception:
        return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=str(ROOT / "runtime" / "browser-profile"))
    parser.add_argument("--scrolls", type=int, default=3)
    parser.add_argument("--step", type=int, default=1400)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--hold", type=float, default=0.0,
                        help="keep browser mode running for N seconds (for external checks)")
    args = parser.parse_args()

    port = discover_port(Path(args.profile))
    if port is None:
        print("!! no debugging browser; start one first")
        return 2
    tabs = list_tabs(port)
    tab = next((t for t in tabs if "kuaikan" in t.url), tabs[0] if tabs else None)
    if tab is None:
        print("!! no tabs")
        return 2
    print(f"port {port} · tab {tab.title[:60]!r}")

    from manga_translator.app import MangaTranslatorApp

    app = MangaTranslatorApp([])
    app._browser_port = port
    app._browser_window = None
    qt = app.qt

    def pump(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            qt.processEvents()
            time.sleep(0.02)

    try:
        if not app.connect_browser_tab(tab):
            print("!! attach failed")
            return 3
        window = window_for_port(port, tab.title)
        print(f"browser window for the overlay: {window.label() if window else None}")

        # wait for the first cards
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            pump(0.2)
            if page_cards(app):
                break
        cards = page_cards(app)
        info = app.browser_mode.info
        print(f"\nfirst render: {len(cards)} card(s) after "
              f"{args.timeout - (deadline - time.time()):.1f}s")
        print(f"status: {info.line()}")
        for result in sorted(app.browser_mode.results.values(), key=lambda r: r.panel.index):
            for block in result.blocks:
                mark = "SFX" if block.is_sfx else ("OK " if block.translation else "---")
                print(f"   [{mark}] {block.text[:26]!r:<30} -> {(block.translation or '')[:46]!r}")
        for card in cards[:8]:
            print(f"   card in page at y={card['y']} h={card['h']} text {card['text'][:48]!r}")

        print("\nfor the independent alignment check run: "
              "python tools\\browser_align_check.py")

        # scroll and watch it follow
        for index in range(1, args.scrolls + 1):
            before = [c["text"] for c in page_cards(app)]
            app.browser_mode.conn.evaluate(f"window.scrollBy(0, {args.step}); true;")
            started = time.time()
            changed = False
            while time.time() - started < 60:
                pump(0.2)
                now = [c["text"] for c in page_cards(app)]
                if now and now != before:
                    changed = True
                    break
            cards = page_cards(app)
            state = app.browser_mode.state
            print(f"\nscroll {index}: after {time.time() - started:.1f}s "
                  f"{len(cards)} card(s), changed={changed}, pending="
                  f"{app.browser_mode.info.pending}, ready={app.browser_mode.info.ready}")
            print(f"   scroll_y={state.scroll_y:.0f} visible panels: "
                  + ", ".join(
                      f"#{p.index}({'ready' if p.key in app.browser_mode.results else 'busy'},"
                      f"{len(app.browser_mode.results[p.key].blocks) if p.key in app.browser_mode.results else 0}b,"
                      f"y={p.doc_y - state.scroll_y:.0f})" for p in state.visible()
                  ))
            for card in cards[:4]:
                print(f"   card y={card['y']} {card['text'][:56]!r}")

        # save artifacts: the page itself, cards included
        from PIL import Image

        region = app.browser_mode.screen_blocks(app.browser_mode.state.visible())[1]
        if region is not None:
            shot = app.capture.grab(region)
            Image.fromarray(shot[:, :, ::-1]).save(str(ARTIFACTS / "browser_page.png"))
            print("artifacts: browser_page.png (the browser window with in-page cards)")
        good = [r for r in app.browser_mode.results.values() if not r.error]
        print(f"\npanels translated: {len(good)} / visible sets; "
              f"queue processed={app.browser_mode.info.processed} "
              f"failures={app.browser_mode.info.failures}")
        print("artifacts: browser_page.png (browser window with the in-page cards)")
        if args.hold > 0:
            print(f"\nholding browser mode for {args.hold:.0f}s — run "
                  f"tools\\browser_align_check.py now")
            deadline = time.time() + args.hold
            while time.time() < deadline:
                pump(0.5)
                cards = page_cards(app)
                print(f"   [{time.strftime('%H:%M:%S')}] cards={len(cards)} "
                      f"ready={app.browser_mode.info.ready} pending="
                      f"{app.browser_mode.info.pending} "
                      f"scroll={app.browser_mode.state.scroll_y:.0f}")
    finally:
        app.disconnect_browser()
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
