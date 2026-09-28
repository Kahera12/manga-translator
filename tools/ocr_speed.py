"""Is the OCR engine safe to call from several threads, and does it get faster?

    python tools/ocr_speed.py [--dir runtime/diag/panels] [--threads 1,2,4]

Runs the same panels through the same engine with 1, 2 and 4 concurrent callers and
compares wall time *and the recognised text*. If the text is identical and the time
drops, the bridge can OCR batches in parallel instead of serialising on one lock.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from manga_translator.browser.reader import decode_image  # noqa: E402
from manga_translator.core.config import Config  # noqa: E402
from manga_translator.ocr import create_engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="runtime/diag/panels")
    parser.add_argument("--threads", default="1,2,4")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    files = sorted(Path(args.dir).glob("*.jpg"))
    if len(files) < 4:
        print(f"need at least 4 panels in {args.dir}")
        return 1
    images = [decode_image(path.read_bytes()) for path in files]
    cfg = Config.load()
    engine, warning = create_engine(cfg.ocr)
    if warning:
        print(f"engine warning: {warning}")
    engine.warmup()

    # Warm up so the first (slow) inference does not skew a single-thread run.
    engine.recognize(images[0])

    baseline: dict = {}
    for workers in [int(w) for w in args.threads.split(",") if w.strip()]:
        lock = threading.Lock()
        results: list = [None] * len(images)

        def work(index: int) -> None:
            spans = engine.recognize(images[index])
            text = "|".join(sorted(s.text for s in spans))
            with lock:
                results[index] = text

        started = time.perf_counter()
        for _ in range(args.repeat):
            if workers == 1:
                for index in range(len(images)):
                    work(index)
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    list(pool.map(work, range(len(images))))
        elapsed = (time.perf_counter() - started) / args.repeat
        texts = list(results)
        if not baseline:
            baseline = {index: text for index, text in enumerate(texts)}
            same = "baseline"
        else:
            differing = [index for index, text in enumerate(texts)
                         if text != baseline.get(index)]
            same = "identical" if not differing else f"DIFFERENT on {differing}"
        print(f"threads={workers}: {elapsed * 1000:.0f} ms for {len(images)} panels "
              f"({elapsed / len(images) * 1000:.0f} ms per panel), text {same}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
