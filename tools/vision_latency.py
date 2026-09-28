"""Measure the gateway on real panel images: streaming vs not, reasoning effort.

    python tools/vision_latency.py [--dir runtime/diag/panels] [--counts 1,3,5]

Sends the *same* saved panels in requests of 1, 3 and 5 images and reports elapsed
time and token usage per mode, so the batching policy can be chosen from numbers
instead of guesses. Writes nothing except stdout.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from manga_translator.core.config import Config  # noqa: E402
from manga_translator.translation.providers import resolve_base_url  # noqa: E402

SYSTEM = """You are a professional manga translator working from Chinese into Russian.
For every page, read every piece of text a reader is meant to understand and
translate it. Ignore watermarks and site logos. Answer with JSON only:
{"pages":[{"page":0,"blocks":[{"text":"...","translation":"...","box":[x,y,w,h]}]}]}
`box` is the printed text in the page's pixel grid (1280x1000)."""


def data_url(path: Path, max_side: int = 1280, quality: int = 85) -> str:
    import cv2
    import numpy as np

    image = cv2.imread(str(path))
    h, w = image.shape[:2]
    if max(h, w) > max_side:
        ratio = max_side / float(max(h, w))
        image = cv2.resize(image, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode("ascii")


def body(model: str, images: list, stream: bool, effort: str) -> dict:
    content: list = [{"type": "text", "text": f"{len(images)} pages, translate all."}]
    for index, url in enumerate(images):
        content.append({"type": "text", "text": f"PAGE {index}"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    payload = {
        "model": model,
        "temperature": 0.1,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": content}],
    }
    if stream:
        payload["stream"] = True
    if effort:
        payload["reasoning_effort"] = effort
    return payload


def run(url: str, headers: dict, payload: dict, timeout: float) -> tuple:
    """Returns (elapsed_s, text, usage, note)."""
    started = time.perf_counter()
    chunks: list = []
    usage: dict = {}
    normal = payload.get("stream") is not True
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, read=timeout)) as client:
            if normal:
                response = client.post(url, headers=headers, json=payload)
                elapsed = time.perf_counter() - started
                if response.status_code != 200:
                    return elapsed, "", {}, f"HTTP {response.status_code} {response.text[:120]}"
                data = response.json()
                usage = data.get("usage") or {}
                choices = data.get("choices") or [{}]
                text = choices[0].get("message", {}).get("content") or ""
                return elapsed, text, usage, ""
            with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    response.read()
                    return time.perf_counter() - started, "", {}, \
                        f"HTTP {response.status_code} {response.text[:120]}"
                content_type = (response.headers.get("content-type") or "").lower()
                if "event-stream" not in content_type:
                    response.read()
                    return time.perf_counter() - started, "", {}, "not an event stream"
                first = None
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    if event.get("usage"):
                        usage = event["usage"]
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        if first is None:
                            first = time.perf_counter() - started
                        chunks.append(piece)
    except Exception as exc:
        return time.perf_counter() - started, "", {}, f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started
    note = f"first token {first:.1f}s" if first else "no content"
    return elapsed, "".join(chunks), usage, note


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="runtime/diag/panels")
    parser.add_argument("--counts", default="1,3,5")
    parser.add_argument("--models", default="", help="comma list: try each model in turn")
    parser.add_argument("--panels", type=int, default=0, help="fixed panel count (with --models)")
    parser.add_argument("--stream", default="", choices=("", "yes", "no"))
    parser.add_argument("--effort", default="")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()

    files = sorted(Path(args.dir).glob("*.jpg"))
    if not files:
        print(f"no panels in {args.dir}; run tools/vision_live.py --save first")
        return 1
    cfg = Config.load()
    base = resolve_base_url(cfg.translation.active_provider(), cfg.translation.base_url)
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.api_key()}", "Content-Type": "application/json"}
    encoded = [data_url(path) for path in files]
    print(f"endpoint {url} ({len(encoded)} panels on disk, "
          f"{len(encoded[0]) / 1024:.0f} KB each)")

    if args.models:
        count = args.panels or len(encoded)
        images = encoded[:count]
        streams = [True] if args.stream != "no" else [False]
        if args.stream == "yes":
            streams = [True]
        for model in [m.strip() for m in args.models.split(",") if m.strip()]:
            for stream in streams:
                payload = body(model, images, stream, args.effort)
                elapsed, text, usage, note = run(url, headers, payload, args.timeout)
                blocks = _count_blocks(text)
                print(f"\n=== {model} ({count} panels, stream={stream}, "
                      f"effort={args.effort or 'default'}): {elapsed:.1f}s "
                      f"({elapsed / max(1, len(images)):.1f}s per panel) {note}")
                print(f"    usage={usage} blocks={blocks} chars={len(text)}")
                if not text:
                    continue
        return 0

    for count in [int(c) for c in args.counts.split(",") if c.strip()]:
        images = encoded[:count]
        if len(images) < count:
            print(f"\n--- {count} panels: only {len(images)} available")
            continue
        for stream in (True, False):
            payload = body(cfg.translation.text_model, images, stream, args.effort)
            elapsed, text, usage, note = run(url, headers, payload, args.timeout)
            blocks = _count_blocks(text)
            print(f"\n--- {count} panel(s), stream={stream}, effort={args.effort or 'default'}: "
                  f"{elapsed:.1f}s ({elapsed / count:.1f}s per panel) {note}")
            print(f"    usage={usage} blocks={blocks} chars={len(text)}")
    return 0


def _count_blocks(text: str) -> int:
    if not text:
        return 0
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        return 0
    pages = parsed.get("pages") if isinstance(parsed, dict) else parsed
    if not isinstance(pages, list):
        return 0
    return sum(len(p.get("blocks") or []) for p in pages if isinstance(p, dict))


if __name__ == "__main__":
    sys.exit(main())
