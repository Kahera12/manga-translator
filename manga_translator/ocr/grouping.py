"""Group OCR lines into dialogue blocks + order them for a translation request.

MVP goal (per spec §7): several OCR lines that belong to one visible dialogue
block must become ONE translation block, while bubbles stay separate. Component
boxes are preserved on the block for future layout work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from ..core.logging_setup import get_logger
from ..core.models import Block, Rect, TextSpan
from .filtering import cjk_count

log = get_logger("ocr.grouping")


@dataclass
class GroupSettings:
    gap_ratio: float = 0.85          # gap between boxes, in units of line height
    same_line_gap_ratio: float = 0.55
    size_ratio: float = 0.55         # max relative glyph-height difference
    overlap_ratio: float = 0.35      # min 1-D overlap to be considered aligned
    iou_merge: float = 0.18
    max_block_chars: int = 400
    max_block_area_ratio: float = 0.75  # never merge across the whole page
    right_to_left: bool = True       # manga reading order for vertical columns


def _overlap_1d(a1: float, a2: float, b1: float, b2: float) -> float:
    inter = min(a2, b2) - max(a1, b1)
    if inter <= 0:
        return 0.0
    shortest = min(a2 - a1, b2 - b1)
    return inter / shortest if shortest > 0 else 0.0


def _iou(a: Rect, b: Rect) -> float:
    inter = a.intersection(b)
    if inter is None:
        return 0.0
    union = a.area + b.area - inter.area
    return inter.area / union if union > 0 else 0.0


def _gap_x(a: Rect, b: Rect) -> float:
    if a.x2 < b.x:
        return b.x - a.x2
    if b.x2 < a.x:
        return a.x - b.x2
    return 0.0


def _gap_y(a: Rect, b: Rect) -> float:
    if a.y2 < b.y:
        return b.y - a.y2
    if b.y2 < a.y:
        return a.y - b.y2
    return 0.0


def _size_similar(a: Rect, b: Rect, ratio: float) -> bool:
    ha, hb = max(1.0, a.h), max(1.0, b.h)
    return abs(ha - hb) / max(ha, hb) <= ratio


def should_merge(a: TextSpan, b: TextSpan, s: GroupSettings, page_area: float = 0.0) -> bool:
    ba, bb = a.bbox, b.bbox
    if ba.area <= 0 or bb.area <= 0:
        return False
    union = ba.union(bb)
    if page_area > 0 and union.area / page_area > s.max_block_area_ratio:
        return False

    if _iou(ba, bb) >= s.iou_merge:
        return True

    v_overlap = _overlap_1d(ba.y, ba.y2, bb.y, bb.y2)
    h_overlap = _overlap_1d(ba.x, ba.x2, bb.x, bb.x2)
    line_h = min(ba.h, bb.h)

    # Same visual line split into fragments (e.g. a word chopped by a balloon edge).
    if v_overlap >= 0.55 and _gap_x(ba, bb) <= s.same_line_gap_ratio * line_h:
        return True

    # Stacked lines of one bubble (horizontal text): aligned horizontally,
    # similar glyph height, close vertically.
    if (
        h_overlap >= s.overlap_ratio
        and _size_similar(ba, bb, s.size_ratio)
        and _gap_y(ba, bb) <= s.gap_ratio * line_h
    ):
        return True

    # Side-by-side CJK columns (vertical text): near-equal heights, small
    # horizontal gap, strong vertical overlap.
    if (
        v_overlap >= s.overlap_ratio
        and abs(ba.h - bb.h) / max(ba.h, bb.h) <= 0.45
        and _gap_x(ba, bb) <= s.gap_ratio * max(8.0, min(ba.w, bb.w))
    ):
        return True

    return False


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[rj] = ri


def _join_text(spans: Sequence[TextSpan]) -> str:
    parts = [s.text.strip() for s in spans if s.text.strip()]
    out = ""
    for part in parts:
        if not out:
            out = part
            continue
        prev = out[-1]
        if cjk_count(prev) and cjk_count(part[0]):
            out += part          # CJK needs no separator
        else:
            out += " " + part
    return out


def _order_spans(spans: Sequence[TextSpan], vertical: bool, rtl: bool) -> List[TextSpan]:
    if vertical:
        # columns right-to-left, characters top-to-bottom
        return sorted(spans, key=lambda s: (-round(s.bbox.cx / 8.0), s.bbox.y) if rtl
                      else (round(s.bbox.cx / 8.0), s.bbox.y))
    # rows top-to-bottom, fragments left-to-right
    return sorted(spans, key=lambda s: (round(s.bbox.cy / 8.0), s.bbox.x))


def group_spans(
    spans: Sequence[TextSpan],
    settings: Optional[GroupSettings] = None,
    page_area: float = 0.0,
) -> List[Block]:
    s = settings or GroupSettings()
    spans = [sp for sp in spans if sp.text.strip()]
    if not spans:
        return []

    uf = _UnionFind(len(spans))
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            if should_merge(spans[i], spans[j], s, page_area):
                uf.union(i, j)

    groups: Dict[int, List[TextSpan]] = {}
    for idx, span in enumerate(spans):
        groups.setdefault(uf.find(idx), []).append(span)

    blocks: List[Block] = []
    for members in groups.values():
        boxes = [m.bbox for m in members]
        bbox = boxes[0]
        for other in boxes[1:]:
            bbox = bbox.union(other)
        vertical_votes = sum(1 for m in members if m.is_vertical)
        tall = bbox.h > bbox.w * 1.2
        vertical = bool(tall and vertical_votes > 0 and (vertical_votes * 2 >= len(members)))
        ordered = _order_spans(members, vertical, s.right_to_left)
        text = _join_text(ordered)
        if len(text) > s.max_block_chars:
            text = text[: s.max_block_chars]
        confidence = sum(m.confidence for m in members) / len(members)
        blocks.append(
            Block(
                id=0,
                text=text,
                bbox=bbox,
                spans=list(ordered),
                direction="vertical" if vertical else "horizontal",
                confidence=confidence,
            )
        )

    _assign_reading_order(blocks, rtl=s.right_to_left)
    return [b for b in blocks if b.text.strip()]


def _assign_reading_order(blocks: List[Block], rtl: bool = True) -> None:
    """Manga reading order: vertical columns right-to-left, otherwise rows."""
    if not blocks:
        return
    vertical_share = sum(1 for b in blocks if b.direction == "vertical") / len(blocks)
    if vertical_share >= 0.5:
        ordered = sorted(blocks, key=lambda b: (-b.bbox.x if rtl else b.bbox.x, b.bbox.y))
    else:
        rows: List[List[Block]] = []
        for block in sorted(blocks, key=lambda b: b.bbox.cy):
            placed = False
            for row in rows:
                ref = row[-1].bbox
                overlap = _overlap_1d(ref.y, ref.y2, block.bbox.y, block.bbox.y2)
                if overlap >= 0.35:
                    row.append(block)
                    placed = True
                    break
            if not placed:
                rows.append([block])
        ordered = []
        for row in rows:
            ordered.extend(sorted(row, key=lambda b: (-b.bbox.x if rtl else b.bbox.x)))
    for i, block in enumerate(ordered, start=1):
        block.id = i
