"""Refine a predicted text box so it hugs the glyphs, but never makes it worse.

A vision model reads a page well; its boxes are approximate (measured on a real
gateway: 0-1 px error on x, 12-15 px on y). Everything downstream - card placement
and in-place masking alike - wants the box of the *glyphs*.

The trap: blindly unioning "the ink inside the predicted box" can clip the text
when the model's box was slightly small (it then finds only the middle glyphs) or
swallow artwork when the box included a bubble outline. So the refinement is only
accepted when it stays close to the model's box; otherwise the model's box is kept
and merely padded a little. Measured on real pages: accepting everything made
placement worse, accepting only small corrections made it better.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import cv2
import numpy as np

Box = Tuple[float, float, float, float]   # x, y, w, h

# A candidate must keep most of the model's box (no clipped text) and must not
# blow up beyond it (no swallowed artwork) - otherwise it is rejected.
MIN_AREA_RATIO = 0.60
MAX_AREA_RATIO = 1.15
MAX_GROWTH = 1.25          # per dimension, relative to the model's box
MIN_IOU = 0.55             # the refined box must stay on the predicted text


def _iou(first: Box, second: Box) -> float:
    """Intersection over union - catches a refinement that *moved* sideways."""
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[0] + first[2], second[0] + second[2])
    y2 = min(first[1] + first[3], second[1] + second[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    intersection = (x2 - x1) * (y2 - y1)
    union = _area(first) + _area(second) - intersection
    return intersection / union if union > 0 else 0.0


def _clip_box(box: Box, width: int, height: int) -> Optional[Box]:
    x, y, w, h = (float(v) for v in box)
    if w <= 0 or h <= 0:
        return None
    x1 = max(0, int(round(x)))
    y1 = max(0, int(round(y)))
    x2 = min(width, int(round(x + w)))
    y2 = min(height, int(round(y + h)))
    if x2 - x1 < 3 or y2 - y1 < 3:
        return None
    return float(x1), float(y1), float(x2 - x1), float(y2 - y1)


def _area(box: Box) -> float:
    return max(0.0, box[2]) * max(0.0, box[3])


def _intersects(a: Box, b: Box, tolerance: float = 0.0) -> bool:
    return not (a[0] + a[2] + tolerance < b[0] or b[0] + b[2] + tolerance < a[0]
                or a[1] + a[3] + tolerance < b[1] or b[1] + b[3] + tolerance < a[1])


def ink_mask(region_bgr: np.ndarray) -> np.ndarray:
    """Binary mask of the glyph pixels in a region (Otsu, minority class = ink)."""
    gray = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2GRAY) if region_bgr.ndim == 3 else region_bgr
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _threshold, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = int(np.count_nonzero(binary == 0))
    light = int(binary.size - dark)
    mask = np.where(binary == 0, 255, 0).astype(np.uint8) if dark <= light \
        else np.where(binary == 255, 255, 0).astype(np.uint8)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))


def _ink_union(image_bgr: np.ndarray, box: Box) -> Optional[Box]:
    """Union bbox of the text-like components whose bbox touches ``box``."""
    x, y, w, h = box
    x1, y1, x2, y2 = int(x), int(y), int(round(x + w)), int(round(y + h))
    region = image_bgr[y1:y2, x1:x2]
    if region.size == 0:
        return None
    mask = ink_mask(region)
    region_h, region_w = region.shape[:2]
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep: List[Tuple[int, int, int, int]] = []
    for index in range(1, count):
        cx, cy, cw, ch, area = (int(v) for v in stats[index])
        if area < 6 or ch < 4:
            continue
        if ch > region_h * 0.95 or cw / max(1.0, float(ch)) > 14.0:
            continue
        if area / max(1.0, float(cw * ch)) < 0.08:
            continue
        keep.append((cx, cy, cw, ch))
    if not keep:
        return None
    ux1 = min(c[0] for c in keep)
    uy1 = min(c[1] for c in keep)
    ux2 = max(c[0] + c[2] for c in keep)
    uy2 = max(c[1] + c[3] for c in keep)
    return float(x1 + ux1), float(y1 + uy1), float(ux2 - ux1), float(uy2 - uy1)


def snap_box(image_bgr: np.ndarray, box: Box, expand: float = 0.14,
             max_expand_px: float = 40.0, pad_ratio: float = 0.07,
             pad_px: float = 5.0) -> Optional[Box]:
    """Tighten ``box`` onto its glyphs; ``None`` when nothing better is on offer.

    The search window is the model's box grown by ``expand`` (so a slightly small
    box can still capture the first and last glyph), but only ink components that
    touch the model's box are used, so a neighbouring bubble cannot be absorbed.
    The candidate is rejected when it shrinks or grows the box too much.
    """
    height, width = image_bgr.shape[:2]
    model = _clip_box(box, width, height)
    if model is None:
        return None
    mx, my, mw, mh = model
    grow_x = min(max_expand_px, mw * expand)
    grow_y = min(max_expand_px, mh * expand)
    window = _clip_box((mx - grow_x, my - grow_y, mw + 2 * grow_x, mh + 2 * grow_y),
                       width, height)
    if window is None:
        return None

    union = _ink_union(image_bgr, window)
    if union is None:
        return None
    # Drop ink that does not belong to this box (a neighbouring bubble's glyphs).
    ux, uy, uw, uh = union
    kept: List[Box] = []
    x1, y1, x2, y2 = int(ux), int(uy), int(round(ux + uw)), int(round(uy + uh))
    region = image_bgr[y1:y2, x1:x2]
    if region.size == 0:
        return None
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        ink_mask(region), connectivity=8)
    for index in range(1, count):
        cx, cy, cw, ch, area = (int(v) for v in stats[index])
        if area < 6 or ch < 4:
            continue
        component = (float(x1 + cx), float(y1 + cy), float(cw), float(ch))
        if _intersects(component, model, tolerance=4.0):
            kept.append(component)
    if not kept:
        return None
    candidate = (
        min(c[0] for c in kept),
        min(c[1] for c in kept),
        max(c[0] + c[2] for c in kept) - min(c[0] for c in kept),
        max(c[1] + c[3] for c in kept) - min(c[1] for c in kept),
    )
    model_area = _area(model)
    ratio = _area(candidate) / model_area if model_area else 0.0
    if not (MIN_AREA_RATIO <= ratio <= MAX_AREA_RATIO):
        return None
    if candidate[2] > mw * MAX_GROWTH or candidate[3] > mh * MAX_GROWTH:
        return None
    # A refinement of the right *size* can still sit in the wrong place (ink from a
    # neighbouring element of the same size). Measured on a real page: an accepted
    # sideways shift put the box ~80 px off the glyphs, which is invisible for a
    # card but ruins the in-place mask.
    if _iou(candidate, model) < MIN_IOU:
        return None
    # Pad so glyph edges and antialiasing stay inside the box.
    pad_x = max(pad_px, candidate[2] * pad_ratio)
    pad_y = max(pad_px, candidate[3] * pad_ratio)
    return _clip_box((candidate[0] - pad_x, candidate[1] - pad_y,
                      candidate[2] + 2 * pad_x, candidate[3] + 2 * pad_y), width, height)


def snap_boxes(image_bgr: np.ndarray, boxes: list) -> list:
    """Apply :func:`snap_box` to a list of boxes, keeping the original on failure."""
    out = []
    for box in boxes:
        snapped = snap_box(image_bgr, box)
        out.append(snapped if snapped is not None else tuple(float(v) for v in box))
    return out
