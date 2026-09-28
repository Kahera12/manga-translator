"""Measure provider latency for the configured gateway/model and variants."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from manga_translator.core.config import Config

cfg = Config.load()
base = cfg.translation.base_url.rstrip("/")
key = cfg.api_key()
headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

BLOCKS = [
    {"id": 1, "text": "你好，今天天气真好。"},
    {"id": 2, "text": "师兄，你来了！"},
    {"id": 3, "text": "灵气失控了"},
    {"id": 4, "text": "为什么？"},
    {"id": 5, "text": "林雪，等一下！"},
]
USER_PROMPT = (
    "Translate this Chinese comic page into Russian. Return JSON "
    '{"translations":[{"id":1,"translation":"..."}]} for every id.\n'
    + json.dumps(BLOCKS, ensure_ascii=False)
)
SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "manga_page_translation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "translations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}, "translation": {"type": "string"}},
                        "required": ["id", "translation"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["translations"],
            "additionalProperties": False,
        },
    },
}


def call(label: str, model: str, extra: dict, timeout: float = 180.0) -> None:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You translate Chinese manga dialogue into natural Russian."},
            {"role": "user", "content": USER_PROMPT},
        ],
        "temperature": 0.2,
        **extra,
    }
    started = time.perf_counter()
    try:
        response = httpx.post(f"{base}/chat/completions", headers=headers, json=body, timeout=timeout)
        elapsed = time.perf_counter() - started
        if response.status_code != 200:
            print(f"{label:<34} HTTP {response.status_code} in {elapsed:5.1f}s :: {response.text[:150]}")
            return
        payload = response.json()
        choice = (payload.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        print(f"{label:<34} {elapsed:5.1f}s  tokens={usage.get('completion_tokens')} "
              f"finish={choice.get('finish_reason')} chars={len(content)}")
        print(f"      -> {content[:180]!r}")
    except Exception as exc:
        elapsed = time.perf_counter() - started
        print(f"{label:<34} {elapsed:5.1f}s  FAILED {type(exc).__name__}: {exc}")


model = cfg.translation.text_model
print(f"gateway: {base}   model: {model}\n")
call("plain (no response_format)", model, {"max_tokens": 1024})
call("response_format=json_object", model, {"max_tokens": 1024, "response_format": {"type": "json_object"}})
call("response_format=json_schema", model, {"max_tokens": 1024, "response_format": SCHEMA})
call("max_tokens=8192", model, {"max_tokens": 8192})
call("no max_tokens", model, {})

print("\n--- other models on this gateway (latency of the same request) ---")
for candidate in ("glm-5.3-flash", "claude-haiku-4-5", "gpt-6-luna", "glm-5-turbo"):
    call(candidate, candidate, {"max_tokens": 1024})
