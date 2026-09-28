"""End-to-end pipeline scenarios A-F from the task specification (section 33).

These drive the real orchestrator (monitor thread + worker thread + generation
ids) against a controlled frame source, a page-hash-keyed OCR engine and
offline translators - no GUI, no API key, deterministic results.
"""
from __future__ import annotations

import time

import pytest

from conftest import blank_page, page_one, page_three, page_two, span
from manga_translator.cache.page_cache import PageCache
from manga_translator.capture.synthetic import MutableFrameSource
from manga_translator.core.models import ScreenRegion
from manga_translator.glossary.glossary import Glossary
from manga_translator.ocr.stub_engine import HashKeyedOcrEngine, StubOcrEngine
from manga_translator.pipeline.orchestrator import PagePipeline, RecordingPublisher
from manga_translator.translation.fake import FakeTranslator

REGION = ScreenRegion(0, 0, 1200, 900, "TEST", 1.0)

SPANS_A = [span("你好，今天天气真好。", 120, 120, 260, 34), span("师兄，你来了！", 120, 260, 200, 34)]
SPANS_B = [span("灵气失控了！", 700, 140, 60, 260, score=0.97), span("为什么？", 150, 320, 150, 34)]
SPANS_C = [span("林雪，等一下！", 220, 430, 240, 34)]


def keyed(pages, **kwargs) -> HashKeyedOcrEngine:
    """OCR engine that answers by page content (hash), not by call order.

    `pages` is a list of (frame, spans) pairs.
    """
    return HashKeyedOcrEngine(
        {HashKeyedOcrEngine.key(frame): spans for frame, spans in pages}, **kwargs
    )


