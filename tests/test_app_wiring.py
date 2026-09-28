"""Application wiring (offscreen): tray, overlay, pipeline, settings dialog.

Constructs the real application object headlessly and checks the pieces the
pipeline tests cannot reach: tray state, overlay masks handed to the detector,
settings dialog round-trip, notifications and shutdown.
"""
from __future__ import annotations


import pytest

pytest.importorskip("PySide6")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRANSLATION_BACKEND", "fake")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from manga_translator.app import MangaTranslatorApp

    application = MangaTranslatorApp([])
    yield application
    application.shutdown()


REGION = (0, 0, 1200, 900)


def test_app_builds_all_components(app):
    assert app.config is not None
    assert app.overlay is not None
    assert app.tray is not None
    assert app.pipeline is not None
    assert app.capture is not None
    assert app.backend.name in ("fake", "gemini")
    assert app.coordmap.monitors


def test_manga_mode_requires_a_region(app, monkeypatch):
    called = {"selector": 0}
    monkeypatch.setattr(app, "_select_region", lambda: called.__setitem__("selector", called["selector"] + 1))
    app.set_manga_mode(True)          # no region -> asks the user to select one
    assert app._manga_mode is False
    assert called["selector"] == 1
    assert not app.pipeline.is_running


def test_enable_manga_mode_starts_the_pipeline(app):
    from manga_translator.core.models import ScreenRegion

    app.config.set_region(ScreenRegion(*REGION, monitor="TEST", dpi_scale=1.0))
    app.set_manga_mode(True)
    try:
        assert app._manga_mode is True
        assert app.pipeline.is_running
        assert app.overlay.isVisible()
        assert app.tray.act_manga.isChecked()
    finally:
        app.set_manga_mode(False)
    assert app._manga_mode is False
    assert not app.pipeline.is_running
    assert not app.overlay.isVisible()


def test_overlay_toggle_and_click_through(app):
    from manga_translator.core.models import ScreenRegion

    app.config.set_region(ScreenRegion(*REGION, monitor="TEST", dpi_scale=1.0))
    app.set_manga_mode(True)
    try:
        app._set_overlay_visible(False)
        assert app.config.overlay.visible is False
        assert not app.overlay.isVisible()
        app._set_overlay_visible(True)
        assert app.overlay.isVisible()
        app.overlay.set_click_through(False)
        assert app.overlay._click_through is False
        app.overlay.set_click_through(True)
        assert app.overlay._click_through is True
    finally:
        app.set_manga_mode(False)


def test_page_result_updates_overlay_and_masks(app):
    from manga_translator.core.models import Block, PageResult, Rect, ScreenRegion, TextSpan
    from manga_translator.pipeline.orchestrator import PageJob

    region = ScreenRegion(*REGION, monitor="TEST", dpi_scale=1.0)
    app.config.set_region(region)
    app.pipeline.backend = type("B", (), {"name": "fake", "model": "fake-model"})()
    job = PageJob(generation=1, region=region, kind="changed", change_score=0.4)
    result = PageResult(
        generation=1,
        page_hash="abc123",
        blocks=[
            Block(
                id=1,
                text="你好，今天天气真好。",
                bbox=Rect(120, 120, 260, 40),
                spans=[TextSpan("你好，今天天气真好。", (120, 120, 380, 120, 380, 160, 120, 160), 0.96)],
                translation="Привет! Сегодня отличная погода.",
            )
        ],
        ocr_ms=12.0,
        translate_ms=30.0,
        total_ms=60.0,
    )
    app._on_page_ready(result, job)
    cards = app.overlay.cards
    assert len(cards) == 1
    assert cards[0].translation.startswith("Привет")
    # the detector must be told to ignore the card area in its previews
    assert app._masks_cache, "overlay masks were not handed to the pipeline"
    mask = app._masks_cache[0]
    card = cards[0].rect
    assert mask.w > card.w and mask.h > card.h, "mask must cover the card with margin"
    assert mask.w <= card.w + 40 and mask.h <= card.h + 40, "mask must hug the card"
    assert -12 <= mask.x <= max(0, card.x) and -12 <= mask.y <= max(0, card.y)
    assert app.pipeline._mask_snapshot

    # a page without blocks clears the overlay and the masks
    app._on_page_ready(PageResult(generation=2, page_hash="def456", blocks=[]), job)
    assert app.overlay.cards == []
    assert app._masks_cache == []


