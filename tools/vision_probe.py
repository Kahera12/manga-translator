"""Does the configured endpoint actually read images? And how good are its boxes?

    python tools/vision_probe.py [--real runtime/diag/<panel>.jpg]

Builds a synthetic "manga page" with known text positions, sends it as a
``image_url`` data-URL content part and compares the boxes the model returns with
the ground truth. This is the whole feasibility question for a vision-based path:
without usable coordinates the overlay cannot be placed, however good the reading
is.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from manga_translator.core.config import Config  # noqa: E402
from manga_translator.translation.providers import resolve_base_url  # noqa: E402

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\arial.ttf",
]

# (text, x, y, font_size) drawn at a known place -> ground truth for the boxes
LINES = [
    ("你好世界", 80, 60, 52),
    ("我不想死", 470, 250, 44),
    ("这么弱只会拖累队友", 120, 420, 40),
]


def load_font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def synthetic() -> tuple[Image.Image, list[dict]]:
    img = Image.new("RGB", (900, 600), "white")
    draw = ImageDraw.Draw(img)
    truth = []
    for text, x, y, size in LINES:
        font = load_font(size)
        draw.ellipse([x - 40, y - 30, x + 40 + size * len(text), y + size + 30],
                     fill="white", outline="black", width=3)
        draw.text((x, y), text, fill="black", font=font)
        truth.append({"text": text, "x": x, "y": y, "size": size})
    return img, truth


PROMPT = """You are a manga translator. Find every speech bubble with text in the
image and answer with JSON only:

{"blocks": [{"text": "<the original text>", "translation": "<Russian>",
             "box": [x, y, w, h]}]}

`box` is the text area in a 0-1000 normalised coordinate system (x, y = top-left
corner, w, h = size). No prose, no markdown."""


MULTI_PROMPT = """You are a manga translator. Each image is one page of the same
chapter, in order. For every page, find every speech bubble with text and answer
with JSON only:

{"pages": [{"page": 0, "blocks": [{"text": "<original>",
                                  "translation": "<Russian>",
                                  "box": [x, y, w, h]}]}]}

