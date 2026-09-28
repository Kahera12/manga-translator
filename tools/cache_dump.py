"""Dump what the app actually stored for the panels it processed.

    python tools/cache_dump.py [--limit 20] [--region browser] [--previews]

Every cached page keeps its OCR blocks (text, translation, bbox, sfx flag), so
this is the ground truth for "why is this bubble not translated": either the
block was never OCR'd, it was classified as SFX, or its translation came back
empty. Previews are written to runtime/diag/ with --previews.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from manga_translator.core.paths import get_paths  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--region", default="", help="substring filter on region_key")
    parser.add_argument("--previews", action="store_true")
    parser.add_argument("--out", default="runtime/diag")
    args = parser.parse_args()

    db = get_paths().cache_db
    if not db.exists():
        print(f"no cache at {db}")
        return 1
    # The app has the file open: work on a copy so a busy writer cannot block us.
    tmp = Path(tempfile.gettempdir()) / "mt_cache_dump.db"
    shutil.copy2(db, tmp)
    conn = sqlite3.connect(str(tmp))
    where = "WHERE region_key LIKE ?" if args.region else ""
    params = (f"%{args.region}%",) if args.region else ()
    rows = conn.execute(
        "SELECT phash, region_key, width, height, blocks, model, preview, last_used, hits"
        f" FROM pages {where} ORDER BY last_used DESC LIMIT ?",
        (*params, args.limit),
    ).fetchall()
    print(f"cache: {db}  ({len(rows)} rows shown)")
    outdir = ROOT / args.out
    if args.previews:
        outdir.mkdir(parents=True, exist_ok=True)

    for phash, region, width, height, blocks_json, model, preview, last_used, hits in rows:
        blocks = json.loads(blocks_json)
        translated = [b for b in blocks if (b.get("translation") or "").strip()]
        sfx = [b for b in blocks if b.get("is_sfx")]
        print(f"\n=== {phash} {region} {width}x{height} model={model} hits={hits} "
              f"blocks={len(blocks)} translated={len(translated)} sfx={len(sfx)}")
        for block in sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0])):
            x, y, w, h = block["bbox"]
            mark = "SFX" if block.get("is_sfx") else ("ok " if (block.get("translation") or "").strip() else "MISS")
            print(f"  [{mark}] #{block['id']} {x:6.0f},{y:6.0f} {w:5.0f}x{h:4.0f} "
                  f"conf={block.get('confidence', 0):.2f} {block.get('text','')!r}"
                  f" -> {(block.get('translation') or '')!r}")
        if args.previews and preview:
            path = outdir / f"{phash}.jpg"
            path.write_bytes(preview)
            print(f"  preview -> {path}")
    conn.close()
    tmp.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
