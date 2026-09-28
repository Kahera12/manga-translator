"""Cheap image similarity: change score for page detection + pHash page identity.

Three cheap signals are combined so no single failure mode dominates:

* ``changed_fraction`` – share of preview pixels that differ a lot. Kills
  cursor moves / hover highlights / 1px scrollbar nudges, fires on page turns.
* ``mad`` – mean absolute difference, catches low-contrast global changes
  (fades, a page sliding in).
* ``phash`` – 64-bit perceptual hash distance, robust to tiny scroll offsets,
  anti-aliasing and scaling. Also used as the cache key.

All of them ignore masked rectangles (the area covered by our own overlay
cards) so the app never reacts to itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

from ..core.models import Rect

_HASH_SIZE = 8
_HIGH_FREQ = 4


@dataclass
class SimilarityConfig:
    pixel_threshold: int = 28     # per-pixel difference considered "changed"
    mad_weight: float = 0.35      # weight of the mean-difference component
    phash_weight: float = 0.25    # weight of the perceptual-hash component
    mad_scale: float = 0.12       # mad value mapped to 1.0
    phash_scale: float = 20.0     # hamming distance mapped to 1.0
    mask_merge_gap: int = 4       # grow masks a little so edges are covered


@dataclass
class ChangeReport:
    changed_fraction: float
    mad: float
    hamming: int
    score: float
    mask_coverage: float
    hash_a: str = ""
    hash_b: str = ""

    def over_threshold(self, threshold: float) -> bool:
        return self.score >= threshold


def phash_bits(gray: np.ndarray, hash_size: int = _HASH_SIZE) -> np.ndarray:
    """64-bit perceptual hash of a grayscale image as a bool array."""
    import cv2

    size = hash_size * _HIGH_FREQ
    resized = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)
    grey = resized.astype("float32")
    dct = cv2.dct(grey)
    low = dct[:hash_size, :hash_size]
    # median of the low-frequency band (excluding DC) -> robust threshold
    flat = low.flatten()[1:] if low.size > 1 else low.flatten()
    median = np.median(flat)
    return (low > median).flatten()


def phash_hex(gray: np.ndarray) -> str:
    bits = phash_bits(gray)
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def hamming_hex(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def _mask_array(shape: Tuple[int, int], masks: Sequence[Rect], gap: int = 4) -> Optional[np.ndarray]:
    if not masks:
        return None
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)
    for rect in masks:
        x1 = int(max(0, rect.x - gap))
        y1 = int(max(0, rect.y - gap))
        x2 = int(min(w, rect.x2 + gap))
        y2 = int(min(h, rect.y2 + gap))
        if x2 > x1 and y2 > y1:
            mask[y1:y2, x1:x2] = True
    return mask if mask.any() else None


def change_score(
    prev: np.ndarray,
    cur: np.ndarray,
    cfg: Optional[SimilarityConfig] = None,
    masks: Sequence[Rect] = (),
    compute_hash: bool = False,
) -> ChangeReport:
    """Score how different two grayscale previews are (0 = identical)."""
    cfg = cfg or SimilarityConfig()
    if prev.shape != cur.shape:
        import cv2

        cur = cv2.resize(cur, (prev.shape[1], prev.shape[0]), interpolation=cv2.INTER_AREA)

    mask = _mask_array(prev.shape, masks, cfg.mask_merge_gap)
    coverage = 0.0 if mask is None else float(mask.mean())

    diff = np.abs(prev.astype("int16") - cur.astype("int16"))
    if mask is not None:
        keep = ~mask
        if not keep.any():
            return ChangeReport(0.0, 0.0, 0, 0.0, coverage)
        pixels = diff[keep]
    else:
        pixels = diff.ravel()

    total = float(pixels.size)
    changed_fraction = float((pixels > cfg.pixel_threshold).sum()) / total
    mad = float(pixels.mean()) / 255.0

    ha = phash_hex(prev)
    hb = phash_hex(cur)
    hamming = bin(int(ha, 16) ^ int(hb, 16)).count("1")

    mad_part = cfg.mad_weight * min(1.0, mad / max(1e-6, cfg.mad_scale))
    # The perceptual hash always covers the whole frame, so its influence is
    # scaled down by how much of the frame we were told to ignore (our own
    # overlay cards). Otherwise masked changes would still trigger a page turn.
    hash_part = (
        cfg.phash_weight
        * (1.0 - min(0.95, coverage))
        * min(1.0, hamming / max(1.0, cfg.phash_scale))
    )
    score = max(changed_fraction, mad_part + hash_part)
    return ChangeReport(
        changed_fraction=changed_fraction,
        mad=mad,
        hamming=hamming,
        score=score,
        mask_coverage=coverage,
        hash_a=ha,
        hash_b=hb,
    )
