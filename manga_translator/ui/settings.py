"""Settings dialog (§30) + glossary editor (§15)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.config import Config
from ..core.logging_setup import get_logger
from ..translation.providers import (
    PROVIDERS,
    default_base_url,
    default_model,
    get_spec,
    provider_choices,
    provider_ids,
)
from . import theme
from ..core.i18n import available_languages, tr
from .model_loader import ModelListWorker, TestWorker

log = get_logger("settings")


def _spin(minimum: int, maximum: int, value: int, suffix: str = "", step: int = 1) -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(int(value))
    box.setSingleStep(step)
    if suffix:
        box.setSuffix(suffix)
    return box


def _dspin(minimum: float, maximum: float, value: float, step: float = 0.01, decimals: int = 3) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setValue(float(value))
    box.setSingleStep(step)
    box.setDecimals(decimals)
    return box


def _check(text: str, value: bool) -> QCheckBox:
    box = QCheckBox(text)
    box.setChecked(bool(value))
    return box


class SettingsDialog(QDialog):
    applied = Signal(object)          # Config
    clearCacheRequested = Signal()
    selectRegionRequested = Signal()

    def __init__(self, config: Config, stats: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.cfg = config
        self.setWindowTitle(tr("Manga Translator — Settings"))
        self.setMinimumWidth(560)
        self._stats = stats or {}

        tabs = QTabWidget(self)
        tabs.addTab(self._general_tab(), tr("General"))
        tabs.addTab(self._ocr_tab(), tr("OCR"))
        tabs.addTab(self._translation_tab(), tr("Translation"))
        tabs.addTab(self._overlay_tab(), tr("Overlay"))
        tabs.addTab(self._cache_tab(), tr("Cache"))
        tabs.addTab(self._hotkeys_tab(), tr("Hotkeys"))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel | QDialogButtonBox.Apply)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self.apply)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # -- tabs -------------------------------------------------------------
    def _general_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        g = self.cfg.general
        self.chk_manga = _check(tr("Manga Mode enabled"), g.manga_mode_enabled)

        self.cmb_language = QComboBox()
        for code, label in available_languages():
            self.cmb_language.addItem(label, code)
        language_index = self.cmb_language.findData(g.language)
        self.cmb_language.setCurrentIndex(language_index if language_index >= 0 else 0)
        form.addRow(tr("Interface language"), self.cmb_language)
        form.addRow(self.chk_manga)

        region = self.cfg.region()
        text = "not selected" if region is None else (
            f"{region.width}×{region.height} at ({region.x},{region.y}) "
            f"· {region.monitor or 'display'} · {region.dpi_scale * 100:.0f}%"
        )
        self.lbl_region = QLabel(text)
        row = QHBoxLayout()
        row.addWidget(self.lbl_region)
        button = QPushButton("Select Region…")
        button.clicked.connect(self.selectRegionRequested.emit)
        row.addWidget(button)
        wrapper = QWidget()
        wrapper.setLayout(row)
        form.addRow(tr("Capture region"), wrapper)

        self.sp_poll = _spin(50, 2000, g.poll_interval_ms, " ms", 10)
        self.sp_stable = _spin(100, 3000, g.stable_duration_ms, " ms", 25)
        self.sp_threshold = _dspin(0.005, 0.9, g.change_threshold, 0.005)
        self.sp_preview = _spin(80, 800, g.preview_max_dim, " px", 20)
        self.sp_maxwait = _spin(500, 20000, g.max_wait_ms, " ms", 250)
        self.chk_debug = _check(tr("Debug mode (OCR boxes, hashes, timings)"), g.debug_mode)
        form.addRow(tr("Change detection interval"), self.sp_poll)
        form.addRow(tr("Stabilization duration"), self.sp_stable)
        form.addRow(tr("Change threshold"), self.sp_threshold)
        form.addRow(tr("Preview size"), self.sp_preview)
        form.addRow(tr("Force processing after"), self.sp_maxwait)
        form.addRow(self.chk_debug)
        return page

    def _ocr_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        o = self.cfg.ocr
        self.cmb_engine = QComboBox()
        self.cmb_engine.addItems(["rapid", "paddle", "stub"])
        self.cmb_engine.setCurrentText(o.engine)
        self.sp_conf = _dspin(0.05, 0.99, o.min_confidence, 0.05, 2)
        self.chk_keep_low = _check(tr("Keep low-confidence text for debug"), o.keep_low_confidence)
        self.chk_simplified = _check(tr("Simplified Chinese"), o.simplified_chinese)
        self.chk_traditional = _check(tr("Traditional Chinese"), o.traditional_chinese)
        self.chk_vertical = _check(tr("Re-read vertical CJK columns"), o.vertical_pass)
        self.chk_chrome = _check(tr("Ignore site UI text (titles, next-chapter bars)"),
                                 o.drop_ui_chrome)
        self.chk_static = _check(tr("Drop site menus that never move"),
                                 o.suppress_static_text)
        self.chk_sfx_browser = _check(
            tr("Browser mode: translate big stylised text too"), o.translate_sfx_in_browser
        )
        self.chk_sfx = _check(tr("Translate sound effects (TRANSLATE_SFX)"), o.translate_sfx)
        self.sp_maxside = _spin(600, 4000, o.max_ocr_side, " px", 100)
        self.sp_gap = _dspin(0.2, 3.0, o.group_gap_ratio, 0.05, 2)
        form.addRow(tr("Engine"), self.cmb_engine)
        form.addRow(tr("Minimum confidence"), self.sp_conf)
        form.addRow(tr("OCR downscale limit"), self.sp_maxside)
        form.addRow(tr("Line grouping gap ratio"), self.sp_gap)
        form.addRow(self.chk_keep_low)
        form.addRow(self.chk_simplified)
        form.addRow(self.chk_traditional)
        form.addRow(self.chk_vertical)
        form.addRow(self.chk_chrome)
        form.addRow(self.chk_static)
        form.addRow(self.chk_sfx_browser)
        form.addRow(self.chk_sfx)
        return page

    def _translation_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        t = self.cfg.translation

        self.cmb_backend = QComboBox()
        for provider_id, label in provider_choices():
            self.cmb_backend.addItem(label, provider_id)
        current = t.active_provider()
        index = self.cmb_backend.findData(current)
        self.cmb_backend.setCurrentIndex(index if index >= 0 else 0)
        self.cmb_backend.currentIndexChanged.connect(self._on_provider_changed)

        self.lbl_provider_info = QLabel("")
        self.lbl_provider_info.setWordWrap(True)
        self.lbl_provider_info.setStyleSheet(f"color: {theme.TEXT_DIM};")

        self.lbl_key = QLabel(self.cfg.api_key_status(current))
        self.lbl_key.setWordWrap(True)
        self.ed_key = QLineEdit(self.cfg.api_key(current))
        self.ed_key.setEchoMode(QLineEdit.Password)
        self.ed_key.setPlaceholderText(tr("key for the selected provider (empty = use .env / environment)"))

        self.cmb_model = QComboBox()
        self.cmb_model.setEditable(True)
        self.cmb_model.setInsertPolicy(QComboBox.NoInsert)
        self.cmb_model.setMinimumWidth(280)
        self.btn_models = QPushButton(tr("Load models"))
        self.btn_models.setToolTip(tr("Ask the provider which models this API key can use"))
        self.btn_models.clicked.connect(self.load_models)
        self.lbl_models = QLabel("")
        self.lbl_models.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.lbl_models.setWordWrap(True)
        model_row = QHBoxLayout()
        model_row.addWidget(self.cmb_model, 1)
        model_row.addWidget(self.btn_models)
        model_widget = QWidget()
        model_widget.setLayout(model_row)

        self.cmb_target = QComboBox()
        self.cmb_target.setEditable(True)
        self.cmb_target.setInsertPolicy(QComboBox.NoInsert)
        self.cmb_target.addItems(["Russian", "English", "Ukrainian", "German", "Spanish"])
        self.cmb_target.setCurrentText(t.target_language)

        self.ed_base = QLineEdit(t.base_url)
        self.btn_base_default = QPushButton(tr("Default"))
        self.btn_base_default.setToolTip(tr("Reset to the provider's default endpoint"))
        self.btn_base_default.clicked.connect(self._reset_base_url)
        base_row = QHBoxLayout()
        base_row.addWidget(self.ed_base, 1)
        base_row.addWidget(self.btn_base_default)
        base_widget = QWidget()
        base_widget.setLayout(base_row)

        self.btn_test = QPushButton(tr("Test connection"))
        self.btn_test.clicked.connect(self.test_connection)
        self.lbl_test = QLabel("")
        self.lbl_test.setWordWrap(True)
        self.lbl_test.setStyleSheet(f"color: {theme.TEXT_DIM};")
        test_row = QHBoxLayout()
        test_row.addWidget(self.btn_test)
        test_row.addWidget(self.lbl_test, 1)
        test_widget = QWidget()
        test_widget.setLayout(test_row)

        self.sp_context = _spin(0, 6, t.context_pages, " pages")
        self.ed_glossary = QLineEdit(str(self.cfg.glossary_file()))
        self.sp_timeout = _dspin(3.0, 180.0, t.timeout_s, 1.0, 1)
        self.sp_retries = _spin(0, 5, t.max_retries)
        self.sp_temp = _dspin(0.0, 1.0, t.temperature, 0.05, 2)
        self.sp_blocks = _spin(5, 200, t.max_blocks_per_request, " blocks", 5)
        self.cmb_response = QComboBox()
        for value, label in (("auto", "auto"), ("schema", "JSON schema"), ("object", "JSON object"),
                             ("off", "prompt only")):
            self.cmb_response.addItem(label, value)
        response_index = self.cmb_response.findData(getattr(t, "response_mode", "auto"))
        self.cmb_response.setCurrentIndex(response_index if response_index >= 0 else 0)
        self.cmb_response.setToolTip(
            "Some gateways/reasoning models answer several times faster when no "
            "response_format is sent; 'auto' starts with the provider default and "
            "drops it when the endpoint is slow"
        )

        form.addRow(tr("Provider"), self.cmb_backend)
        form.addRow("", self.lbl_provider_info)
        form.addRow(tr("API key status"), self.lbl_key)
        form.addRow(tr("API key"), self.ed_key)
        form.addRow(tr("Model"), model_widget)
        form.addRow("", self.lbl_models)
        form.addRow(tr("Target language"), self.cmb_target)
        form.addRow(tr("Endpoint"), base_widget)
        form.addRow(tr("Context pages"), self.sp_context)
        form.addRow(tr("Glossary file"), self.ed_glossary)
        form.addRow(tr("Request timeout"), self.sp_timeout)
        form.addRow(tr("API retries"), self.sp_retries)
        form.addRow(tr("Blocks per request"), self.sp_blocks)
        form.addRow(tr("Structured output"), self.cmb_response)
        form.addRow(tr("Temperature (LLMs)"), self.sp_temp)
        form.addRow("", test_widget)

        self._models_worker = None
        self._test_worker = None
        self._provider_default_base = default_base_url(current)
        self._populate_models(current, t.text_model)
        self._update_provider_labels(current)
        return page

    # -- provider helpers -------------------------------------------------
    def _current_provider(self) -> str:
        return str(self.cmb_backend.currentData() or "gemini")

    def _update_provider_labels(self, provider: str) -> None:
        spec = get_spec(provider)
        if spec is None:
            self.lbl_provider_info.setText("")
            return
        bits = [f"<b>{spec.label}</b>"]
        if spec.note:
            bits.append(spec.note)
        if spec.key_help:
            bits.append(f"key: {spec.key_help}")
        if spec.local:
            bits.append("runs on this machine")
        if not spec.is_llm:
            bits.append("no page context — but one request per page, quick and cheap")
        self.lbl_provider_info.setText(" · ".join(bits))
        self.lbl_key.setText(self.cfg.api_key_status(provider))
        self.ed_key.setPlaceholderText(
            f"{spec.key_label} — empty = use "
            + (" / ".join(spec.key_env) if spec.key_env else "no key")
        )
        self.cmb_target.setToolTip(
            "Target language" if spec.is_llm else "Target language code sent to the MT API"
        )
        self.btn_models.setEnabled(spec.supports_model_list or spec.supports_language_list)
        self.btn_models.setText(
            tr("Load languages") if spec.supports_language_list and not spec.is_llm
            else tr("Load models")
        )

    def _populate_models(self, provider: str, current: str) -> None:
        spec = get_spec(provider)
        self.cmb_model.blockSignals(True)
        self.cmb_model.clear()
        models = list(spec.static_models) if spec else []
        # a model saved for a *different* provider must not stick around
        foreign = {
            name
            for other_id, other in PROVIDERS.items() if other_id != provider
            for name in other.static_models
        }
        if current and current not in models and current in foreign:
            current = default_model(provider)
        if current and current not in models:
            models.insert(0, current)
        for name in models:
            self.cmb_model.addItem(name)
        if current:
            index = self.cmb_model.findText(current)
            self.cmb_model.setCurrentIndex(index if index >= 0 else 0)
        self.cmb_model.blockSignals(False)
        self.lbl_models.setText(
            f"{len(models)} built-in option(s); press "
            f"'{self.btn_models.text()}' to fetch the live list with your key"
            if models else "press 'Load models' to fetch the list with your key"
        )

    def _on_provider_changed(self, _index: int) -> None:
        provider = self._current_provider()
        # keep a custom endpoint, but follow the provider default when the field
        # still holds some provider's default (or is empty)
        if self._is_default_base(self.ed_base.text()):
            self._provider_default_base = default_base_url(provider)
            self.ed_base.setText(self._provider_default_base)
        self._populate_models(provider, default_model(provider))
        self._update_provider_labels(provider)
        self.lbl_test.setText("")
        if self.cfg.api_key(provider):
            self.load_models()

    @staticmethod
    def _is_default_base(text: str) -> bool:
        value = (text or "").strip()
        if not value:
            return True
        return value in {default_base_url(pid) for pid in provider_ids() if default_base_url(pid)}

    def _reset_base_url(self) -> None:
        self._provider_default_base = default_base_url(self._current_provider())
        self.ed_base.setText(self._provider_default_base)

    def _set_models_label(self, text: str, color: str = None) -> None:
        self.lbl_models.setText(text)
        self.lbl_models.setStyleSheet(f"color: {color or theme.TEXT_DIM};")

    def load_models(self) -> None:
        """Fetch the model/language list for the current provider + key."""
        if self._models_worker is not None and self._models_worker.isRunning():
            return
        provider = self._current_provider()
        key = self.ed_key.text().strip() or self.cfg.api_key(provider)
        self._set_models_label(tr("loading from {provider}…").format(provider=provider))
        self.btn_models.setEnabled(False)
        worker = ModelListWorker(provider, self.ed_base.text().strip(), key, self)
        worker.loaded.connect(self._on_models_loaded)
        worker.finished.connect(lambda: self.btn_models.setEnabled(True))
        self._models_worker = worker
        worker.start()

    def _on_models_loaded(self, items: list, unit: str, error: str) -> None:
        provider = self._current_provider()
        spec = get_spec(provider)
        current = self.cmb_model.currentText().strip()
        if items:
            self.cmb_model.blockSignals(True)
            self.cmb_model.clear()
            for name in items:
                self.cmb_model.addItem(str(name))
            index = self.cmb_model.findText(current)
            if index < 0 and current:
                self.cmb_model.insertItem(0, current)
                index = 0
            self.cmb_model.setCurrentIndex(max(0, index))
            self.cmb_model.blockSignals(False)
            if unit == "language":
                self.cmb_target.blockSignals(True)
                if spec is not None and not spec.is_llm:
                    self.cmb_target.clear()
                    for code in items:
                        self.cmb_target.addItem(str(code))
                    for candidate in (self.cfg.translation.target_language, "ru", "Russian"):
                        at = self.cmb_target.findText(candidate)
                        if at >= 0:
                            self.cmb_target.setCurrentIndex(at)
                            break
                self.cmb_target.blockSignals(False)
            suffix = f" · {error}" if error else ""
            self._set_models_label(
                f"{len(items)} {unit}s from {spec.label if spec else provider}{suffix}",
                theme.OK if not error else theme.WARN,
            )
            self.lbl_key.setText(self.cfg.api_key_status(provider))
        else:
            self._populate_models(provider, current)
            self._set_models_label(
                f"could not fetch the list ({error or 'no key'}) — using built-in options",
                theme.WARN,
            )

    def test_connection(self) -> None:
        if self._test_worker is not None and self._test_worker.isRunning():
            return
        provider = self._current_provider()
        key = self.ed_key.text().strip() or self.cfg.api_key(provider)
        self.lbl_test.setText(tr("translating a test line…"))
        self.lbl_test.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.btn_test.setEnabled(False)
        worker = TestWorker(
            self.cfg, provider, self.ed_base.text().strip(), key,
            self.cmb_model.currentText().strip(), self.cmb_target.currentText().strip(), self,
        )
        worker.done.connect(self._on_test_done)
        worker.finished.connect(lambda: self.btn_test.setEnabled(True))
        self._test_worker = worker
        worker.start()

    def _on_test_done(self, ok: bool, message: str) -> None:
        self.lbl_test.setText(message)
        self.lbl_test.setStyleSheet(f"color: {theme.OK if ok else theme.ERROR};")

    def _overlay_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        o = self.cfg.overlay
        self.sp_font = _dspin(6.0, 48.0, o.font_size, 1.0, 1)
        self.sp_opacity = _dspin(0.1, 1.0, o.opacity, 0.05, 2)
        self.sp_padding = _dspin(2.0, 30.0, o.padding, 1.0, 1)
        self.sp_width = _spin(120, 900, o.max_card_width, " px", 20)
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["translation", "original", "both"])
        self.cmb_mode.setCurrentText(o.mode)
        self.chk_click = _check(tr("Click-through (never blocks scrolling/clicking)"), o.click_through)
        self.chk_hud = _check(tr("Show debug HUD"), o.show_debug_hud)
        self.chk_visible = _check(tr("Overlay visible"), o.visible)
        form.addRow(tr("Font size"), self.sp_font)
        form.addRow(tr("Card opacity"), self.sp_opacity)
        form.addRow(tr("Card padding"), self.sp_padding)
        form.addRow(tr("Max card width"), self.sp_width)
        form.addRow(tr("Display mode"), self.cmb_mode)
        form.addRow(self.chk_click)
        form.addRow(self.chk_hud)
        form.addRow(self.chk_visible)
        return page

    def _cache_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        c = self.cfg.cache
        self.chk_cache = _check(tr("Cache enabled"), c.enabled)
        self.sp_maxmb = _spin(16, 20000, c.max_mb, " MB", 100)
        self.sp_hamming = _spin(0, 20, c.hamming_threshold)
        self.sp_age = _spin(0, 3650, c.max_age_days, " days", 5)
        stats = self._stats.get("cache") or {}
        label = QLabel(
            f"{int(stats.get('entries', 0))} pages · "
            f"{float(stats.get('bytes', 0)) / 1e6:.1f} MB · {int(stats.get('hits', 0))} hits"
        )
        button = QPushButton(tr("Clear cache now"))
        button.clicked.connect(self.clearCacheRequested.emit)
        form.addRow(self.chk_cache)
        form.addRow(tr("Maximum size"), self.sp_maxmb)
        form.addRow(tr("Page match tolerance (pHash bits)"), self.sp_hamming)
        form.addRow(tr("Maximum age"), self.sp_age)
        form.addRow(tr("Stored"), label)
        form.addRow(button)
        return page

    def _hotkeys_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        h = self.cfg.hotkeys
        self.chk_hotkeys = _check(tr("Global hotkeys enabled"), h.enabled)
        self.ed_toggle = QLineEdit(h.toggle_manga_mode)
        self.ed_overlay = QLineEdit(h.toggle_overlay)
        self.ed_refresh = QLineEdit(h.refresh_page)
        self.ed_region = QLineEdit(h.select_region)
        hint = QLabel(tr("Format: ctrl+alt+m · modifiers: ctrl, alt, shift"))
        hint.setStyleSheet("color: #94a3b8;")
        form.addRow(self.chk_hotkeys)
        form.addRow(tr("Toggle Manga Mode"), self.ed_toggle)
        form.addRow(tr("Show/Hide translation"), self.ed_overlay)
        form.addRow(tr("Refresh page"), self.ed_refresh)
        form.addRow(tr("Select region"), self.ed_region)
        form.addRow(hint)
        return page

    # -- collect / apply ---------------------------------------------------
    def collect(self) -> Config:
        cfg = self.cfg
        provider = self._current_provider()
        g, o, t, ov, c, h = (
            cfg.general,
            cfg.ocr,
            cfg.translation,
            cfg.overlay,
            cfg.cache,
            cfg.hotkeys,
        )
        g.manga_mode_enabled = self.chk_manga.isChecked()
        g.language = str(self.cmb_language.currentData() or g.language)
        g.poll_interval_ms = self.sp_poll.value()
        g.stable_duration_ms = self.sp_stable.value()
        g.change_threshold = self.sp_threshold.value()
        g.preview_max_dim = self.sp_preview.value()
        g.max_wait_ms = self.sp_maxwait.value()
        g.debug_mode = self.chk_debug.isChecked()

        o.engine = self.cmb_engine.currentText()
        o.min_confidence = self.sp_conf.value()
        o.max_ocr_side = self.sp_maxside.value()
        o.group_gap_ratio = self.sp_gap.value()
        o.keep_low_confidence = self.chk_keep_low.isChecked()
        o.simplified_chinese = self.chk_simplified.isChecked()
        o.traditional_chinese = self.chk_traditional.isChecked()
        o.vertical_pass = self.chk_vertical.isChecked()
        o.drop_ui_chrome = self.chk_chrome.isChecked()
        o.suppress_static_text = self.chk_static.isChecked()
        o.translate_sfx_in_browser = self.chk_sfx_browser.isChecked()
        o.translate_sfx = self.chk_sfx.isChecked()

        t.backend = self.cmb_backend.currentText()
        entered = self.ed_key.text().strip()
        if entered and entered != "***":
            cfg.set_api_key(provider, entered)
        t.text_model = self.cmb_model.currentText().strip() or t.text_model
        t.target_language = self.cmb_target.currentText().strip() or "Russian"
        t.provider = provider
        t.backend = provider
        t.base_url = self.ed_base.text().strip() or t.base_url
        t.context_pages = self.sp_context.value()
        t.glossary_path = self.ed_glossary.text().strip()
        t.timeout_s = self.sp_timeout.value()
        t.max_retries = self.sp_retries.value()
        t.temperature = self.sp_temp.value()
        t.max_blocks_per_request = self.sp_blocks.value()
        t.response_mode = str(self.cmb_response.currentData() or "auto")

        ov.font_size = self.sp_font.value()
        ov.opacity = self.sp_opacity.value()
        ov.padding = self.sp_padding.value()
        ov.max_card_width = self.sp_width.value()
        ov.mode = self.cmb_mode.currentText()
        ov.click_through = self.chk_click.isChecked()
        ov.show_debug_hud = self.chk_hud.isChecked()
        ov.visible = self.chk_visible.isChecked()

        c.enabled = self.chk_cache.isChecked()
        c.max_mb = self.sp_maxmb.value()
        c.hamming_threshold = self.sp_hamming.value()
        c.max_age_days = self.sp_age.value()

        h.enabled = self.chk_hotkeys.isChecked()
        h.toggle_manga_mode = self.ed_toggle.text().strip() or h.toggle_manga_mode
        h.toggle_overlay = self.ed_overlay.text().strip() or h.toggle_overlay
        h.refresh_page = self.ed_refresh.text().strip() or h.refresh_page
        h.select_region = self.ed_region.text().strip() or h.select_region
        return cfg

    def apply(self) -> None:
        cfg = self.collect()
        cfg.save()
        self.applied.emit(cfg)

    def _on_ok(self) -> None:
        self.apply()
        self.accept()


class GlossaryDialog(QDialog):
    """Simple two-column glossary editor."""

    saved = Signal(object)  # Glossary

    def __init__(self, glossary, parent=None):
        super().__init__(parent)
        self.glossary = glossary
        self.setWindowTitle(tr("Glossary — term consistency"))
        self.resize(460, 420)

        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels([tr("Chinese source"), tr("Russian target")])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for source, target in glossary.as_dict().items():
            self._add_row(source, target)

        add = QPushButton(tr("Add term"))
        add.clicked.connect(lambda: self._add_row("", ""))
        remove = QPushButton(tr("Remove selected"))
        remove.clicked.connect(self._remove_selected)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Stored in: {glossary.path}"))
        layout.addWidget(self.table)
        layout.addLayout(row)
        layout.addWidget(buttons)

    def _add_row(self, source: str, target: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(source))
        self.table.setItem(row, 1, QTableWidgetItem(target))

    def _remove_selected(self) -> None:
        for index in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(index)

    def _save(self) -> None:
        entries = {}
        for row in range(self.table.rowCount()):
            source_item = self.table.item(row, 0)
            target_item = self.table.item(row, 1)
            source = (source_item.text() if source_item else "").strip()
            target = (target_item.text() if target_item else "").strip()
            if source and target:
                entries[source] = target
        self.glossary.replace_all(entries)
        self.glossary.save()
        self.saved.emit(self.glossary)
        self.accept()


class StatsDialog(QDialog):
    def __init__(self, payload: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Manga Translator — Statistics"))
        self.resize(420, 340)
        text = QLabel(_format_stats(payload))
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _format_stats(payload: dict) -> str:
    stats = payload.get("stats", {})
    cache = payload.get("cache", {})
    region = payload.get("region") or {}
    lines = [
        f"OCR engine:      {payload.get('engine', '-')}",
        f"Translator:      {payload.get('backend', '-')} ({payload.get('model', '-')})",
        f"Generation:      {payload.get('generation', 0)}",
        f"Pages processed: {stats.get('pages_processed', 0)}",
        f"Cache hits:      {stats.get('cache_hits', 0)}",
        f"Stale discarded: {stats.get('stale_discarded', 0)}",
        f"Avg OCR:         {stats.get('avg_ocr_ms', 0)} ms",
        f"Avg translation: {stats.get('avg_translate_ms', 0)} ms",
        f"Blocks total:    {stats.get('blocks_total', 0)}",
        f"Errors:          {stats.get('errors', 0)}",
        "",
        f"Cache entries:   {int(cache.get('entries', 0))}",
        f"Cache size:      {float(cache.get('bytes', 0)) / 1e6:.1f} MB",
        "",
        "Region:          "
        + (
            f"{region.get('width')}×{region.get('height')} at ({region.get('x')},{region.get('y')})"
            if region
            else "not selected"
        ),
    ]
    return "\n".join(lines)
