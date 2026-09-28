"""The translation service behind the WebSocket bridge.

One *session* per connected browser tab. It answers the only question the
extension asks - "here are my panel images, give me the translated blocks" - the
way a reader would want:

1. What is on screen goes first (``visible`` panels are pushed to the front of the
   queue), the rest of the chapter follows in reading order;
2. Panels are read in **batches**: one vision request carries up to
   ``vision_panels_per_request`` page images and returns text, translation and box
   for every one of them - one request per several panels instead of one request
   per speech bubble. Without a vision-capable model the same batch is read by the
   local OCR engine and translated with one text request per
   ``max_blocks_per_request`` lines;
3. Every result is cached twice - the finished page by image hash, each line in the
   translation memory - so scrolling back costs nothing;
4. A failed panel is retried instead of silently disappearing. That was the real
   reason bubbles "went missing": a single timed-out request used to kill a panel
   for good, because the queue marked it done whether or not it had been translated.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..cache.page_cache import PageCache, settings_fingerprint
from ..core.config import Config
from ..core.logging_setup import get_logger
from ..core.models import Block
from ..glossary.glossary import Glossary
from ..ocr.base import OcrEngine
from ..ocr.filtering import StaticTextTracker
from ..translation.base import (
    CancelledError,
    TranslateItem,
    TranslateRequest,
    TranslationBackend,
)
from ..translation.context import ContextStore
from ..translation.providers import get_spec
from ..vision import VisionReader, VisionUnavailable
from .protocol import BlockPayload, PanelPayload, PanelSpec, PROTOCOL_VERSION, encode

log = get_logger("bridge.service")

MAX_PANEL_ATTEMPTS = 3
IMAGE_CACHE = 64          # decoded panel images kept per session
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/154 Safari/537.36")


@dataclass
class SessionStatus:
    connected: bool = False
    page_url: str = ""
    page_title: str = ""
    panels: int = 0
    processed: int = 0
    failed: int = 0
    queued: int = 0
    blocks: int = 0
    memory_hits: int = 0
    phase: str = "idle"          # idle | reading | translating | done
    model: str = ""
    provider: str = ""
    last_error: str = ""
    vision: bool = False
    vision_only: bool = False
    mode: str = "cards"
    per_panel_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "connected": self.connected, "page_url": self.page_url,
            "page_title": self.page_title, "panels": self.panels,
            "processed": self.processed, "failed": self.failed, "queued": self.queued,
            "blocks": self.blocks, "memory_hits": self.memory_hits,
            "phase": self.phase, "model": self.model, "provider": self.provider,
            "vision": self.vision, "vision_only": self.vision_only, "mode": self.mode,
            "per_panel_ms": round(self.per_panel_ms, 1), "last_error": self.last_error,
        }

    def line(self) -> str:
        state = "vision" if self.vision_only else ("OCR+vision" if self.vision else "OCR")
        return (f"{self.processed}/{self.panels} panels, {self.blocks} blocks, "
                f"{self.failed} failed [{state}, {self.phase}]")


class BridgeSession:
    """Panels in, translations out - one worker thread per connected tab."""

    def __init__(
        self,
        config: Config,
        cache: PageCache,
        glossary: Glossary,
        backend: TranslationBackend,
        send: Callable[[str], None],
        engine_getter: Optional[Callable[[], OcrEngine]] = None,
        fetch_image: Optional[Callable[[PanelSpec], Optional[np.ndarray]]] = None,
    ):
        self.cfg = config
        self.cache = cache
        self.glossary = glossary
        self.backend = backend
        self._send_raw = send
        self._engine_getter = engine_getter
        self._fetch_image = fetch_image
        self._engine: Optional[OcrEngine] = None
        self._static_text = StaticTextTracker()
        self._last_hash = ""
        self.context = ContextStore(max_pages=config.translation.context_pages)
        self.status = SessionStatus(
            provider=config.translation.active_provider(),
            model=getattr(backend, "model", "") or config.translation.text_model,
        )

        self.region_key = f"browser#{settings_fingerprint(config)}"
        spec = get_spec(config.translation.active_provider())
        self._vision: Optional[VisionReader] = None
        self._vision_only = config.translation.vision_only()
        if spec is not None and config.translation.vision_enabled():
            self._vision = VisionReader(config.translation, spec, config.api_key(), backend=backend)
            self.status.vision = self._vision.available
            self.status.vision_only = self._vision_only

        self.mode = "cards"
        self._panels: Dict[str, PanelSpec] = {}
        self._order: List[str] = []
        self._done: Dict[str, float] = {}
        self._attempts: Dict[str, int] = {}
        self._retry_at: Dict[str, float] = {}
        self._images: Dict[str, np.ndarray] = {}
        # Reentrant: `_mark` reports the queue depth while already holding the lock
        # (a plain Lock deadlocked the worker on the first finished panel).
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._batch_panels = max(1, int(getattr(config.translation, "vision_panels_per_request", 8)))

        # Reading is mostly *waiting* for the model, so several batches are read at
        # once; OCR itself stays serialised (one engine, ~0.3 s per panel).
        bridge_cfg = getattr(config, "bridge", None)
        self._workers = max(1, int(getattr(bridge_cfg, "parallel_batches", 1) or 1))
        self._threads: List[threading.Thread] = []
        self._inflight: set = set()
        self._ocr_lock = threading.Lock()
        # Panels that need the vision model travel to their own worker, so a 3-40 s
        # request never blocks the fast OCR + text queue.
        self._gaps: Deque[Tuple[PanelSpec, np.ndarray]] = deque()
        self._gap_wake = threading.Event()
        self._gap_thread: Optional[threading.Thread] = None
        self._gap_cap = max(0, int(getattr(config.translation, "vision_max_gap_panels", 60)))
        self._gap_queued = 0
        self._gap_pass = bool(getattr(bridge_cfg, "gap_pass", True))

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._loop, name=f"mt-bridge-{index}", daemon=True)
            for index in range(self._workers)
        ]
        for thread in self._threads:
            thread.start()
        if self._gap_pass and self._vision is not None:
            self._gap_thread = threading.Thread(target=self._gap_loop, name="mt-bridge-gaps",
                                                daemon=True)
            self._gap_thread.start()
        log.info("session started: %d reader(s), vision gap pass %s",
                 self._workers, "on" if self._gap_thread else "off")

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._gap_wake.set()
        threads = list(self._threads)
        gap, self._gap_thread = self._gap_thread, None
        self._threads = []
        if gap is not None:
            threads.append(gap)
        for thread in threads:
            thread.join(timeout=5.0)

    @property
    def running(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    # -- protocol ---------------------------------------------------------
    def hello(self, url: str = "", title: str = "", tab=None) -> None:
        self.status.connected = True
        self.status.page_url = url or self.status.page_url
        self.status.page_title = title or self.status.page_title
        self.send("welcome", version=PROTOCOL_VERSION,
                  provider=self.status.provider, model=self.status.model,
                  target_language=self.cfg.translation.target_language,
                  batch_size=self._batch_panels, vision=self.status.vision)

    def register(self, specs: Sequence[PanelSpec], page: str = "") -> None:
        """Merge the page's panel list: new panels queue, known ones are kept."""
        if page:
            self.status.page_url = page
        added = 0
        with self._lock:
            for spec in specs:
                if not spec.key:
                    continue
                known = self._panels.get(spec.key)
                if known is None:
                    self._panels[spec.key] = spec
                    self._order.append(spec.key)
                    added += 1
                else:
                    known.visible = spec.visible or known.visible
                    known.order = spec.order if spec.order else known.order
                    known.url = spec.url or known.url
                    known.width = spec.width or known.width
                    known.height = spec.height or known.height
            self.status.panels = len(self._panels)
        if added:
            log.info("session: %d new panel(s) registered (%d total, %d queued)",
                     added, len(self._panels), self.pending)
        self._wake.set()

    def request(self, keys: Sequence[str], mode: str = "", priority: bool = True) -> None:
        if mode in ("cards", "inplace"):
            self.mode = mode
            self.status.mode = mode
        with self._lock:
            for key in keys:
                spec = self._panels.get(key)
                if spec is None:
                    continue
                spec.visible = priority or spec.visible
                # A re-request re-emits the panel (instantly, from the page cache):
                # the extension may have just scrolled to it after a reconnect.
                self._done.pop(key, None)
                self._retry_at.pop(key, None)
                self._attempts.pop(key, None)
        self._wake.set()

    def cancel(self, keys: Sequence[str]) -> None:
        with self._lock:
            for key in keys:
                self._panels.pop(key, None)
                self._done.pop(key, None)
            if keys:
                self._order = [k for k in self._order if k in self._panels]
            self.status.panels = len(self._panels)
            self.status.processed = len(self._done)
        self._wake.set()

    def send_status(self) -> None:
        payload = self.status.as_dict()
        payload.update({"queue": self.pending, "memory": self.cache.translation_memory_stats()})
        self.send("status", **payload)

    def send(self, message_type: str, **payload) -> None:
        try:
            self._send_raw(encode(message_type, **payload))
        except Exception as exc:  # the socket may be gone; never kill the worker
            log.debug("bridge send failed: %s", exc)

    # -- queue ------------------------------------------------------------
    @property
    def pending(self) -> int:
        with self._lock:
            now = time.time()
            return sum(1 for key in self._order
                       if key not in self._done and key not in self._inflight
                       and self._retry_at.get(key, 0.0) <= now)

    def _take_batch(self) -> List[PanelSpec]:
        """Visible panels first, then reading order; at most one batch.

        Taken panels are marked *in flight* so two readers never process the same
        panel twice (which would duplicate API calls and cache writes).
        """
        with self._lock:
            now = time.time()
            ready = [self._panels[key] for key in self._order
                     if key in self._panels and key not in self._done
                     and key not in self._inflight
                     and self._retry_at.get(key, 0.0) <= now]
            if not ready:
                return []
            visible = sorted((p for p in ready if p.visible), key=lambda p: (p.order, p.doc_y))
            rest = sorted((p for p in ready if not p.visible), key=lambda p: (p.order, p.doc_y))
            batch = (visible + rest)[:self._batch_panels]
            for spec in batch:
                self._inflight.add(spec.key)
            return batch

    def _mark(self, key: str, ok: bool, elapsed_ms: float = 0.0) -> None:
        now = time.time()
        with self._lock:
            self._inflight.discard(key)
            if key in self._done:
                return          # already finished or already given up: never count twice
            if ok:
                self._done[key] = elapsed_ms
                self._attempts.pop(key, None)
                self._retry_at.pop(key, None)
            else:
                attempts = self._attempts.get(key, 0) + 1
                self._attempts[key] = attempts
                if attempts < MAX_PANEL_ATTEMPTS:
                    # Back off but keep it queued: a dropped panel is a speech
                    # bubble that stays Chinese forever.
                    self._retry_at[key] = now + min(20.0, 2.0 * attempts)
                else:
                    self._done[key] = 0.0
                    self.status.failed += 1
            self.status.processed = len(self._done)
            self.status.queued = sum(1 for k in self._order
                                     if k not in self._done and self._retry_at.get(k, 0.0) <= now)

    # -- worker -----------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            batch = self._take_batch()
            if not batch:
                if self.status.phase not in ("idle", "done") and self.pending == 0:
                    self.status.phase = "done"
                self._wake.clear()      # only when there is nothing to do
                self._wake.wait(0.25)
                continue
            try:
                self._process(batch)
            except CancelledError:
                for spec in batch:
                    self._mark(spec.key, True)   # another reader may still retry it
                return
            except Exception as exc:      # keep the session alive, retry the batch
                log.warning("batch failed (%d panels): %s: %s", len(batch),
                            exc.__class__.__name__, exc)
                for spec in batch:
                    self._mark(spec.key, False)
                self.status.last_error = f"{exc.__class__.__name__}: {exc}"[:200]
                self.send("error", message=self.status.last_error, kind="batch")
            self._send_progress()

    # -- vision gap worker ------------------------------------------------
    def _gap_loop(self) -> None:
        """Re-read the panels OCR could not translate, off the critical path.

        These are the panels where the OCR *detector* did see text but every line
        was filtered out - the ones that used to come back empty and stay Chinese.
        Running them here means a 3-40 s model request never holds up the panels the
        reader is actually looking at.
        """
        while not self._stop.is_set():
            self._gap_wake.clear()
            if not self._gaps:
                self._gap_wake.wait(0.5)
                continue
            batch: List[Tuple[PanelSpec, np.ndarray]] = []
            with self._lock:
                while self._gaps and len(batch) < self._batch_panels:
                    batch.append(self._gaps.popleft())
            if not batch:
                continue
            if self._vision is None or not self._vision.available:
                log.info("vision gap pass stopped (%d panel(s) left in the queue)",
                         len(self._gaps) + len(batch))
                return
            self._fill_gaps(batch)

    def _queue_gaps(self, gaps: Sequence[Tuple[PanelSpec, np.ndarray]]) -> None:
        if not self._gap_pass or self._gap_thread is None or not gaps:
            return
        with self._lock:
            room = self._gap_cap - self._gap_queued
            accepted = list(gaps)[:max(0, room)]
            self._gap_queued += len(accepted)
            self._gaps.extend(accepted)
        if accepted:
            self._gap_wake.set()
            log.debug("%d panel(s) queued for the vision gap pass", len(accepted))

    def _send_progress(self) -> None:
        self.status.queued = self.pending
        self.send("progress", done=len(self._done), total=len(self._panels),
                  queued=self.status.queued, failed=self.status.failed,
                  phase=self.status.phase)

    def _process(self, batch: Sequence[PanelSpec]) -> None:
        self.status.phase = "reading"
        images: List[Tuple[PanelSpec, np.ndarray]] = []
        for spec in batch:
            image = self._image_for(spec)
            if image is None:
                self._mark(spec.key, False)
                self.send("panel", **PanelPayload(spec.key, 0, 0, [],
                                                  error="image download failed").to_dict())
                continue
            images.append((spec, image))
        if not images:
            return

        # 1. Panels already translated (the reader scrolled back): instant answer.
        #    An *empty* cached page is not an answer though - it is a page the OCR
        #    once found nothing on, so it goes back into the reading queue where the
        #    vision gap pass can pick it up.
        fresh: List[Tuple[PanelSpec, np.ndarray]] = []
        for spec, image in images:
            page_hash = PageCache.hash_frame(image)
            # Exact match only: webtoon panels share large flat areas, so a
            # tolerance of a few pHash bits can match the *neighbouring* panel and
            # serve its boxes and translations (which then land in the wrong
            # place). Re-reading a genuinely identical panel is still instant.
            cached = (self.cache.find(page_hash, self.region_key, threshold=0)
                      if self.cache.enabled else None)
            if cached is not None and cached.blocks:
                self.cache.touch(page_hash)
                self.status.memory_hits += 1
                self._emit(spec, image, cached.blocks, from_cache=True)
                log.info("panel %s from the page cache (%d blocks)",
                         spec.key[-16:], len(cached.blocks))
            else:
                fresh.append((spec, image))
        if not fresh:
            return

        # 2. Read the batch. Local OCR takes ~0.4 s per panel, a vision model
        #    25-70 s, so in the default "auto" mode OCR reads everything now and
        #    the model is asked later only about the panels where OCR found nothing
        #    - which is exactly where bubbles used to disappear.
        #    In-place rendering is the exception: it rewrites the artwork, so it
        #    needs the model's (better) boxes rather than OCR guesses.
        want_vision = self._vision is not None and self._vision.available and (
            self._vision_only or self.mode == "inplace")
        if want_vision:
            blocks_by_key = self._read_vision(fresh)
            if blocks_by_key is not None:
                for spec, image in fresh:
                    self._finish(spec, image, blocks_by_key.get(spec.key, []))
                return
            self.status.vision = False
        gaps = self._read_ocr(fresh)
        if gaps:
            self._queue_gaps(gaps)

    # -- vision path ------------------------------------------------------
    def _read_vision(self, fresh: Sequence[Tuple[PanelSpec, np.ndarray]]
                     ) -> Optional[Dict[str, List[Block]]]:
        """One request for the whole batch; ``None`` means "use OCR instead"."""
        started = time.perf_counter()
        pages = [(spec.key, image) for spec, image in fresh]
        try:
            result = self._vision.read_pages(
                pages,
                target_language=self.cfg.translation.target_language,
                glossary=self._glossary_lines(),
                is_cancelled=self._stop.is_set,
            )
        except VisionUnavailable as exc:
            log.warning("vision unavailable (%s); using OCR for %d panel(s)", exc, len(fresh))
            self.status.vision = False
            return None
        except CancelledError:
            raise
        except Exception as exc:
            reason = self._vision.handle_error(exc)
            if reason:
                self.status.vision = False
                return None
            raise
        elapsed = (time.perf_counter() - started) * 1000.0
        per_panel = elapsed / max(1, len(fresh))
        self.status.per_panel_ms = per_panel
        out: Dict[str, List[Block]] = {}
        for page in result.values():
            out[page.key] = page.blocks
            self._remember(page.blocks)
        log.info("vision batch: %d panel(s), %d block(s) in %.0f ms (%.0f ms per panel)",
                 len(fresh), sum(len(b) for b in out.values()), elapsed, per_panel)
        return out

    def _fill_gaps(self, batch: Sequence[Tuple[PanelSpec, np.ndarray]]) -> None:
        """Second pass: let the model read the panels OCR could not translate.

        Those panels already have a payload on the page (an empty one), so the new
        blocks are only *added* - the extension updates its cards in place and
        nothing flickers. Runs on its own thread: the fast queue never waits for it.
        """
        log.info("vision gap fill: %d panel(s) the OCR filters emptied", len(batch))
        try:
            result = self._vision.read_pages(
                [(spec.key, image) for spec, image in batch],
                target_language=self.cfg.translation.target_language,
                glossary=self._glossary_lines(),
                is_cancelled=self._stop.is_set,
            )
        except VisionUnavailable as exc:
            log.warning("vision unavailable for the gap pass (%s)", exc)
            self.status.vision = False
            return
        except CancelledError:
            raise
        except Exception as exc:
            reason = self._vision.handle_error(exc)
            if reason:
                self.status.vision = False
            log.warning("vision gap pass failed: %s: %s", exc.__class__.__name__, exc)
            return
        filled = 0
        for spec, image in batch:
            page = result.get(spec.key)
            if page is None or not page.blocks:
                continue
            filled += len(page.blocks)
            self._remember(page.blocks)
            self._finish(spec, image, page.blocks)
        log.info("vision gap fill: %d block(s) recovered on %d panel(s)",
                 filled, len(batch))

    def _glossary_lines(self) -> List[str]:
        entries = self.glossary.as_dict()
        return [f"{src} → {dst}" for src, dst in list(entries.items())[:60]]

    # -- OCR + text translation path --------------------------------------
    def _read_ocr(self, fresh: Sequence[Tuple[PanelSpec, np.ndarray]]
                  ) -> List[Tuple[PanelSpec, np.ndarray]]:
        """Read with the local engine and translate the whole batch in one request.

        Returns the panels OCR found no dialogue on - the candidates for the
        vision gap pass.
        """
        gaps: List[Tuple[PanelSpec, np.ndarray]] = []
        engine = self._engine_instance()
        if engine is None:
            for spec, _image in fresh:
                self._mark(spec.key, False)
                self.send("panel", **PanelPayload(spec.key, 0, 0, [],
                                                  error="OCR engine unavailable").to_dict())
            return gaps
        from ..browser.service import recognize_image

        recognised: List[Tuple[PanelSpec, np.ndarray, List[Block]]] = []
        for spec, image in fresh:
            try:
                page_hash = PageCache.hash_frame(image)
                # One engine, one shared tracker: the OCR call itself is serialised
                # (0.3 s per panel), while the model requests of other readers run
                # in parallel.
                with self._ocr_lock:
                    outcome = recognize_image(
                        self.cfg, engine, self._static_text, image, page_hash,
                        self._last_hash, log_prefix="bridge",
                    )
                    self._last_hash = page_hash
                blocks = outcome.blocks
            except Exception as exc:
                log.warning("OCR failed for panel %s: %s", spec.key[-16:], exc)
                self._mark(spec.key, False)
                self.send("panel", **PanelPayload(spec.key, 0, 0, [],
                                                  error=f"OCR failed: {exc}").to_dict())
                continue
            # A panel earns a (slow) vision request only when the OCR *detector* saw
            # text that our own filters then threw away for a reason that could be
            # wrong (a two-character minimum, low confidence, a non-CJK guess).
            # Pure artwork has zero detected spans, and a site watermark is dropped
            # by an explicit rule, so neither costs a request any more.
            has_text = any(not b.is_sfx and b.text.strip() for b in blocks)
            if outcome.detected_spans > 0 and not has_text and outcome.suspicious_drops > 0:
                gaps.append((spec, image))
            recognised.append((spec, image, blocks))

        # One text request covers many panels: the lines of the whole batch go in
        # together, so the batch costs one request instead of one per bubble.
        pending: List[Block] = []
        target = self.cfg.translation.target_language
        for _spec, _image, blocks in recognised:
            translatable = [b for b in blocks if not b.is_sfx and b.text.strip()]
            known: Dict[str, str] = {}
            if self.cache.enabled and translatable:
                known = self.cache.get_translations([b.text for b in translatable], target)
            for block in translatable:
                hit = known.get(self.cache.normalise_text(block.text))
                if hit:
                    block.translation = hit
                    self.status.memory_hits += 1
                else:
                    pending.append(block)

        if pending:
            self.status.phase = "translating"
            request = TranslateRequest(
                items=[TranslateItem(id=position, text=block.text, direction=block.direction)
                       for position, block in enumerate(pending)],
                target_language=target,
                page_index=len(self._done),
                context_lines=self.context.lines(),
                glossary=self.glossary.relevant([b.text for b in pending]),
            )
            try:
                result = self.backend.translate_page(request, self._stop.is_set)
                for position, block in enumerate(pending):
                    text = (result.translations.get(position) or "").strip()
                    block.translation = self.glossary.enforce(block.text, text) if text else ""
                self._remember(pending)
            except CancelledError:
                raise
            except Exception as exc:
                log.warning("batch translation failed for %d line(s): %s", len(pending), exc)
                self.status.last_error = f"{exc.__class__.__name__}: {exc}"[:200]
                self.send("error", message=self.status.last_error, kind="translate")

        for spec, image, blocks in recognised:
            translated = [b for b in blocks if b.translation and not b.is_sfx]
            if translated:
                self.context.add_page(translated)
            self._finish(spec, image, blocks)
        return gaps

    def _engine_instance(self) -> Optional[OcrEngine]:
        if self._engine is None and self._engine_getter is not None:
            try:
                self._engine = self._engine_getter()
            except Exception as exc:
                log.error("could not create the OCR engine: %s", exc)
        return self._engine

    # -- results ----------------------------------------------------------
    def _remember(self, blocks: Sequence[Block]) -> None:
        pairs = [(b.text, b.translation) for b in blocks
                 if b.translation and b.text and not b.is_sfx]
        if pairs and self.cache.enabled:
            self.cache.store_translations(pairs, self.cfg.translation.target_language,
                                          self.status.model)

    def _finish(self, spec: PanelSpec, image: np.ndarray, blocks: List[Block]) -> None:
        model = getattr(self.backend, "model", "") or self.status.model
        if self.cache.enabled:
            self.cache.store(PageCache.hash_frame(image), self.region_key,
                             image.shape[1], image.shape[0], blocks, model=model,
                             preview_bgr=image)
        self._emit(spec, image, blocks, from_cache=False)

    def _emit(self, spec: PanelSpec, image: np.ndarray, blocks: Sequence[Block],
              from_cache: bool) -> None:
        height, width = image.shape[:2]
        payloads = [
            BlockPayload(
                id=f"{self._panel_index(spec)}:{block.id}",
                x=float(block.bbox.x), y=float(block.bbox.y),
                w=float(block.bbox.w), h=float(block.bbox.h),
                text=block.text, translation=block.translation,
                vertical=(block.direction == "vertical"),
                confidence=float(block.confidence), sfx=bool(block.is_sfx),
            )
            for block in blocks
        ]
        inplace = None
        if self.mode == "inplace" and any(p.translation for p in payloads):
            inplace = self._render_inplace(image, blocks)
        payload = PanelPayload(spec.key, width, height, payloads, from_cache=from_cache,
                               inplace=inplace)
        self.status.blocks += len(payloads)
        self.send("panel", **payload.to_dict())
        self._mark(spec.key, True)

    def _panel_index(self, spec: PanelSpec) -> int:
        with self._lock:
            try:
                return self._order.index(spec.key)
            except ValueError:
                return spec.order or 0

    def _render_inplace(self, image: np.ndarray, blocks: Sequence[Block]) -> Optional[dict]:
        try:
            from ..render import encode_inplace, render_inplace

            started = time.perf_counter()
            rendered = render_inplace(image, [b for b in blocks if b.translation and not b.is_sfx])
            mime, data = encode_inplace(rendered, quality=85)
            log.info("in-place render of %dx%d in %.0f ms (%.0f KB)",
                     image.shape[1], image.shape[0],
                     (time.perf_counter() - started) * 1000.0, len(data) / 1024.0)
            _dump_inplace(rendered, image)
            return {"mime": mime, "data": data}
        except Exception as exc:
            log.warning("in-place render failed: %s", exc)
            return None

    # -- images -----------------------------------------------------------
    def _image_for(self, spec: PanelSpec) -> Optional[np.ndarray]:
        cached = self._images.get(spec.key)
        if cached is not None:
            return cached
        image: Optional[np.ndarray] = None
        if self._fetch_image is not None:
            try:
                image = self._fetch_image(spec)
            except Exception as exc:
                log.debug("image fetch failed for %s: %s", spec.key[-16:], exc)
        if image is None and spec.url:
            image = download_image(spec.url)
        if image is None:
            return None
        if len(self._images) >= IMAGE_CACHE:
            self._images.clear()
        self._images[spec.key] = image
        return image


