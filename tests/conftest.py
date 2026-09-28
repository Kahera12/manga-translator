"""Shared test fixtures/helpers."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

# Qt must run headless in tests (set before any PySide6 widget import).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
# Unit tests assert the canonical (English) strings; the localisation tests
# switch languages explicitly. The shipped default is Russian.
os.environ.setdefault("UI_LANGUAGE", "en")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Unit tests assert the canonical (English) strings; the localisation tests
# switch languages explicitly.
from manga_translator.core.i18n import set_language  # noqa: E402

set_language("en")

from manga_translator.capture.synthetic import (  # noqa: E402
    BubbleSpec,
    MutableFrameSource,
    SequenceFrameSource,
    make_manga_page,
)
from manga_translator.core.config import Config  # noqa: E402
from manga_translator.core.models import Rect, ScreenRegion, TextSpan  # noqa: E402
from manga_translator.core.paths import Paths  # noqa: E402
from manga_translator.ocr.stub_engine import StubOcrEngine, span  # noqa: E402


@pytest.fixture(autouse=True)
def _english_ui():
    """Keep unit-test assertions in English whatever a previous test switched to."""
    set_language("en")
    yield
    set_language("en")


@pytest.fixture(scope="session")
def qapp():
    """A QApplication is mandatory before any QWidget is constructed."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application


@pytest.fixture()
def tmp_paths(tmp_path) -> Paths:
    root = tmp_path / "mt"
    return Paths(
        root=root,
        config=root / "config.json",
        glossary=root / "glossary.json",
        cache_dir=root / "cache",
        cache_db=root / "cache" / "pages.db",
        log_dir=root / "logs",
        log_file=root / "logs" / "app.log",
        debug_dir=root / "debug",
        env_file=root / ".env",
    ).ensure()


@pytest.fixture()
def fast_config(tmp_paths) -> Config:
    cfg = Config.load(tmp_paths)
    cfg.general.poll_interval_ms = 30
    cfg.general.stable_duration_ms = 70
    cfg.general.preview_max_dim = 200
    cfg.general.change_threshold = 0.075
    cfg.general.min_unstable_frames = 1
    cfg.translation.backend = "fake"
    cfg.ocr.engine = "stub"
    cfg.cache.enabled = True
    cfg.cache.hamming_threshold = 6
    cfg.overlay.show_debug_hud = False
    cfg.hotkeys.enabled = False
    return cfg


@pytest.fixture()
def region() -> ScreenRegion:
    return ScreenRegion(x=0, y=0, width=1200, height=900, monitor="TEST", dpi_scale=1.0)


def page_one() -> np.ndarray:
    return make_manga_page(
        bubbles=[
            BubbleSpec("你好，今天天气真好。", 120, 120),
            BubbleSpec("师兄，你来了！", 120, 260),
        ],
        seed=11,
    )


def page_two() -> np.ndarray:
    return make_manga_page(
        bubbles=[
            BubbleSpec("灵气失控了！", 700, 140, vertical=True, font_size=26),
            BubbleSpec("为什么？", 150, 320),
        ],
        seed=22,
    )


def page_three() -> np.ndarray:
    return make_manga_page(bubbles=[BubbleSpec("林雪，等一下！", 220, 430)], seed=33)


def blank_page() -> np.ndarray:
    """A page with artwork but no Chinese text at all."""
    return make_manga_page(bubbles=[], seed=44, noise=12)


def scripted_engine(pages: list) -> StubOcrEngine:
    """Stub OCR returning the given span lists, one per call, cycling."""
    return StubOcrEngine(script=pages)


__all__ = [
    "Rect",
    "ScreenRegion",
    "TextSpan",
    "StubOcrEngine",
    "span",
    "make_manga_page",
    "MutableFrameSource",
    "SequenceFrameSource",
    "BubbleSpec",
    "page_one",
    "page_two",
    "page_three",
    "blank_page",
    "scripted_engine",
]
