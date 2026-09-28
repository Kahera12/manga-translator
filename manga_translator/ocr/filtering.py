"""Confidence filtering + sound-effect (SFX) heuristics.

Manga OCR is noisy, so low-confidence text is *kept for the debug overlay* but
never translated, and obvious decorative SFX is skipped unless
``TRANSLATE_SFX=true``.

Two rules are page-relative rather than absolute, because webtoons (and zoomed
readers) draw everything large:

* SFX = much bigger glyphs than *this page's* own text (plus an absolute and a
  region-share floor), so a page whose only text is the big impact word is still
  translated.
* Site chrome (titles, breadcrumbs, "next chapter" bars) is dropped: single wide
  short lines pinned to the top/bottom edge of the capture.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

from ..core.models import Block, TextSpan

# Text burned into the artwork by the reading site itself. Kept short and explicit:
# a wrong pattern here silently deletes dialogue, so only brand/promo strings that
# a speech bubble cannot plausibly contain are listed. Notably absent: the bare
# "快看" ("look!") and "看漫画" ("read comics"), which are ordinary Chinese and were
# dropped from real lines ("快看那边！") in testing.
WATERMARK_PATTERNS = (
    "快看漫画", "腾讯动漫", "哔哩哔哩", "bilibili", "manhuagui", "copymanga",
    "colamanga", "dmzj", "www.", "http", "©",
)


def is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x30FF  # kana (rare in CN, harmless)
        or 0xFF01 <= code <= 0xFF60  # fullwidth punctuation
    )


def cjk_count(text: str) -> int:
    return sum(1 for ch in text if is_cjk(ch) and not _is_fullwidth_punct(ch))


def _is_fullwidth_punct(ch: str) -> bool:
    return ch in "，。！？：；、“”‘’《》（）—…·「」『』【】"


def cjk_ratio(text: str) -> float:
    stripped = "".join(ch for ch in text if not ch.isspace())
    if not stripped:
        return 0.0
    return cjk_count(text) / float(len(stripped))


@dataclass
class FilterResult:
    accepted: List[TextSpan] = field(default_factory=list)
    low_confidence: List[TextSpan] = field(default_factory=list)
    non_cjk: List[TextSpan] = field(default_factory=list)
    empty: List[TextSpan] = field(default_factory=list)

    @property
    def dropped(self) -> List[TextSpan]:
        return self.low_confidence + self.non_cjk + self.empty


@dataclass
class FilterSettings:
    min_confidence: float = 0.55
    keep_low_confidence: bool = True
    drop_without_cjk: bool = True
    simplified: bool = True
    traditional: bool = True
    min_chars: int = 1
    min_block_chars: int = 2   # single characters are almost always art noise


def filter_blocks(blocks: Sequence[Block], settings: FilterSettings) -> Tuple[List[Block], List[Block]]:
    """Split blocks into (kept, dropped): single-glyph noise is dropped.

    Stylised artwork constantly yields one-character detections ("口", "十") that
    translate into nonsense; real speech bubbles carry at least two characters.
    """
    kept: List[Block] = []
    dropped: List[Block] = []
    for block in blocks:
        text = (block.text or "").strip()
        if len(text) < max(1, settings.min_block_chars):
            dropped.append(block)
            continue
        kept.append(block)
    return kept, dropped


# Characters that only exist in Traditional Chinese - used for the "traditional
# enabled" gate and for telling the translator which flavour it is looking at.
_TRADITIONAL_MARKERS = set(
    "個們來說話這裡麼樣體聽見錯過實現經濟藝術嚴麼顯傳統雙國際觀點難認識讓車馬鳥龍門風雲飛開關間聞長期實務處鄉園員圓當盡極頭顏顧"
)


def filter_spans(spans: Sequence[TextSpan], settings: FilterSettings) -> FilterResult:
    result = FilterResult()
    for span in spans:
        text = (span.text or "").strip()
        if len(text) < settings.min_chars:
            result.empty.append(span)
            continue
        has_cjk = cjk_count(text) > 0
        if settings.drop_without_cjk and not has_cjk:
            result.non_cjk.append(span)
            continue
        if not settings.simplified and not settings.traditional:
            result.non_cjk.append(span)
            continue
        if span.confidence < settings.min_confidence:
            if settings.keep_low_confidence:
                result.low_confidence.append(span)
            continue
        result.accepted.append(span)
    return result


@dataclass
class SfxSettings:
    translate_sfx: bool = False
    min_glyph: float = 90.0           # absolute glyph-size floor (physical px)
    median_factor: float = 2.2        # SFX is much bigger than the page's own text
    min_glyph_ratio: float = 0.055    # …and a sizeable share of the region height
    min_area_ratio: float = 0.16      # block area / page area
    max_aspect: float = 6.0           # very irregular box
    min_tilt_deg: float = 22.0        # rotated lettering
    low_confidence: float = 0.62
    # site chrome (page title, breadcrumbs, "next chapter" bars)
    drop_ui_chrome: bool = True
    chrome_aspect: float = 8.0        # single wide line …
    chrome_max_height_ratio: float = 0.035
    chrome_edge_ratio: float = 0.06   # … pinned to the top/bottom edge
    # watermarks/logos burned into the artwork itself (see looks_like_watermark)
    drop_watermarks: bool = True


def traditional_share(text: str) -> float:
    letters = [ch for ch in text if is_cjk(ch)]
    if not letters:
        return 0.0
    return sum(1 for ch in letters if ch in _TRADITIONAL_MARKERS) / float(len(letters))


def glyph_size(block: Block) -> float:
    """Approximate glyph height: a long CJK column is dialogue, not SFX.

    ``max(w, h) / characters`` gives the size of a single character for both
    horizontal lines and vertical columns.
    """
    count = max(1, cjk_count(block.text) or len(block.text.strip()) or 1)
    return max(block.bbox.w, block.bbox.h) / float(count)


def page_median_glyph(blocks: Sequence[Block]) -> float:
    sizes = sorted(glyph_size(b) for b in blocks if b.text.strip())
    if not sizes:
        return 0.0
    middle = len(sizes) // 2
    if len(sizes) % 2:
        return sizes[middle]
    return (sizes[middle - 1] + sizes[middle]) / 2.0


def looks_like_ui_chrome(block: Block, page_size: tuple, settings: SfxSettings) -> bool:
    """Reader/site furniture rather than manga text.

    Page titles, breadcrumbs and "next chapter" bars are single, very wide and
    short lines pinned to the top or bottom edge of the captured region; they are
    not dialogue and must never be translated.
    """
    if not settings.drop_ui_chrome:
        return False
    height = float(page_size[1] or 0)
    if height <= 0:
        return False
    bbox = block.bbox
    if bbox.h <= 0 or bbox.h > height * settings.chrome_max_height_ratio:
        return False
    if bbox.w / bbox.h < settings.chrome_aspect:
        return False
    if not (bbox.y <= height * settings.chrome_edge_ratio
            or bbox.y2 >= height * (1.0 - settings.chrome_edge_ratio)):
        return False
    text = block.text
    latin = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    marks = sum(1 for ch in text if ch in "|｜·-–—_:：/\\()[]{}<>")
    return (latin + marks) >= 2 or len(text) >= 12


def looks_like_watermark(block: Block, page_size: tuple, settings: SfxSettings) -> bool:
    """Watermarks, site logos and reader furniture printed *on* the artwork.

    The tested site burns "快看！快看漫画" into panel images at the left edge, and the
    OCR happily reads it as dialogue ("Скорее! Смотри комикс!"). Unlike the UI-chrome
    rule this one looks at the text itself, because a watermark is not wide-and-short
    and often sits mid-page.

    Deliberately conservative: it only fires on explicit site/promo patterns, or on
    a short mostly-ASCII/latin string hugging the very edge of the page. A real
    speech bubble never matches, so no dialogue is lost to this rule.
    """
    if not getattr(settings, "drop_watermarks", True):
        return False
    text = " ".join((block.text or "").split())
    if not text:
        return False
    lowered = text.lower()
    for pattern in WATERMARK_PATTERNS:
        if pattern in text or pattern in lowered:
            return True
    width, height = float(page_size[0] or 0), float(page_size[1] or 0)
    if width <= 0 or height <= 0:
        return False
    bbox = block.bbox
    # A short latin/digit string (URL, page number, "©2024 site") pinned to an edge.
    latin = sum(1 for ch in text if ch.isascii() and (ch.isalnum() or ch in ".:/©-"))
    if len(text) <= 24 and latin / max(1, len(text)) >= 0.6:
        return (bbox.x <= width * 0.02 or bbox.x2 >= width * 0.98
                or bbox.y <= height * 0.02 or bbox.y2 >= height * 0.98)
    return False


def sfx_threshold(blocks: Sequence[Block], page_size: tuple, settings: SfxSettings) -> float:
    """Page-relative SFX size threshold.

    An absolute "giant glyph = SFX" rule breaks on webtoons, where the whole page
    is drawn large: a page whose only text *is* the big impact word must still be
    translated. SFX is therefore "much bigger than this page's own text", with an
    absolute floor and a share-of-region floor.

    With a single block there is no distribution to compare against, so a much
    higher bar is used (1.5x the floor and at least 10% of the region height):
    a lone big line is dialogue unless it is really enormous.
    """
    height = float(page_size[1] or 0)
    floor = max(settings.min_glyph, height * settings.min_glyph_ratio)
    median = page_median_glyph(blocks)
    if len(blocks) >= 2 and median > 0:
        return max(floor, median * settings.median_factor)
    return max(floor * 1.5, height * 0.10)


def classify_sfx(blocks: Sequence[Block], page_size: tuple, settings: SfxSettings) -> None:
    """Mark sound effects using the page-relative threshold."""
    if settings.translate_sfx:
        for block in blocks:
            block.is_sfx = False
        return
    threshold = sfx_threshold(blocks, page_size, settings)
    for block in blocks:
        block.is_sfx = bool(sfx_reason(block, page_size, settings, threshold=threshold))


def sfx_reason(block: Block, page_size, settings: SfxSettings, threshold: float = 0.0) -> str:
    """Which heuristic fired (empty string = normal dialogue). Used by the log
    and the debug HUD to tune the thresholds."""
    if settings.translate_sfx:
        return ""
    width = float(page_size[0] or 0)
    height = float(page_size[1] or 0)
    page_area = width * height
    text = block.text.strip()
    count = max(1, cjk_count(text) or len(text) or 1)
    bbox = block.bbox
    size = glyph_size(block)
    if not threshold:
        threshold = max(settings.min_glyph, height * settings.min_glyph_ratio)

    if size >= threshold and count <= 8:
        return f"giant glyphs ({size:.0f}px >= {threshold:.0f}px)"
    if page_area > 0 and bbox.area / page_area >= settings.min_area_ratio and count <= 12:
        return f"huge box ({bbox.area / page_area * 100:.0f}% of the page)"
    long_side = max(bbox.w, bbox.h)
    short_side = max(1.0, min(bbox.w, bbox.h))
    if (
        count <= 2
        and long_side / short_side >= settings.max_aspect
        and size >= 0.6 * threshold
    ):
        return "extreme aspect ratio"
    tilts = [abs(s.angle) for s in block.spans] or [0.0]
    if max(tilts) >= settings.min_tilt_deg and count <= 6:
        return f"rotated lettering ({max(tilts):.0f} deg)"
    if block.confidence and block.confidence < settings.low_confidence and count <= 3:
        return f"short and low confidence ({block.confidence:.2f})"
    return ""


class StaticTextTracker:
    """Detects site furniture that never moves (language menus, buttons, badges).

    Webtoon sites draw their own UI *inside* the captured area. Such text keeps
    appearing at the same spot while the comic scrolls underneath, so it is
    recognised after a couple of pages and dropped.

    Matching uses the position (quantised to a few pixels) *plus* text similarity,
    because OCR splits the same widget differently between frames ("英语中文 (简体)"
    vs "英语" + "中文 (简体)"). Two *different* dialogue lines at the same spot do
    not match, so real text is never dropped by this rule.
    """

    def __init__(self, hits_required: int = 2, position_tolerance: int = 14,
                 edge_ratio: float = 0.12, max_entries: int = 400):
        self.hits_required = max(2, int(hits_required))
        self.tolerance = max(1, int(position_tolerance))
        self.edge_ratio = float(edge_ratio)
        self.max_entries = max(16, int(max_entries))
        self._seen: dict = {}
        self._last_hash = ""

    @staticmethod
    def similar(first: str, second: str) -> bool:
        """True when one text is (nearly) a piece of the other."""
        if not first or not second:
            return False
        if first in second or second in first:
            return True
        common = sum(1 for ch in set(first) if ch in second)
        return common >= 2 and common / max(1, min(len(set(first)), len(set(second)))) >= 0.6

    def _near_edge(self, block: Block, page_size: tuple) -> bool:
        width, height = float(page_size[0] or 0), float(page_size[1] or 0)
        if width <= 0 or height <= 0:
            return True
        bbox = block.bbox
        return (
            bbox.x <= width * self.edge_ratio
            or bbox.x2 >= width * (1.0 - self.edge_ratio)
            or bbox.y <= height * self.edge_ratio
            or bbox.y2 >= height * (1.0 - self.edge_ratio)
        )

    def observed(self, blocks: Sequence[Block], page_size: tuple = (0, 0),
                 page_hash: str = "", moved_bits: int = 0) -> List[int]:
        """Feed one page; returns the ids of blocks that look like static UI.

        Two guards keep real dialogue safe:

        * only text in the outer 12% of the region is considered (site menus and
          badges live in corners/edges; bubbles live in the middle), and
        * counting only happens when the page content really moved (the hash
          changed a lot), so a bubble that is still on screen after a spurious
          trigger is not mistaken for furniture.
        """
        content_moved = moved_bits >= 10 or not self._last_hash
        self._last_hash = page_hash or self._last_hash

        slots: dict = {}
        for block in blocks:
            text = " ".join((block.text or "").split())
            if len(text) < 2 or len(text) > 40 or not self._near_edge(block, page_size):
                continue
            key = (int(round(block.bbox.x / self.tolerance)),
                   int(round(block.bbox.y / self.tolerance)))
            slots.setdefault(key, []).append((block.id, text))

        static: List[int] = []
        updated: dict = {}
        for key, entries in slots.items():
            texts = [text for _id, text in entries]
            previous, count = self._seen.get(key, ((), 0))
            if content_moved and previous and any(
                self.similar(text, old) for text in texts for old in previous
            ):
                count += 1
                if count >= self.hits_required:
                    static.extend(block_id for block_id, _text in entries)
            elif not content_moved:
                count = count or 1
            else:
                count = 1
            updated[key] = (texts, count)
        self._seen = updated if len(updated) <= self.max_entries else dict(
            list(updated.items())[-self.max_entries:]
        )
        return static

    def reset(self) -> None:
        self._seen.clear()
        self._last_hash = ""


def is_static_ui(block: Block, page_size: tuple, settings: SfxSettings) -> bool:
    return looks_like_ui_chrome(block, page_size, settings)


def detect_sfx(block: Block, page_size=None, settings: SfxSettings = None, page_area=None) -> bool:
    """Single-block SFX check for callers that have no page context.

    Accepts either a ``(width, height)`` page size or a page area (legacy call).
    """
    settings = settings or SfxSettings()
    if page_size is None and page_area is not None:
        page_size = page_area
    if page_size is None:
        page_size = (0, 0)
    if isinstance(page_size, (int, float)):
        side = int(math.sqrt(max(1.0, float(page_size))))
        page_size = (side, side)
    return bool(sfx_reason(block, page_size, settings))