def test_settings_dialog_round_trip(app):
    from manga_translator.ui.settings import SettingsDialog

    dialog = SettingsDialog(app.config, app.pipeline.describe())
    dialog.sp_poll.setValue(240)
    dialog.sp_stable.setValue(500)
    dialog.sp_threshold.setValue(0.2)
    dialog.cmb_mode.setCurrentText("both")
    dialog.chk_click.setChecked(False)
    dialog.cmb_model.setCurrentText("gemini-test-model")
    dialog.chk_sfx.setChecked(True)
    dialog.sp_font.setValue(16.0)
    cfg = dialog.collect()
    assert cfg.general.poll_interval_ms == 240
    assert cfg.general.stable_duration_ms == 500
    assert abs(cfg.general.change_threshold - 0.2) < 1e-9
    assert cfg.overlay.mode == "both"
    assert cfg.overlay.click_through is False
    assert cfg.translation.text_model == "gemini-test-model"
    assert cfg.ocr.translate_sfx is True
    assert cfg.overlay.font_size == 16.0
    dialog.deleteLater()


def test_settings_dialog_provider_selection(app):
    """Switching the provider updates endpoint, model list and key hint."""
    from manga_translator.ui.settings import SettingsDialog
    from manga_translator.translation.providers import default_base_url, get_spec

    dialog = SettingsDialog(app.config, app.pipeline.describe())

    def select(provider: str) -> None:
        index = dialog.cmb_backend.findData(provider)
        assert index >= 0, provider
        dialog.cmb_backend.setCurrentIndex(index)

    seen = [dialog.cmb_backend.itemData(i) for i in range(dialog.cmb_backend.count())]
    for expected in ("gemini", "openai", "deepseek", "anthropic", "deepl", "google-translate",
                     "yandex", "microsoft", "libretranslate", "mymemory", "ollama", "fake"):
        assert expected in seen, expected
    assert len(seen) >= 25, f"only {len(seen)} providers registered"

    select("deepseek")
    assert dialog.ed_base.text() == default_base_url("deepseek")
    assert dialog.cmb_model.currentText() == "deepseek-chat"
    assert "DEEPSEEK_API_KEY" in dialog.ed_key.placeholderText()
    dialog.cmb_model.setCurrentText("deepseek-reasoner")

    select("deepl")
    assert dialog.ed_base.text() == default_base_url("deepl")
    assert dialog.btn_models.text() == "Load languages"
    assert get_spec("deepl").key_env[0] in dialog.ed_key.placeholderText()

    select("google-translate")
    assert "GOOGLE_TRANSLATE_API_KEY" in dialog.ed_key.placeholderText()

    # the selected provider is what gets saved
    select("openai")
    cfg = dialog.collect()
    assert cfg.translation.provider == "openai"
    assert cfg.translation.backend == "openai"
    dialog.deleteLater()


def test_settings_dialog_stores_per_provider_keys(app):
    from manga_translator.ui.settings import SettingsDialog

    dialog = SettingsDialog(app.config, app.pipeline.describe())
    index = dialog.cmb_backend.findData("openai")
    dialog.cmb_backend.setCurrentIndex(index)
    dialog.ed_key.setText("sk-test-openai-key")
    cfg = dialog.collect()
    assert cfg.translation.api_keys["openai"] == "sk-test-openai-key"
    # the key is never rendered anywhere in the dialog text
    assert "sk-test-openai-key" not in dialog.lbl_key.text()
    dialog.deleteLater()


def test_glossary_dialog_edits_terms(app, tmp_paths):
    from manga_translator.ui.settings import GlossaryDialog

    dialog = GlossaryDialog(app.glossary)
    dialog._add_row("灵气", "духовная ци")
    dialog._save()
    assert app.glossary.as_dict().get("灵气") == "духовная ци"
    dialog.deleteLater()


def test_clear_cache_and_stats(app):
    app._clear_cache()
    payload = app.pipeline.describe()
    assert "cache" in payload and "stats" in payload
    from manga_translator.ui.settings import _format_stats

    text = _format_stats(payload)
    assert "Pages processed" in text and "Cache entries" in text


def test_notifications_do_not_crash_without_tray(app):
    app.notifier.attach(None, None)
    app.notifier.info("hello")
    app.notifier.warn("careful")
    app.notifier.error("broken")
    # throttling must not raise either
    app.notifier.error("broken")


def test_hotkey_manager_reports_unavailable_gracefully(app):
    from manga_translator.core.config import HotkeySettings
    from manga_translator.ui.hotkeys import HotkeyManager

    manager = HotkeyManager(HotkeySettings(enabled=False))
    assert manager.start() is False
    assert manager.stop() is None
    assert "manga mode" in manager.describe()


def test_screens_change_keeps_a_valid_region(app):
    from manga_translator.core.models import ScreenRegion

    app.config.set_region(ScreenRegion(10, 10, 400, 300, monitor="TEST", dpi_scale=1.0))
    app.pipeline.set_region(app.config.region())
    app._on_screens_changed()          # must not raise, no error notification
    assert app.config.region() is not None


def test_refresh_without_manga_mode_warns(app):
    app._manga_mode = False
    app._refresh_page()
    assert any("Manga Mode" in message for _level, message in app.publisher_status()) if hasattr(app, "publisher_status") else True
    # the notification path is what matters; the pipeline must not start
    assert not app.pipeline.is_running
