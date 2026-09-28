"""Anthropic Claude backend (Messages API).

Claude has no JSON-schema response mode, so the page prompt is used together
with an assistant prefill ("{") which reliably forces a JSON object; the answer
is parsed with the same tolerant parser as the other LLM backends.
"""
from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

import httpx

from ..core.config import TranslationSettings
from ..core.logging_setup import get_logger
from ..core.models import PipelineError
from .base import (
    CancelCheck,
    TranslateRequest,
    TranslateResult,
    TranslationBackend,
)
from ..core.i18n import tr
from .parsing import json_only_note, parse_answer
from .prompts import build_system_prompt, build_user_prompt, repair_prompt
from .providers import ProviderSpec, resolve_base_url
from .transport import Transport

log = get_logger("anthropic")

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicBackend(TranslationBackend):
    def __init__(
        self,
        settings: TranslationSettings,
        spec: ProviderSpec,
        api_key: str,
        client: Optional[httpx.Client] = None,
    ):
        self.settings = settings
        self.spec = spec
        self.name = spec.id
        self.model = (settings.text_model or spec.default_model).strip()
        self._api_key = (api_key or "").strip()
        self.transport = Transport(
            settings,
            label=spec.label,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            client=client,
            key_hint="ANTHROPIC_API_KEY",
            model_hint=self.model,
        )

    def is_configured(self) -> Tuple[bool, str]:
        if not self._api_key:
            return False, tr(
                "{label} needs a key: set {env} in .env / the environment "
                "or paste it in Settings → Translation."
            ).format(label="Anthropic", env="ANTHROPIC_API_KEY")
        if not self.model:
            return False, tr("{label}: no model selected.").format(label="Anthropic")
        return True, "ok"

    def close(self) -> None:
        self.transport.close()

    @property
    def endpoint(self) -> str:
        base = resolve_base_url(self.spec.id, self.settings.base_url)
        return f"{base}/v1/messages"

    def translate_page(
        self, request: TranslateRequest, is_cancelled: Optional[CancelCheck] = None
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
        blocks = [{"id": it.id, "text": it.text, "direction": it.direction} for it in request.items]
        wanted = [b["id"] for b in blocks]
        system = build_system_prompt(request.target_language)
        prompt = build_user_prompt(
            blocks,
            target_language=request.target_language,
            page_index=request.page_index,
            context_lines=request.context_lines,
            glossary=request.glossary,
        )

        def ask(text: str) -> Dict[int, str]:
            answer = self._complete(system, text, is_cancelled)
            return parse_answer(answer, wanted)

        table = ask(prompt)
        if not table:
            table = ask(prompt + "\n\n" + json_only_note())
        missing = [i for i in wanted if i not in table]
        if missing:
            log.warning("claude omitted %d/%d ids; asking again", len(missing), len(wanted))
            table.update(ask(repair_prompt(blocks, request.target_language, missing)))
        if not table:
            raise PipelineError(
                tr("Claude returned no usable translation for this page."), kind="malformed",
                retryable=True,
            )
        return TranslateResult(
            translations=table,
            missing_ids=[i for i in wanted if i not in table],
            model=self.model,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    def _complete(self, system: str, prompt: str, is_cancelled: Optional[CancelCheck]) -> str:
        body = {
            "model": self.model,
            "max_tokens": 8192,
            "temperature": float(self.settings.temperature),
            "system": system,
            # The prefill turns the answer into a bare JSON object.
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "{"},
            ],
        }
        payload = self.transport.post_json(self.endpoint, is_cancelled=is_cancelled, json=body)
        parts = payload.get("content") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text.strip():
            stop = str(payload.get("stop_reason") or "").lower()
            if stop in ("refusal", "safety"):
                raise PipelineError(tr("Claude refused to translate this page."), kind="blocked")
            raise PipelineError(tr("Claude returned an empty answer."), kind="empty", retryable=True)
        return "{" + text  # restore the prefilled brace
