"""Live check of the *extension* in a real Chrome (the acceptance test).

    python tools/extension_live.py [--url <chapter>] [--scrolls 3] [--hold 90]

Launches Chrome with `--load-extension=extension` on a throwaway profile, opens a
chapter, scrolls and reports what the extension actually put on the page: how many
`.mt-card` elements exist, their texts, their coordinates as a percentage of the
image box (the anti-flicker anchoring contract), plus screenshots.

This is the only test that proves the whole chain: page -> extension -> WebSocket
bridge -> app -> batched translation -> DOM.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from manga_translator.browser.cdp import (  # noqa: E402
    CdpConnection,
    discover_port,
    find_browser_executable,
    list_tabs,
)

DEFAULT_URL = "https://www.kuaikanmanhua.com/webs/comic-next/185055"

JS_STATS = """
(() => {
  const cards = [...document.querySelectorAll('.mt-card')];
  const holders = [...document.querySelectorAll('.mt-holder')];
  const inplace = [...document.querySelectorAll('.mt-inplace')];
  const badge = document.querySelector('[data-mt-badge]');
  return JSON.stringify({
    cards: cards.length,
    holders: holders.length,
    inplace: inplace.length,
    badge: badge ? badge.textContent : '',
    sample: cards.slice(0, 8).map(c => ({
      id: c.getAttribute('data-mt-id'),
      text: (c.textContent || '').slice(0, 60),
      left: c.style.left, top: c.style.top, maxWidth: c.style.maxWidth,
      transform: c.style.transform,
    })),
    scrollY: window.scrollY,
    viewport: [window.innerWidth, window.innerHeight],
  });
})()
"""


def launch(profile: Path, extension: Path, url: str) -> int:
    executable = find_browser_executable()
    if executable is None:
        raise SystemExit("no Chromium-based browser found")
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        executable,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        # Chrome 137+ ignores --load-extension entirely (and --disable-extensions-except
        # additionally makes a CDP-side install invisible), so the extension is
        # installed over CDP *after* the browser is up: see load_extension().
        "--enable-unsafe-extension-debugging",
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate,TranslateUI",
        "--window-size=1240,1330", "--window-position=60,30",
        url,
    ]
    print("launching:", " ".join(args[:4]), "...")
    subprocess.Popen(args, close_fds=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        port = discover_port(profile)
        if port:
            return port
        time.sleep(0.5)
    return 0


def page_tab(port: int):
    for _ in range(30):
        tabs = [t for t in list_tabs(port) if t.type == "page"
                and "kuaikan" in (t.url or "")]
        if tabs:
            return tabs[0]
        time.sleep(1.0)
    tabs = [t for t in list_tabs(port) if t.type == "page"]
    return tabs[0] if tabs else None


def load_extension(port: int, extension: Path) -> str:
    """Install the unpacked extension over CDP (Chrome 137+ ignores --load-extension)."""
    import httpx
    from websockets.sync.client import connect

    try:
        version = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=5.0).json()
    except Exception as exc:
        print(f"could not reach the browser endpoint: {exc}")
        return ""
    ws_url = version.get("webSocketDebuggerUrl") or ""
    if not ws_url:
        return ""
    with connect(ws_url, open_timeout=10.0, max_size=16 * 1024 * 1024) as socket:
        socket.send(json.dumps({"id": 1, "method": "Extensions.loadUnpacked",
                                "params": {"path": str(extension)}}))
        while True:
            message = json.loads(socket.recv(timeout=30.0))
            if message.get("id") == 1:
                if "error" in message:
                    print(f"Extensions.loadUnpacked failed: {message['error'].get('message')}")
                    return ""
                return str((message.get("result") or {}).get("id") or "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--profile", default="runtime/extension-profile")
    parser.add_argument("--extension", default="extension")
    parser.add_argument("--scrolls", type=int, default=3)
    parser.add_argument("--hold", type=float, default=120.0)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--out", default="runtime/artifacts")
    args = parser.parse_args()

    profile = ROOT / args.profile
    extension = (ROOT / args.extension).resolve()
    port = args.port or discover_port(profile) or 0
    if not port:
        port = launch(profile, extension, args.url)
    if not port:
        print("could not start a browser with the extension")
        return 1
    print(f"devtools port {port}, extension {extension}")

    extension_id = load_extension(port, extension)
    if extension_id:
        print(f"extension loaded over CDP, id={extension_id}")
    else:
        print("WARNING: could not load the extension; cards will not appear")

    tab = page_tab(port)
    if tab is None:
        print("no page tab")
        return 1
    print(f"tab: {tab.title[:70]}")
    conn = CdpConnection(tab.ws_url)
    # Content scripts only appear in documents created after the extension loaded.
    conn.call("Page.reload", {"ignoreCache": False})
    time.sleep(6.0)
    tab = page_tab(port)
    if tab is not None:
        conn.close()
        conn = CdpConnection(tab.ws_url)
    outdir = ROOT / args.out
    outdir.mkdir(parents=True, exist_ok=True)

    # Give the extension time to load, register and receive its first panels.
    deadline = time.time() + args.hold
    best = {"cards": 0}
    shot = 0
    while time.time() < deadline:
        try:
            stats = json.loads(conn.evaluate(JS_STATS) or "{}")
        except Exception as exc:
            print(f"evaluate failed: {exc}")
            break
        cards = int(stats.get("cards") or 0)
        if cards != best.get("cards") or shot == 0:
            best = stats
            print(f"[{time.strftime('%H:%M:%S')}] cards={cards} holders={stats.get('holders')} "
                  f"inplace={stats.get('inplace')} badge={stats.get('badge')!r} "
                  f"scrollY={int(stats.get('scrollY') or 0)}")
            for item in stats.get("sample") or []:
                print(f"    {item['id']}: {item['text']!r} left={item['left']} top={item['top']} "
                      f"max-width={item['maxWidth']} transform={item['transform']}")
            path = outdir / f"extension_{shot:02d}.png"
            try:
                data = conn.call("Page.captureScreenshot", {"format": "png"})
                path.write_bytes(base64.b64decode(data.get("result", {}).get("data", "")
                                                  or data.get("data", "")))
                print(f"    screenshot -> {path}")
            except Exception as exc:
                print(f"    screenshot failed: {exc}")
            shot += 1
        if cards and shot >= args.scrolls:
            break
        time.sleep(3.0)

    if best.get("cards"):
        print(f"\nOK: the extension drew {best['cards']} card(s) on the page")
        return 0
    print("\nFAILED: no .mt-card elements appeared")
    return 2


if __name__ == "__main__":
    sys.exit(main())
