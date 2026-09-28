"""Compare prompt sizes/latency on real page data from the live site."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from manga_translator.core.config import Config
from manga_translator.translation.prompts import build_system_prompt, build_user_prompt

cfg = Config.load()
base = cfg.translation.base_url.rstrip("/")
key = cfg.api_key()
headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
MODEL = cfg.translation.text_model

# real blocks captured from the live page
BLOCKS = [
    {"id": 1, "text": "我啊"},
    {"id": 2, "text": "英语"},
    {"id": 3, "text": "中文 (简体)"},
    {"id": 4, "text": "为什么是我"},
]

CONCISE_SYSTEM = (
    "Ты переводишь китайские комиксы на русский. Отвечай только JSON, без пояснений."
)
CONCISE_USER = (
    'Переведи реплики на русский. Ответ: {"translations":[{"id":1,"translation":"..."}]}\n'
    + json.dumps(BLOCKS, ensure_ascii=False)
)


def run(label: str, system: str, user: str, stream: bool = False, timeout: float = 240.0) -> None:
    body = {
        "model": MODEL,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": stream,
    }
    started = time.perf_counter()
    try:
        if stream:
            first = None
            text = []
            with httpx.stream("POST", f"{base}/chat/completions", headers=headers, json=body,
                              timeout=timeout) as response:
                if response.status_code != 200:
                    print(f"{label:<28} HTTP {response.status_code}: {response.read()[:120]}")
                    return
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    if first is None:
                        first = time.perf_counter() - started
                    try:
                        delta = json.loads(payload)["choices"][0].get("delta", {})
                    except Exception:
                        continue
                    text.append(delta.get("content") or "")
            elapsed = time.perf_counter() - started
            print(f"{label:<28} {elapsed:5.1f}s  first_token={first:.1f}s  "
                  f"chars={len(''.join(text))}")
            print(f"      -> {''.join(text)[:160]!r}")
        else:
            response = httpx.post(f"{base}/chat/completions", headers=headers, json=body, timeout=timeout)
            elapsed = time.perf_counter() - started
            if response.status_code != 200:
                print(f"{label:<28} HTTP {response.status_code}: {response.text[:120]}")
                return
            choice = (response.json().get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            print(f"{label:<28} {elapsed:5.1f}s  chars={len(content)}")
            print(f"      -> {content[:160]!r}")
    except Exception as exc:
        print(f"{label:<28} {time.perf_counter() - started:5.1f}s FAILED {type(exc).__name__}: {exc}")


system_full = build_system_prompt("Russian")
user_full = build_user_prompt(BLOCKS, target_language="Russian", page_index=1)
print(f"full prompt   : system {len(system_full)} chars, user {len(user_full)} chars "
      f"(~{(len(system_full) + len(user_full)) // 3} tokens)")
print(f"concise prompt: system {len(CONCISE_SYSTEM)} chars, user {len(CONCISE_USER)} chars "
      f"(~{(len(CONCISE_SYSTEM) + len(CONCISE_USER)) // 3} tokens)\n")

for attempt in range(2):
    run(f"full #{attempt + 1}", system_full, user_full)
for attempt in range(2):
    run(f"concise #{attempt + 1}", CONCISE_SYSTEM, CONCISE_USER)
run("concise + stream", CONCISE_SYSTEM, CONCISE_USER, stream=True)
run("full + stream", system_full, user_full, stream=True)
