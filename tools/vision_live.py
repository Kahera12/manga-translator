"""Live check of the vision path on a real chapter: gateway -> blocks -> boxes.

    python tools/vision_live.py [--panels 6] [--port 2738] [--profile <dir>]

Downloads real panel images through the attached browser (or directly over HTTP),
reads them in ONE vision request and reports what came back: pages read, blocks per
page, text, Russian translation, snapped box. Writes an annotated page to
runtime/artifacts/vision_page_00.png so the boxes can be eyeballed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import httpx  # noqa: E402
import numpy as np  # noqa: E402

from manga_translator.browser.cdp import (  # noqa: E402
    CdpConnection,
    discover_port,
    launch_browser,
    list_tabs,
)
from manga_translator.browser.reader import decode_image, fetch_panel_bytes, read_state  # noqa: E402
from manga_translator.core.config import Config  # noqa: E402
from manga_translator.core.paths import get_paths  # noqa: E402
from manga_translator.translation import create_backend  # noqa: E402
from manga_translator.translation.providers import get_spec  # noqa: E402
from manga_translator.vision import VisionReader  # noqa: E402

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36"


def grab(port: int, count: int, save_dir: Optional[Path] = None) -> list:
    tabs = [t for t in list_tabs(port) if t.type == "page"]
    if not tabs:
        print(f"no page tab on port {port}")
        return []
    tab = tabs[0]
    print(f"tab: {tab.title[:70]}")
    conn = CdpConnection(tab.ws_url)
    state = read_state(conn)
    panels = state.panels
    print(f"panels in the DOM: {len(panels)}")
    if not panels:
        return []
    http = httpx.Client(timeout=httpx.Timeout(20.0, read=60.0),
                        headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    picked = []
    for panel in panels:
        if len(picked) >= count:
            break
        data = None
        try:
            response = http.get(panel.src)
            if response.status_code == 200 and response.content:
                data = response.content
        except Exception as exc:
            print(f"  direct download failed: {exc}")
        if data is None:
            data = fetch_panel_bytes(conn, panel.src)
        if data is None:
            continue
        try:
            image = decode_image(data)
        except Exception as exc:
            print(f"  decode failed: {exc}")
            continue
        picked.append((panel, image))
        print(f"  + panel {panel.index} {image.shape[1]}x{image.shape[0]} "
              f"({len(data) / 1024:.0f} KB) {panel.src.split('/')[-1][:48]}")
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            target = save_dir / f"{len(picked) - 1:02d}.jpg"
            target.write_bytes(data)
    conn.close()
    return picked


def load_dir(directory: Path) -> list:
    """Panels from a previous run (``--save``) so prompts can be iterated offline."""
    picked = []
    for path in sorted(directory.glob("*.jpg")):
        data = path.read_bytes()
        image = decode_image(data)
        picked.append((None, image))
        print(f"  + {path.name} {image.shape[1]}x{image.shape[0]} ({len(data) / 1024:.0f} KB)")
    return picked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panels", type=int, default=6)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--profile", default="")
    parser.add_argument("--launch", default="",
                        help="chapter URL: launch a debug browser and open it first")
    parser.add_argument("--save", default="", help="save the downloaded panels here")
    parser.add_argument("--from", dest="load", default="",
                        help="read panels from this directory instead of the browser")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--out", default="runtime/artifacts")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)-28s %(message)s")

    picked: list = []
    if args.load:
        print(f"panels from {args.load}:")
        picked = load_dir(Path(args.load))
    else:
        profile = Path(args.profile) if args.profile else get_paths().root / "browser-profile"
        port = args.port or discover_port(profile) or 0
        if not port and args.launch:
            print(f"launching a debug browser on {profile} ...")
            port = launch_browser(profile, args.launch) or 0
            time.sleep(4.0)
        print(f"devtools port: {port} (profile {profile})")
        if not port:
            print("no browser with remote debugging found; start it from the app's Browser mode")
            return 1
        picked = grab(port, args.panels, Path(args.save) if args.save else None)
    if not picked:
        print("no panel images could be downloaded")
        return 2

    cfg = Config.load()
    provider = cfg.translation.active_provider()
    backend, warning = create_backend(cfg.translation, cfg.api_key())
    if warning:
        print(f"backend warning: {warning}")
    spec = get_spec(provider)
    reader = VisionReader(cfg.translation, spec, cfg.api_key(), backend=backend)
    print(f"provider={provider} model={cfg.translation.text_model} "
          f"vision_mode={cfg.translation.vision_mode} available={reader.available}")

    pages = [(f"panel{index}", image) for index, (_panel, image) in enumerate(picked)]
    started = time.perf_counter()
    try:
        result = reader.read_pages(pages, cfg.translation.target_language)
    except Exception as exc:
        print(f"FAILED after {time.perf_counter() - started:.1f}s: "
              f"{type(exc).__name__}: {exc}")
        return 3
    elapsed = time.perf_counter() - started
    total = sum(len(page.blocks) for page in result.values())
    print(f"\nread {len(result)} page(s), {total} block(s) in {elapsed:.1f}s "
          f"({elapsed / max(1, len(result)):.1f}s per page)\n")

    outdir = ROOT / args.out
    outdir.mkdir(parents=True, exist_ok=True)
    for index, (key, image) in enumerate(picked):
        page = result.get(f"panel{index}")
        print(f"--- panel {index} {image.shape[1]}x{image.shape[0]}: "
              f"{len(page.blocks) if page else 0} block(s)")
        annotated = image.copy()
        if page:
            for block in page.blocks:
                box = block.bbox
                cv2.rectangle(annotated, (int(box.x), int(box.y)),
                              (int(box.x2), int(box.y2)), (0, 200, 80), 3)
                print(f"    #{block.id} ({box.x:.0f},{box.y:.0f} {box.w:.0f}x{box.h:.0f}) "
                      f"{block.text!r} -> {block.translation!r}")
        path = outdir / f"vision_page_{index:02d}.png"
        cv2.imwrite(str(path), annotated)
        print(f"    annotated -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