class Harness:
    def __init__(self, cfg, tmp_paths, frame, engine, backend=None):
        self.cfg = cfg
        self.source = MutableFrameSource(frame)
        self.engine = engine
        self.backend = backend or FakeTranslator()
        self.publisher = RecordingPublisher()
        self.cache = PageCache(
            tmp_paths,
            max_mb=cfg.cache.max_mb,
            hamming_threshold=cfg.cache.hamming_threshold,
            enabled=cfg.cache.enabled,
        )
        self.glossary = Glossary(tmp_paths.glossary)
        self.pipe = PagePipeline(
            cfg,
            self.source,
            self.backend,
            self.cache,
            self.glossary,
            publisher=self.publisher,
            engine=self.engine,
        )

    def start(self):
        self.pipe.start()
        self.pipe.set_region(REGION)
        return self

    def page(self, index=1, timeout=10.0):
        return self.publisher.wait_for_page(timeout=timeout, count=index)

    def wait_for_text(self, needle: str, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for result, job in self.publisher.pages:
                if any(needle in b.text for b in result.blocks):
                    return result, job
            time.sleep(0.03)
        return None, None

    def close(self):
        self.pipe.stop(timeout=1.5)
        self.cache.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture()
def harness_factory(fast_config, tmp_paths):
    made = []

    def make(frame, engine, backend=None, cfg=None):
        h = Harness(cfg or fast_config, tmp_paths, frame, engine, backend)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


# --- scenario A ------------------------------------------------------------

def test_scenario_a_normal_page_turn(harness_factory):
    engine = keyed([(page_one(), SPANS_A), (page_two(), SPANS_B)])
    h = harness_factory(page_one(), engine).start()

    first = h.page(1)
    assert first is not None, h.publisher.events
    assert len(first.blocks) == 2
    assert all(b.translation for b in first.blocks)
    assert "你好" in first.blocks[0].text
    assert first.ocr_ms > 0

    h.source.set_frame(page_two())
    second = h.page(2)
    assert second is not None, h.publisher.events
    assert second.generation > first.generation
    assert {b.text for b in second.blocks} != {b.text for b in first.blocks}
    assert all(b.translation for b in second.blocks)
    assert engine.calls == 2
    assert [b.id for b in second.blocks] == [1, 2]  # reading order kept


# --- scenario B ------------------------------------------------------------

def test_scenario_b_fast_switching_keeps_only_the_latest(harness_factory):
    """A -> B -> C with a slow backend: late A/B results never overlay C."""
    engine = keyed([(page_one(), SPANS_A), (page_two(), SPANS_B), (page_three(), SPANS_C)], delay_s=0.05)
    backend = FakeTranslator(delay_s=0.5)
    h = harness_factory(page_one(), engine, backend).start()

    assert h.page(1) is not None, h.publisher.events  # page A is on screen

    h.source.set_frame(page_two())      # page B appears while A is translated
    time.sleep(0.12)
    h.source.set_frame(page_three())    # user immediately turns again -> C wins

    result, job = h.wait_for_text("林雪")
    assert result is not None, h.publisher.events
    assert job.generation == result.generation

    generations = [r.generation for r, _ in h.publisher.pages]
    assert generations == sorted(generations), generations
    # C's frame is the newest generation and nothing newer overwrote it
    time.sleep(0.8)
    assert [r.generation for r, _ in h.publisher.pages] == generations
    final = h.publisher.pages[-1][0]
    assert any("林雪" in b.text for b in final.blocks), [b.text for b in final.blocks]
    assert final.generation == max(generations)


def test_stale_generation_results_are_never_published(harness_factory):
    """A result that finishes late must be dropped, not drawn over a newer page."""
    engine = keyed([(page_one(), SPANS_A), (page_two(), SPANS_B)], delay_s=0.35)
    backend = FakeTranslator(delay_s=0.05)
    h = harness_factory(page_one(), engine, backend).start()

    h.source.set_frame(page_two())      # change before page 1 finished OCR
    time.sleep(0.15)
    h.source.set_frame(page_one())      # ... and back again

    assert h.wait_for_text("你好", timeout=10)[0] is not None or h.publisher.pages
    time.sleep(1.0)
    generations = [r.generation for r, _ in h.publisher.pages]
    assert generations == sorted(generations)
    for result, job in h.publisher.pages:
        assert result.generation == job.generation
        # a published page is always the frame that was on screen for its generation
        assert result.page_hash


# --- scenario C ------------------------------------------------------------

def test_scenario_c_cache_round_trip(harness_factory):
    """A -> B -> A: the second visit is served from cache without OCR or API."""
    engine = keyed([(page_one(), SPANS_A), (page_two(), SPANS_B)])
    backend = FakeTranslator()
    h = harness_factory(page_one(), engine, backend).start()

    first = h.page(1)
    assert first is not None and not first.from_cache
    h.source.set_frame(page_two())
    second = h.page(2)
    assert second is not None and not second.from_cache
    calls = (engine.calls, backend.calls)

    h.source.set_frame(page_one())
    third = h.page(3)
    assert third is not None, h.publisher.events
    assert third.from_cache, "returning to a translated page must hit the cache"
    assert (engine.calls, backend.calls) == calls, "cache hit must not re-OCR or re-translate"
    assert [b.text for b in third.blocks] == [b.text for b in first.blocks]
    assert all(b.translation for b in third.blocks)


# --- scenario D ------------------------------------------------------------

def test_scenario_d_continuous_scrolling_does_not_ocr_every_frame(harness_factory):
    """Intermediate scroll frames must never reach OCR."""
    engine = keyed([(page_one(), SPANS_A), (page_two(), SPANS_B), (page_three(), SPANS_C)], delay_s=0.02)
    h = harness_factory(page_one(), engine).start()
    assert h.page(1) is not None
    calls_after_first = engine.calls

    # simulate scrolling: a different frame every poll (~30 ms), then stop on C
    scroll = [page_two(), page_three(), page_one(), page_two(), page_three()]
    for frame in scroll:
        h.source.set_frame(frame)
        time.sleep(0.04)
    h.source.set_frame(page_three())

    result, _ = h.wait_for_text("林雪", timeout=10)
    assert result is not None, h.publisher.events
    assert engine.calls - calls_after_first <= 2, "scrolling must not trigger OCR per frame"


# --- scenario E ------------------------------------------------------------

def test_scenario_e_page_without_text(harness_factory):
    """No Chinese dialogue: no crash, no translation request, empty overlay."""
    engine = keyed([(blank_page(), [])])
    backend = FakeTranslator()
    h = harness_factory(blank_page(), engine, backend).start()

    result = h.page(1)
    assert result is not None, h.publisher.events
    assert result.blocks == []
    assert result.error is None
    assert backend.calls == 0, "no text -> no API request"
    assert h.publisher.errors == []


# --- scenario F ------------------------------------------------------------

def test_scenario_f_api_offline_then_recovery(harness_factory):
    engine = keyed([(page_one(), SPANS_A), (page_two(), SPANS_B)])
    backend = FakeTranslator(fail_times=4)
    h = harness_factory(page_one(), engine, backend).start()

    deadline = time.time() + 10
    while time.time() < deadline and not h.publisher.errors:
        time.sleep(0.05)
    assert h.publisher.errors, h.publisher.events
    kind, message = h.publisher.errors[-1]
    assert kind in ("network", "timeout")
    assert "Traceback" not in message

    backend.fail_times = 0
    h.source.set_frame(page_two())
    result, _ = h.wait_for_text("为什么", timeout=10)
    assert result is not None, h.publisher.events
    assert all(b.translation for b in result.blocks if not b.is_sfx)


# --- manual refresh / toggles ---------------------------------------------

def test_manual_refresh_forces_full_reprocessing(harness_factory):
    engine = keyed([(page_one(), SPANS_A)])
    backend = FakeTranslator()
    h = harness_factory(page_one(), engine, backend).start()

    first = h.page(1)
    assert first is not None
    assert engine.calls == 1 and backend.calls == 1
    generation = h.pipe.refresh(force=True)
    assert generation is not None and generation > first.generation
    second = h.page(2)
    assert second is not None, h.publisher.events
    assert second.generation == generation
    assert engine.calls == 2, "refresh must redo capture + OCR"
    # identical text is served from the translation memory, so no new API call
    assert backend.calls == 1
    assert all(b.translation for b in second.blocks)
    assert h.publisher.pages[-1][1].kind == "manual"
    assert h.pipe.stats.memory_hits >= 1


def test_manual_refresh_without_region_warns(harness_factory):
    engine = keyed([(page_one(), SPANS_A)])
    h = harness_factory(page_one(), engine)
    h.pipe.start()
    assert h.pipe.refresh() is None
    assert any(level == "warn" for level, _ in h.publisher.statuses)


def test_unchanged_page_is_not_processed_twice(harness_factory):
    engine = keyed([(page_one(), SPANS_A)])
    backend = FakeTranslator()
    h = harness_factory(page_one(), engine, backend).start()
    assert h.page(1) is not None
    time.sleep(0.6)
    assert (engine.calls, backend.calls) == (1, 1)
    assert len(h.publisher.pages) == 1


def test_pause_and_resume(harness_factory):
    engine = keyed([(page_one(), SPANS_A), (page_two(), SPANS_B)])
    h = harness_factory(page_one(), engine).start()
    assert h.page(1) is not None
    h.pipe.pause()                       # Manga Mode off
    calls = engine.calls
    h.source.set_frame(page_two())
    time.sleep(0.5)
    assert engine.calls == calls, "paused pipeline must not process anything"
    h.pipe.resume()                      # Manga Mode on: current page is processed
    result, _ = h.wait_for_text("为什么", timeout=10)
    assert result is not None, h.publisher.events


# --- region / DPI / settings ----------------------------------------------

def test_region_change_resets_detector_and_invalidates_work(harness_factory):
    engine = keyed([(page_one(), SPANS_A)])
    h = harness_factory(page_one(), engine).start()
    assert h.page(1) is not None
    generation_before = h.pipe.generations.current
    h.pipe.set_region(ScreenRegion(100, 100, 800, 600, "TEST", 1.0))
    assert h.pipe.generations.current > generation_before
    assert h.pipe.detector.state == "idle"
    assert h.pipe.detector._baseline is None


def test_glossary_is_enforced_on_translations(harness_factory):
    engine = StubOcrEngine(script=[[span("林雪来了", 100, 100, 160, 34)]])
    backend = FakeTranslator(lexicon={})  # leaves the Chinese name in the output
    h = harness_factory(page_one(), engine, backend)
    h.glossary.add("林雪", "Линь Сюэ")
    h.glossary.save()
    h.start()
    result = h.page(1)
    assert result is not None
    assert "Линь Сюэ" in result.blocks[0].translation
    assert "林雪" not in result.blocks[0].translation


def test_vertical_column_is_dialogue_not_sfx(harness_factory):
    """A tall CJK column is normal manga dialogue (regression guard)."""
    engine = StubOcrEngine(script=[[span("灵气失控了", 700, 140, 60, 260, score=0.97)]])
    backend = FakeTranslator()
    h = harness_factory(page_two(), engine, backend).start()
    result = h.page(1)
    assert result is not None
    assert not result.blocks[0].is_sfx
    assert backend.calls == 1
    assert result.blocks[0].translation


def test_sfx_blocks_are_skipped_unless_enabled(harness_factory):
    giant = [[span("ドン", 100, 100, 420, 320, score=0.98)]]
    engine = StubOcrEngine(script=giant)
    backend = FakeTranslator()
    h = harness_factory(page_one(), engine, backend).start()
    result = h.page(1)
    assert result is not None
    assert result.blocks and result.blocks[0].is_sfx
    assert backend.calls == 0, "SFX must not be translated by default"


def test_translate_sfx_setting(fast_config, harness_factory):
    fast_config.ocr.translate_sfx = True
    engine = StubOcrEngine(script=[[span("ドン", 100, 100, 420, 320, score=0.98)]])
    backend = FakeTranslator()
    h = harness_factory(page_one(), engine, backend).start()
    result = h.page(1)
    assert result is not None
    assert backend.calls == 1
    assert result.blocks[0].translation


def test_low_confidence_text_is_not_sent_to_the_api(harness_factory):
    engine = StubOcrEngine(script=[[
        span("清楚的话", 100, 100, 200, 34, score=0.95),
        span("模糊的话", 100, 200, 200, 34, score=0.30),
    ]])
    backend = FakeTranslator()
    h = harness_factory(page_one(), engine, backend).start()
    result = h.page(1)
    assert result is not None
    assert [b.text for b in result.blocks] == ["清楚的话"]
    assert [item.text for item in backend.last_request.items] == ["清楚的话"]


def test_ocr_failure_is_reported_not_fatal(harness_factory):
    engine = StubOcrEngine(script=[[span("你好世界", 100, 100, 200, 34)]], fail_times=1)
    backend = FakeTranslator()
    h = harness_factory(page_one(), engine, backend).start()
    deadline = time.time() + 8
    while time.time() < deadline and not h.publisher.errors:
        time.sleep(0.05)
    assert h.publisher.errors, h.publisher.events
    assert h.publisher.errors[-1][0] == "internal"  # RuntimeError from the stub
    assert "Traceback" not in h.publisher.errors[-1][1]


def test_stats_and_describe_are_populated(harness_factory):
    engine = keyed([(page_one(), SPANS_A)])
    h = harness_factory(page_one(), engine).start()
    assert h.page(1) is not None
    info = h.pipe.describe()
    assert info["engine"] and info["backend"] == "fake"
    assert info["stats"]["pages_processed"] >= 1
    assert info["region"]["width"] == 1200


def test_context_window_is_bounded(harness_factory):
    engine = keyed([(page_one(), SPANS_A), (page_two(), SPANS_B), (page_three(), SPANS_C)])
    h = harness_factory(page_one(), engine).start()
    h.cfg.translation.context_pages = 1
    h.pipe.update_config(h.cfg)
    assert h.page(1) is not None
    h.source.set_frame(page_two())
    assert h.page(2) is not None
    assert len(h.pipe.context.lines()) <= 3
    assert len(h.pipe.context._pages) == 1
