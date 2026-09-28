"""Offline deterministic translator (TRANSLATION_BACKEND=fake).

Used by the self-test harness, by the test-suite and as a fallback demo mode
when no API key is configured: it produces stable, recognisable output so the
whole pipeline (including the overlay) can be verified without network access.
"""
from __future__ import annotations

import time
from typing import Dict, Optional, Sequence

from ..core.logging_setup import get_logger
from ..core.models import PipelineError
from ..core.i18n import tr
from .base import (
    CancelledError,
    CancelCheck,
    TranslateRequest,
    TranslateResult,
    TranslationBackend,
)

log = get_logger("fake_translator")

DEFAULT_LEXICON: Dict[str, str] = {
    "你好": "Привет",
    "谢谢": "Спасибо",
    "师兄": "старший брат по школе",
    "灵气": "духовная ци",
    "林雪": "Линь Сюэ",
    "什么": "что",
    "为什么": "почему",
    "等一下": "подожди",
    "不可能": "не может быть",
    "太好了": "отлично",
}


class FakeTranslator(TranslationBackend):
    name = "fake"

    def __init__(
        self,
        lexicon: Optional[Dict[str, str]] = None,
        delay_s: float = 0.0,
        fail_times: int = 0,
        drop_ids: Sequence[int] = (),
        model: str = "fake-model",
    ):
        self.lexicon = {**DEFAULT_LEXICON, **(lexicon or {})}
        self.delay_s = delay_s
        self.fail_times = fail_times
        self.drop_ids = set(drop_ids)
        self.model = model
        self.calls = 0
        self.last_request: Optional[TranslateRequest] = None

    def is_configured(self):
        return True, "offline stub"

    def translate_page(
        self, request: TranslateRequest, is_cancelled: Optional[CancelCheck] = None
    ) -> TranslateResult:
        started = time.perf_counter()
        self.calls += 1
        self.last_request = request
        if self.delay_s:
            deadline = time.monotonic() + self.delay_s
            while time.monotonic() < deadline:
                if is_cancelled and is_cancelled():
                    raise CancelledError("page generation changed")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise PipelineError(tr("Offline stub translation failure."), kind="network", retryable=True)

        out: Dict[int, str] = {}
        missing = []
        for item in request.items:
            if item.id in self.drop_ids:
                missing.append(item.id)
                continue
            out[item.id] = self._translate_one(item.text)
        return TranslateResult(
            translations=out,
            missing_ids=missing,
            model=self.model,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            raw_preview=f"{len(out)} of {len(request.items)} blocks",
        )

    def _translate_one(self, text: str) -> str:
        clean = text.strip()
        if not clean:
            return ""
        for src, dst in self.lexicon.items():
            if clean == src:
                return dst
        translated = clean
        for src, dst in sorted(self.lexicon.items(), key=lambda kv: -len(kv[0])):
            translated = translated.replace(src, dst)
        if translated == clean:
            return f"[RU] {clean}"
        return translated
