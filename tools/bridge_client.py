"""Pretend to be the browser extension: drive the bridge end to end.

    # panels from the debug browser that is already showing a chapter
    python tools/bridge_client.py --from-browser --limit 8

    # or a fixed list of image URLs
    python tools/bridge_client.py --url "https://.../p1.jpg" --url "https://.../p2.jpg"

The script speaks the real wire protocol over the real socket, so it verifies the
whole app-side path (register -> batched read -> panel payloads) without Chrome.
Exit code 0 means every requested panel came back with a payload.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from manga_translator.bridge.protocol import DEFAULT_PORT, decode, encode  # noqa: E402
from manga_translator.core.paths import get_paths  # noqa: E402


def panels_from_browser(limit: int, skip: int = 0, visible: int = 3,
                        collect: bool = False) -> list:
    from manga_translator.browser.cdp import CdpConnection, discover_port, list_tabs
    from manga_translator.browser.reader import collect_chapter, read_state

    profile = get_paths().root / "browser-profile"
    port = discover_port(profile)
    if not port:
        print("no debug browser found (start Browser mode or tools/browser_live.py)")
        return []
    tabs = [t for t in list_tabs(port) if t.type == "page"]
    if not tabs:
        return []
    conn = CdpConnection(tabs[0].ws_url)
    if collect:
        panels = collect_chapter(conn, progress=lambda step, y, doc_h, found: print(
            f"  scroll step {step}: y={int(y)}/{int(doc_h)} panels={found}"))
    else:
        panels = read_state(conn).panels
    conn.close()
    print(f"panels in the document: {len(panels)}")
    specs = []
    ordered = sorted(panels, key=lambda p: (p.doc_y, p.doc_x))[skip:]
    for position, panel in enumerate(ordered):
        if len(specs) >= limit:
            break
        specs.append({"key": f"p{skip + position}", "url": panel.src, "width": panel.nat_w,
                      "height": panel.nat_h, "order": skip + position,
                      "visible": position < visible, "doc_y": panel.doc_y})
    return specs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--from-browser", action="store_true")
    parser.add_argument("--skip", type=int, default=0,
                        help="skip the first N panels (use fresh, uncached pages)")
    parser.add_argument("--collect", action="store_true",
                        help="scroll the whole chapter first so every panel is loaded")
    parser.add_argument("--mode", default="cards", choices=("cards", "inplace"))
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    if args.url:
        specs = [{"key": f"u{index}", "url": url, "order": index, "visible": index < 3,
                  "width": 1280, "height": 1000} for index, url in enumerate(args.url)]
    elif args.from_browser:
        specs = panels_from_browser(args.limit, args.skip, collect=args.collect)
    else:
        print("pass --from-browser or at least one --url")
        return 2
    if not specs:
        print("no panels to translate")
        return 2
    print(f"{len(specs)} panel(s) to translate, mode={args.mode}")

    from websockets.sync.client import connect

    url = f"ws://127.0.0.1:{args.port}/ws"
    started = time.perf_counter()
    received: dict = {}
    errors: list = []
    welcome: dict = {}
    try:
        with connect(url, open_timeout=10.0, max_size=64 * 1024 * 1024) as socket:
            socket.send(encode("hello", version=1, url="https://example.invalid/chapter",
                               title="bridge client", tab=1, frame="top"))
            socket.send(encode("register", tab=1, page="https://example.invalid/chapter",
                               panels=specs))
            socket.send(encode("request", keys=[s["key"] for s in specs], mode=args.mode,
                               priority=True))
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                if len(received) >= len(specs):
                    break
                try:
                    raw = socket.recv(timeout=max(1.0, deadline - time.time()))
                except TimeoutError:
                    break
                message = decode(raw)
                kind = message.get("type")
                if kind == "welcome":
                    welcome = message
                    print(f"welcome: provider={message.get('provider')} "
                          f"model={message.get('model')} vision={message.get('vision')} "
                          f"batch={message.get('batch_size')}")
                elif kind == "panel":
                    key = message.get("key")
                    blocks = message.get("blocks") or []
                    received[key] = message
                    translated = [b for b in blocks if b.get("translation")]
                    inplace = message.get("inplace") or {}
                    inplace_note = ""
                    if inplace:
                        inplace_note = f", in-place {len(inplace.get('data', '')) // 1024} KB"
                    error_note = f" ERROR {message.get('error')}" if message.get("error") else ""
                    print(f"panel {key}: {len(blocks)} block(s), {len(translated)} translated"
                          f"{inplace_note}{error_note}"
                          f"  [{time.perf_counter() - started:.1f}s]")
                    for block in blocks[:4]:
                        print(f"    {block.get('id')} ({block.get('x'):.0f},{block.get('y'):.0f}"
                              f" {block.get('w'):.0f}x{block.get('h'):.0f}) "
                              f"{block.get('text')!r} -> {block.get('translation')!r}")
                elif kind == "progress":
                    print(f"progress: {message.get('done')}/{message.get('total')} "
                          f"queued={message.get('queued')} failed={message.get('failed')} "
                          f"phase={message.get('phase')}  [{time.perf_counter() - started:.1f}s]")
                elif kind == "error":
                    errors.append(message.get("message"))
                    print(f"error: {message.get('kind')}: {message.get('message')}")
                elif kind == "status":
                    print(f"status: {json.dumps({k: v for k, v in message.items() if k != 'type'}, ensure_ascii=False)[:400]}")
    except Exception as exc:
        print(f"connection failed: {type(exc).__name__}: {exc}")
        return 3

    elapsed = time.perf_counter() - started
    total_blocks = sum(len(m.get("blocks") or []) for m in received.values())
    translated = sum(1 for m in received.values() for b in (m.get("blocks") or [])
                     if b.get("translation"))
    print(f"\n{len(received)}/{len(specs)} panel(s), {translated}/{total_blocks} blocks "
          f"translated in {elapsed:.1f}s")
    if errors:
        print("errors:", errors[:3])
    missing = [s["key"] for s in specs if s["key"] not in received]
    if missing:
        print("no answer for:", missing)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
