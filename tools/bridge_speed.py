"""Cold-start performance check for the bridge: how fast is a whole chapter read?

    python tools/bridge_speed.py [--panels 40] [--skip 0] [--clear]

Forces a cold page cache for the browser region, drives the bridge directly (no
browser extension involved) with N panels, and reports how long each one took and
the resulting panels-per-minute. This is the measurement behind the batching /
parallel-reader / gap-pass work: the same command on the same chapter is the A/B.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from manga_translator.bridge.protocol import DEFAULT_PORT, decode, encode  # noqa: E402
from manga_translator.core.paths import get_paths  # noqa: E402


def clear_browser_cache() -> int:
    """Drop the cached *panels* (the screen-capture pages are kept)."""
    db = get_paths().cache_db
    if not db.exists():
        return 0
    conn = sqlite3.connect(str(db), timeout=10.0)
    removed = conn.execute("DELETE FROM pages WHERE region_key LIKE 'browser#%'").rowcount
    conn.commit()
    conn.close()
    return int(removed or 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panels", type=int, default=40)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--clear", action="store_true", help="empty the panel cache first")
    parser.add_argument("--profile", default="",
                        help="browser profile with debugging (default: try the known ones)")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    if args.clear:
        print(f"cleared {clear_browser_cache()} cached panel(s)")

    from manga_translator.browser.cdp import CdpConnection, discover_port, list_tabs
    from manga_translator.browser.reader import collect_chapter

    root = get_paths().root
    candidates = ([Path(args.profile).expanduser()] if args.profile
                  else [root / "browser-profile", ROOT / "runtime/check-profile",
                        ROOT / "runtime/ext-live", ROOT / "runtime/ext-watch"])
    port = 0
    for candidate in candidates:
        port = discover_port(candidate) or 0
        if port:
            print(f"debug browser on port {port} ({candidate.name})")
            break
    if not port:
        print("no debug browser; start it first (tools/open_for_check.py or Browser mode)")
        return 1
    tabs = [t for t in list_tabs(port) if t.type == "page"]
    conn = CdpConnection(tabs[0].ws_url)
    panels = collect_chapter(conn, progress=lambda *a: None)
    conn.close()
    ordered = sorted(panels, key=lambda p: (p.doc_y, p.doc_x))[args.skip:]
    specs = [{"key": f"p{args.skip + index}", "url": p.src, "width": p.nat_w,
              "height": p.nat_h, "order": args.skip + index,
              "visible": index < 3, "doc_y": p.doc_y}
             for index, p in enumerate(ordered[:args.panels])]
    if not specs:
        print("no panels")
        return 2
    print(f"{len(specs)} panel(s) starting at {args.skip}")

    from websockets.sync.client import connect

    started = time.perf_counter()
    first = None
    times: dict = {}
    blocks = 0
    try:
        with connect(f"ws://127.0.0.1:{args.port}/ws", open_timeout=10.0,
                     max_size=64 * 1024 * 1024) as socket:
            socket.send(encode("hello", version=1, url="speed", title="speed", tab=1))
            socket.send(encode("register", tab=1, page="speed", panels=specs))
            socket.send(encode("request", keys=[s["key"] for s in specs], mode="cards",
                               priority=True))
            deadline = time.time() + args.timeout
            while len(times) < len(specs) and time.time() < deadline:
                try:
                    message = decode(socket.recv(timeout=max(1.0, deadline - time.time())))
                except TimeoutError:
                    break
                if message.get("type") == "panel" and message.get("key") in {s["key"] for s in specs}:
                    elapsed = time.perf_counter() - started
                    times[message["key"]] = elapsed
                    blocks += len(message.get("blocks") or [])
                    if first is None:
                        first = elapsed
                        print(f"first panel answered after {elapsed:.1f}s")
    except Exception as exc:
        print(f"connection failed: {type(exc).__name__}: {exc}")
        return 3

    total = time.perf_counter() - started
    done = len(times)
    rate = done / total * 60.0 if total else 0.0
    print(f"\n{done}/{len(specs)} panel(s), {blocks} block(s) in {total:.1f}s "
          f"= {rate:.1f} panels/min, {total / max(1, done):.2f}s per panel")
    if first is not None:
        print(f"first answer: {first:.1f}s")
    slowest = sorted(times.items(), key=lambda item: item[1])[-3:]
    print("last three:", ", ".join(f"{k}@{v:.1f}s" for k, v in slowest))
    return 0 if done == len(specs) else 4


if __name__ == "__main__":
    sys.exit(main())
