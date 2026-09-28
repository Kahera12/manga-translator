"""System tray icon + menu (§28). No large main window is required."""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..core.logging_setup import get_logger
from ..core.i18n import tr

log = get_logger("tray")


def make_icon(active: bool = False, size: int = 64) -> QIcon:
    """Draw the tray icon in code - no binary assets to ship."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor("#0ea5e9" if active else "#334155"))
        painter.setPen(QColor("#e2e8f0"))
        painter.drawRoundedRect(3, 3, size - 6, size - 6, size * 0.22, size * 0.22)
        painter.setPen(QColor("#ffffff"))
        font = QFont("Segoe UI", int(size * 0.42))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter, "訳")
    finally:
        painter.end()
    return QIcon(pixmap)


class Tray(QObject):
    mangaModeToggled = Signal(bool)
    overlayToggled = Signal(bool)
    selectRegionRequested = Signal()
    selectWindowRequested = Signal(int)
    refreshRequested = Signal()
    settingsRequested = Signal()
    glossaryRequested = Signal()
    clearCacheRequested = Signal()
    statsRequested = Signal()
    openLogsRequested = Signal()
    showWindowRequested = Signal()
    browserModeRequested = Signal()
    quitRequested = Signal()

    def __init__(self, window_lister: Optional[Callable[[], List]] = None, parent=None):
        super().__init__(parent)
        self._window_lister = window_lister
        self._manga_mode = False
        self._overlay_visible = True
        self._status = ""
        self.icon = QSystemTrayIcon(make_icon(False), self)
        self.icon.setToolTip(tr("Manga Translator"))
        self.menu = QMenu()
        self._build()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)

    # -- menu -------------------------------------------------------------
    def _build(self) -> None:
        self.act_show = QAction(tr("Open control panel"), self.menu)
        self.act_show.triggered.connect(self.showWindowRequested.emit)

        self.act_manga = QAction(tr("Enable Manga Mode"), self.menu, checkable=True)
        self.act_manga.toggled.connect(self._on_manga_toggled)

        self.act_overlay = QAction(tr("Show translation"), self.menu, checkable=True, checked=True)
        self.act_overlay.toggled.connect(self.overlayToggled.emit)

        self.act_region = QAction(tr("Select Region…"), self.menu)
        self.act_region.triggered.connect(self.selectRegionRequested.emit)

        self.menu_windows = QMenu(tr("Select Window…"), self.menu)
        self.menu_windows.aboutToShow.connect(self._populate_windows)

        self.act_browser = QAction(tr("Browser mode…"), self.menu)
        self.act_browser.triggered.connect(self.browserModeRequested.emit)

        self.act_refresh = QAction(tr("Refresh Page"), self.menu)
        self.act_refresh.triggered.connect(self.refreshRequested.emit)

        self.act_settings = QAction(tr("Settings…"), self.menu)
        self.act_settings.triggered.connect(self.settingsRequested.emit)

        self.act_glossary = QAction(tr("Glossary…"), self.menu)
        self.act_glossary.triggered.connect(self.glossaryRequested.emit)

        self.act_stats = QAction(tr("Statistics…"), self.menu)
        self.act_stats.triggered.connect(self.statsRequested.emit)

        self.act_clear = QAction(tr("Clear Cache"), self.menu)
        self.act_clear.triggered.connect(self.clearCacheRequested.emit)

        self.act_logs = QAction(tr("Open Log Folder"), self.menu)
        self.act_logs.triggered.connect(self.openLogsRequested.emit)

        self.act_quit = QAction(tr("Exit"), self.menu)
        self.act_quit.triggered.connect(self.quitRequested.emit)

        self.menu.addAction(self.act_show)
        self.menu.addSeparator()
        self.menu.addAction(self.act_manga)
        self.menu.addSeparator()
        self.menu.addAction(self.act_region)
        self.menu.addMenu(self.menu_windows)
        self.menu.addAction(self.act_browser)
        self.menu.addAction(self.act_refresh)
        self.menu.addSeparator()
        self.menu.addAction(self.act_overlay)
        self.menu.addSeparator()
        self.menu.addAction(self.act_settings)
        self.menu.addAction(self.act_glossary)
        self.menu.addAction(self.act_stats)
        self.menu.addAction(self.act_clear)
        self.menu.addAction(self.act_logs)
        self.menu.addSeparator()
        self.menu.addAction(self.act_quit)

    def retranslate(self) -> None:
        """Re-apply every visible string after a language change."""
        self.act_show.setText(tr("Open control panel"))
        self.act_manga.setText(tr("Disable Manga Mode") if self._manga_mode else tr("Enable Manga Mode"))
        self.act_overlay.setText(tr("Hide translation") if self._overlay_visible else tr("Show translation"))
        self.act_region.setText(tr("Select Region…"))
        self.menu_windows.setTitle(tr("Select Window…"))
        self.act_browser.setText(tr("Browser mode…"))
        self.act_refresh.setText(tr("Refresh Page"))
        self.act_settings.setText(tr("Settings…"))
        self.act_glossary.setText(tr("Glossary…"))
        self.act_stats.setText(tr("Statistics…"))
        self.act_clear.setText(tr("Clear Cache"))
        self.act_logs.setText(tr("Open Log Folder"))
        self.act_quit.setText(tr("Exit"))
        self.set_status(self._status)

    def _on_manga_toggled(self, enabled: bool) -> None:
        self._manga_mode = bool(enabled)
        self.act_manga.setText(tr("Disable Manga Mode") if enabled else tr("Enable Manga Mode"))
        self.mangaModeToggled.emit(bool(enabled))

    def _populate_windows(self) -> None:
        self.menu_windows.clear()
        if self._window_lister is None:
            self.menu_windows.addAction(tr("(not available)")).setEnabled(False)
            return
        windows = self._window_lister()
        if not windows:
            self.menu_windows.addAction(tr("(no windows found)")).setEnabled(False)
            return
        for info in windows[:25]:
            action = self.menu_windows.addAction(info.label())
            action.triggered.connect(lambda _checked=False, h=info.handle: self.selectWindowRequested.emit(h))

    # -- state ------------------------------------------------------------
    def set_manga_mode(self, enabled: bool) -> None:
        self._manga_mode = bool(enabled)
        self.act_manga.blockSignals(True)
        self.act_manga.setChecked(enabled)
        self.act_manga.blockSignals(False)
        self.act_manga.setText(tr("Disable Manga Mode") if enabled else tr("Enable Manga Mode"))
        self.icon.setIcon(make_icon(enabled))

    def set_overlay_visible(self, visible: bool) -> None:
        self._overlay_visible = bool(visible)
        self.act_overlay.blockSignals(True)
        self.act_overlay.setChecked(visible)
        self.act_overlay.blockSignals(False)
        self.act_overlay.setText(tr("Hide translation") if visible else tr("Show translation"))

    def set_status(self, text: str) -> None:
        self._status = text or ""
        self.icon.setToolTip(tr("Manga Translator — {status}").format(status=text) if text
                             else tr("Manga Translator"))

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def message(self, title: str, text: str, level="info", msec: int = 4000) -> None:
        icon = {
            "error": QSystemTrayIcon.Critical,
            "warn": QSystemTrayIcon.Warning,
        }.get(level, QSystemTrayIcon.Information)
        self.icon.showMessage(title, text, icon, msec)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.showWindowRequested.emit()
