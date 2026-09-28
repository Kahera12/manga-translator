"""Probe the configured provider: models, one real translation, raw errors."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from manga_translator.core.config import Config
from manga_translator.translation import create_backend
from manga_translator.translation.base import TranslateItem, TranslateRequest

cfg = Config.load()
provider = cfg.translation.active_provider()
model = cfg.translation.text_model
base = cfg.translation.base_url
key = cfg.api_key()
print(f"provider : {provider}")
print(f"model    : {model}")
print(f"base_url : {base}")
print(f"key      : {'set, ' + str(len(key)) + ' chars, …' + key[-4:] if key else 'MISSING'}")
print(f"target   : {cfg.translation.target_language}")
print(f"retries  : {cfg.translation.max_retries}  timeout: {cfg.translation.timeout_s}s")

print("\n--- raw /models ---")
url = base.rstrip("/") + "/models"
try:
    response = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=20)
    print("status:", response.status_code)
    try:
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else payload
        ids = [item.get("id") for item in (data or []) if isinstance(item, dict)]
        print(f"models ({len(ids)}):", ids[:20])
    except ValueError:
        print("body:", response.text[:400])
except Exception as exc:
    print("FAILED:", type(exc).__name__, exc)

print("\n--- one page translation ---")
backend, warning = create_backend(cfg.translation, key)
print("backend:", type(backend).__name__, "| warning:", warning)
items = [
    TranslateItem(id=1, text="你好，今天天气真好。"),
    TranslateItem(id=2, text="师兄，你来了！"),
    TranslateItem(id=3, text="灵气失控了"),
]
started = time.perf_counter()
try:
    result = backend.translate_page(
        TranslateRequest(items=items, target_language="Russian", page_index=0)
    )
    print(f"elapsed: {(time.perf_counter() - started) * 1000:.0f} ms")
    print("model:", result.model, "| attempts:", result.attempts)
    for item in items:
        print(f"  {item.id}: {item.text!r} -> {result.translations.get(item.id)!r}")
    print("missing:", result.missing_ids, "unexpected:", result.unexpected_ids)
except Exception as exc:
    print(f"FAILED after {(time.perf_counter() - started) * 1000:.0f} ms:",
          type(exc).__name__, getattr(exc, "kind", ""), exc)

print("\n--- raw chat/completions (no response_format) ---")
body = {
    "model": model,
    "messages": [
        {"role": "system", "content": "Translate Chinese manga dialogue into Russian. Answer with JSON only."},
        {"role": "user", "content": 'Translate: [{"id":1,"text":"你好，今天天气真好。"}] '
                                    'Answer: {"translations":[{"id":1,"translation":"..."}]}'},
    ],
    "temperature": 0.2,
}
try:
    response = httpx.post(base.rstrip("/") + "/chat/completions",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          json=body, timeout=60)
    print("status:", response.status_code)
    print("body:", json.dumps(response.json(), ensure_ascii=False)[:900])
except Exception as exc:
    print("FAILED:", type(exc).__name__, exc)
