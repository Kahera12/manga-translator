"""RapidOCR (PaddleOCR PP-OCRv4 models on ONNX Runtime) — default Chinese engine.

Chosen over raw PaddleOCR because it ships the same det/rec/cls models inside the
wheel (fully offline, ~16 MB), installs with plain pip, has no paddlepaddle/CUDA
requirement and is 2-4x faster on CPU. Chosen over Tesseract because Tesseract
is markedly worse on manga: vertical CJK columns, small dialogue text and text
over artwork.

Adds a vertical pass: tall CJK columns are cropped, rotated 90° and re-run
through the recognizer; the better-scoring reading wins.
"""
from __future__ import annotations

import math
import threading
from typing import List, Optional, Tuple

import numpy as np

from ..core.logging_setup import get_logger
from ..core.models import TextSpan
from .base import OcrCapabilities, OcrEngine

log = get_logger("ocr.rapid")


class RapidOcrEngine(OcrEngine):
    capabilities = OcrCapabilities(
        name="rapid",
        label="RapidOCR PP-OCRv4 (offline)",
        offline=True,
        vertical_text=True,
    )

    def __init__(self, min_confidence: float = 0.55, max_side: int = 1600,
                 vertical_pass: bool = True):
        self.min_confidence = min_confidence
        self.max_side = max_side
        self.vertical_pass = vertical_pass
        self._engine = None
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------
    def warmup(self) -> None:
        with self._lock:
            if self._engine is not None:
                return
            from rapidocr_onnxruntime import RapidOCR  # heavy import, worker thread only

            self._engine = RapidOCR(
                # Keep weak detections so grouping can still merge fragments; the
                # real cut-off happens in ocr/filtering.py (OCR_MIN_CONFIDENCE).
                text_score=max(0.25, min(0.5, self.min_confidence - 0.25)),
                max_side_len=int(self.max_side),
                use_cls=True,
            )
            log.info("RapidOCR ready (PP-OCRv4 det+cls+rec, max_side=%d)", self.max_side)

    def is_ready(self) -> bool:
        return self._engine is not None

    # -- recognition ------------------------------------------------------
    def recognize(self, img_bgr: np.ndarray) -> List[TextSpan]:
        if self._engine is None:
            self.warmup()
        raw, _elapse = self._engine(img_bgr)
        spans: List[TextSpan] = []
        for row in raw or []:
            quad_raw, text, score = row[0], row[1], float(row[2])
            text = (text or "").strip()
            if not text:
                continue
            quad = _flatten_quad(quad_raw)
            if quad is None:
                continue
            spans.append(TextSpan(text=text, quad=quad, confidence=score,
                                  angle=_quad_angle(quad)))
        if self.vertical_pass:
            self._vertical_pass(img_bgr, spans)
        return spans

    def _vertical_pass(self, img_bgr: np.ndarray, spans: List[TextSpan]) -> None:
        """Re-read tall CJK columns rotated upright; keep the better reading."""
        targets = [s for s in spans if s.is_vertical and s.bbox.h >= 24]
        if not targets:
            return
        crops: List[np.ndarray] = []
        keep: List[TextSpan] = []
        for span in targets:
            crop = _crop_quad(img_bgr, span.quad)
            if crop is None:
                continue
            h, w = crop.shape[:2]
            if h < 12 or w < 4:
                continue
            rotated = _rotate_upright(crop)
            if rotated is None:
                continue
            crops.append(rotated)
            keep.append(span)
        if not crops:
            return
        try:
            results, _ = self._engine.text_rec(crops)
        except Exception as exc:  # pragma: no cover - engine internals changed
            log.debug("vertical re-recognition skipped: %s", exc.__class__.__name__)
            return
        for span, (text, score) in zip(keep, results or []):
            text = (text or "").strip()
            if not text:
                continue
            if score > span.confidence + 0.02 or (
                _cjk_count(text) > _cjk_count(span.text) and score > span.confidence - 0.15
            ):
                log.debug("vertical fix %r(%.2f) -> %r(%.2f)", span.text, span.confidence, text, score)
                span.text = text
                span.confidence = float(score)
                span.angle = 0.0  # read upright now: no tilt left


# --- geometry helpers ------------------------------------------------------


def _flatten_quad(quad) -> Optional[Tuple[float, float, float, float, float, float, float, float]]:
    try:
        pts = [(float(p[0]), float(p[1])) for p in quad]
    except (TypeError, ValueError, IndexError):
        return None
    if len(pts) != 4:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        pts = [(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys))]
    # Order points tl, tr, br, bl so the warp is stable.
    pts = sorted(pts, key=lambda p: p[1])
    top = sorted(pts[:2], key=lambda p: p[0])
    bottom = sorted(pts[2:], key=lambda p: p[0])
    ordered = [top[0], top[1], bottom[1], bottom[0]]
    return (ordered[0][0], ordered[0][1], ordered[1][0], ordered[1][1],
            ordered[2][0], ordered[2][1], ordered[3][0], ordered[3][1])


def _quad_angle(quad) -> float:
    """Tilt of the detected box relative to the nearest text axis, in degrees.

    0 means the glyphs run along the box's long side (a normal horizontal line or
    a normal vertical CJK column); large values mean stylised rotated lettering
    (typical for sound effects). Folding to [0, 45] keeps the value meaningful in
    both directions instead of reporting a bogus 90 deg for plain horizontal text.
    """
    dx = quad[2] - quad[0]
    dy = quad[3] - quad[1]
    if dx == 0.0 and dy == 0.0:
        return 0.0
    angle = math.degrees(math.atan2(dy, dx))
    folded = ((angle + 45.0) % 90.0) - 45.0
    return float(abs(folded))


def _crop_quad(img: np.ndarray, quad, pad: int = 1) -> Optional[np.ndarray]:
    import cv2

    pts = np.array([(quad[i], quad[i + 1]) for i in range(0, 8, 2)], dtype="float32")
    w = max(int(np.linalg.norm(pts[0] - pts[1])), int(np.linalg.norm(pts[3] - pts[2])))
    h = max(int(np.linalg.norm(pts[0] - pts[3])), int(np.linalg.norm(pts[1] - pts[2])))
    if w < 2 or h < 2:
        return None
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype="float32")
    matrix = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(img, matrix, (w + pad, h + pad), flags=cv2.INTER_LINEAR)


def _rotate_upright(crop: np.ndarray):
    """Rotate a vertical CJK column so it reads left-to-right for the recognizer."""
    import cv2

    h, w = crop.shape[:2]
    if h <= w:
        return None
    # Columns of single glyphs are best read after 90° CW rotation.
    rotated = cv2.rotate(crop, cv2.ROTATE_90_CLOCKWISE)
    rh, rw = rotated.shape[:2]
    if rw < 8 or rh < 8:
        return None
    if rw / float(rh) > 40:  # recognizer input would be absurdly wide
        rotated = cv2.resize(rotated, (int(rh * 30), rh), interpolation=cv2.INTER_AREA)
    return rotated


def _cjk_count(text: str) -> int:
    return sum(1 for ch in text if _is_cjk(ch))


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2FA1F
    )
