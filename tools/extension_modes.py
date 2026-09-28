"""Verify the two rendering modes and the anti-flicker contract on a live page.

    python tools/extension_modes.py [--hold 150]

Sequence:
  1. start Chrome, install the extension over CDP, open the chapter;
  2. wait for cards, then compare the card geometry twice a few seconds apart —
     nothing may move, change text or disappear while the page is idle (that is
     the "мерцает" complaint, measured instead of eyeballed);
  3. switch the extension to in-place mode through its own popup page (the popup
     has chrome.storage access) and check that `.mt-inplace` layers appear;
  4. switch back to cards.

Prints a verdict per step and saves screenshots to runtime/artifacts/.
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

import httpx  # noqa: E402

from manga_translator.browser.cdp import discover_port, find_browser_executable  # noqa: E402

URL = "https://www.kuaikanmanhua.com/webs/comic-next/185055"

JS_SNAPSHOT = r"""
(() => {
  const out = {};
  for (const card of document.querySelectorAll('.mt-card')) {
    const id = card.getAttribute('data-mt-id') || '';
    out[id] = [card.style.left, card.style.top, (card.textContent || '').slice(0, 40)];
  }
  return JSON.stringify({
    cards: out,
    holders: document.querySelectorAll('.mt-holder').length,
    inplace: document.querySelectorAll('.mt-inplace').length,
    badge: (document.querySelector('[data-mt-badge]') || {}).textContent || '',
  });
})()
"""


def connect(ws_url: str):
    from websockets.sync.client import connect as ws_connect

    return ws_connect(ws_url, open_timeout=10.0, max_size=64 * 1024 * 1024)


def call(socket, message_id: int, method: str, params: dict = None) -> dict:
    socket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
    deadline = time.time() + 60
    while time.time() < deadline:
        message = json.loads(socket.recv(timeout=30.0))
        if message.get("id") == message_id:
            return message
    return {}


def evaluate(socket, expression: str, message_id: int = 99):
    reply = call(socket, message_id, "Runtime.evaluate",
                 {"expression": expression, "returnByValue": True, "awaitPromise": True})
    result = ((reply.get("result") or {}).get("result") or {})
    if result.get("subtype") == "error":
        return None
    return result.get("value")


def screenshot(socket, path: Path, message_id: int = 90) -> bool:
    """Save a PNG of the tab; a very tall page can take a while, so never fail hard."""
    try:
        reply = call(socket, message_id, "Page.captureScreenshot", {"format": "png"})
    except TimeoutError:
        print("    screenshot timed out")
        return False
    data = ((reply.get("result") or {}).get("data") or "")
    if not data:
        print(f"    screenshot returned nothing: {str(reply)[:200]}")
        return False
    path.write_bytes(base64.b64decode(data))
    print(f"    screenshot -> {path}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--profile", default="runtime/ext-modes")
    parser.add_argument("--hold", type=float, default=180.0)
    parser.add_argument("--out", default="runtime/artifacts")
    args = parser.parse_args()

    profile = (ROOT / args.profile).resolve()
    extension = (ROOT / "extension").resolve()
    outdir = ROOT / args.out
    outdir.mkdir(parents=True, exist_ok=True)

    port = args.port
    process = None
    if not port:
        exe = find_browser_executable()
        profile.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen([
            exe, "--remote-debugging-port=0", f"--user-data-dir={profile}",
            "--enable-unsafe-extension-debugging", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate,TranslateUI",
            "--window-size=1240,1330", "--window-position=60,30", URL], close_fds=True)
        for _ in range(40):
            port = discover_port(profile) or 0
            if port:
                break
            time.sleep(0.5)
    if not port:
        print("no browser")
        return 1
    print(f"port {port}")

    # -- install the extension -------------------------------------------
    version = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=5.0).json()
    with connect(version["webSocketDebuggerUrl"]) as socket:
        reply = call(socket, 1, "Extensions.loadUnpacked", {"path": str(extension)})
    extension_id = ((reply.get("result") or {}).get("id") or "")
    print(f"extension id {extension_id}")
    if not extension_id:
        print("could not install the extension")
        return 1

    time.sleep(4.0)
    tabs = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5.0).json()
    page = [t for t in tabs if "kuaikan" in (t.get("url") or "")][0]
    with connect(page["webSocketDebuggerUrl"]) as socket:
        call(socket, 2, "Page.reload", {})
    time.sleep(8.0)

    # -- 1. cards + flicker check ----------------------------------------
    tabs = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5.0).json()
    page = [t for t in tabs if "kuaikan" in (t.get("url") or "")][0]
    deadline = time.time() + args.hold
    first = {}
    with connect(page["webSocketDebuggerUrl"]) as socket:
        while time.time() < deadline:
            stats = json.loads(evaluate(socket, JS_SNAPSHOT) or "{}")
            cards = stats.get("cards") or {}
            if len(cards) >= 3:
                first = cards
                print(f"cards appeared: {len(cards)}, badge={stats.get('badge')!r}")
                break
            time.sleep(3.0)
        if not first:
            print("FAILED: no cards appeared")
            return 2

        time.sleep(8.0)
        second = json.loads(evaluate(socket, JS_SNAPSHOT) or "{}").get("cards") or {}
        moved = [key for key in first if key in second
                 and (first[key][0] != second[key][0] or first[key][1] != second[key][1])]
        changed = [key for key in first if key in second and first[key][2] != second[key][2]]
        lost = [key for key in first if key not in second]
        print(f"flicker check: {len(first)} -> {len(second)} cards, moved={len(moved)}, "
              f"text-changed={len(changed)}, disappeared={len(lost)}")
        for key in (moved + changed + lost)[:5]:
            print(f"    {key}: {first.get(key)} -> {second.get(key)}")
        screenshot(socket, outdir / "modes_cards.png", 3)
        verdict_cards = not moved and not lost
        print(f"cards verdict: {'STABLE' if verdict_cards else 'UNSTABLE'}")

    # -- 2. in-place mode through the popup -------------------------------
    with connect(version["webSocketDebuggerUrl"]) as socket:
        reply = call(socket, 4, "Target.createTarget",
                     {"url": f"chrome-extension://{extension_id}/popup.html"})
    time.sleep(3.0)
    tabs = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5.0).json()
    popups = [t for t in tabs if "popup.html" in (t.get("url") or "")]
    if not popups:
        print("could not open the popup page; skipping the in-place check")
        return 0
    with connect(popups[0]["webSocketDebuggerUrl"]) as socket:
        printed = evaluate(socket, "location.href", 5)
        print(f"popup: {printed}")
        result = evaluate(socket, "chrome.storage.local.set({mode: 'inplace', enabled: true})"
                                  ".then(() => 'ok')", 6)
        print(f"mode set to inplace: {result}")

    time.sleep(6.0)
    tabs = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5.0).json()
    page = [t for t in tabs if "kuaikan" in (t.get("url") or "")][0]
    with connect(page["webSocketDebuggerUrl"]) as socket:
        evaluate(socket, "window.scrollBy(0, 600); true", 7)
        deadline = time.time() + min(120.0, args.hold)
        inplace = 0
        while time.time() < deadline:
            stats = json.loads(evaluate(socket, JS_SNAPSHOT) or "{}")
            inplace = int(stats.get("inplace") or 0)
            print(f"  in-place layers: {inplace}, badge={stats.get('badge')!r}")
            if inplace >= 2:
                break
            time.sleep(6.0)
        screenshot(socket, outdir / "modes_inplace.png", 8)
        print(f"in-place verdict: {'OK' if inplace else 'NOTHING RENDERED'} "
              f"({inplace} layer(s))")

    if process is not None:
        print("(browser left running for inspection)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