`box` is the text area in a 0-1000 normalised coordinate system of *that* page.
Only include pages that contain text. No prose, no markdown."""


def multi_pages(count: int) -> tuple[list[Image.Image], list[list[dict]]]:
    """`count` distinct synthetic pages, each with its own known text."""
    texts = ["你好世界", "我不想死", "他强得不像人类", "这么弱只会拖累队友",
             "哈哈哈哈哈笑死人了", "他不是绰号最弱兵器吗"]
    images, truth = [], []
    for index in range(count):
        text = texts[index % len(texts)]
        img = Image.new("RGB", (760, 520), "white")
        draw = ImageDraw.Draw(img)
        font = load_font(44)
        x, y = 120, 220
        draw.ellipse([x - 60, y - 50, x + 60 + 46 * len(text), y + 44 + 50],
                     fill="white", outline="black", width=3)
        draw.text((x, y), text, fill="black", font=font)
        draw.text((30, 30), f"page {index}", fill="#bbbbbb", font=load_font(28))
        images.append(img)
        truth.append([{"text": text, "x": x, "y": y}])
    return images, truth


def encode(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def run_multi(cfg, url: str, model: str, count: int) -> int:
    images, truth = multi_pages(count)
    content: list[dict] = [{"type": "text", "text": f"{count} pages, translate all of them."}]
    for img in images:
        content.append({"type": "image_url", "image_url": {"url": encode(img)}})
    body = {
        "model": model,
        "temperature": 0.1,
        "messages": [{"role": "system", "content": MULTI_PROMPT},
                     {"role": "user", "content": content}],
    }
    headers = {"Authorization": f"Bearer {cfg.api_key()}", "Content-Type": "application/json"}
    print(f"\n=== one request with {count} page images ===")
    started = time.perf_counter()
    with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0)) as client:
        response = client.post(url, headers=headers, json=body)
    elapsed = time.perf_counter() - started
    print(f"HTTP {response.status_code} in {elapsed:.1f}s ({elapsed / count:.1f}s per page)")
    if response.status_code != 200:
        print("  body:", response.text[:600])
        return 3
    payload = response.json()
    print("  usage:", payload.get("usage") or {})
    content_text = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if isinstance(content_text, list):
        content_text = "".join(p.get("text", "") for p in content_text if isinstance(p, dict))
    text = content_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
    except ValueError:
        print("  not JSON:", text[:500])
        return 4
    pages = parsed.get("pages") if isinstance(parsed, dict) else parsed
    if not isinstance(pages, list):
        print("  unexpected shape:", str(parsed)[:300])
        return 5
    seen = 0
    for page in pages:
        index = int(page.get("page", -1))
        blocks = page.get("blocks") or []
        want = truth[index][0] if 0 <= index < len(truth) else None
        found = any(want and want["text"][:2] in (b.get("text") or "") for b in blocks) if want else False
        seen += 1
        print(f"  page {index}: {len(blocks)} block(s) "
              f"{[(b.get('text'), b.get('translation')) for b in blocks]}")
        if want:
            print(f"    expected {want['text']!r}: {'read' if found else 'MISSED'}")
    print(f"  pages returned: {seen}/{count}")
    return 0 if seen == count else 6


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", default="", help="use this image file instead")
    parser.add_argument("--model", default="")
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--multi", type=int, default=0,
                        help="send N page images in ONE request (batch test)")
    args = parser.parse_args()

    cfg = Config.load()
    model = args.model or cfg.translation.text_model
    base = resolve_base_url(cfg.translation.active_provider(), cfg.translation.base_url)
    url = f"{base}/chat/completions"
    print(f"endpoint: {url}\nmodel: {model}")

    if args.real:
        img = Image.open(args.real).convert("RGB")
        truth: list[dict] = []
        print(f"image: {args.real} {img.size[0]}x{img.size[1]}")
    else:
        img, truth = synthetic()
        print(f"image: synthetic {img.size[0]}x{img.size[1]}")
        for item in truth:
            print(f"  truth {item['text']!r} at ({item['x']},{item['y']}) size {item['size']}")

    if args.multi:
        return run_multi(cfg, url, model, args.multi)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    print(f"payload: {len(data_url) / 1024:.0f} KB of image data")

    body = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "Translate this page."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
    }
    headers = {"Authorization": f"Bearer {cfg.api_key()}", "Content-Type": "application/json"}
    for attempt in range(1, max(1, args.attempts) + 1):
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=httpx.Timeout(20.0, read=120.0)) as client:
                response = client.post(url, headers=headers, json=body)
        except Exception as exc:
            print(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            continue
        elapsed = time.perf_counter() - started
        print(f"attempt {attempt}: HTTP {response.status_code} in {elapsed:.1f}s")
        if response.status_code != 200:
            print("  body:", response.text[:600])
            continue
        payload = response.json()
        usage = payload.get("usage") or {}
        print(f"  usage: {usage}")
        content = (payload.get("choices") or [{}])[0].get("message", {}).get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        print("  raw:", (content or "")[:800])
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            print(f"  not JSON: {exc}")
            continue
        blocks = parsed.get("blocks") if isinstance(parsed, dict) else parsed
        if not isinstance(blocks, list):
            print("  unexpected shape:", str(parsed)[:300])
            continue
        width, height = img.size
        print(f"  {len(blocks)} block(s):")
        for block in blocks:
            box = block.get("box") or block.get("bbox") or []
            pixel = [round(float(box[0]) / 1000 * width), round(float(box[1]) / 1000 * height),
                     round(float(box[2]) / 1000 * width), round(float(box[3]) / 1000 * height)] \
                if len(box) == 4 else []
            print(f"    box={box} -> {pixel}px {block.get('text')!r} -> {block.get('translation')!r}")
        if truth:
            for item in truth:
                best = None
                for block in blocks:
                    if item["text"][:3] in (block.get("text") or ""):
                        best = block
                        break
                if not best:
                    print(f"  MISS: {item['text']!r} was not read back")
                    continue
                box = best.get("box") or []
                if len(box) == 4:
                    px = [float(box[0]) / 1000 * width, float(box[1]) / 1000 * height]
                    print(f"  {item['text']!r}: predicted top-left {px[0]:.0f},{px[1]:.0f} "
                          f"vs truth {item['x']},{item['y']} "
                          f"(error {abs(px[0] - item['x']):.0f},{abs(px[1] - item['y']):.0f} px)")
        return 0
    return 3


if __name__ == "__main__":
    sys.exit(main())
