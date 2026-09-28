"""Pipeline orchestrator: monitor thread + processing worker + generation ids.

    monitor thread                processing worker                 GUI thread
    --------------                -----------------                 ----------
    preview every 150 ms   --->   full grab -> cache -> OCR   --->  overlay cards
    cheap similarity              -> grouping -> 1 LLM call
    stabilize 350 ms              -> publish (generation checked)
    bump generation

Only the newest job matters: queued stale jobs are dropped, in-flight work is
cancelled or invalidated through the generation counter, and the overlay only
ever receives results whose generation is still current.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from ..cache.page_cache import PageCache, region_key, settings_fingerprint
from ..capture.screen_capture import FrameSource
from ..core.config import Config
from ..core.logging_setup import get_logger
from ..core.i18n import tr
from ..core.models import Block, PageResult, PipelineError, Rect, ScreenRegion
from ..detection.image_similarity import hamming_hex
from ..detection.page_change import DetectorSettings, PageChangeDetector
from ..glossary.glossary import Glossary
from ..ocr import (
    create_engine,
    filter_settings,
    filter_spans,
    group_settings,
    group_spans,
    postprocess_blocks,
)
from ..ocr.filtering import StaticTextTracker
from ..ocr.base import OcrEngine
from ..translation.base import (
    CancelledError,
    TranslateItem,
    TranslateRequest,
    TranslateResult,
    TranslationBackend,
)
from ..translation.context import ContextStore
from .generation import GenerationCounter

log = get_logger("pipeline")


@dataclass
class PageJob:
    generation: int
    region: ScreenRegion
    kind: str = "changed"          # first | changed | forced | manual | preview
    change_score: float = 0.0
    created_at: float = field(default_factory=time.time)
    force: bool = False            # manual refresh: skip cache and de-duplication


class Publisher:
    """Receives everything the GUI needs. Implemented by the Qt bridge in ui/."""

    def page_started(self, job: PageJob) -> None:
        pass

    def page_ready(self, result: PageResult, job: PageJob) -> None:
        pass

    def error(self, message: str, kind: str = "error") -> None:
        pass

    def status(self, message: str, level: str = "info") -> None:
        pass

    def detector(self, state: str, score: float, generation: int) -> None:
        pass

    def busy(self, processing: bool) -> None:
        pass

    def preview(self, frame: np.ndarray, region: ScreenRegion) -> None:
        """A fresh frame for the control-panel preview (no OCR was run)."""


class RecordingPublisher(Publisher):
    """Test/self-test publisher that keeps everything in memory."""

    def __init__(self):
        self.pages: List[tuple] = []
        self.errors: List[tuple] = []
        self.statuses: List[tuple] = []
        self.events: List[str] = []
        self.detector_states: List[tuple] = []
        self.previews: List[tuple] = []

    def preview(self, frame: np.ndarray, region: ScreenRegion) -> None:
        self.previews.append((frame, region))
        self.events.append("preview")

    def page_started(self, job: PageJob) -> None:
        self.events.append(f"started:{job.generation}:{job.kind}")

    def page_ready(self, result: PageResult, job: PageJob) -> None:
        self.pages.append((result, job))
        self.events.append(f"ready:{result.generation}:cache={result.from_cache}")

    def error(self, message: str, kind: str = "error") -> None:
        self.errors.append((kind, message))
        self.events.append(f"error:{kind}")

    def status(self, message: str, level: str = "info") -> None:
        self.statuses.append((level, message))

    def detector(self, state: str, score: float, generation: int) -> None:
        self.detector_states.append((state, round(score, 4), generation))

    def busy(self, processing: bool) -> None:
        self.events.append(f"busy:{processing}")

    # helpers -----------------------------------------------------------
    @property
    def last_page(self) -> Optional[PageResult]:
        return self.pages[-1][0] if self.pages else None

    def wait_for_page(self, timeout: float = 10.0, count: int = 1) -> Optional[PageResult]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.pages) >= count:
                return self.pages[count - 1][0]
            time.sleep(0.02)
        return None


class PipelineStats:
    def __init__(self):
        self.pages_processed = 0
        self.cache_hits = 0
        self.stale_discarded = 0
        self.ocr_ms_total = 0.0
        self.translate_ms_total = 0.0
        self.blocks_total = 0
        self.memory_hits = 0
        self.errors = 0
        self.last_ocr_ms = 0.0
        self.last_translate_ms = 0.0

    def as_dict(self) -> dict:
        pages = max(1, self.pages_processed)
        return {
            "pages_processed": self.pages_processed,
            "cache_hits": self.cache_hits,
            "stale_discarded": self.stale_discarded,
            "avg_ocr_ms": round(self.ocr_ms_total / pages, 1),
            "avg_translate_ms": round(self.translate_ms_total / pages, 1),
            "blocks_total": self.blocks_total,
            "memory_hits": self.memory_hits,
            "errors": self.errors,
        }


class PagePipeline:
    def __init__(
        self,
        config: Config,
        source: FrameSource,
        backend: TranslationBackend,
        cache: PageCache,
        glossary: Glossary,
        publisher: Optional[Publisher] = None,
        engine: Optional[OcrEngine] = None,
        mask_provider: Optional[Callable[[], Sequence[Rect]]] = None,
    ):
        self.cfg = config
        self.source = source
        self.backend = backend
        self.cache = cache
        self.glossary = glossary
        self.publisher = publisher or Publisher()
        self.generations = GenerationCounter()
        self.context = ContextStore(
            max_pages=config.translation.context_pages, max_lines=60
        )
        self.stats = PipelineStats()

        self._engine_warning: Optional[str] = None
        self.engine = engine or self._build_engine()
        self.mask_provider = mask_provider or (lambda: ())
        self.detector = PageChangeDetector(self._detector_settings())

        self._region: Optional[ScreenRegion] = None
        self._jobs: "queue.Queue[PageJob]" = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()  # set == paused
        self._threads: List[threading.Thread] = []
        self._lock = threading.RLock()
        self._last_hash = ""
        self._last_region_key = ""
        self._page_index = 0
        self._running = False
        self._mask_snapshot: List[Rect] = []
        self._mask_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._last_frame: Optional[np.ndarray] = None      # last full-resolution capture
        self._last_frame_region: Optional[ScreenRegion] = None
        self._last_result: Optional[PageResult] = None
        self._last_preview_score = 0.0
        self._static_text = StaticTextTracker()
        self._fp_cache: Optional[str] = None

    # -- construction helpers --------------------------------------------
    def _fingerprint(self) -> str:
        """Cache-key suffix; changes when OCR/translation settings change."""
        value = self._fp_cache
        if value is None:
            value = settings_fingerprint(self.cfg)
            self._fp_cache = value
        return value

    def _build_engine(self) -> OcrEngine:
        engine, warning = create_engine(self.cfg.ocr)
        self._engine_warning = warning
        return engine

    @property
    def engine_warning(self) -> Optional[str]:
        return self._engine_warning

    def _detector_settings(self) -> DetectorSettings:
        g = self.cfg.general
        return DetectorSettings(
            change_threshold=float(g.change_threshold),
            stable_duration_ms=int(g.stable_duration_ms),
            min_unstable_frames=int(g.min_unstable_frames),
            max_wait_ms=int(g.max_wait_ms),
            preview_max_dim=int(g.preview_max_dim),
        )

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._running:
            self._pause.clear()
            return
        self._stop.clear()
        self._pause.clear()  # start() means "monitor now"; pause() stops detection
        self._running = True
        names = [("mt-monitor", self._monitor_loop), ("mt-worker", self._worker_loop)]
        self._threads = []
        for name, target in names:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("pipeline started (engine=%s, backend=%s)", self.engine.capabilities.name, self.backend.name)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
        self._running = False
        log.info("pipeline stopped")

    def pause(self) -> None:
        """Manga Mode off: stop detecting, drop pending work, clear generation."""
        self._pause.set()
        self.generations.bump()
        self._drain_jobs()
        self.detector.reset("paused")

    def resume(self) -> None:
        self.detector.reset("resumed")
        self._pause.clear()

    @property
    def is_running(self) -> bool:
        return self._running and not self._pause.is_set()

    # -- configuration ----------------------------------------------------
    def update_config(self, config: Config) -> None:
        with self._lock:
            self.cfg = config
            self._fp_cache = None
            self.detector.s = self._detector_settings()
            self.context.configure(config.translation.context_pages)
            self.cache.configure(
                config.cache.enabled, config.cache.max_mb, config.cache.hamming_threshold
            )

    def set_region(self, region: Optional[ScreenRegion]) -> None:
        with self._lock:
            self._region = region
            self._last_hash = ""
            self._last_region_key = region_key(region.x, region.y, region.width, region.height) if region else ""
            self.generations.bump()
            self._drain_jobs()
            self.detector.reset("region changed")
            self._static_text.reset()
        if region:
            log.info("region set to %dx%d at (%d,%d) monitor=%s scale=%.2f",
                     region.width, region.height, region.x, region.y, region.monitor, region.dpi_scale)

    def set_masks(self, masks: Sequence[Rect]) -> None:
        """Called from the GUI thread when overlay cards change."""
        with self._mask_lock:
            self._mask_snapshot = list(masks)

    def refresh(self, force: bool = True) -> Optional[int]:
        """Manual 'Refresh Page': capture -> OCR -> translate -> overlay now."""
        region = self._region
        if region is None:
            self.publisher.status(tr("Select a region first."), "warn")
            return None
        generation = self.generations.bump()
        self._drain_jobs()
        self._submit(PageJob(generation, region, kind="manual", change_score=1.0, force=force))
        log.info("manual refresh requested (generation=%d)", generation)
        return generation

    def request_preview(self) -> None:
        """Ask the worker for one fresh frame (control-panel preview, no OCR)."""
        region = self._region
        if region is None or not self._running:
            return
        self._submit(PageJob(self.generations.current, region, kind="preview"))

    # -- shared state for the UI ------------------------------------------
    def latest_frame(self):
        """(frame_bgr, region) of the most recent capture, or (None, None)."""
        with self._frame_lock:
            return self._last_frame, self._last_frame_region

    def last_result(self) -> Optional[PageResult]:
        with self._frame_lock:
            return self._last_result

    def detector_status(self) -> dict:
        status = self.detector.status
        return {
            "state": status.state,
            "score": status.score,
            "changed_fraction": status.changed_fraction,
            "hamming": status.hamming,
            "stable_for_ms": status.stable_for_ms,
            "page_hash": status.page_hash or self._last_hash,
        }

    def _store_frame(self, frame: np.ndarray, region: ScreenRegion) -> None:
        with self._frame_lock:
            self._last_frame = frame
            self._last_frame_region = region

    def _publish_page(self, result: PageResult, job: PageJob) -> None:
        """Remember the result for the control panel, then notify the UI."""
        with self._frame_lock:
            self._last_result = result
        self.publisher.page_ready(result, job)

    # -- job plumbing -----------------------------------------------------
    def _drain_jobs(self) -> None:
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            else:
                if job.generation != self.generations.current:
                    self.stats.stale_discarded += 1
                    log.info("discarded queued job for stale generation %d", job.generation)

    def _submit(self, job: PageJob) -> None:
        self._drain_jobs()
        try:
            self._jobs.put_nowait(job)
        except queue.Full:  # pragma: no cover - defensive
            log.warning("job queue full; dropping generation %d", job.generation)

    # -- monitor thread ---------------------------------------------------
    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            started = time.perf_counter()
            interval = max(0.03, float(self.cfg.general.poll_interval_ms) / 1000.0)
            if self._pause.is_set():
                self._stop.wait(0.1)
                continue
            region = self._region
            if region is None:
                self._stop.wait(0.2)
                continue
            try:
                preview = self.source.grab_preview(region, int(self.cfg.general.preview_max_dim))
            except PipelineError as exc:
                self.publisher.error(exc.message, exc.kind)
                self._stop.wait(1.5)
                continue
            except Exception as exc:  # pragma: no cover - unexpected
                log.exception("preview grab failed: %s", exc.__class__.__name__)
                self.publisher.error(tr("Screen capture failed. Select the region again."), "capture")
                self._stop.wait(1.5)
                continue

            masks = self._preview_masks(preview, region)
            event = self.detector.feed(preview, masks)
            self.publisher.detector(self.detector.status.state, self.detector.status.score,
                                    self.generations.current)
            if event is not None:
                if event.kind == "changed":
                    # A real page turn invalidates everything in flight.
                    generation = self.generations.bump()
                elif event.kind == "first":
                    generation = self.generations.bump()
                else:
                    generation = self.generations.current
                self._submit(
                    PageJob(
                        generation=generation,
                        region=region,
                        kind=event.kind,
                        change_score=event.report.score,
                    )
                )
            elapsed = time.perf_counter() - started
            self._stop.wait(max(0.005, interval - elapsed))

    def _preview_masks(self, preview: np.ndarray, region: ScreenRegion) -> List[Rect]:
        with self._mask_lock:
            snapshot = list(self._mask_snapshot)
        if not snapshot or region.width <= 0 or region.height <= 0:
            return []
        sx = preview.shape[1] / float(region.width)
        sy = preview.shape[0] / float(region.height)
        return [Rect(r.x * sx, r.y * sy, r.w * sx, r.h * sy) for r in snapshot]

    # -- worker thread ----------------------------------------------------
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.15)
            except queue.Empty:
                continue
            try:
                self._process(job)
            except CancelledError:
                self.stats.stale_discarded += 1
                log.info("generation %d cancelled during processing", job.generation)
            except PipelineError as exc:
                self.stats.errors += 1
                log.warning("page %d failed: [%s] %s", job.generation, exc.kind, exc.message)
                self.publisher.error(exc.message, exc.kind)
            except Exception as exc:  # unexpected: never crash the app
                self.stats.errors += 1
                log.exception("unexpected pipeline failure for generation %d", job.generation)
                self.publisher.error(
                    tr("Unexpected internal error ({kind}). See the log file.").format(
                        kind=exc.__class__.__name__
                    ),
                    "internal",
                )
            finally:
                self.publisher.busy(False)

    def _process(self, job: PageJob) -> None:
        if self.generations.is_stale(job.generation):
            self.stats.stale_discarded += 1
            log.info("skipping stale job (generation %d)", job.generation)
            return
        region = job.region

        if job.kind == "preview":
            # Control-panel preview: capture only, no OCR / no API call.
            frame = self.source.grab(region)
            self._check(job)
            self._store_frame(frame, region)
            self.publisher.preview(frame, region)
            self.publisher.busy(False)
            return

        started = time.perf_counter()
        self.publisher.page_started(job)
        self.publisher.busy(True)
        self.publisher.status(tr("Reading the page…"))

        frame = self.source.grab(region)
        self._check(job)
        self._store_frame(frame, region)

        page_hash = PageCache.hash_frame(frame)
        key = (
            f"{region_key(region.x, region.y, region.width, region.height)}"
            f"#{self._fingerprint()}"
        )

        # Same page as last time (e.g. a false trigger from a cursor): skip work.
        if not job.force and self._last_hash and key == self._last_region_key:
            if hamming_hex(self._last_hash, page_hash) <= self.cache.hamming_threshold:
                self.publisher.status(tr("Page unchanged."), "info")
                log.info("page unchanged (hash distance within cache threshold); nothing to do")
                return

        if self.cache.enabled and not job.force:
            cached = self.cache.find(page_hash, key)
            if cached is not None:
                self._check(job)
                self.cache.touch(page_hash)
                self.stats.cache_hits += 1
                self.stats.pages_processed += 1
                result = PageResult(
                    generation=job.generation,
                    page_hash=page_hash,
                    blocks=cached.blocks,
                    from_cache=True,
                    total_ms=(time.perf_counter() - started) * 1000.0,
                )
                self.publisher.status(
                    tr("Page from cache ({n} blocks).").format(n=len(cached.blocks)), "info"
                )
                log.info("cache hit for generation %d (distance=%d)", job.generation, cached.distance)
                self._publish_page(result, job)
                self._last_hash = page_hash
                return

        blocks, translatable, ocr_ms, debug_spans = self._recognize(frame, region, job, page_hash)
        self._check(job)

        if not translatable:
            self.stats.pages_processed += 1
            self._last_hash = page_hash
            self._last_region_key = key
            result = PageResult(
                generation=job.generation,
                page_hash=page_hash,
                blocks=blocks,
                extra_spans=debug_spans,
                ocr_ms=ocr_ms,
                total_ms=(time.perf_counter() - started) * 1000.0,
            )
            self.publisher.status(tr("No Chinese dialogue found on this page."), "info")
            log.info("no translatable blocks (generation %d)", job.generation)
            self._publish_page(result, job)
            return

        # --- translation memory: reuse lines translated on earlier pages -----
        target_lang = self.cfg.translation.target_language
        known: Dict[str, str] = {}
        if self.cache.enabled:
            known = self.cache.get_translations([b.text for b in translatable], target_lang)
        pending: List[Block] = []
        for block in translatable:
            hit = known.get(self.cache.normalise_text(block.text))
            if hit:
                block.translation = hit
            else:
                pending.append(block)
        if known:
            self.stats.memory_hits += len(known)

        def _result() -> PageResult:
            return PageResult(
                generation=job.generation,
                page_hash=page_hash,
                blocks=blocks,
                extra_spans=debug_spans,
                from_cache=False,
                translate_ms=translation_ms,
                total_ms=(time.perf_counter() - started) * 1000.0,
            )

        translation_ms = 0.0
        model_name = getattr(self.backend, "model", "")
        partial = None
        if known and pending:
            # Show what we already know straight away, then finish the rest.
            partial = _result()
            self.publisher.status(
                tr("Showing {known} known lines, translating {n} new…").format(
                    known=len(known), n=len(pending)
                )
            )
            self._publish_page(partial, job)
            self._check(job)

        if pending:
            self.publisher.status(tr("Translating {n} blocks…").format(n=len(pending)))
            translation = self._translate(pending, job)
            self._check(job)
            self._apply_translations(pending, translation, job)
            translation_ms = translation.elapsed_ms
            model_name = translation.model or model_name
            if self.cache.enabled:
                self.cache.store_translations(
                    [(b.text, b.translation) for b in pending if b.translation],
                    target_lang,
                    model_name,
                )
        else:
            translation = TranslateResult(model=model_name, translations={})
            self.publisher.status(tr("All lines came from the translation memory."), "info")

        self.stats.pages_processed += 1
        self.stats.blocks_total += len(translatable)
        self._last_hash = page_hash
        self._last_region_key = key

        if self.cache.enabled:
            self.cache.store(
                page_hash,
                key,
                region.width,
                region.height,
                blocks,
                model=model_name,
                preview_bgr=frame,
            )
        self.context.add_page(blocks)
        self._page_index += 1

        result = PageResult(
            generation=job.generation,
            page_hash=page_hash,
            blocks=blocks,
            extra_spans=debug_spans,
            from_cache=False,
            ocr_ms=ocr_ms,
            translate_ms=translation_ms,
            total_ms=(time.perf_counter() - started) * 1000.0,
        )
        if translation.missing_ids:
            log.warning("model returned no translation for ids %s", translation.missing_ids)
            result.error = tr("{n} blocks came back untranslated.").format(
                n=len(translation.missing_ids)
            )
        log.info(
            "page %d ready in %.0f ms (ocr %.0f ms, translate %.0f ms, %d blocks)",
            job.generation, result.total_ms, result.ocr_ms, result.translate_ms, len(blocks),
        )
        self._publish_page(result, job)

    # -- stages -----------------------------------------------------------
    def _recognize(self, frame: np.ndarray, region: ScreenRegion, job: PageJob,
                   page_hash: str = ""):
        started = time.perf_counter()
        if not self.engine.is_ready():
            self.publisher.status(tr("Loading the OCR model…"))
        self.engine.warmup()
        spans = self.engine.recognize(frame)
        ocr_ms = (time.perf_counter() - started) * 1000.0
        self.stats.ocr_ms_total += ocr_ms
        self.stats.last_ocr_ms = ocr_ms
        log.info("OCR: %d spans in %.0f ms (generation %d)", len(spans), ocr_ms, job.generation)

        filtered = filter_spans(spans, filter_settings(self.cfg.ocr))
        if filtered.low_confidence:
            log.info("kept %d low-confidence spans for the debug overlay", len(filtered.low_confidence))
        blocks = group_spans(
            filtered.accepted,
            group_settings(self.cfg.ocr),
            page_area=float(region.width * region.height),
        )
        page_size = (region.width, region.height)
        blocks, translatable = postprocess_blocks(blocks, page_size, self.cfg.ocr)
        if self.cfg.ocr.suppress_static_text and blocks:
            moved = hamming_hex(self._last_hash, page_hash) if self._last_hash else 99
            static_ids = set(self._static_text.observed(blocks, page_size, page_hash, moved))
            if static_ids:
                dropped = [b for b in blocks if b.id in static_ids]
                log.info(
                    "dropped %d static site element(s) (same text/position on %d pages): %s",
                    len(dropped), self._static_text.hits_required,
                    [b.text[:20] for b in dropped],
                )
                blocks = [b for b in blocks if b.id not in static_ids]
                translatable = [b for b in blocks if not b.is_sfx and b.text.strip()]
        if blocks and not translatable:
            log.info("no translatable text on this page (all %d blocks are SFX)", len(blocks))
        debug_spans = list(filtered.low_confidence)
        log.debug("grouped %d spans into %d blocks (%d translatable)",
                  len(spans), len(blocks), len(translatable))
        return blocks, translatable, ocr_ms, debug_spans

    def _translate(self, blocks: Sequence[Block], job: PageJob) -> TranslateResult:
        started = time.perf_counter()
        items = [
            TranslateItem(id=b.id, text=b.text, direction=b.direction, confidence=b.confidence)
            for b in blocks
        ]
        glossary_terms = self.glossary.relevant([b.text for b in blocks])
        request = TranslateRequest(
            items=items,
            target_language=self.cfg.translation.target_language,
            page_index=self._page_index,
            context_lines=self.context.lines(),
            glossary=glossary_terms,
        )
        is_cancelled = self.generations.cancellable(job.generation)
        attempt = 0
        while True:
            attempt += 1
            try:
                result = self.backend.translate_page(request, is_cancelled)
                break
            except CancelledError:
                raise
            except PipelineError as exc:
                # The backend already retried transport-level failures; one extra
                # attempt here covers malformed/empty answers. Never keep retrying
                # a page the user has left (generation check).
                if exc.retryable and attempt <= 1 and not is_cancelled():
                    delay = min(6.0, float(self.cfg.translation.backoff_base_s) * (2 ** (attempt - 1)))
                    log.info("translation retry %d in %.1fs (%s)", attempt, delay, exc.kind)
                    self.publisher.status(
                        tr("Translation retry in {s:.0f}s ({kind})…").format(s=delay, kind=exc.kind),
                        "warn",
                    )
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline:
                        if is_cancelled():
                            raise CancelledError("page generation changed")
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                    continue
                raise
        translate_ms = (time.perf_counter() - started) * 1000.0
        self.stats.translate_ms_total += translate_ms
        self.stats.last_translate_ms = translate_ms
        log.info("translation: %d/%d blocks in %.0f ms (model=%s)",
                 len(result.translations), len(items), translate_ms, result.model)
        return result

    def _apply_translations(
        self, blocks: Sequence[Block], result: TranslateResult, job: PageJob
    ) -> None:
        for block in blocks:
            text = result.translations.get(block.id, "")
            block.translation = self.glossary.enforce(block.text, text).strip() if text else ""
        if not any(b.translation for b in blocks):
            raise PipelineError(
                tr("No translation was produced for this page."), kind="empty", retryable=False
            )

    # -- misc -------------------------------------------------------------
    def _check(self, job: PageJob) -> None:
        if self.generations.is_stale(job.generation):
            raise CancelledError(f"generation {job.generation} is stale")

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._jobs.empty():
                time.sleep(0.05)
                if self._jobs.empty():
                    return True
            time.sleep(0.02)
        return False

    def describe(self) -> Dict[str, object]:
        return {
            "engine": self.engine.capabilities.label,
            "backend": getattr(self.backend, "name", "?"),
            "model": getattr(self.backend, "model", ""),
            "generation": self.generations.current,
            "page_index": self._page_index,
            "region": self._region.to_dict() if self._region else None,
            "stats": self.stats.as_dict(),
            "cache": self.cache.stats(),
        }
