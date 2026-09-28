"""OCR package: engine factory + the page-level OCR stage."""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ..core.config import OcrSettings
from ..core.logging_setup import get_logger
from ..core.models import Block
from .base import OcrCapabilities, OcrEngine
from .filtering import (
    FilterSettings,
    FilterResult,
    SfxSettings,
    classify_sfx,
    detect_sfx,
    filter_blocks,
    filter_spans,
    looks_like_ui_chrome,
    looks_like_watermark,
)
from .grouping import GroupSettings, group_spans

log = get_logger("ocr")


def create_engine(settings: OcrSettings) -> Tuple[OcrEngine, Optional[str]]:
    """Build the configured OCR engine.

    Returns (engine, warning). Unknown/unavailable engines fall back to the
    bundled offline engine instead of failing, and the warning is surfaced to
    the user as a notification.
    """
    name = (settings.engine or "rapid").strip().lower()
    warning: Optional[str] = None

    if name == "stub":
        from .stub_engine import StubOcrEngine

        return StubOcrEngine(), None

    if name == "paddle":
        from .paddle_engine import PaddleOcrEngine

        if PaddleOcrEngine.available():
            return PaddleOcrEngine(), None
        warning = "PaddleOCR is not installed; using the bundled offline OCR instead."
        log.warning(warning)
        name = "rapid"

    if name not in ("rapid", "auto"):
        warning = f"Unknown OCR engine '{settings.engine}'; using RapidOCR."
        log.warning(warning)

    from .rapid_engine import RapidOcrEngine

    return (
        RapidOcrEngine(
            min_confidence=settings.min_confidence,
            max_side=settings.max_ocr_side,
            vertical_pass=settings.vertical_pass,
        ),
        warning,
    )


def filter_settings(settings: OcrSettings) -> FilterSettings:
    return FilterSettings(
        min_confidence=settings.min_confidence,
        keep_low_confidence=settings.keep_low_confidence,
        drop_without_cjk=settings.drop_blocks_without_cjk,
        simplified=settings.simplified_chinese,
        traditional=settings.traditional_chinese,
    )


def group_settings(settings: OcrSettings) -> GroupSettings:
    return GroupSettings(
        gap_ratio=settings.group_gap_ratio,
        size_ratio=settings.group_size_ratio,
    )


def sfx_settings(settings: OcrSettings) -> SfxSettings:
    return SfxSettings(
        translate_sfx=settings.translate_sfx,
        min_area_ratio=settings.sfx_min_area_ratio,
        min_glyph=settings.sfx_min_glyph,
        median_factor=settings.sfx_median_factor,
        drop_ui_chrome=settings.drop_ui_chrome,
    )


def postprocess_blocks(
    blocks: Sequence[Block], page_size: tuple, settings: OcrSettings,
    counts: Optional[dict] = None,
) -> Tuple[List[Block], List[Block]]:
    """Drop site chrome and single-glyph noise, flag sound effects, split
    translatable from the rest.

    Returns ``(kept_blocks, translatable_blocks)``. When ``counts`` is a dict it is
    filled with *why* blocks disappeared (``chrome``, ``watermark``, ``short``):
    "no text, it was a watermark" is not the same as "no text, our filters may have
    thrown real dialogue away", and only the latter is worth re-reading with a
    vision model.
    """
    sfx = sfx_settings(settings)
    kept: List[Block] = []
    for block in blocks:
        if looks_like_ui_chrome(block, page_size, sfx):
            log.info("dropped site UI text (%.0fx%.0f at top/bottom): %r",
                     block.bbox.w, block.bbox.h, block.text[:40])
            if counts is not None:
                counts["chrome"] = counts.get("chrome", 0) + 1
            continue
        if looks_like_watermark(block, page_size, sfx):
            log.info("dropped a watermark/logo printed on the artwork: %r", block.text[:40])
            if counts is not None:
                counts["watermark"] = counts.get("watermark", 0) + 1
            continue
        kept.append(block)
    kept, short = filter_blocks(kept, filter_settings(settings))
    for block in short:
        log.debug("dropped %d-character block as artwork noise: %r",
                  len(block.text.strip()), block.text[:20])
    if counts is not None and short:
        counts["short"] = counts.get("short", 0) + len(short)
    classify_sfx(kept, page_size, sfx)
    translatable = [b for b in kept if not b.is_sfx and b.text.strip()]
    return kept, translatable


__all__ = [
    "OcrEngine",
    "OcrCapabilities",
    "FilterResult",
    "create_engine",
    "filter_settings",
    "group_settings",
    "sfx_settings",
    "postprocess_blocks",
    "detect_sfx",
    "filter_spans",
    "group_spans",
]
