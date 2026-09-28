"""Control panel (offscreen): preview widget, panels, window wiring."""
from __future__ import annotations


import numpy as np
import pytest

pytest.importorskip("PySide6")

from manga_translator.core.models import Block, PageResult, Rect, ScreenRegion, TextSpan  # noqa: E402
from manga_translator.pipeline.orchestrator import PageJob  # noqa: E402
from manga_translator.ui import theme  # noqa: E402
from manga_translator.ui.panels import BlocksPanel, LogPanel, StatusPanel  # noqa: E402
from manga_translator.ui.preview import PagePreviewWidget, bbox_to_qrect, bgr_to_qimage  # noqa: E402


@pytest.fixture(autouse=True)
def _qt(qapp):
    """Every test in this module builds QWidgets, so Qt must be initialised."""
    return qapp


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRANSLATION_BACKEND", "fake")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from manga_translator.app import MangaTranslatorApp

    application = MangaTranslatorApp([])
    yield application
    application.shutdown()


REGION = ScreenRegion(0, 0, 900, 640, "TEST", 1.0)


def page_result(generation: int = 3, with_translation: bool = True) -> PageResult:
    blocks = [
        Block(
            id=1,
            text="你好，今天天气真好。",
            bbox=Rect(120, 120, 260, 40),
            spans=[TextSpan("你好，今天天气真好。", (120, 120, 380, 120, 380, 160, 120, 160), 0.96)],
            translation="Привет! Сегодня отличная погода." if with_translation else "",
            confidence=0.96,
        ),
        Block(
            id=2,
            text="师兄，你来了！",
            bbox=Rect(120, 300, 200, 40),
            spans=[TextSpan("师兄，你来了！", (120, 300, 320, 300, 320, 340, 120, 340), 0.94)],
            translation="Старший брат, ты пришёл!" if with_translation else "",
            confidence=0.94,
        ),
        Block(
            id=3,
            text="ドン",
            bbox=Rect(600, 480, 180, 120),
            spans=[TextSpan("ドン", (600, 480, 780, 480, 780, 600, 600, 600), 0.9)],
            translation="",
            confidence=0.9,
            is_sfx=True,
        ),
    ]
    return PageResult(
        generation=generation,
        page_hash="bd64cc9bc292d998",
        blocks=blocks,
        ocr_ms=138.0,
        translate_ms=420.0,
        total_ms=560.0,
    )


def fake_frame(width: int = 900, height: int = 640) -> np.ndarray:
    frame = np.full((height, width, 3), 240, dtype="uint8")
    frame[100:420, 100:420] = 60      # a dark "panel"
    frame[460:620, 560:820] = 120
    return frame


# --- preview widget --------------------------------------------------------

def test_bgr_to_qimage_keeps_size_and_channels():
    image = bgr_to_qimage(fake_frame(80, 40))
    assert image.width() == 80 and image.height() == 40
    # BGR(60,60,60) stays grey, and the light background stays light
    assert image.pixelColor(0, 0).red() > 200
    assert image.pixelColor(120, 120).red() < 120


def test_preview_maps_boxes_into_the_widget():
    widget = PagePreviewWidget()
    widget.resize(500, 400)
    widget.set_frame(fake_frame())
    widget.set_analysis(
        page_result().blocks,
        [Rect(140, 90, 220, 80)],
        show_translation=True,
    )
    target = widget.image_rect()
    assert target.width() > 0 and target.height() > 0
    scale = target.width() / 900.0
    mapped = bbox_to_qrect(Rect(120, 120, 260, 40), scale, target.topLeft())
    assert mapped.x() >= target.left() - 1 and mapped.x() < target.right()
    assert mapped.y() >= target.top() - 1 and mapped.y() < target.bottom()


