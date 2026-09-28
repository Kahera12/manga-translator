"""Panel-level OCR + translation for browser mode.

A *panel* is one page image of the reader, processed at its native resolution
(1280x1000 on the tested site) instead of a screenshot: no site UI, no scaling
blur, so OCR is markedly better, and because panels can be fetched ahead of time
the reader's scroll is usually already translated.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional

import numpy as np

from ..cache.page_cache import PageCache
from ..core.config import Config
from ..core.logging_setup import get_logger
from ..core.models import Block
from ..glossary.glossary import Glossary
from ..ocr import (
    filter_settings,
    filter_spans,
    group_settings,
    group_spans,
    postprocess_blocks,
)
from ..ocr.base import OcrEngine
from ..ocr.filtering import StaticTextTracker
from ..translation.base import (
    CancelCheck,
    TranslateItem,
    TranslateRequest,
    TranslationBackend,
)
from ..translation.context import ContextStore
from .reader import Panel

log = get_logger("browser.service")

BROWSER_REGION = "browser"        # cache region key for panel-based reads
BROWSER_REGION_KEY = "browser"    # + a settings fingerprint at runtime


@dataclass
class PanelResult:
    key: str
    image_hash: str
    blocks: List[Block] = field(default_factory=list)
    from_cache: bool = False
    ocr_ms: float = 0.0
    translate_ms: float = 0.0
    total_ms: float = 0.0
    panel: Optional[Panel] = None
    error: str = ""

    def translated(self) -> List[Block]:
        return [b for b in self.blocks if b.translation and not b.is_sfx]


@dataclass
class RecognizeOutcome:
    """What one page image produced, including the *detector's* own verdict.

    ``detected_spans`` is the number of text boxes the OCR detector found *before*
    any filtering. It is the difference between "this panel is pure artwork" (0) and
    "there is text here that our filters dropped" (>0) - which decides whether a
    panel is worth sending to a vision model, and saves a 3-40 s request on every
    artwork-only panel.
    """

    blocks: List[Block] = field(default_factory=list)
    ocr_ms: float = 0.0
    detected_spans: int = 0
    accepted_spans: int = 0
    dropped_chrome: int = 0
    dropped_watermark: int = 0
    dropped_short: int = 0
    low_confidence: int = 0
    non_cjk: int = 0

    @property
    def suspicious_drops(self) -> int:
        """Blocks dropped for reasons that *might* have been real dialogue.

        Site chrome and watermarks are confidently not dialogue; a two-character
        minimum, a low confidence score or a non-CJK guess can all be wrong, and
        those are exactly the panels a vision model should look at again.
        """
        return self.dropped_short + self.low_confidence + self.non_cjk


def recognize_image(
    config: Config,
    engine: OcrEngine,
    tracker: StaticTextTracker,
    image_bgr: np.ndarray,
    page_hash: str = "",
    last_hash: str = "",
    log_prefix: str = "panel",
) -> RecognizeOutcome:
    """OCR one page image -> grouped, filtered blocks (image coordinates)."""
    started = time.perf_counter()
    engine.warmup()
    spans = engine.recognize(image_bgr)
    ocr_ms = (time.perf_counter() - started) * 1000.0
    filtered = filter_spans(spans, filter_settings(config.ocr))
    blocks = group_spans(
        filtered.accepted,
        group_settings(config.ocr),
        page_area=float(image_bgr.shape[0] * image_bgr.shape[1]),
    )
    page_size = (image_bgr.shape[1], image_bgr.shape[0])
    settings = config.ocr
    if config.ocr.translate_sfx_in_browser and not config.ocr.translate_sfx:
        settings = replace(config.ocr, translate_sfx=True)
    counts: dict = {}
    blocks, _translatable = postprocess_blocks(blocks, page_size, settings, counts)
    if config.ocr.suppress_static_text and blocks:
        from ..detection.image_similarity import hamming_hex

        moved = hamming_hex(last_hash, page_hash) if last_hash else 99
        static_ids = set(tracker.observed(blocks, page_size, page_hash, moved))
        if static_ids:
            blocks = [b for b in blocks if b.id not in static_ids]
    log.debug("%s OCR: %d spans -> %d blocks in %.0f ms", log_prefix, len(spans),
              len(blocks), ocr_ms)
    return RecognizeOutcome(
        blocks=blocks, ocr_ms=ocr_ms, detected_spans=len(spans),
        accepted_spans=len(filtered.accepted),
        dropped_chrome=int(counts.get("chrome", 0)),
        dropped_watermark=int(counts.get("watermark", 0)),
        dropped_short=int(counts.get("short", 0)),
        low_confidence=len(filtered.low_confidence),
        non_cjk=len(filtered.non_cjk),
    )


def recognize_blocks(
    config: Config,
    engine: OcrEngine,
    tracker: StaticTextTracker,
    image_bgr: np.ndarray,
    page_hash: str = "",
    last_hash: str = "",
    log_prefix: str = "panel",
) -> tuple:
    """Backwards-compatible wrapper: ``(blocks, ocr_ms)``.

    Shared by the screen-side browser mode and by the extension bridge, so both
    produce identical blocks for identical artwork.
    """
    outcome = recognize_image(config, engine, tracker, image_bgr, page_hash, last_hash,
                              log_prefix)
    return outcome.blocks, outcome.ocr_ms


class PanelTranslator:
    """Turns a panel image into translated blocks (in image coordinates)."""

    def __init__(
        self,
        config: Config,
        engine: OcrEngine,
        backend: TranslationBackend,
        cache: PageCache,
        glossary: Glossary,
        context: Optional[ContextStore] = None,
    ):
        self.cfg = config
        self.engine = engine
        self.backend = backend
        self.cache = cache
        self.glossary = glossary
        self.context = context or ContextStore(max_pages=config.translation.context_pages)
        self.page_index = 0
        self._lock = threading.Lock()
        self._static_text = StaticTextTracker()
        self._last_hash = ""
        from ..cache.page_cache import settings_fingerprint

        self.region_key = f"{BROWSER_REGION_KEY}#{settings_fingerprint(config)}"

    # -- public -----------------------------------------------------------
    def process(
        self,
        panel: Panel,
        image_bgr: np.ndarray,
        is_cancelled: Optional[CancelCheck] = None,
    ) -> PanelResult:
        started = time.perf_counter()
        page_hash = PageCache.hash_frame(image_bgr)

        if self.cache.enabled:
            # Panels are pixel-identical when revisited, and a tolerant pHash match
            # can land on the *adjacent* panel (webtoon pages share large flat
            # areas) - which shows the wrong lines in the wrong place.
            cached = self.cache.find(page_hash, self.region_key, threshold=0)
            if cached is not None:
                self.cache.touch(page_hash)
                log.info("panel %s served from the page cache (%d blocks)",
                         panel.key[-24:], len(cached.blocks))
                return PanelResult(
                    key=panel.key, image_hash=page_hash, blocks=cached.blocks,
                    from_cache=True, panel=panel,
                    total_ms=(time.perf_counter() - started) * 1000.0,
                )

        blocks, ocr_ms = self._recognize(image_bgr, page_hash)
        if is_cancelled and is_cancelled():
            return PanelResult(key=panel.key, image_hash=page_hash, panel=panel,
                               error="cancelled")

        translatable = [b for b in blocks if not b.is_sfx and b.text.strip()]
        target = self.cfg.translation.target_language
        known: Dict[str, str] = {}
        if self.cache.enabled and translatable:
            known = self.cache.get_translations([b.text for b in translatable], target)
        pending: List[Block] = []
        for block in translatable:
            hit = known.get(self.cache.normalise_text(block.text))
            if hit:
                block.translation = hit
            else:
                pending.append(block)

        translate_ms = 0.0
        model = getattr(self.backend, "model", "")
        if pending:
            request = TranslateRequest(
                items=[TranslateItem(id=b.id, text=b.text, direction=b.direction) for b in pending],
                target_language=target,
                page_index=self.page_index,
                context_lines=self.context.lines(),
                glossary=self.glossary.relevant([b.text for b in pending]),
            )
            result = self.backend.translate_page(request, is_cancelled)
            translate_ms = result.elapsed_ms
            model = result.model or model
            for block in pending:
                text = (result.translations.get(block.id) or "").strip()
                block.translation = self.glossary.enforce(block.text, text) if text else ""
            if self.cache.enabled:
                self.cache.store_translations(
                    [(b.text, b.translation) for b in pending if b.translation], target, model
                )
            missing = [b.id for b in pending if not b.translation]
            if missing:
                log.warning("panel %s: no translation for ids %s", panel.key[-24:], missing)

        with self._lock:
            self.page_index += 1
        if translatable:
            self.context.add_page(translatable)

        if self.cache.enabled:
            self.cache.store(
                page_hash, self.region_key, image_bgr.shape[1], image_bgr.shape[0],
                blocks, model=model, preview_bgr=image_bgr,
            )
        self._last_hash = page_hash
        log.info(
            "panel %s: %d blocks (%d translatable, %d from memory) in %.0f ms "
            "(ocr %.0f ms, mt %.0f ms)",
            panel.key[-24:], len(blocks), len(translatable), len(known),
            (time.perf_counter() - started) * 1000.0, ocr_ms, translate_ms,
        )
        return PanelResult(
            key=panel.key, image_hash=page_hash, blocks=blocks, from_cache=False,
            ocr_ms=ocr_ms, translate_ms=translate_ms,
            total_ms=(time.perf_counter() - started) * 1000.0, panel=panel,
        )

    # -- internals --------------------------------------------------------
    def _recognize(self, image_bgr: np.ndarray, page_hash: str):
        return recognize_blocks(
            self.cfg, self.engine, self._static_text, image_bgr, page_hash,
            self._last_hash, log_prefix="panel",
        )


class PanelQueue:
    """Background worker that translates panels, nearest-to-the-reader first.

    Visible panels are pushed to the front, so what is on screen becomes ready
    first; the rest of the chapter follows while the user reads. Results are
    reported through a callback (called on the worker thread).
    """

    def __init__(
        self,
        translator: PanelTranslator,
        fetch: Callable[[Panel], Optional[np.ndarray]],
        on_result: Callable[[PanelResult], None],
        on_progress: Optional[Callable[[int, int], None]] = None,
    ):
        self.translator = translator
        self.fetch = fetch
        self.on_result = on_result
        self.on_progress = on_progress
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._queue: List[Panel] = []
        self._queued: set = set()
        self._done: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.processed = 0
        self.failures = 0

    # -- queue management -------------------------------------------------
    def push_visible(self, panels) -> None:
        with self._lock:
            for panel in reversed(list(panels)):
                self._enqueue(panel, front=True)

    def push_all(self, panels) -> None:
        with self._lock:
            for panel in panels:
                self._enqueue(panel, front=False)

    def _enqueue(self, panel: Panel, front: bool) -> None:
        if panel.key in self._done:
            return
        if panel.key in self._queued:
            if not front:
                return
            # already waiting: move it to the front so what is on screen wins
            self._queue = [p for p in self._queue if p.key != panel.key]
        else:
            self._queued.add(panel.key)
        if front:
            self._queue.insert(0, panel)
        else:
            self._queue.append(panel)

    def mark_done(self, panel: Panel) -> None:
        with self._lock:
            self._done.add(panel.key)
            self._queued.discard(panel.key)

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- worker -----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mt-panel-queue", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _next(self) -> Optional[Panel]:
        with self._lock:
            if not self._queue:
                return None
            return self._queue.pop(0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            panel = self._next()
            if panel is None:
                self._wake.wait(0.3)
                self._wake.clear()
                continue
            try:
                image = self.fetch(panel)
                if image is None:
                    self.failures += 1
                    self.mark_done(panel)
                    self.on_result(PanelResult(key=panel.key, image_hash="", panel=panel,
                                               error="download failed"))
                    continue
                result = self.translator.process(panel, image, self._stop.is_set)
                self.on_result(result)
            except Exception as exc:
                self.failures += 1
                log.warning("panel %s failed: %s", panel.key[-24:], exc)
                self.on_result(PanelResult(key=panel.key, image_hash="", panel=panel,
                                           error=f"{exc.__class__.__name__}: {exc}"))
            finally:
                self.processed += 1
                self.mark_done(panel)
                if self.on_progress is not None:
                    self.on_progress(self.processed, self.failures)
