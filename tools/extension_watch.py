"""Watch the extension on a live page: scroll it and print what the page says.

    python tools/extension_watch.py --port 1234 --minutes 10 [--scroll-every 8]

Keeps a raw CDP session with `Runtime`/`Log` enabled, scrolls the chapter step by
step and prints, for every sample:
  * the number of `.mt-card` / `.mt-holder` / `.mt-inplace` elements and the badge,
  * the number of known panels the content script reports (`window.__mtDebug`),
  * every console message, warning and uncaught exception from any world.

This is the instrument for "cards disappeared while scrolling": the DOM counters show
*what* vanished and the console shows *why*.
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

JS_STATS = r"""
(() => {
  const cards = document.querySelectorAll('.mt-card');
  const holders = document.querySelectorAll('.mt-holder');
  const inplace = document.querySelectorAll('.mt-inplace');
  const badge = document.querySelector('[data-mt-badge]');
  return JSON.stringify({
    cards: cards.length, holders: holders.length, inplace: inplace.length,
    badge: badge ? badge.textContent : '',
    imgs: [...document.images].filter(i => i.naturalWidth >= 500 && i.naturalHeight >= 500).length,
    scrollY: Math.round(window.scrollY), docH: document.documentElement.scrollHeight,
  });
})()
"""

# Evaluated inside the content script's isolated world (see the context id).
JS_INTERNAL = r"""
(() => {
  if (typeof globalThis.__mtDebug !== 'function') return JSON.stringify({hook: false});
  try { return JSON.stringify({hook: true, ...globalThis.__mtDebug()}); }
  catch (err) { return JSON.stringify({hook: true, error: String(err)}); }
})()
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--scroll-every", type=float, default=8.0)
    parser.add_argument("--step", type=int, default=1500)
    args = parser.parse_args()

    from websockets.sync.client import connect

    tabs = httpx.get(f"http://127.0.0.1:{args.port}/json/list", timeout=5.0).json()
    pages = [t for t in tabs if t.get("type") == "page" and "chrome://" not in (t.get("url") or "")]
    if not pages:
        print("no page target")
        return 1
    page = pages[0]
    print(f"watching {page['url'][:80]} for {args.minutes} min")

    counter = [0]
    problems = 0
    with connect(page["webSocketDebuggerUrl"], open_timeout=10.0,
                 max_size=64 * 1024 * 1024) as socket:
        def send(method: str, params: dict = None) -> int:
            counter[0] += 1
            payload = {"id": counter[0], "method": method}
            if params:
                payload["params"] = params
            socket.send(json.dumps(payload))
            return counter[0]

        send("Runtime.enable")
        send("Log.enable")
        deadline = time.time() + args.minutes * 60
        next_sample = 0.0
        next_scroll = time.time() + args.scroll_every
        content_context = 0
        internal: dict = {}
        while time.time() < deadline:
            now = time.time()
            pending_stats = None
            pending_internal = None
            if now >= next_sample:
                pending_stats = send("Runtime.evaluate",
                                     {"expression": JS_STATS, "returnByValue": True})
                if content_context:
                    pending_internal = send("Runtime.evaluate",
                                            {"expression": JS_INTERNAL,
                                             "returnByValue": True,
                                             "contextId": content_context})
                next_sample = now + 5.0
            if now >= next_scroll:
                send("Runtime.evaluate",
                     {"expression": f"window.scrollBy(0, {args.step}); true",
                      "returnByValue": True})
                next_scroll = now + args.scroll_every
            try:
                message = json.loads(socket.recv(timeout=1.0))
            except TimeoutError:
                continue
            method = message.get("method")
            if method == "Runtime.executionContextCreated":
                context = message["params"]["context"]
                origin = context.get("origin") or ""
                if "chrome-extension" in origin:
                    content_context = int(context.get("id") or 0)
                    print(f"  content-script world: context #{content_context} {context.get('name')!r}")
            elif method == "Runtime.consoleAPICalled":
                parts = []
                for item in message["params"].get("args") or []:
                    parts.append(str(item.get("value", item.get("description", "")))[:200])
                level = message["params"].get("type")
                text = " ".join(parts)[:300]
                if level in ("error", "warning") or "[mt]" in text:
                    print(f"  console.{level}: {text}")
                if level == "error":
                    problems += 1
            elif method == "Runtime.exceptionThrown":
                details = message["params"].get("exceptionDetails") or {}
                where = f"{details.get('url', '')}:{details.get('lineNumber')}"
                text = (details.get("exception") or {}).get("description") or details.get("text")
                print(f"  EXCEPTION at {where}: {str(text)[:300]}")
                problems += 1
            elif method == "Log.entryAdded":
                entry = message["params"].get("entry") or {}
                print(f"  log[{entry.get('level')}] {entry.get('source')}: "
                      f"{str(entry.get('text'))[:250]}")
                if entry.get("level") == "error":
                    problems += 1
            elif pending_internal is not None and message.get("id") == pending_internal:
                value = ((message.get("result") or {}).get("result") or {}).get("value")
                if value:
                    internal = json.loads(value)
            elif pending_stats is not None and message.get("id") == pending_stats:
                value = ((message.get("result") or {}).get("result") or {}).get("value")
                if value:
                    stats = json.loads(value)
                    print(f"[{time.strftime('%H:%M:%S')}] cards={stats['cards']} "
                          f"holders={stats['holders']} inplace={stats['inplace']} "
                          f"imgs={stats['imgs']} y={stats['scrollY']}/{stats['docH']} "
                          f"badge={stats['badge']!r} content={internal}")
    print(f"\nfinished; {problems} error-level console event(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
