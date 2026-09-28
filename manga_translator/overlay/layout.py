"""Overlay card layout: wrapping, placement near the original bubble, clamping.

Pure geometry + text metrics passed in as a callable, so the whole layout is
unit-testable without a GUI. All coordinates here are Qt **logical** pixels
relative to the overlay window (i.e. relative to the capture region).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..core.logging_setup import get_logger
from ..core.models import Block, Rect

log = get_logger("overlay.layout")

MeasureFn = Callable[[str], float]  # width of a single line at the card font


@dataclass
class LayoutSettings:
    font_size: float = 13.0
    padding: float = 8.0
    max_card_width: float = 360.0
    min_card_width: float = 90.0
    gap: float = 6.0
    line_spacing: float = 1.18
    max_lines: int = 8
    allow_cover: bool = True          # may cover the original bubble if nothing else fits
    overlap_tolerance: float = 0.22   # share of a card that may overlap another card


@dataclass
class CardLayout:
    block_id: int
    rect: Rect                 # card body in window coordinates
    text_rect: Rect
    lines: List[str]
    anchor: Rect               # original OCR bbox in window coordinates
    font_size: float
    line_height: float
    truncated: bool = False
    translation: str = ""
    confidence: float = 0.0
    direction: str = "horizontal"
    covered: bool = False      # card sits on top of the anchor

    @property
    def is_empty(self) -> bool:
        return not self.lines or self.rect.w <= 1 or self.rect.h <= 1


def wrap_text(text: str, measure: MeasureFn, max_width: float, max_lines: int = 8) -> Tuple[List[str], bool]:
    """Greedy wrap that also breaks long CJK/space-less runs. Explicit newlines
    start a new line (used by the "translation + original" display mode)."""
    paragraphs = [p for p in str(text).replace("\r", "").split("\n")]
    lines: List[str] = []
    truncated = False
    for index, paragraph in enumerate(paragraphs):
        clean = " ".join(paragraph.split())
        if not clean:
            if index and lines:
                lines.append("")
            continue
        wrapped, cut = _wrap_single(clean, measure, max_width)
        lines.extend(wrapped)
        truncated = truncated or cut
        if len(lines) >= max_lines:
            break
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    if truncated and lines:
        lines[-1] = lines[-1].rstrip() + "..."
    return lines, truncated


def _wrap_single(clean: str, measure: MeasureFn, max_width: float) -> Tuple[List[str], bool]:
    if not clean:
        return [], False
    if max_width <= 1:
        return [clean], False

    tokens: List[str] = []
    current = ""
    for ch in clean:
        if ch == " ":
            if current:
                tokens.append(current)
                current = ""
            tokens.append(" ")
            continue
        current += ch
    if current:
        tokens.append(current)

    lines: List[str] = []
    line = ""
    truncated = False
    for token in tokens:
        if token == " ":
            if not line:
                continue
            candidate = line + " "
        else:
            candidate = line + token if line else token
        if measure(candidate) <= max_width or not line:
            if token != " " and measure(candidate) > max_width and not line:
                # Single token wider than the card: hard-split it (CJK has no spaces).
                piece = ""
                for ch in token:
                    if measure(piece + ch) > max_width and piece:
                        lines.append(piece)
                        piece = ch
                    else:
                        piece += ch
                line = piece
                continue
            line = candidate
            continue
        lines.append(line.rstrip())
        line = token if token != " " else ""
        if len(lines) >= max_lines_guard():
            truncated = True
            break
    if line.strip():
        lines.append(line.rstrip())
    return lines, truncated


def max_lines_guard() -> int:
    return 64


def _candidate_rects(anchor: Rect, size: Tuple[float, float], gap: float) -> List[Tuple[Rect, bool]]:
    """Placement candidates in preference order: above, below, beside, on top."""
    w, h = size
    cx = anchor.cx - w / 2.0
    cy = anchor.cy - h / 2.0
    return [
        (Rect(cx, anchor.y - gap - h, w, h), False),          # above the bubble
        (Rect(cx, anchor.y2 + gap, w, h), False),             # below the bubble
        (Rect(anchor.x2 + gap, cy, w, h), False),             # right
        (Rect(anchor.x - gap - w, cy, w, h), False),          # left
        (Rect(cx, anchor.y + max(0.0, (anchor.h - h) / 2.0), w, h), True),  # over it
    ]


def _overlap_ratio(rect: Rect, others: Sequence[Rect]) -> float:
    if rect.area <= 0:
        return 1.0
    worst = 0.0
    for other in others:
        inter = rect.intersection(other)
        if inter is not None:
            worst = max(worst, inter.area / rect.area)
    return worst


def _clamp(rect: Rect, bounds: Rect) -> Rect:
    x = min(max(rect.x, bounds.x), max(bounds.x, bounds.x2 - rect.w))
    y = min(max(rect.y, bounds.y), max(bounds.y, bounds.y2 - rect.h))
    return Rect(x, y, rect.w, rect.h)


def layout_cards(
    blocks: Sequence[Block],
    display_size: Tuple[float, float],
    anchors: Dict[int, Rect],
    measure: MeasureFn,
    settings: LayoutSettings,
) -> List[CardLayout]:
    """Compute card rectangles for one page.

    ``anchors`` maps block id -> OCR bbox already converted to window logical px.
    """
    bounds = Rect(0, 0, float(display_size[0]), float(display_size[1]))
    placed: List[Rect] = []
    cards: List[CardLayout] = []

    for block in blocks:
        text = (block.translation or "").strip()
        if not text:
            continue
        anchor = anchors.get(block.id)
        if anchor is None:
            continue

        target_w = min(
            settings.max_card_width,
            max(settings.min_card_width, max(anchor.w * 1.06, min(anchor.w + 60.0, 240.0))),
        )
        target_w = min(target_w, max(40.0, bounds.w - 2 * settings.gap))
        inner_w = max(20.0, target_w - 2 * settings.padding)
        lines, truncated = wrap_text(text, measure, inner_w, settings.max_lines)
        if not lines:
            continue
        widest = max(measure(line) for line in lines)
        width = min(settings.max_card_width, max(settings.min_card_width, widest + 2 * settings.padding))
        width = min(width, max(40.0, bounds.w - 2 * settings.gap))
        line_height = settings.font_size * settings.line_spacing
        height = len(lines) * line_height + 2 * settings.padding

        best: Optional[Tuple[float, Rect, bool]] = None
        for candidate, covers_hint in _candidate_rects(anchor, (width, height), settings.gap):
            clamped = _clamp(candidate, bounds)
            penalty = _overlap_ratio(clamped, placed)
            if covers_hint:
                penalty += 0.35  # prefer not to hide the artwork
            if penalty <= settings.overlap_tolerance:
                best = (penalty, clamped, covers_hint)
                break
            if best is None or penalty < best[0]:
                best = (penalty, clamped, covers_hint)

        if best is None:
            continue
        penalty, rect, _covers_hint = best
        # "covered" describes the geometry we actually ended up with, not the
        # candidate we picked: clamping may have pushed the card onto the bubble.
        covered = (
            rect.coverage_of(anchor) > 0.4 or anchor.coverage_of(rect) > 0.4
        )
        if covered and not settings.allow_cover:
            continue
        placed.append(rect)
        cards.append(
            CardLayout(
                block_id=block.id,
                rect=rect,
                text_rect=Rect(
                    rect.x + settings.padding,
                    rect.y + settings.padding,
                    rect.w - 2 * settings.padding,
                    rect.h - 2 * settings.padding,
                ),
                lines=lines,
                anchor=anchor,
                font_size=settings.font_size,
                line_height=line_height,
                truncated=truncated,
                translation=text,
                confidence=block.confidence,
                direction=block.direction,
                covered=covered,
            )
        )

    return cards


def cards_bounding_boxes(cards: Sequence[CardLayout], margin: float = 4.0) -> List[Rect]:
    """Card rects (grown a little) — used to mask the overlay out of previews."""
    return [c.rect.expanded(margin) for c in cards]
