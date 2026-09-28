"""Configuration, API-key handling, logging redaction and the glossary."""
from __future__ import annotations

import json
import logging

from manga_translator.core.config import Config
from manga_translator.core.logging_setup import _RedactFilter, setup_logging
from manga_translator.core.models import ScreenRegion
from manga_translator.glossary.glossary import Glossary


def test_defaults_are_sane():
    cfg = Config()
    assert cfg.translation.text_model == "gemini-3.8-flash"
    assert cfg.translation.target_language == "Russian"
    assert cfg.ocr.translate_sfx is False
    assert cfg.cache.enabled is True
    assert cfg.general.poll_interval_ms == 150
    assert cfg.general.stable_duration_ms == 350
    assert 0 < cfg.general.change_threshold < 0.5
    assert cfg.overlay.click_through is True


def test_save_and_load_round_trip(tmp_paths):
    cfg = Config.load(tmp_paths)
    cfg.overlay.font_size = 17.5
    cfg.general.change_threshold = 0.12
    cfg.ocr.min_confidence = 0.61
    cfg.set_region(ScreenRegion(10, 20, 800, 600, r"\\.\DISPLAY1", 1.25))
    cfg.save()

    reloaded = Config.load(tmp_paths)
    assert reloaded.overlay.font_size == 17.5
    assert abs(reloaded.general.change_threshold - 0.12) < 1e-9
    assert abs(reloaded.ocr.min_confidence - 0.61) < 1e-9
    region = reloaded.region()
    assert region == ScreenRegion(10, 20, 800, 600, r"\\.\DISPLAY1", 1.25)


def test_config_redacts_the_api_key_for_display_and_logs(tmp_paths, monkeypatch):
    """The key may be stored locally (outside the repo) but must never be shown
    to the user or written to diagnostics."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = Config.load(tmp_paths)
    cfg.translation.api_key = "AIzaSySECRET_should_never_be_displayed"
    cfg.save()

    # stored in the machine-local config (%LOCALAPPDATA%), never in the project
    assert tmp_paths.config.exists()
    assert "AIzaSySECRET" in tmp_paths.config.read_text(encoding="utf-8")

    # ... but redacted wherever it could be displayed or logged
    assert cfg.to_dict()["translation"]["api_key"] == "***"
    status = cfg.api_key_status()
    assert "AIzaSySECRET" not in status
    assert "…" in status  # only the last 4 characters are shown


def test_corrupt_config_falls_back_to_defaults(tmp_paths):
    tmp_paths.config.write_text("{ not json", encoding="utf-8")
    cfg = Config.load(tmp_paths)
    assert cfg.general.poll_interval_ms == 150


def test_env_overrides(tmp_paths, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL", "gemini-test-model")
    monkeypatch.setenv("TARGET_LANGUAGE", "English")
    monkeypatch.setenv("TRANSLATION_BACKEND", "fake")
    monkeypatch.setenv("POLL_INTERVAL_MS", "275")
    monkeypatch.setenv("CHANGE_THRESHOLD", "0.2")
    cfg = Config.load(tmp_paths)
    assert cfg.translation.text_model == "gemini-test-model"
    assert cfg.translation.target_language == "English"
    assert cfg.translation.backend == "fake"
    assert cfg.general.poll_interval_ms == 275
    assert abs(cfg.general.change_threshold - 0.2) < 1e-9


def test_api_key_resolution_order(tmp_paths, monkeypatch):
    cfg = Config.load(tmp_paths)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert cfg.api_key() == ""
    status = cfg.api_key_status()
    assert status.startswith("not set")
    assert "GEMINI_API_KEY" in status          # tells the user which variable to set

    tmp_paths.env_file.write_text("GEMINI_API_KEY=AIzaSyFROMDOTENV\n", encoding="utf-8")
    assert cfg.api_key() == "AIzaSyFROMDOTENV"
    assert "…" in cfg.api_key_status()
    assert "AIzaSyFROMDOTENV" not in cfg.api_key_status()

    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyFROMENV")
    assert cfg.api_key() == "AIzaSyFROMENV"
    assert "env" in cfg.api_key_status()


def test_hotkeys_convert_to_pynput_syntax():
    from manga_translator.core.config import HotkeySettings

    mapping = HotkeySettings().as_pynput()
    assert mapping["toggle_manga_mode"] == "<ctrl>+<alt>+M"
    assert mapping["refresh_page"] == "<ctrl>+<alt>+R"
    assert set(mapping) == {"toggle_manga_mode", "toggle_overlay", "refresh_page", "select_region"}


def test_snapshot_is_independent(tmp_paths):
    cfg = Config.load(tmp_paths)
    snap = cfg.snapshot()
    snap.overlay.font_size = 99.0
    snap.general.region = {"x": 1, "y": 2, "width": 3, "height": 4}
    assert cfg.overlay.font_size != 99.0
    assert cfg.general.region is None


def test_log_redaction_hides_keys():
    flt = _RedactFilter()
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1,
        "request to https://api?key=AIzaSyABCDEFGHIJKLMNOPQRSTUV with api_key=secret123", None, None,
    )
    assert flt.filter(record) is True
    message = record.getMessage()
    assert "AIzaSyABCDEFGHIJKLMNOPQRSTUV" not in message
    assert "secret123" not in message
    assert "redacted" in message


def test_logging_writes_to_file(tmp_paths):
    """setup_logging is a process-wide singleton, so the root handlers are
    swapped out around this test to keep it order independent."""
    from manga_translator.core import logging_setup

    root = logging.getLogger()
    saved = list(root.handlers)
    for handler in saved:
        root.removeHandler(handler)
    logging_setup._CONFIGURED = False
    try:
        logger = setup_logging(tmp_paths.log_file, debug=False, console=False)
        logging.getLogger("mt.test").info("hello from the test")
        for handler in logger.handlers:
            handler.flush()
        assert tmp_paths.log_file.exists()
        assert "hello from the test" in tmp_paths.log_file.read_text(encoding="utf-8")
    finally:
        for handler in list(root.handlers):
            if handler not in saved:
                root.removeHandler(handler)
                handler.close()
        for handler in saved:
            root.addHandler(handler)
        logging_setup._CONFIGURED = bool(saved)


def test_glossary_round_trip_and_relevance(tmp_paths):
    path = tmp_paths.glossary
    g = Glossary(path)
    g.add("林雪", "Линь Сюэ")
    g.add("灵气", "духовная ци")
    g.save()

    reloaded = Glossary(path).load()
    assert len(reloaded) == 2
    assert reloaded.as_dict()["林雪"] == "Линь Сюэ"

    relevant = reloaded.relevant(["林雪来了", "无关的句子"])
    assert list(relevant) == ["林雪"]


def test_glossary_enforce_replaces_leftover_source_terms(tmp_paths):
    g = Glossary(tmp_paths.glossary, {"林雪": "Линь Сюэ", "师兄": "старший брат"})
    out = g.enforce("林雪对师兄说", "林雪 сказала 师兄, что...")
    assert "林雪" not in out and "师兄" not in out
    assert "Линь Сюэ" in out and "старший брат" in out


def test_glossary_loads_list_format(tmp_paths):
    tmp_paths.glossary.write_text(
        json.dumps([{"source": "灵气", "target": "ци"}], ensure_ascii=False), encoding="utf-8"
    )
    g = Glossary(tmp_paths.glossary).load()
    assert g.as_dict() == {"灵气": "ци"}


def test_glossary_sample_creation(tmp_paths):
    g = Glossary(tmp_paths.glossary).load(create_sample=True)
    assert len(g) >= 3
    assert tmp_paths.glossary.exists()
