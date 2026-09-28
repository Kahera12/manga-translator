"""Change detection, debounce/stabilization and image similarity."""
from __future__ import annotations

import numpy as np

from conftest import blank_page, page_one, page_two
from manga_translator.capture.screen_capture import downscale_gray
from manga_translator.core.models import Rect
from manga_translator.detection.image_similarity import (
    SimilarityConfig,
    change_score,
    hamming_hex,
    phash_hex,
)
from manga_translator.detection.page_change import DetectorSettings, PageChangeDetector


def _preview(page: np.ndarray) -> np.ndarray:
    return downscale_gray(page, 240)


def test_identical_frames_score_zero():
    frame = _preview(page_one())
    report = change_score(frame, frame.copy())
    assert report.changed_fraction == 0.0
    assert report.score < 0.01
    assert report.hamming == 0


def test_page_turn_is_detected():
    a, b = _preview(page_one()), _preview(page_two())
    report = change_score(a, b)
    assert report.score >= 0.075, report
    assert report.changed_fraction > 0.1


def test_cursor_sized_change_is_ignored():
    """A mouse cursor / hover highlight must never look like a new page."""
    base = page_one()
    moved = base.copy()
    moved[420:436, 600:616] = 0  # 16x16 dark blob
    report = change_score(_preview(base), _preview(moved))
    assert report.score < 0.075, report


def test_phash_survives_scaling_and_antialiasing():
    page = page_one()

    tiny_offset = np.roll(page, 2, axis=1)
    h1 = phash_hex(downscale_gray(page, 240))
    h2 = phash_hex(downscale_gray(tiny_offset, 240))
    assert hamming_hex(h1, h2) <= 6
    other = phash_hex(downscale_gray(page_two(), 240))
    assert hamming_hex(h1, other) > 6


def test_masked_overlay_area_is_ignored():
    """Changes under our own overlay cards must not trigger a page change."""
    base = page_one()
    with_overlay = base.copy()
    # A big blob exactly where a Russian card would be drawn (preview space mask).
    with_overlay[90:410, 90:510] = 0
    mask = [Rect(15, 15, 95, 75)]  # page px 75..475 x 75..375 -> preview px
    report = change_score(_preview(base), _preview(with_overlay), masks=mask)
    assert report.score < 0.075, report
    assert report.mask_coverage > 0.02
    # Without the mask the same change is a clear trigger.
    unmasked = change_score(_preview(base), _preview(with_overlay))
    assert unmasked.score >= 0.075


def test_detector_debounces_scrolling_and_waits_for_stability():
    """Scenario D: intermediate scroll frames must not produce pages."""
    settings = DetectorSettings(
        change_threshold=0.075, stable_duration_ms=350, max_wait_ms=4000
    )
    det = PageChangeDetector(settings)
    a, b, c = _preview(page_one()), _preview(page_two()), _preview(blank_page())

    now = 0.0
    assert det.feed(a, now=now) is None          # first frame -> CHANGING
    now += 100
    assert det.feed(a, now=now) is None          # stabilising
    now += 400
    first = det.feed(a, now=now)                 # stable long enough -> page
    assert first is not None and first.kind == "first"

    # A scroll: many different frames, no stable window long enough.
    events = []
    for i, frame in enumerate([b, c, a, b, c, a, b]):
        now += 100
        events.append(det.feed(frame, now=now))
    assert all(e is None for e in events), "intermediate scroll frames must be ignored"

    # Scrolling stops on page C: after stable_duration_ms one page is produced.
    event = None
    for _ in range(6):
        now += 100
        event = det.feed(c, now=now)
        if event is not None:
            break
    assert event is not None
    assert event.kind == "changed"
    assert event.peak_score >= settings.change_threshold
    assert event.report.score < settings.change_threshold  # stabilized frame
    assert event.stable_for_ms >= settings.stable_duration_ms


def test_detector_forced_event_when_page_never_settles():
    settings = DetectorSettings(change_threshold=0.05, stable_duration_ms=300, max_wait_ms=500)
    det = PageChangeDetector(settings)
    a = _preview(page_one())
    b = _preview(page_two())
    now = 0.0
    det.feed(a, now=now)
    events = []
    for i in range(12):
        now += 100
        events.append(det.feed(b if i % 2 == 0 else a, now=now))
    kinds = [e.kind for e in events if e is not None]
    assert "forced" in kinds, kinds


def test_similarity_config_thresholds_are_configurable():
    a, b = _preview(page_one()), _preview(page_two())
    report = change_score(a, b, SimilarityConfig(mad_weight=0.0, phash_weight=0.0))
    # With the helper components disabled only the changed-pixel fraction remains.
    assert abs(report.score - report.changed_fraction) < 1e-9
