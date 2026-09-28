"""Render in-place pages offline from what the app already cached.

    python tools/inplace_live.py [--dir runtime/diag/panels] [--panels 4]

For each saved panel it looks the page up in the page cache (by image hash), takes
the blocks *and their translations* the app produced during a live run, renders the
in-place page and writes `runtime/artifacts/inplace_<n>_before.png` / `_after.png`
so the mask quality can be judged by eye - and crops the erased area next to the
drawn text for a close look.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from manga_translator.browser.reader import decode_image  # noqa: E402
from manga_translator.cache.page_cache import PageCache, block_from_dict  # noqa: E402
from manga_translator.core.config import Config  # noqa: E402
from manga_translator.core.paths import get_paths  # noqa: E402
from manga_translator.render import InplaceStyle, encode_inplace, render_inplace  # noqa: E402


def cached_blocks(db: Path, page_hash: str):
    tmp = Path(tempfile.gettempdir()) / "mt_inplace_dump.db"
    shutil.copy2(db, tmp)
    conn = sqlite3.connect(str(tmp))
    rows = conn.execute(
        "SELECT blocks FROM pages ORDER BY last_used DESC LIMIT 5000").fetchall()
    conn.close()
    tmp.unlink(missing_ok=True)
    from manga_translator.detection.image_similarity import hamming_hex

    for (payload,) in rows:
        try:
            import json

            data = json.loads(payload)
        except ValueError:
            continue
        if not data:
            continue
        # The stored hash is not in this row; match by the block set instead.
        yield [block_from_dict(item) for item in data]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="runtime/diag/panels")
    parser.add_argument("--panels", type=int, default=4)
    parser.add_argument("--out", default="runtime/artifacts")
    args = parser.parse_args()

    cfg = Config.load()
    paths = get_paths()
    cache = PageCache(paths, enabled=True, hamming_threshold=cfg.cache.hamming_threshold)
    files = sorted(Path(args.dir).glob("*.jpg"))[:args.panels]
    outdir = ROOT / args.out
    outdir.mkdir(parents=True, exist_ok=True)
    if not files:
        print(f"no panels in {args.dir}")
        return 1

    for index, path in enumerate(files):
        image = decode_image(path.read_bytes())
        page_hash = PageCache.hash_frame(image)
        found = cache.find(page_hash, None, threshold=0)
        if found is None or not found.blocks:
            print(f"--- {path.name}: nothing in the cache for this panel "
                  f"(hash {page_hash})")
            continue
        blocks = [b for b in found.blocks if b.translation and not b.is_sfx]
        print(f"--- {path.name}: {len(blocks)} translated block(s) from the cache")
        for block in blocks:
            print(f"    #{block.id} ({block.bbox.x:.0f},{block.bbox.y:.0f} "
                  f"{block.bbox.w:.0f}x{block.bbox.h:.0f}) {block.text!r} -> {block.translation!r}")
        started = time.perf_counter()
        rendered = render_inplace(image, blocks, InplaceStyle(outline_px=0, max_font_px=48))
        elapsed = (time.perf_counter() - started) * 1000.0
        mime, data = encode_inplace(rendered, quality=88)
        print(f"    rendered in {elapsed:.0f} ms, {len(data) / 1024:.0f} KB base64 ({mime})")
        cv2.imwrite(str(outdir / f"inplace_{index:02d}_before.png"), image)
        cv2.imwrite(str(outdir / f"inplace_{index:02d}_after.png"), rendered)
        # Side-by-side crop of the first block: original | rendered.
        first = blocks[0].bbox
        pad = 40
        x1 = max(0, int(first.x) - pad)
        y1 = max(0, int(first.y) - pad)
        x2 = min(image.shape[1], int(first.x2) + pad)
        y2 = min(image.shape[0], int(first.y2) + pad)
        if x2 - x1 > 10 and y2 - y1 > 10:
            left = image[y1:y2, x1:x2]
            right = rendered[y1:y2, x1:x2]
            strip = np.hstack([left, np.full((left.shape[0], 6, 3), 255, np.uint8), right])
            cv2.imwrite(str(outdir / f"inplace_{index:02d}_crop.png"), strip)
            print(f"    crop -> {outdir / f'inplace_{index:02d}_crop.png'}")
    print(f"\nimages in {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
