"""Core value types: regions, geometry, OCR spans, dialogue blocks, page results."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence, Tuple

Quad = Tuple[float, float, float, float, float, float, float, float]


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle in a fixed pixel space (documented by the owner)."""

    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def expanded(self, dx: float, dy: Optional[float] = None) -> "Rect":
        """Grow the rectangle by ``dx`` horizontally and ``dy`` vertically
        (``dy`` defaults to ``dx``: a uniform margin)."""
        dy = dx if dy is None else dy
        return Rect(self.x - dx, self.y - dy, self.w + 2 * dx, self.h + 2 * dy)

    def translated(self, dx: float, dy: float) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def intersection(self, other: "Rect") -> Optional["Rect"]:
        x1, y1 = max(self.x, other.x), max(self.y, other.y)
        x2, y2 = min(self.x2, other.x2), min(self.y2, other.y2)
        if x2 <= x1 or y2 <= y1:
            return None
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def union(self, other: "Rect") -> "Rect":
        x1, y1 = min(self.x, other.x), min(self.y, other.y)
        x2, y2 = max(self.x2, other.x2), max(self.y2, other.y2)
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def intersects(self, other: "Rect") -> bool:
        return self.intersection(other) is not None

    def coverage_of(self, other: "Rect") -> float:
        """Fraction of `other`'s area covered by self (masking helper)."""
        if other.area <= 0:
            return 0.0
        inter = self.intersection(other)
        return 0.0 if inter is None else inter.area / other.area

    def scaled(self, factor: float) -> "Rect":
        return Rect(self.x * factor, self.y * factor, self.w * factor, self.h * factor)

    def rounded(self) -> "Rect":
        x, y = int(round(self.x)), int(round(self.y))
        return Rect(x, y, int(round(self.x2)) - x, int(round(self.y2)) - y)

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)

    @staticmethod
    def from_bounds(x1: float, y1: float, x2: float, y2: float) -> "Rect":
        return Rect(x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1))


@dataclass(frozen=True)
class ScreenRegion:
    """A screen region in *physical* device pixels (mss / Win32 coordinate space)."""

    x: int
    y: int
    width: int
    height: int
    monitor: str = ""
    dpi_scale: float = 1.0  # 1.0 / 1.25 / 1.5 ...

    @property
    def rect(self) -> Rect:
        return Rect(self.x, self.y, self.width, self.height)

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def is_valid(self) -> bool:
        return self.width >= 16 and self.height >= 16

    def to_dict(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "monitor": self.monitor,
            "dpi_scale": round(self.dpi_scale, 4),
        }

    @staticmethod
    def from_dict(data: dict) -> Optional["ScreenRegion"]:
        try:
            return ScreenRegion(
                x=int(data["x"]),
                y=int(data["y"]),
                width=int(data["width"]),
                height=int(data["height"]),
                monitor=str(data.get("monitor", "")),
                dpi_scale=float(data.get("dpi_scale", 1.0)) or 1.0,
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class TextSpan:
    """One OCR detection, in capture-region relative physical pixels."""

    text: str
    quad: Quad
    confidence: float
    angle: float = 0.0  # 0 = horizontal, ~90 = vertical (CJK column)

    @property
    def bbox(self) -> Rect:
        xs = self.quad[0::2]
        ys = self.quad[1::2]
        return Rect.from_bounds(min(xs), min(ys), max(xs), max(ys))

    @property
    def is_vertical(self) -> bool:
        """Tall CJK columns are vertical text. `angle` holds the tilt from the
        nearest text axis (0..45 deg) and is only used for the SFX heuristic."""
        b = self.bbox
        if b.h <= 0 or b.w <= 0:
            return self.angle >= 45
        return (b.h / b.w) > 1.35


@dataclass
class Block:
    """A grouped dialogue block: several OCR lines that belong to one bubble."""

    id: int
    text: str
    bbox: Rect
    spans: list = field(default_factory=list)
    direction: str = "horizontal"
    confidence: float = 0.0
    is_sfx: bool = False
    translation: str = ""

    @property
    def char_count(self) -> int:
        return len(self.text)

    def component_bboxes(self) -> list:
        return [s.bbox for s in self.spans]


@dataclass
class PageResult:
    """Outcome of processing one page generation."""

    generation: int
    page_hash: str
    blocks: list = field(default_factory=list)
    extra_spans: list = field(default_factory=list)  # low-confidence spans (debug HUD)
    from_cache: bool = False
    ocr_ms: float = 0.0
    translate_ms: float = 0.0
    total_ms: float = 0.0
    error: Optional[str] = None  # user-readable, never a traceback
    stale: bool = False

    @property
    def translatable(self) -> Iterable[Block]:
        return [b for b in self.blocks if b.translation]


@dataclass
class TranslationIssue:
    """Non fatal translation problems (missing ids, malformed rows, ...)."""

    missing_ids: Sequence[int] = ()
    unexpected_ids: Sequence[int] = ()
    raw_preview: str = ""


class PipelineError(RuntimeError):
    """User-presentable pipeline error with a machine-readable kind."""

    def __init__(self, message: str, kind: str = "error", retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.retryable = retryable
