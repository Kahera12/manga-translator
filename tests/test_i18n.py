"""UI localisation: dictionary completeness, placeholders and live widgets."""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from manga_translator.core.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    RU,
    available_languages,
    get_language,
    set_language,
    tr,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "manga_translator"

PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)[^}]*\}")


@pytest.fixture(autouse=True)
def english_afterwards():
    """Every test starts and ends in English so other modules stay unaffected."""
    set_language("en")
    yield
    set_language("en")


def _tr_keys_in_sources() -> dict:
    """Collect every tr("...") literal used in the package."""
    keys: dict = {}
    for path in PACKAGE.rglob("*.py"):
        if path.name == "i18n.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_tr = (isinstance(func, ast.Name) and func.id == "tr") or (
                isinstance(func, ast.Attribute) and func.attr == "tr"
            )
            if not is_tr or not node.args:
                continue
            first = node.args[0]
            parts = []
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                parts = [first.value]
            elif isinstance(first, ast.BinOp):  # implicit concatenation is folded by ast
                parts = [""]
            elif isinstance(first, ast.IfExp):
                for branch in (first.body, first.orelse):
                    if isinstance(branch, ast.Constant) and isinstance(branch.value, str):
                        parts.append(branch.value)
            for value in parts:
                if value:
                    keys.setdefault(value, set()).add(str(path.relative_to(ROOT)))
    return keys


def test_languages_are_declared():
    codes = {code for code, _label in available_languages()}
    assert {"ru", "en"} <= codes
    assert DEFAULT_LANGUAGE == "ru", "the interface ships in Russian by default"
    assert all(label for _code, label in LANGUAGES)


def test_translation_is_used_for_key_ui_strings():
    set_language("ru")
    for english, expected in (
        ("Manga Mode", "Режим манги"),
        ("Refresh Page", "Обновить страницу"),
        ("Dialogue blocks", "Блоки диалога"),
        ("Settings", "Настройки"),
        ("Log", "Журнал"),
        ("Select a region first.", "Сначала выберите область."),
        ("Reading the page…", "Читаю страницу…"),
        ("No Chinese dialogue found on this page.", "Китайский текст на этой странице не найден."),
        ("Page unchanged.", "Страница не изменилась."),
        ("Original (zh)", "Оригинал (zh)"),
        ("Copy translations", "Копировать перевод"),
        ("Load models", "Загрузить модели"),
        ("Test connection", "Проверить соединение"),
        ("Interface language", "Язык интерфейса"),
    ):
        assert tr(english) == expected, english
    set_language("en")
    assert tr("Manga Mode") == "Manga Mode", "English falls back to the source string"


def test_every_tr_call_has_a_translation():
    missing = {}
    for key, files in _tr_keys_in_sources().items():
        if key not in RU:
            missing[key] = sorted(files)
    assert not missing, f"untranslated UI strings: {missing}"


def test_placeholders_match_between_languages():
    mismatched = {}
    for english, russian in RU.items():
        en = set(PLACEHOLDER.findall(english))
        ru = set(PLACEHOLDER.findall(russian))
        if en != ru:
            mismatched[english] = (sorted(en), sorted(ru))
    assert not mismatched, f"placeholder mismatch: {mismatched}"


def test_no_leftover_english_markers_in_russian():
    """A few strings are intentionally identical (product/glyph names)."""
    allowed = {
        "Manga Translator", "Manga Translator — {status}", "OCR", "SFX", "OK", "Google Gemini",
        "Default", "Endpoint", "docker run -p 5000:5000 libretranslate/libretranslate",
        "Translator",
        # pure HTML/markup wrappers, nothing to translate inside
        "<b>{name}</b> {version}<br>", "OCR: {engine}<br>",
    }
    suspicious = [
        key for key, value in RU.items()
        if value == key and key not in allowed and len(key) > 4
    ]
    assert not suspicious, f"untranslated values: {suspicious}"


def test_set_language_ignores_unknown_codes():
    assert set_language("de") == DEFAULT_LANGUAGE
    assert set_language("en") == "en"
    assert get_language() == "en"
    assert set_language("") == DEFAULT_LANGUAGE


