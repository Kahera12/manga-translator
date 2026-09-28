"""Why is the extension not doing anything? Raw CDP session with events.

    python tools/extension_diag.py --port 11564 [--reload]

Listens to Runtime/Log events while the page loads and prints:
  * every execution context (an extension content script gets its own isolated
    world, so its presence/absence is the definitive "did the script run?" answer),
  * console messages and uncaught exceptions from any world,
  * the extension's own errors from the service worker, when one exists.

Nothing is modified; this is a read-only diagnostic.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402


def targets(port: int) -> list:
    return httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5.0).json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--seconds", type=float, default=12.0)
    args = parser.parse_args()

    pages = [t for t in targets(args.port) if t.get("type") == "page"
             and "chrome://" not in (t.get("url") or "")]
    if not pages:
        print("no page target")
        return 1
    tab = pages[0]
    print(f"attaching to {tab['url'][:80]}")
    from websockets.sync.client import connect

    with connect(tab["webSocketDebuggerUrl"], open_timeout=10.0,
                 max_size=32 * 1024 * 1024) as socket:
        counter = [0]

        def send(method: str, params: dict = None) -> int:
            counter[0] += 1
            payload = {"id": counter[0], "method": method}
            if params:
                payload["params"] = params
            socket.send(json.dumps(payload))
            return counter[0]

        send("Runtime.enable")
        send("Log.enable")
        send("Page.enable")
        if args.reload:
            print("reloading...")
            send("Page.reload", {"ignoreCache": False})

        deadline = time.time() + args.seconds
        contexts = []
        while time.time() < deadline:
            try:
                message = json.loads(socket.recv(timeout=max(0.5, deadline - time.time())))
            except TimeoutError:
                break
            method = message.get("method")
            params = message.get("params") or {}
            if method == "Runtime.executionContextCreated":
                context = params.get("context") or {}
                origin = context.get("origin", "")
                name = context.get("name", "")
                aux = (context.get("auxData") or {}).get("type", "")
                contexts.append((context.get("id"), origin, name, aux))
                print(f"  context #{context.get('id')} type={aux} name={name!r} origin={origin[:70]}")
            elif method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails") or {}
                text = (details.get("exception") or {}).get("description") or details.get("text")
                print(f"  EXCEPTION: {str(text)[:400]}")
            elif method == "Runtime.consoleAPICalled":
                parts = [str(a.get("value", a.get("description", "")))[:200]
                         for a in params.get("args") or []]
                print(f"  console.{params.get('type')}: {' '.join(parts)[:300]}")
            elif method == "Log.entryAdded":
                entry = params.get("entry") or {}
                print(f"  log[{entry.get('level')}] {entry.get('source')}: "
                      f"{str(entry.get('text'))[:250]}")

        send("Runtime.evaluate", {"expression":
             "JSON.stringify({cards: document.querySelectorAll('.mt-card').length,"
             " holders: document.querySelectorAll('.mt-holder').length,"
             " mtNodes: document.querySelectorAll('[class^=mt-]').length,"
             " imgs: document.images.length,"
             " firstImg: (document.images[0]||{}).naturalWidth || 0})",
             "returnByValue": True})
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                message = json.loads(socket.recv(timeout=2.0))
            except TimeoutError:
                break
            if message.get("result", {}).get("result", {}).get("value"):
                print("page state:", message["result"]["result"]["value"])
                break

    worlds = [c for c in contexts if c[3] == "isolated" or "chrome-extension" in (c[1] or "")]
    print(f"\nisolated worlds (content scripts): {len(worlds)}")
    for world in worlds:
        print("  ", world)
    if not worlds:
        print("=> no content script ran in this page at all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