def _dump_inplace(rendered: np.ndarray, original: np.ndarray) -> None:
    """With ``MT_INPLACE_DUMP=<dir>`` keep before/after images of every panel.

    Rendering quality is judged by eye, and a JPEG that travelled through the
    extension is hard to inspect, so the pair is written where it was produced.
    """
    import os

    directory = os.environ.get("MT_INPLACE_DUMP", "").strip()
    if not directory:
        return
    try:
        import cv2
        from pathlib import Path

        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        stamp = f"{time.time():.3f}".replace(".", "")
        cv2.imwrite(str(target / f"inplace_{stamp}_before.png"), original)
        cv2.imwrite(str(target / f"inplace_{stamp}_after.png"), rendered)
    except Exception as exc:  # pragma: no cover - debugging aid
        log.debug("could not dump the in-place render: %s", exc)


def download_image(url: str, timeout: float = 30.0) -> Optional[np.ndarray]:
    """Fetch a panel image straight from the CDN (the tested site serves it directly)."""
    import httpx

    from ..browser.reader import decode_image

    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, read=timeout), follow_redirects=True,
                          headers={"User-Agent": USER_AGENT,
                                   "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}) as client:
            response = client.get(url)
    except Exception as exc:
        log.warning("panel download failed (%s): %s", exc.__class__.__name__, url[:80])
        return None
    if response.status_code != 200 or not response.content:
        log.warning("panel download returned HTTP %s for %s", response.status_code, url[:80])
        return None
    try:
        return decode_image(response.content)
    except Exception as exc:
        log.warning("panel image could not be decoded: %s", exc)
        return None
