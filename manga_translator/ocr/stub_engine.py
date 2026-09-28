"""Deterministic OCR engine for tests and the headless self-test harness."""
from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np

from ..core.logging_setup import get_logger
from ..core.models import TextSpan
from .base import OcrCapabilities, OcrEngine

log = get_logger("ocr.stub")


class StubOcrEngine(OcrEngine):
    """Returns scripted spans; can simulate slow/failing OCR."""

    capabilities = OcrCapabilities(
        name="stub", label="Stub (tests only)", offline=True, vertical_text=True
    )

    def __init__(self, script: Optional[Sequence[Sequence[TextSpan]]] = None,
                 delay_s: float = 0.0, fail_times: int = 0,
                 repeat_last: bool = True):
        self.script = [list(page) for page in (script or [])]
        self.delay_s = delay_s
        self.fail_times = fail_times
        self.repeat_last = repeat_last
        self.calls = 0
        self.images: List[np.ndarray] = []

    def warmup(self) -> None:
        return None

    def recognize(self, img_bgr: np.ndarray) -> List[TextSpan]:
        import time

        self.calls += 1
        self.images.append(img_bgr)
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("stub OCR failure")
        if not self.script:
            return []
        if len(self.script) > 1:
            page = self.script.pop(0)
            if self.repeat_last:
                self.script.append(page)
        else:
            page = self.script[0]
        return list(page)


def span(text: str, x: float, y: float, w: float, h: float, score: float = 0.95) -> TextSpan:
    """Helper to build a horizontal span from a bbox."""
    quad = (x, y, x + w, y, x + w, y + h, x, y + h)
    return TextSpan(text=text, quad=quad, confidence=score)


class HashKeyedOcrEngine(OcrEngine):
    """Returns spans chosen by the *content* of the frame (its perceptual hash).

    Deterministic regardless of call order - the right tool for pipeline tests
    where several pages are in flight at once.
    """

    capabilities = OcrCapabilities(
        name="stub-hash", label="Stub by page hash (tests only)", offline=True
    )

    def __init__(self, pages: dict, delay_s: float = 0.0, max_distance: int = 16):
        self.pages = dict(pages)          # phash hex -> list[TextSpan]
        self.delay_s = delay_s
        self.max_distance = max_distance
        self.calls = 0
        self.seen: List[str] = []

    def warmup(self) -> None:
        return None

    @staticmethod
    def key(frame: np.ndarray) -> str:
        from ..detection.image_similarity import phash_hex

        gray = frame
        if gray.ndim == 3:
            import cv2

            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        return phash_hex(gray)

    def recognize(self, img_bgr: np.ndarray) -> List[TextSpan]:
        import time

        from ..detection.image_similarity import hamming_hex

        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        digest = self.key(img_bgr)
        self.seen.append(digest)
        best_key, best_distance = None, 999
        for key in self.pages:
            distance = hamming_hex(digest, key)
            if distance < best_distance:
                best_key, best_distance = key, distance
        if best_key is None or best_distance > self.max_distance:
            return []
        return list(self.pages[best_key])
