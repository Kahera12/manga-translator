"""Optional PaddleOCR engine (PP-OCRv5 when available).

Kept as a plug-in so the app is not hardwired to a single OCR stack. It is not
the default: `paddleocr` + `paddlepaddle` are a ~600 MB install and the ONNX
port of the same models (rapid) is faster to set up and faster on CPU.
Select with OCR_ENGINE=paddle.
"""
from __future__ import annotations

import threading
from typing import List, Optional

import numpy as np

from ..core.logging_setup import get_logger
from ..core.models import TextSpan
from .base import OcrCapabilities, OcrEngine

log = get_logger("ocr.paddle")


class PaddleOcrEngine(OcrEngine):
    capabilities = OcrCapabilities(
        name="paddle",
        label="PaddleOCR",
        offline=True,
        vertical_text=True,
        needs_download=True,
        note="requires paddleocr + paddlepaddle",
    )

    def __init__(self, lang: str = "ch", use_gpu: bool = False):
        self.lang = lang
        self.use_gpu = use_gpu
        self._engine = None
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        try:
            import paddleocr  # noqa: F401
        except Exception:
            return False
        return True

    def warmup(self) -> None:
        with self._lock:
            if self._engine is not None:
                return
            from paddleocr import PaddleOCR

            # PP-OCRv5 if this paddleocr build offers it, else the latest default.
            try:
                self._engine = PaddleOCR(
                    lang=self.lang, use_angle_cls=True, show_log=False, use_gpu=self.use_gpu
                )
            except TypeError:
                self._engine = PaddleOCR(lang=self.lang, use_angle_cls=True)
            log.info("PaddleOCR ready (lang=%s)", self.lang)

    def is_ready(self) -> bool:
        return self._engine is not None

    def recognize(self, img_bgr: np.ndarray) -> List[TextSpan]:
        if self._engine is None:
            self.warmup()
        result = self._engine.ocr(img_bgr, cls=True)
        spans: List[TextSpan] = []
        for page in result or []:
            for item in page or []:
                try:
                    quad_raw, (text, score) = item[0], item[1]
                except (TypeError, IndexError, ValueError):
                    continue
                if not text:
                    continue
                xs = [float(p[0]) for p in quad_raw]
                ys = [float(p[1]) for p in quad_raw]
                quad = (xs[0], ys[0], xs[1], ys[1], xs[2], ys[2], xs[3], ys[3])
                w = max(xs) - min(xs)
                h = max(ys) - min(ys)
                angle = 90.0 if h > w * 1.35 else 0.0
                spans.append(TextSpan(text=str(text).strip(), quad=quad,
                                      confidence=float(score), angle=angle))
        return spans