def test_preview_renders_pixels_offscreen():
    widget = PagePreviewWidget()
    widget.resize(600, 420)
    widget.set_frame(fake_frame())
    result = page_result()
    widget.set_analysis(result.blocks, [Rect(140, 90, 220, 80)], True)
    pixmap = widget.grab()
    assert pixmap.width() == 600 and pixmap.height() == 420
    image = pixmap.toImage()
    colors = {image.pixelColor(x, y).name() for x in range(0, 600, 40) for y in range(0, 420, 40)}
    assert len(colors) > 3, "preview should paint frame + annotations"


def test_preview_placeholder_without_frame():
    widget = PagePreviewWidget()
    widget.resize(200, 120)
    widget.set_frame(None)
    widget.set_placeholder("nothing here")
    assert widget.frame_size == (0, 0)
    assert not widget.image_rect().isValid() or widget.image_rect().isEmpty()


# --- panels ----------------------------------------------------------------

def test_status_panel_formats_payload():
    panel = StatusPanel()
    panel.update_state(
        {
            "manga_mode": True,
            "region": {"width": 1200, "height": 900, "x": 10, "y": 20, "dpi_scale": 1.25,
                       "monitor": r"\\.\DISPLAY1"},
            "displays": "DISPLAY1 2560x1440px @ 125%",
            "engine": "RapidOCR PP-OCRv4 (offline)",
            "backend": "gemini",
            "model": "gemini-3.8-flash",
            "generation": 7,
            "detector": {"state": "idle", "score": 0.031, "changed_fraction": 0.004,
                         "hamming": 2, "stable_for_ms": 420, "page_hash": "abc123"},
            "last_page": {"total_ms": 540.0, "ocr_ms": 130.0, "translate_ms": 400.0,
                          "blocks": 4, "from_cache": True},
            "cache": {"entries": 12, "bytes": 3_500_000, "hits": 5, "enabled": 1},
            "stats": {"pages_processed": 9, "cache_hits": 5, "stale_discarded": 2, "errors": 0},
        }
    )
    assert panel.lbl_mode.text() == "ON"
    assert "1200×900" in panel.lbl_region.text()
    assert "125%" in panel.lbl_display.text()
    assert panel.lbl_generation.text() == "7"
    assert "abc123" in panel.lbl_hash.text()
    assert "from cache" in panel.lbl_timing.text()
    assert "12 pages" in panel.lbl_cache.text()
    assert "stale dropped 2" in panel.lbl_stats.text()
    assert "0.031" in panel.lbl_change.text()


def test_blocks_panel_table_and_export():
    panel = BlocksPanel()
    panel.set_blocks(page_result().blocks)
    assert panel.table.rowCount() == 3
    assert panel.table.item(0, 1).text() == "你好，今天天气真好。"
    assert panel.table.item(0, 2).text().startswith("Привет")
    assert panel.table.item(2, 4).text() == "SFX"
    assert "2 translated" in panel.lbl_summary.text()
    text = panel.page_text()
    assert "你好，今天天气真好。" in text and "Старший брат" in text
    copied = panel.copy_translations()
    assert "Привет" in copied


def test_blocks_panel_empty_page():
    panel = BlocksPanel()
    panel.set_blocks([])
    assert panel.table.rowCount() == 0
    assert "no dialogue" in panel.lbl_summary.text()


