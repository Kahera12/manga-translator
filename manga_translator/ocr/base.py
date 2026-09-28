"""OCR engine abstraction. Engines return TextSpan lists in capture-relative px."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np

from ..core.models import TextSpan


@dataclass
class OcrCapabilities:
    name: str
    label: str
    offline: bool = True
    vertical_text: bool = True
    needs_download: bool = False
    note: str = ""


class OcrEngine:
    capabilities = OcrCapabilities(name="base", label="base")

    def warmup(self) -> None:
        """Load models. Called on the worker thread, never on the GUI thread."""

    def recognize(self, img_bgr: np.ndarray) -> List[TextSpan]:  # pragma: no cover
        raise NotImplementedError

    def is_ready(self) -> bool:
        return True

    def shutdown(self) -> None:
        pass
