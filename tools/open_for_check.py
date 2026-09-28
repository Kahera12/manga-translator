"""Open the chapter in Chrome with the extension installed, ready for a human check.

    python tools/open_for_check.py [--url <chapter>] [--profile runtime/check-profile]

Starts the app-side bridge (the app must already be running), launches Chrome,
installs the unpacked extension over CDP (Chrome 137+ ignores `--load-extension`),
reloads the page so the content script injects, and prints what is on screen.
Everything is left running for inspection.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from manga_translator.bridge.server import port_is_free  # noqa: E402
from manga_translator.browser.cdp import discover_port, find_browser_executable  # noqa: E402

URL = "https://www.kuaikanmanhua.com/webs/comic-next/185055"
JS = ("JSON.stringify({cards: document.querySelectorAll('.mt-card').length,"
      " holders: document.querySelectorAll('.mt-holder').length,"
      " inplace: document.querySelectorAll('.mt-inplace').length,"
      " badge: (document.querySelector('[data-mt-badge]') || {}).textContent || ''})")


def call(socket, message_id: int, method: str, params: dict = None) -> dict:
    socket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
    deadline = time.time() + 60
    while time.time() < deadline:
        message = json.loads(socket.recv(timeout=30.0))
        if message.get("id") == message_id:
            return message
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=URL)
    parser.add_argument("--profile", default="runtime/check-profile")
    parser.add_argument("--extension", default="extension")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()

    if port_is_free(args.port):
        print(f"WARNING: the app is not listening on 127.0.0.1:{args.port} — "
              f"start it first (python main.py)")
    else:
        print(f"app bridge detected on 127.0.0.1:{args.port}")

    profile = (ROOT / args.profile).resolve()
    extension = (ROOT / args.extension).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    executable = find_browser_executable()
    if executable is None:
        print("no Chromium-based browser found")
        return 1

    port = discover_port(profile) or 0
    if port:
        print(f"reusing the browser already running on port {port}")
    else:
        subprocess.Popen([
            executable, "--remote-debugging-port=0", f"--user-data-dir={profile}",
            "--enable-unsafe-extension-debugging", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate,TranslateUI",
            "--window-size=1240,1330", "--window-position=60,30", args.url,
        ], close_fds=True)
        for _ in range(40):
            port = discover_port(profile) or 0
            if port:
                break
            time.sleep(0.5)
    if not port:
        print("could not start the browser")
        return 1

    from websockets.sync.client import connect

    version = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=5.0).json()
    with connect(version["webSocketDebuggerUrl"], open_timeout=10.0,
                 max_size=16 * 1024 * 1024) as socket:
        reply = call(socket, 1, "Extensions.loadUnpacked", {"path": str(extension)})
    extension_id = ((reply.get("result") or {}).get("id") or "")
    print(f"extension: {extension_id or 'NOT LOADED'}")

    time.sleep(3.0)
    for _ in range(20):
        tabs = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5.0).json()
        pages = [t for t in tabs if t.get("type") == "page"
                 and "chrome://" not in (t.get("url") or "")]
        if pages:
            break
        time.sleep(1.0)
    if not pages:
        print("no page tab")
        return 1
    with connect(pages[0]["webSocketDebuggerUrl"], open_timeout=10.0,
                 max_size=16 * 1024 * 1024) as socket:
        call(socket, 2, "Page.reload", {})
    print("page reloaded; waiting for the first cards ...")

    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(4.0)
        tabs = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5.0).json()
        pages = [t for t in tabs if t.get("type") == "page"
                 and "chrome://" not in (t.get("url") or "")]
        if not pages:
            continue
        try:
            with connect(pages[0]["webSocketDebuggerUrl"], open_timeout=10.0,
                         max_size=16 * 1024 * 1024) as socket:
                reply = call(socket, 3, "Runtime.evaluate",
                             {"expression": JS, "returnByValue": True})
        except Exception as exc:
            print(f"  (probe failed: {exc})")
            continue
        value = ((reply.get("result") or {}).get("result") or {}).get("value")
        if value:
            stats = json.loads(value)
            print(f"  cards={stats['cards']} holders={stats['holders']} "
                  f"inplace={stats['inplace']} badge={stats['badge']!r}")
            if stats["cards"]:
                print("\nREADY: the page is translating. Chrome window and the app are running.")
                return 0
    print("\nthe page is open, but no cards appeared yet — check the app's log")
    return 2


if __name__ == "__main__":
    sys.exit(main())