def test_log_panel_tails_the_file(tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text("first line\n", encoding="utf-8")
    panel = LogPanel(log_file)
    assert "first line" in panel.view.toPlainText()
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write("second line\n")
    panel.reload()
    content = panel.view.toPlainText()
    assert "first line" in content and "second line" in content
    # rotation: file shrinks -> view resets instead of duplicating
    log_file.write_text("after rotation\n", encoding="utf-8")
    panel.reload()
    assert "after rotation" in panel.view.toPlainText()
    assert "second line" not in panel.view.toPlainText()


# --- window ----------------------------------------------------------------

def test_window_builds_and_reflects_state(app):
    window = app.window
    window.show()
    assert window.preview is not None
    assert window.blocks_panel.table.columnCount() == 6
    assert window.act_manga.text() in ("Manga Mode", "Manga Mode: ON")
    assert window.act_overlay.isChecked() is True
    payload = app.describe()
    assert "detector" in payload and "displays" in payload and "cache" in payload
    window.refresh_state()
    assert window.status_panel.lbl_engine.text() != "—"


def test_window_shows_a_page_result(app):
    window = app.window
    window.show()
    job = PageJob(generation=3, region=REGION, kind="changed", change_score=0.42)
    app.pipeline._store_frame(fake_frame(), REGION)
    app._on_page_ready(page_result(), job)
    assert window.blocks_panel.table.rowCount() == 3
    assert window.preview.frame_size == (900, 640)
    assert window.preview._blocks, "preview received the blocks"
    assert "gen 3" in window.preview._debug.generation.__str__() or window.preview._debug.generation == 3
    # the masks handed to the detector come from the on-screen overlay cards
    assert window._card_rects or not app.overlay.cards


def test_window_toolbar_emits_commands(app):
    window = app.window
    seen = {"manga": [], "region": 0, "refresh": 0, "quit": 0}
    window.mangaModeToggled.connect(lambda on: seen["manga"].append(on))
    window.selectRegionRequested.connect(lambda: seen.__setitem__("region", seen["region"] + 1))
    window.refreshRequested.connect(lambda: seen.__setitem__("refresh", seen["refresh"] + 1))
    window.quitRequested.connect(lambda: seen.__setitem__("quit", seen["quit"] + 1))

    window.act_manga.setChecked(True)
    assert seen["manga"] == [True], "toolbar toggle must reach the app signal"
    window.act_region.trigger()
    window.act_refresh.trigger()
    assert seen["region"] == 1 and seen["refresh"] == 1


def test_closing_the_window_hides_instead_of_quitting(app):
    from PySide6.QtGui import QCloseEvent

    window = app.window
    window.show()
    hidden = []
    window.hideToTrayRequested.connect(lambda: hidden.append(True))
    event = QCloseEvent()
    window.closeEvent(event)
    assert not event.isAccepted(), "close must be ignored so the tray keeps running"
    assert hidden == [True]
    assert not window.isVisible()


def test_window_preview_request_starts_pipeline_grab(app, monkeypatch):
    """While the pipeline runs, preview work must go to its worker thread."""
    app.config.set_region(REGION)
    requested = []
    monkeypatch.setattr(
        type(app.pipeline), "is_running", property(lambda self: True), raising=False
    )
    monkeypatch.setattr(app.pipeline, "request_preview", lambda: requested.append(True))
    app.request_preview()
    assert requested == [True]


def test_preview_request_grabs_directly_when_pipeline_is_idle(app, monkeypatch):
    """With Manga Mode off the panel still shows the region (GUI-thread grab)."""
    app.config.set_region(REGION)
    frame = fake_frame()
    monkeypatch.setattr(app.capture, "grab", lambda region: frame)
    app.request_preview()
    assert app.window.preview.frame_size == (900, 640)


def test_hotkey_mapping_is_rendered(app):
    window = app.window
    mapping = app.hotkey_mapping()
    window.set_hotkeys(mapping)
    assert "Toggle Manga Mode" in mapping
    assert window.hotkey_panel.form.rowCount() == len(mapping)


def test_theme_stylesheet_is_valid_qss(app):
    window = app.window
    assert theme.ACCENT in window.styleSheet()
    assert "purple" not in window.styleSheet(), "no placeholder colours left"
    assert len(window.styleSheet()) > 500


def test_window_grab_is_not_blank(app):
    window = app.window
    window.resize(1000, 700)
    window.show()
    app.pipeline._store_frame(fake_frame(), REGION)
    app._on_page_ready(page_result(), PageJob(generation=5, region=REGION, kind="changed"))
    pixmap = window.grab()
    image = pixmap.toImage()
    assert image.width() >= 900
    sample = {image.pixelColor(x, y).name() for x in range(0, image.width(), 60)
              for y in range(0, image.height(), 60)}
    assert len(sample) > 5, "the window must render real content, not a flat colour"