def test_templates_format_correctly():
    set_language("ru")
    text = tr("{blocks} blocks · {translated} translated").format(blocks=3, translated=2)
    assert text == "3 блоков · переведено 2"
    status = tr("pages {pages} · cache hits {hits} · stale dropped {stale} · errors {errors}").format(
        pages=9, hits=4, stale=1, errors=0
    )
    assert status == "страниц 9 · попаданий в кэш 4 · устаревших отброшено 1 · ошибок 0"
    region = tr("{w}×{h} at ({x},{y}) · {monitor}").format(w=1200, h=900, x=10, y=20, monitor="TEST")
    assert region == "1200×900 в (10,20) · TEST"


def test_config_defaults_to_russian_and_env_override(tmp_path, monkeypatch):
    from manga_translator.core.config import Config
    from manga_translator.core.paths import Paths

    root = tmp_path / "mt"
    paths = Paths(
        root=root, config=root / "config.json", glossary=root / "glossary.json",
        cache_dir=root / "cache", cache_db=root / "cache" / "pages.db",
        log_dir=root / "logs", log_file=root / "logs" / "app.log",
        debug_dir=root / "debug", env_file=root / ".env",
    ).ensure()

    monkeypatch.delenv("UI_LANGUAGE", raising=False)
    assert Config.load(paths).general.language == "ru"

    monkeypatch.setenv("UI_LANGUAGE", "en")
    assert Config.load(paths).general.language == "en"


def test_settings_dialog_switches_language(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRANSLATION_BACKEND", "fake")

    from manga_translator.app import MangaTranslatorApp
    from manga_translator.ui.settings import SettingsDialog

    application = MangaTranslatorApp([])
    try:
        dialog = SettingsDialog(application.config, application.pipeline.describe())
        index = dialog.cmb_language.findData("en")
        dialog.cmb_language.setCurrentIndex(index)
        assert dialog.collect().general.language == "en"
        assert dialog.cmb_language.currentText() in ("English", "Русский")
        dialog.deleteLater()
    finally:
        application.shutdown()


def test_window_retranslate_switches_all_labels(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRANSLATION_BACKEND", "fake")

    from manga_translator.app import MangaTranslatorApp

    application = MangaTranslatorApp([])
    try:
        window = application.window
        set_language("en")
        window.retranslate()
        assert window.act_region.text() == "Select Region"
        assert window.blocks_panel.btn_copy.text() == "Copy translations"

        set_language("ru")
        window.retranslate()
        assert window.act_region.text() == "Выбрать область"
        assert window.act_region.text() != "Select Region"
        assert window.blocks_panel.btn_copy.text() == "Копировать перевод"
        assert window.blocks_panel.table.horizontalHeaderItem(1).text() == "Оригинал (zh)"
        assert window.status_panel.lbl_mode.text() in ("ВКЛ", "выкл")
        assert "панель управления" in window.windowTitle()
        assert window.act_refresh.text() == "Обновить страницу"
        from PySide6.QtWidgets import QFormLayout

        hotkey_label = window.hotkey_panel.form.itemAt(0, QFormLayout.LabelRole).widget()
        assert hotkey_label is not None and hotkey_label.text() != "Select region"
    finally:
        application.shutdown()


def test_tray_retranslate(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MT_DATA_DIR", str(tmp_path / "data"))

    from PySide6.QtWidgets import QApplication

    from manga_translator.ui.tray import Tray

    QApplication.instance() or QApplication([])
    tray = Tray()
    set_language("ru")
    tray.retranslate()
    assert tray.act_region.text() == "Выбрать область…"
    assert tray.act_quit.text() == "Выход"
    tray.set_manga_mode(True)
    assert tray.act_manga.text() == "Выключить режим манги"
    tray.set_overlay_visible(False)
    assert tray.act_overlay.text() == "Показать перевод"
    set_language("en")
    tray.retranslate()
    assert tray.act_region.text() == "Select Region…"
    assert tray.act_manga.text() == "Disable Manga Mode"
