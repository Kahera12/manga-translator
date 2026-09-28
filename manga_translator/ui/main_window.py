"""Control-panel window: live preview, status, dialogue blocks, log, controls.

The window is a pure view: it renders whatever the pipeline publishes and emits
signals for every user action. `MangaTranslatorApp` connects those signals to the
same handlers the tray menu uses, so both entry points behave identically.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStyle,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, __version__
from ..core.logging_setup import get_logger
from ..core.models import PageResult, Rect, ScreenRegion
from ..overlay.widgets import DebugInfo
from ..pipeline.orchestrator import PageJob
from . import theme
from ..core.i18n import tr
from .panels import BlocksPanel, BridgePanel, HotkeyPanel, LogPanel, StatusPanel
from .preview import PagePreviewWidget

log = get_logger("ui.window")


class MainWindow(QMainWindow):
    # commands (connected by the app to the same handlers the tray uses)
    mangaModeToggled = Signal(bool)
    overlayToggled = Signal(bool)
    selectRegionRequested = Signal()
    selectWindowRequested = Signal(int)
    browserModeRequested = Signal()
    bridgeToggled = Signal(bool)
    refreshRequested = Signal()
    settingsRequested = Signal()
    glossaryRequested = Signal()
    statisticsRequested = Signal()
    clearCacheRequested = Signal()
    openLogsRequested = Signal()
    openDataRequested = Signal()
    quitRequested = Signal()
    previewRequested = Signal()
    debugHudToggled = Signal(bool)
    hideToTrayRequested = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._last_result: Optional[PageResult] = None
        self._last_region: Optional[ScreenRegion] = None
        self._busy = False
        self._detector_state = "idle"

        self.setWindowTitle(tr("Manga Translator — control panel"))
        self.setMinimumSize(980, 640)
        self.setStyleSheet(theme.STYLESHEET)

        self._build_toolbar()
        self._build_menu()
        self._build_body()
        self._build_statusbar()

        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(1000)
        self._preview_timer.timeout.connect(self._request_preview)
        self._preview_timer.start()

        self._state_timer = QTimer(self)
        self._state_timer.setInterval(1000)
        self._state_timer.timeout.connect(self.refresh_state)
        self._state_timer.start()

        self.refresh_state()

    # -- construction -----------------------------------------------------
    def _icon(self, pixmap) -> object:
        return self.style().standardIcon(pixmap)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setMovable(False)
        bar.setIconSize(QSize(16, 16))
        self.addToolBar(bar)

        self.act_manga = QAction(self._icon(QStyle.SP_MediaPlay), tr("Manga Mode"), self)
        self.act_manga.setCheckable(True)
        self.act_manga.setToolTip(tr("Enable/disable automatic page translation"))
        self.act_manga.toggled.connect(self.mangaModeToggled.emit)
        bar.addAction(self.act_manga)

        self.act_overlay = QAction(self._icon(QStyle.SP_FileDialogDetailedView), tr("Translation"), self)
        self.act_overlay.setCheckable(True)
        self.act_overlay.setChecked(True)
        self.act_overlay.setToolTip(tr("Show/hide the on-screen translation overlay"))
        self.act_overlay.toggled.connect(self.overlayToggled.emit)
        bar.addAction(self.act_overlay)

        bar.addSeparator()

        self.act_region = QAction(self._icon(QStyle.SP_DialogResetButton), tr("Select Region"), self)
        self.act_region.triggered.connect(self.selectRegionRequested.emit)
        bar.addAction(self.act_region)

        self.act_window = QAction(self._icon(QStyle.SP_DirOpenIcon), tr("Select Window"), self)
        self.act_window.triggered.connect(self._choose_window)
        bar.addAction(self.act_window)

        self.act_browser = QAction(self._icon(QStyle.SP_DriveNetIcon), tr("Browser mode…"), self)
        self.act_browser.setToolTip(tr("Translate a browser tab directly (no screen capture)"))
        self.act_browser.triggered.connect(self.browserModeRequested.emit)
        bar.addAction(self.act_browser)

        self.act_refresh = QAction(self._icon(QStyle.SP_BrowserReload), tr("Refresh Page"), self)
        self.act_refresh.triggered.connect(self.refreshRequested.emit)
        bar.addAction(self.act_refresh)

        bar.addSeparator()

        self.act_settings = QAction(self._icon(QStyle.SP_FileDialogContentsView), tr("Settings"), self)
        self.act_settings.triggered.connect(self.settingsRequested.emit)
        bar.addAction(self.act_settings)

        self.act_glossary = QAction(tr("Glossary"), self)
        self.act_glossary.triggered.connect(self.glossaryRequested.emit)
        bar.addAction(self.act_glossary)

        self.act_tray = QAction(tr("Hide to tray"), self)
        self.act_tray.triggered.connect(self._hide_to_tray)
        bar.addAction(self.act_tray)

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu(tr("File"))
        file_menu.addAction(self.act_settings)
        file_menu.addAction(self.act_glossary)
        file_menu.addSeparator()
        hide = file_menu.addAction(tr("Hide to tray"))
        hide.setShortcut(QKeySequence("Ctrl+W"))
        hide.triggered.connect(self._hide_to_tray)
        quit_action = file_menu.addAction(tr("Exit"))
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.quitRequested.emit)

        run_menu = menu.addMenu(tr("Run"))
        run_menu.addAction(self.act_manga)
        run_menu.addAction(self.act_refresh)
        run_menu.addSeparator()
        run_menu.addAction(self.act_region)
        run_menu.addAction(self.act_window)
        run_menu.addAction(self.act_browser)

        view_menu = menu.addMenu(tr("View"))
        view_menu.addAction(self.act_overlay)
        self.act_hud = QAction(tr("Debug HUD on overlay"), self, checkable=True)
        self.act_hud.toggled.connect(self.debugHudToggled.emit)
        view_menu.addAction(self.act_hud)
        self.act_boxes = QAction(tr("Draw OCR boxes in preview"), self, checkable=True, checked=True)
        self.act_boxes.toggled.connect(lambda on: self.preview.set_analysis(
            self._last_result.blocks if self._last_result else [],
            self._card_rects,
            self.act_overlay.isChecked(),
            self._debug_info(),
        ))
        view_menu.addAction(self.act_boxes)
        view_menu.addSeparator()
        view_menu.addAction(tr("Refresh preview"), self._request_preview)

        tools_menu = menu.addMenu(tr("Tools"))
        tools_menu.addAction(tr("Statistics…"), self.statisticsRequested.emit)
        tools_menu.addAction(tr("Clear cache"), self.clearCacheRequested.emit)
        tools_menu.addSeparator()
        tools_menu.addAction(tr("Open log file"), self.openLogsRequested.emit)
        tools_menu.addAction(tr("Open data folder"), self.openDataRequested.emit)

        help_menu = menu.addMenu(tr("Help"))
        help_menu.addAction(tr("Hotkeys"), self._show_hotkeys)
        help_menu.addAction(tr("About"), self._show_about)

    def _build_body(self) -> None:
        # left: preview
        self.preview = PagePreviewWidget(self)
        self.preview.doubleClicked.connect(self.refreshRequested.emit)
        self.preview.set_placeholder(tr("Select a region to start (Run → Select Region)"))
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 4, 8)
        title = QLabel(tr("Live page preview"))
        title.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self._header_title = title
        left_layout.addWidget(title)
        left_layout.addWidget(self.preview, 1)

        # right: status + blocks
        self.status_panel = StatusPanel(self)
        self.bridge_panel = BridgePanel(self)
        self.bridge_panel.toggleRequested.connect(self.bridgeToggled.emit)
        self.blocks_panel = BlocksPanel(self)
        self.blocks_panel.saved.connect(self._save_page_text)
        right = QWidget(self)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 8, 8, 8)
        right_layout.addWidget(self.status_panel)
        right_layout.addWidget(self.bridge_panel)
        right_layout.addWidget(self.blocks_panel, 1)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        splitter.setChildrenCollapsible(False)

        # bottom: log + controls
        self.log_panel = LogPanel(self._log_file(), self)
        self.log_panel.btn_open.clicked.connect(self.openLogsRequested.emit)
        self.hotkey_panel = HotkeyPanel(self)
        self.about_panel = QLabel(self._about_html())
        self.about_panel.setWordWrap(True)
        self.about_panel.setStyleSheet(f"color: {theme.TEXT_DIM}; padding: 10px;")

        tabs = QTabWidget(self)
        tabs.addTab(self.log_panel, tr("Log"))
        tabs.addTab(self.hotkey_panel, tr("Controls"))
        tabs.addTab(self.about_panel, tr("About"))
        tabs.setMaximumHeight(220)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(splitter, 1)
        layout.addWidget(tabs)
        self.setCentralWidget(central)

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        self.lbl_status = QLabel(tr("ready"))
        self.lbl_status.setObjectName("status")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)          # indeterminate
        self.progress.setFixedWidth(120)
        self.progress.setVisible(False)
        self.lbl_mode_indicator = QLabel(tr("Manga Mode off"))
        self.lbl_mode_indicator.setStyleSheet(f"color: {theme.TEXT_DIM};")
        bar.addWidget(self.lbl_status, 1)
        bar.addPermanentWidget(self.progress)
        bar.addPermanentWidget(self.lbl_mode_indicator)

    # -- helpers ----------------------------------------------------------
    def retranslate(self) -> None:
        """Re-apply every visible string (called after a language change)."""
        self.setWindowTitle(tr("Manga Translator — control panel"))
        for action, key in (
            (self.act_manga, "Manga Mode: ON" if self.act_manga.isChecked() else "Manga Mode"),
            (self.act_overlay, "Translation"),
            (self.act_region, "Select Region"),
            (self.act_window, "Select Window"),
            (self.act_browser, "Browser mode…"),
            (self.act_refresh, "Refresh Page"),
            (self.act_settings, "Settings"),
            (self.act_glossary, "Glossary"),
            (self.act_tray, "Hide to tray"),
            (self.act_hud, "Debug HUD on overlay"),
            (self.act_boxes, "Draw OCR boxes in preview"),
        ):
            action.setText(tr(key))
        self.act_manga.setToolTip(tr("Enable/disable automatic page translation"))
        self.act_overlay.setToolTip(tr("Show/hide the on-screen translation overlay"))
        self._header_title.setText(tr("Live page preview"))
        self.status_panel.retranslate()
        self.bridge_panel.retranslate()
        self.blocks_panel.retranslate()
        self.log_panel.retranslate()
        self.hotkey_panel.retranslate()
        self.about_panel.setText(self._about_html())
        tabs = self.findChild(QTabWidget)
        if tabs is not None:
            tabs.setTabText(0, tr("Log"))
            tabs.setTabText(1, tr("Controls"))
            tabs.setTabText(2, tr("About"))
        self._rebuild_menu()
        self.lbl_mode_indicator.setText(
            tr("Manga Mode ON") if self.act_manga.isChecked() else tr("Manga Mode off")
        )
        if not self._last_result:
            self.preview.set_placeholder(tr("Select a region to start (Run → Select Region)"))
        self.refresh_state()   # re-render every value in the new language

    def _about_html(self) -> str:
        payload = self._describe()
        return (
            tr("<b>{name}</b> {version}<br>").format(name=APP_DISPLAY_NAME, version=__version__)
            + tr("Region → page change detection → PP-OCRv4 → one Gemini request per page → "
                 "click-through overlay.<br><br>")
            + tr("Hotkeys are global and work while another window (your reader) has focus.<br>"
                 "The overlay itself is click-through: scrolling and clicking keep working.")
            + f"<br><br>{tr('OCR: {engine}<br>').format(engine=payload.get('engine', '?'))}"
            + tr("Translator: {backend} ({model})<br>").format(
                backend=payload.get("backend", "?"), model=payload.get("model", "?")
            )
            + tr("Data folder: ")
            + str(getattr(getattr(self.controller, "paths", None), "root", "?"))
        )

    def _rebuild_menu(self) -> None:
        self.menuBar().clear()
        self._build_menu()

    def _log_file(self):
        paths = getattr(self.controller, "paths", None)
        return getattr(paths, "log_file", None)

    @property
    def _card_rects(self) -> List[Rect]:
        return list(getattr(self, "_cards_cache", []))

    def _debug_info(self) -> Optional[DebugInfo]:
        if self._last_result is None:
            return None
        payload = self._describe()
        detector = payload.get("detector") or {}
        region = self._last_region
        return DebugInfo(
            generation=self._last_result.generation,
            page_hash=self._last_result.page_hash,
            change_score=float(detector.get("score", 0.0)),
            state=str(detector.get("state", "idle")),
            stage=tr("cache") if self._last_result.from_cache else tr("ocr+mt"),
            from_cache=self._last_result.from_cache,
            ocr_ms=self._last_result.ocr_ms,
            translate_ms=self._last_result.translate_ms,
            block_count=len(self._last_result.blocks),
            extra=[f"region {region.width}x{region.height}@{region.dpi_scale:.2f}"] if region else [],
        )

    def _describe(self) -> Dict[str, object]:
        try:
            return self.controller.describe()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("describe() failed: %s", exc)
            return {}

    # -- pipeline -> window ----------------------------------------------
    def set_page(self, result: PageResult, job: Optional[PageJob] = None,
                 cards: Sequence[Rect] = ()) -> None:
        self._last_result = result
        if job is not None:
            self._last_region = job.region
        self._cards_cache = list(cards)
        frame, region = (None, None)
        try:
            frame, region = self.controller.pipeline.latest_frame()
        except Exception:  # pragma: no cover
            pass
        if frame is not None:
            self.preview.set_frame(frame)
        elif region is not None:
            self._last_region = region
        self.preview.set_stale(False)
        self.preview.set_analysis(
            result.blocks if self.act_boxes.isChecked() else [],
            self._cards_cache if self.act_overlay.isChecked() else [],
            self.act_overlay.isChecked(),
            self._debug_info(),
        )
        self.blocks_panel.set_blocks(result.blocks, result.from_cache)
        self.refresh_state()

    def set_preview_frame(self, frame, region) -> None:
        self._last_region = region
        self.preview.set_frame(frame)
        if self._last_result is not None:
            self.preview.set_analysis(
                self._last_result.blocks if self.act_boxes.isChecked() else [],
                self._card_rects if self.act_overlay.isChecked() else [],
                self.act_overlay.isChecked(),
                self._debug_info(),
            )

    def set_status_message(self, message: str, level: str = "info") -> None:
        color = {"error": theme.ERROR, "warn": theme.WARN}.get(level, theme.TEXT)
        self.lbl_status.setText(message)
        self.lbl_status.setStyleSheet(f"color: {color}; padding: 2px 6px;")

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.progress.setVisible(self._busy)

    def set_bridge_state(self, status: Dict[str, object]) -> None:
        """Update the browser-extension panel (called on the GUI thread)."""
        try:
            self.bridge_panel.update_state(status or {})
        except RuntimeError:  # pragma: no cover - window already destroyed
            pass

    def set_detector_state(self, state: str, score: float, generation: int) -> None:
        self._detector_state = state
        if self._last_result is not None and self.isVisible():
            self.preview.set_analysis(
                self._last_result.blocks if self.act_boxes.isChecked() else [],
                self._card_rects if self.act_overlay.isChecked() else [],
                self.act_overlay.isChecked(),
                self._debug_info(),
            )

    def set_mode_state(self, manga_mode: bool, overlay_visible: bool, debug_hud: bool) -> None:
        for action, value in (
            (self.act_manga, manga_mode),
            (self.act_overlay, overlay_visible),
            (self.act_hud, debug_hud),
        ):
            action.blockSignals(True)
            action.setChecked(bool(value))
            action.blockSignals(False)
        self.act_manga.setText(tr("Manga Mode: ON") if manga_mode else tr("Manga Mode"))
        self.lbl_mode_indicator.setText(
            tr("Manga Mode ON") if manga_mode else tr("Manga Mode off")
        )
        self.lbl_mode_indicator.setStyleSheet(
            f"color: {theme.OK}; font-weight: bold;" if manga_mode else f"color: {theme.TEXT_DIM};"
        )
        self.preview.set_placeholder(
            tr("Monitoring the selected region…") if manga_mode
            else tr("Select a region to start (Run → Select Region)")
        )

    def set_hotkeys(self, mapping: Dict[str, str]) -> None:
        self.hotkey_panel.set_hotkeys(mapping)

    def refresh_state(self) -> None:
        payload = self._describe()
        self.status_panel.update_state(payload)
        self.act_manga.blockSignals(True)
        self.act_manga.setChecked(bool(payload.get("manga_mode")))
        self.act_manga.blockSignals(False)
        self.act_manga.setText(
            tr("Manga Mode: ON") if payload.get("manga_mode") else tr("Manga Mode")
        )
        self.lbl_mode_indicator.setText(
            tr("Manga Mode ON") if payload.get("manga_mode") else tr("Manga Mode off")
        )
        self.lbl_mode_indicator.setStyleSheet(
            f"color: {theme.OK}; font-weight: bold;"
            if payload.get("manga_mode") else f"color: {theme.TEXT_DIM};"
        )
        if not self._card_rects and payload.get("manga_mode") is False:
            self.preview.set_stale(self._last_result is not None)

    # -- window -> app ----------------------------------------------------
    def _request_preview(self) -> None:
        if self.isVisible() and self.act_manga.isChecked():
            self.previewRequested.emit()

    def _choose_window(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        try:
            windows = self.controller.window_list()
        except Exception as exc:  # pragma: no cover
            log.warning("window list failed: %s", exc)
            windows = []
        if not windows:
            QMessageBox.information(self, APP_DISPLAY_NAME,
                                    tr("No suitable windows found. Use Select Region instead."))
            return
        labels = [info.label() for info in windows]
        choice, ok = QInputDialog.getItem(
            self, tr("Select Window"), tr("Capture and follow:"), labels, 0, False
        )
        if ok and choice:
            index = labels.index(choice)
            self.selectWindowRequested.emit(windows[index].handle)

    def _hide_to_tray(self) -> None:
        self.hide()
        self.hideToTrayRequested.emit()

    def _save_page_text(self) -> None:
        if self._last_result is None:
            return
        suggested = f"page_{self._last_result.generation:04d}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Save page text"), suggested, tr("Text files (*.txt)")
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.blocks_panel.page_text() + "\n")
        except OSError as exc:
            QMessageBox.warning(self, APP_DISPLAY_NAME, tr("Could not save the file:\n") + str(exc))
            return
        self.set_status_message(tr("saved ") + str(path))

    def _show_hotkeys(self) -> None:
        mapping = getattr(self.controller, "hotkey_mapping", lambda: {})()
        text = "\n".join(f"{name}:  {keys}" for name, keys in mapping.items()) or tr("hotkeys disabled")
        QMessageBox.information(self, tr("Global hotkeys"), text)

    def _show_about(self) -> None:
        payload = self._describe()
        QMessageBox.about(
            self,
            APP_DISPLAY_NAME,
            f"<b>{APP_DISPLAY_NAME}</b> {__version__}<br><br>"
            f"OCR: {payload.get('engine', '?')}<br>"
            f"Translator: {payload.get('backend', '?')} ({payload.get('model', '?')})<br>"
            f"Data folder: {getattr(getattr(self.controller, 'paths', None), 'root', '?')}<br><br>"
            "Region → change detection → OCR → one request per page → overlay.",
        )

    # -- events -----------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Closing hides the window; the tray keeps translating."""
        event.ignore()
        self._hide_to_tray()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.refresh_state()
        self._request_preview()
