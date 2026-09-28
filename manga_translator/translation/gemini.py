"""Google Gemini backend (REST, structured JSON output, retries, cancel).

Implemented against the public REST endpoint with `httpx` instead of the
`google-genai` SDK: one less heavyweight dependency, no version conflicts with
other tools on the machine, and full control over the schema-constrained
request, timeouts and backoff. The key is sent in the `x-goog-api-key` header so
it can never leak into logs or URLs.
"""
from __future__ import annotations

import time
from typing import Dict, Optional, Sequence, Tuple

import httpx

from ..core.config import TranslationSettings
from ..core.logging_setup import get_logger
from ..core.models import PipelineError
from .base import CancelCheck, TranslateRequest, TranslateResult, TranslationBackend
from ..core.i18n import tr
from .parsing import json_only_note, parse_answer
from .prompts import (
    RESPONSE_SCHEMA,
    build_system_prompt,
    build_user_prompt,
    repair_prompt,
)
from .providers import resolve_base_url
from .transport import Transport

log = get_logger("gemini")

API_VERSION = "v1beta"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"


class GeminiBackend(TranslationBackend):
    name = "gemini"

    def __init__(
        self,
        settings: TranslationSettings,
        api_key: str,
        client: Optional[httpx.Client] = None,
    ):
        self.settings = settings
        self.model = (settings.text_model or "gemini-3.8-flash").strip()
        self._api_key = (api_key or "").strip()
        self.transport = Transport(
            settings,
            label="Google Gemini",
            headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
            client=client,
            key_hint="GEMINI_API_KEY / GOOGLE_API_KEY",
            model_hint=self.model,
        )

    # -- configuration ----------------------------------------------------
    def is_configured(self) -> Tuple[bool, str]:
        if not self._api_key:
            return False, tr(
                "Gemini API key is not set. Put GEMINI_API_KEY=... into .env "
                "(next to main.py), set the environment variable, or paste it in "
                "Settings → Translation."
            )
        if not self.model:
            return False, tr("TEXT_MODEL is empty - set it to a Gemini text model name.")
        return True, "ok"

    def close(self) -> None:
        self.transport.close()

    @property
    def endpoint(self) -> str:
        base = resolve_base_url("gemini", self.settings.base_url) or DEFAULT_BASE_URL
        return f"{base}/{API_VERSION}/models/{self.model}:generateContent"

    # -- api --------------------------------------------------------------
    def translate_page(
        self,
        request: TranslateRequest,
        is_cancelled: Optional[CancelCheck] = None,
    ) -> TranslateResult:
        ok, reason = self.is_configured()
        if not ok:
            raise PipelineError(reason, kind="auth")

        limit = max(1, int(self.settings.max_blocks_per_request))
        items = list(request.items)
        if not items:
            return TranslateResult(model=self.model)

        result = TranslateResult(model=self.model)
        for start in range(0, len(items), limit):
            self.transport.raise_if_cancelled(is_cancelled)
            chunk = TranslateRequest(
                items=items[start : start + limit],
                target_language=request.target_language,
                page_index=request.page_index,
                context_lines=request.context_lines,
                glossary=request.glossary,
            )
            part = self._translate_chunk(chunk, is_cancelled)
            result = part if not result.translations else result.merged(part)
        return result

    def _translate_chunk(
        self, request: TranslateRequest, is_cancelled: Optional[CancelCheck]
    ) -> TranslateResult:
        started = time.perf_counter()
        blocks = [
            {"id": it.id, "text": it.text, "direction": it.direction} for it in request.items
        ]
        wanted = [b["id"] for b in blocks]

        def ask(prompt: str) -> Dict[int, str]:
            return self._request_and_parse(prompt, wanted, is_cancelled)

        prompt = build_user_prompt(
            blocks,
            target_language=request.target_language,
            page_index=request.page_index,
            context_lines=request.context_lines,
            glossary=request.glossary,
        )
        result = ask(prompt)
        if not result:
            # One repair attempt with an explicit "JSON only" instruction.
            result = ask(prompt + "\n\n" + json_only_note())
            if not result:
                raise PipelineError(
                    tr("{label} returned an answer that is not valid JSON for this page. "
                       "Try again or switch the model/provider.").format(label="Gemini"),
                    kind="malformed",
                    retryable=True,
                )

        missing = [i for i in wanted if i not in result]
        if missing:
            log.warning("model omitted %d/%d block ids; requesting them again",
                        len(missing), len(wanted))
            result.update(ask(repair_prompt(blocks, request.target_language, missing)))

        return TranslateResult(
            translations=result,
            missing_ids=[i for i in wanted if i not in result],
            unexpected_ids=[i for i in result if i not in wanted],
            model=self.model,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    def _request_and_parse(
        self, prompt: str, wanted: Sequence[int], is_cancelled: Optional[CancelCheck]
    ) -> Dict[int, str]:
        body = {
            "systemInstruction": {
                "role": "system",
                "parts": [{"text": build_system_prompt(self.settings.target_language)}],
            },
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": float(self.settings.temperature),
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                "maxOutputTokens": 8192,
                "candidateCount": 1,
            },
        }
        payload = self.transport.post_json(self.endpoint, is_cancelled=is_cancelled, json=body)
        return parse_answer(_extract_text(payload), wanted)


def _extract_text(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise PipelineError(
            tr("Gemini blocked this page (safety filter). Nothing was translated."),
            kind="blocked",
        )
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    candidate = candidates[0] or {}
    finish = (candidate.get("finishReason") or "").upper()
    parts = ((candidate.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text:
        if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            raise PipelineError(
                tr("Gemini refused to translate this page (safety filter)."), kind="blocked"
            )
        if finish == "MAX_TOKENS":
            raise PipelineError(
                tr("The page had too much text for one answer. It will be split into smaller parts."),
                kind="toolong",
                retryable=True,
            )
        raise PipelineError(tr("{label} returned an empty answer.").format(label="Gemini"),
                            kind="empty", retryable=True)
    return text
