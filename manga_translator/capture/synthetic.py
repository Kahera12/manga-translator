"""Synthetic frame sources + a test manga page generator.

Used by the test-suite and the headless self-test so the whole pipeline
(change detection -> OCR -> translation -> overlay) can be exercised without a
real desktop, a real reader or an API key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from ..core.logging_setup import get_logger
from .screen_capture import FrameSource, downscale_gray

log = get_logger("capture.synthetic")


class SequenceFrameSource(FrameSource):
    """Plays back a script of frames (each entry may repeat for N polls)."""

    def __init__(self, frames: Sequence[np.ndarray], repeats: int = 1):
        self.frames = [np.ascontiguousarray(f) for f in frames]
        self.repeats = max(1, int(repeats))
        self.index = 0
        self.polls = 0
        self.grabs = 0

    def desktop_size(self) -> tuple:
        if not self.frames:
            return (0, 0)
        h, w = self.frames[0].shape[:2]
        return (w, h)

    def _current(self) -> np.ndarray:
        if not self.frames:
            return np.zeros((64, 64, 3), dtype="uint8")
        idx = min(self.index, len(self.frames) - 1)
        return self.frames[idx]

    def advance(self, steps: int = 1) -> None:
        self.polls += steps
        if self.polls % self.repeats == 0:
            self.index = min(self.index + 1, max(0, len(self.frames) - 1))

    def grab(self, region) -> np.ndarray:
        """Full frame as it is *right now* (no script advance)."""
        self.grabs += 1
        frame = self._current()
        x = max(0, int(region.x))
        y = max(0, int(region.y))
        crop = frame[y : y + int(region.height), x : x + int(region.width)]
        if crop.size == 0:
            crop = frame
        return np.ascontiguousarray(crop)

    def grab_preview(self, region, max_dim: int = 240) -> np.ndarray:
        """Polling a preview advances the scripted playback."""
        preview = downscale_gray(self.grab(region), max_dim)
        self.advance()
        return preview


class StaticFrameSource(SequenceFrameSource):
    def __init__(self, frame: np.ndarray):
        super().__init__([frame])


class MutableFrameSource(FrameSource):
    """Test source whose current frame the test sets explicitly."""

    def __init__(self, frame: np.ndarray):
        self.frame = np.ascontiguousarray(frame)
        self.grabs = 0
        self.previews = 0

    def set_frame(self, frame: np.ndarray) -> None:
        self.frame = np.ascontiguousarray(frame)

    def desktop_size(self) -> tuple:
        h, w = self.frame.shape[:2]
        return (w, h)

    def grab(self, region) -> np.ndarray:
        self.grabs += 1
        x = max(0, int(region.x))
        y = max(0, int(region.y))
        crop = self.frame[y : y + int(region.height), x : x + int(region.width)]
        if crop.size == 0:
            crop = self.frame
        return np.ascontiguousarray(crop)

    def grab_preview(self, region, max_dim: int = 240) -> np.ndarray:
        self.previews += 1
        return downscale_gray(self.grab(region), max_dim)


# --- synthetic manga pages -------------------------------------------------

try:  # CJK font shipped with Windows
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = None


FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def find_cjk_font() -> Optional[str]:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


@dataclass
class BubbleSpec:
    text: str
    x: int
    y: int
    vertical: bool = False
    font_size: int = 30


def make_manga_page(
    width: int = 1200,
    height: int = 900,
    bubbles: Sequence[BubbleSpec] = (),
    background: int = 245,
    seed: int = 0,
    noise: int = 6,
    panels: int = 4,
) -> np.ndarray:
    """Render a fake manga page: panels + bubbles with Chinese text.

    ``panels`` greyscale blocks make page-to-page differences realistic (a real
    manga page turn changes a large part of the screen, not just the balloons).
    """
    if Image is None:  # pragma: no cover
        raise RuntimeError("Pillow is required to build synthetic pages")
    rng = np.random.default_rng(seed)
    canvas = np.full((height, width, 3), background, dtype="uint8")
    if noise:
        canvas = np.clip(
            canvas.astype("int16") + rng.integers(-noise, noise, canvas.shape, dtype="int16"),
            0,
            255,
        ).astype("uint8")
    image = Image.fromarray(canvas)
    draw = ImageDraw.Draw(image)

    for index in range(max(0, panels)):
        px = int(rng.integers(10, max(20, width // 2)))
        py = int(rng.integers(10, max(20, height // 2)))
        pw = int(rng.integers(width // 4, max(width // 3, width // 2)))
        ph = int(rng.integers(height // 4, max(height // 3, height // 2)))
        shade = int(rng.integers(35, 140))
        jitter = int(rng.integers(0, 40))
        draw.rectangle(
            [px, py, min(width - 5, px + pw), min(height - 5, py + ph)],
            outline=(20, 20, 20),
            width=3,
            fill=(shade, min(255, shade + jitter), max(0, shade - jitter // 2)),
        )
        # a few "line art" strokes so the panel is not a flat colour
        for _ in range(int(rng.integers(2, 6))):
            x1 = int(rng.integers(px, max(px + 1, px + pw)))
            y1 = int(rng.integers(py, max(py + 1, py + ph)))
            draw.line(
                [x1, y1, x1 + int(rng.integers(-80, 80)), y1 + int(rng.integers(-80, 80))],
                fill=(245, 245, 245),
                width=2,
            )

    font_path = find_cjk_font()
    for bubble in bubbles:
        font = ImageFont.truetype(font_path, bubble.font_size) if font_path else ImageFont.load_default()
        if bubble.vertical:
            char_h = bubble.font_size + 4
            box = [
                bubble.x - 14,
                bubble.y - 14,
                bubble.x + bubble.font_size + 16,
                bubble.y + char_h * len(bubble.text) + 8,
            ]
            draw.ellipse(box, outline=(30, 30, 30), width=2, fill=(255, 255, 255))
            for i, ch in enumerate(bubble.text):
                draw.text((bubble.x, bubble.y + i * char_h), ch, font=font, fill=(10, 10, 10))
        else:
            text_w = draw.textlength(bubble.text, font=font)
            box = [
                bubble.x - 14,
                bubble.y - 12,
                bubble.x + text_w + 16,
                bubble.y + bubble.font_size + 14,
            ]
            draw.ellipse(box, outline=(30, 30, 30), width=2, fill=(255, 255, 255))
            draw.text((bubble.x, bubble.y), bubble.text, font=font, fill=(10, 10, 10))
    return np.ascontiguousarray(np.array(image)[:, :, ::-1])  # RGB -> BGR


def default_test_pages() -> List[np.ndarray]:
    """Three recognisable Chinese pages (also used as the OCR smoke test)."""
    return [
        make_manga_page(
            bubbles=[
                BubbleSpec("你好，今天天气真好。", 120, 120),
                BubbleSpec("师兄，你来了！", 120, 240),
            ],
            seed=1,
        ),
        make_manga_page(
            bubbles=[
                BubbleSpec("灵气失控了，快跑！", 640, 120, vertical=True, font_size=28),
                BubbleSpec("为什么？", 140, 300),
            ],
            seed=2,
        ),
        make_manga_page(
            bubbles=[BubbleSpec("林雪，等一下！", 200, 420)],
            seed=3,
        ),
    ]
