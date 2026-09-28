"""Analyse one captured page: OCR spans, filtering, grouping, ASCII layout.

    python tools/page_analyze.py runtime/artifacts/site_page.png [--translate]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from manga_translator.core.config import Config  # noqa: E402
from manga_translator.ocr import create_engine, filter_settings, filter_spans, group_settings, group_spans  # noqa: E402
from manga_translator.ocr.filtering import SfxSettings, detect_sfx, glyph_size, sfx_reason  # noqa: E402
from manga_translator.translation import create_backend  # noqa: E402
from manga_translator.translation.base import TranslateItem, TranslateRequest  # noqa: E402


def ascii_map(blocks, size, cols: int = 46, rows: int = 30) -> str:
    width, height = size
    grid = [["." for _ in range(cols)] for _ in range(rows)]
    for index, block in enumerate(blocks, start=1):
        x1 = int(block.bbox.x / width * cols)
        y1 = int(block.bbox.y / height * rows)
        x2 = max(x1 + 1, int(block.bbox.x2 / width * cols))
        y2 = max(y1 + 1, int(block.bbox.y2 / height * rows))
        for y in range(max(0, y1), min(rows, y2)):
            for x in range(max(0, x1), min(cols, x2)):
                grid[y][x] = "#" if block.is_sfx else str(index % 10)
    return "\n".join("".join(row) for row in grid)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--translate", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    path = Path(args.image)
    image = Image.open(path).convert("RGB")
    bgr = np.asarray(image)[:, :, ::-1].copy()
    width, height = image.size
    print(f"image: {path.name} {width}x{height}")

    cfg = Config.load()
    engine, warning = create_engine(cfg.ocr)
    started = time.perf_counter()
    spans = engine.recognize(bgr)
    ocr_ms = (time.perf_counter() - started) * 1000
    print(f"\nOCR: {len(spans)} spans in {ocr_ms:.0f} ms "
          f"(engine={engine.capabilities.name}, warning={warning})")
    for span in sorted(spans, key=lambda s: (s.bbox.y, s.bbox.x)):
        flag = "V" if span.is_vertical else "H"
        print(f"  [{flag}] {span.confidence:.2f} box=({span.bbox.x:5.0f},{span.bbox.y:5.0f},"
              f"{span.bbox.w:4.0f}x{span.bbox.h:4.0f}) tilt={span.angle:4.1f} {span.text!r}")

    filtered = filter_spans(spans, filter_settings(cfg.ocr))
    print(f"\nfilter: accepted={len(filtered.accepted)} low_conf={len(filtered.low_confidence)} "
          f"non_cjk={len(filtered.non_cjk)} empty={len(filtered.empty)}")
    for span in filtered.non_cjk:
        print(f"  dropped non-CJK: {span.text!r} ({span.confidence:.2f})")
    for span in filtered.low_confidence:
        print(f"  low confidence: {span.text!r} ({span.confidence:.2f})")

    blocks = group_spans(filtered.accepted, group_settings(cfg.ocr), page_area=float(width * height))
    sfx_settings = SfxSettings(translate_sfx=cfg.ocr.translate_sfx)
    for block in blocks:
        block.is_sfx = detect_sfx(block, float(width * height), sfx_settings)

    print(f"\nblocks: {len(blocks)}")
    for block in blocks:
        reason = sfx_reason(block, float(width * height), sfx_settings)
        print(f"  #{block.id} {block.direction[:1].upper()} conf={block.confidence:.2f} "
              f"glyph={glyph_size(block):.0f} box=({block.bbox.x:.0f},{block.bbox.y:.0f},"
              f"{block.bbox.w:.0f}x{block.bbox.h:.0f}) spans={len(block.spans)} "
              f"sfx={bool(reason)} {reason} :: {block.text!r}")

    print("\nlayout (digits = block id, # = SFX):")
    print(ascii_map(blocks, (width, height)))

    out = Path(args.out) if args.out else path.with_name(path.stem + "_annotated.png")
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)
    for block in blocks:
        colour = (245, 158, 11) if block.is_sfx else (34, 197, 94)
        draw.rectangle([block.bbox.x, block.bbox.y, block.bbox.x2, block.bbox.y2], outline=colour, width=3)
        draw.text((block.bbox.x + 4, block.bbox.y + 4), f"#{block.id}", fill=colour)
    for span in filtered.low_confidence:
        draw.rectangle([span.bbox.x, span.bbox.y, span.bbox.x2, span.bbox.y2], outline=(120, 120, 120), width=1)
    annotated.save(out)
    print(f"annotated image: {out}")

    translatable = [b for b in blocks if not b.is_sfx and b.text.strip()]
    payload = {"blocks": [
        {"id": b.id, "text": b.text, "bbox": [b.bbox.x, b.bbox.y, b.bbox.w, b.bbox.h],
         "direction": b.direction, "sfx": b.is_sfx} for b in blocks
    ]}
    json_path = path.with_name(path.stem + "_blocks.json")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"blocks json: {json_path}")

    if args.translate and translatable:
        backend, warn = create_backend(cfg.translation, cfg.api_key())
        print(f"\ntranslation via {cfg.translation.active_provider()}/{cfg.translation.text_model} "
              f"(warning={warn})")
        started = time.perf_counter()
        request = TranslateRequest(
            items=[TranslateItem(id=b.id, text=b.text, direction=b.direction) for b in translatable],
            target_language=cfg.translation.target_language,
            page_index=0,
        )
        try:
            result = backend.translate_page(request)
        except Exception as exc:
            print(f"  FAILED after {(time.perf_counter() - started) * 1000:.0f} ms: "
                  f"{type(exc).__name__} {getattr(exc, 'kind', '')}: {exc}")
            return 3
        print(f"  {len(result.translations)}/{len(translatable)} translated in "
              f"{result.elapsed_ms:.0f} ms")
        for item in translatable:
            print(f"  #{item.id} {item.text!r}\n      -> {result.translations.get(item.id)!r}")
        if result.missing_ids:
            print("  missing:", result.missing_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
