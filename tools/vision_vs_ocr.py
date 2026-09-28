"""Compare the local OCR path with a vision read on the *same* saved panels.

    python tools/vision_vs_ocr.py [--dir runtime/diag/panels]

Answers the question "do we still need OCR?" with numbers: how many blocks each
path finds per page, whether the text matches, and how long each takes. Writes
annotated pages (green = OCR, blue = vision) into runtime/artifacts/compare_*.png.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402

from manga_translator.browser.reader import decode_image  # noqa: E402
from manga_translator.browser.service import recognize_blocks  # noqa: E402
from manga_translator.cache.page_cache import PageCache  # noqa: E402
from manga_translator.core.config import Config  # noqa: E402
from manga_translator.ocr import create_engine  # noqa: E402
from manga_translator.ocr.filtering import StaticTextTracker  # noqa: E402
from manga_translator.translation import create_backend  # noqa: E402
from manga_translator.translation.providers import get_spec  # noqa: E402
from manga_translator.vision import VisionReader  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="runtime/diag/panels")
    parser.add_argument("--reuse", default="", help="vision JSON from a previous run")
    parser.add_argument("--refresh-vision", action="store_true")
    parser.add_argument("--out", default="runtime/artifacts")
    args = parser.parse_args()

    files = sorted(Path(args.dir).glob("*.jpg"))
    if not files:
        print(f"no panels in {args.dir}")
        return 1
    cfg = Config.load()
    images = [(path.stem, decode_image(path.read_bytes())) for path in files]
    print(f"{len(images)} panel(s) from {args.dir}")

    # -- local OCR --------------------------------------------------------
    engine, warning = create_engine(cfg.ocr)
    if warning:
        print(f"OCR warning: {warning}")
    tracker = StaticTextTracker()
    ocr_blocks: dict = {}
    started = time.perf_counter()
    last_hash = ""
    for key, image in images:
        page_hash = PageCache.hash_frame(image)
        blocks, _ms = recognize_blocks(cfg, engine, tracker, image, page_hash, last_hash,
                                       log_prefix="compare")
        last_hash = page_hash
        ocr_blocks[key] = [(b.bbox.x, b.bbox.y, b.bbox.w, b.bbox.h, b.text) for b in blocks]
    ocr_ms = (time.perf_counter() - started) * 1000.0

    # -- vision -----------------------------------------------------------
    cache_path = Path(args.out) / "compare_vision.json"
    vision_blocks: dict = {}
    vision_ms = 0.0
    if cache_path.exists() and not args.refresh_vision:
        vision_blocks = json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"vision read reused from {cache_path}")
    else:
        backend, _warn = create_backend(cfg.translation, cfg.api_key())
        spec = get_spec(cfg.translation.active_provider())
        reader = VisionReader(cfg.translation, spec, cfg.api_key(), backend=backend)
        if not reader.available:
            print("vision is not available; run with a multimodal model to compare")
        else:
            started = time.perf_counter()
            result = reader.read_pages([(key, image) for key, image in images],
                                       cfg.translation.target_language)
            vision_ms = (time.perf_counter() - started) * 1000.0
            for key, page in result.items():
                vision_blocks[key] = [(b.bbox.x, b.bbox.y, b.bbox.w, b.bbox.h, b.text,
                                       b.translation) for b in page.blocks]
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(vision_blocks, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
            print(f"vision read cached to {cache_path}")

    outdir = ROOT / args.out
    outdir.mkdir(parents=True, exist_ok=True)
    total_ocr = total_vision = 0
    for index, (key, image) in enumerate(images):
        ocr = ocr_blocks.get(key, [])
        vision = vision_blocks.get(key, [])
        total_ocr += len(ocr)
        total_vision += len(vision)
        print(f"\n--- {key}: OCR {len(ocr)} block(s) [{ocr_ms / len(images):.0f} ms/page], "
              f"vision {len(vision)} block(s) "
              f"[{vision_ms / max(1, len(images)):.0f} ms/page]")
        for box in ocr:
            print(f"    OCR    ({box[0]:6.0f},{box[1]:6.0f} {box[2]:5.0f}x{box[3]:4.0f}) {box[4]!r}")
        for box in vision:
            print(f"    VISION ({box[0]:6.0f},{box[1]:6.0f} {box[2]:5.0f}x{box[3]:4.0f}) {box[4]!r}"
                  f" -> {box[5]!r}")
        annotated = image.copy()
        for box in ocr:
            cv2.rectangle(annotated, (int(box[0]), int(box[1])),
                          (int(box[0] + box[2]), int(box[1] + box[3])), (0, 200, 80), 3)
        for box in vision:
            cv2.rectangle(annotated, (int(box[0]), int(box[1])),
                          (int(box[0] + box[2]), int(box[1] + box[3])), (255, 120, 0), 2)
        path = outdir / f"compare_{index:02d}.png"
        cv2.imwrite(str(path), annotated)

    print(f"\ntotal: OCR {total_ocr} block(s) in {ocr_ms:.0f} ms "
          f"({ocr_ms / len(images):.0f} ms per panel); "
          f"vision {total_vision} block(s) in {vision_ms:.0f} ms "
          f"({vision_ms / max(1, len(images)):.0f} ms per panel)")
    print(f"annotated pages (green = OCR, blue = vision): {outdir}\\compare_*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
