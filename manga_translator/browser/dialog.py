"""Browser-mode dialog: connect to a tab, translate the visible page or the chapter."""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..core.i18n import tr
from ..core.logging_setup import get_logger
from .cdp import Tab, list_tabs

log = get_logger("ui.browser")


class _ChapterWorker(QThread):
    """Scrolls the page to enumerate every panel (never on the GUI thread)."""

    collected = Signal(list, str)

    def __init__(self, ws_url: str, parent=None):
        super().__init__(parent)
        self.ws_url = ws_url

    def run(self) -> None:  # pragma: no cover - needs a live browser
        from .cdp import CdpConnection
        from .reader import collect_chapter

        try:
            with CdpConnection(self.ws_url, timeout=30) as conn:
                panels = collect_chapter(conn)
            self.collected.emit(list(panels), "")
        except Exception as exc:
            self.collected.emit([], f"{exc.__class__.__name__}: {exc}")


class BrowserDialog(QDialog):
    """Choose a browser tab and control browser mode."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller           # MangaTranslatorApp
        self.browser = controller.browser_mode
        self.setWindowTitle(tr("Browser mode"))
        self.setMinimumWidth(620)

        self.lbl_hint = QLabel(tr(
            "Browser mode reads the page images straight from the tab: no screen "
            "capture, full resolution and no reader UI. Chrome must be started with "
            "remote debugging (the button below does that with its own profile)."
        ))
        self.lbl_hint.setWordWrap(True)

        self.lbl_status = QLabel(tr("Not connected"))
        self.lbl_status.setWordWrap(True)

        self.list_tabs = QListWidget(self)
        self.list_tabs.setMinimumHeight(140)

        self.btn_find = QPushButton(tr("Find tabs"))
        self.btn_launch = QPushButton(tr("Open Chrome with debugging"))
        self.btn_connect = QPushButton(tr("Connect"))
        self.btn_chapter = QPushButton(tr("Translate the whole chapter"))
        self.btn_visible = QPushButton(tr("Translate visible pages"))
        self.btn_disconnect = QPushButton(tr("Disconnect"))

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)

        row_top = QHBoxLayout()
        row_top.addWidget(self.btn_launch)
        row_top.addWidget(self.btn_find)
        row_top.addStretch(1)

        row_actions = QHBoxLayout()
        row_actions.addWidget(self.btn_connect)
        row_actions.addWidget(self.btn_visible)
        row_actions.addWidget(self.btn_chapter)
        row_actions.addWidget(self.btn_disconnect)
        row_actions.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self.lbl_hint)
        layout.addWidget(self.lbl_status)
        layout.addLayout(row_top)
        layout.addWidget(self.list_tabs)
        layout.addLayout(row_actions)
        layout.addWidget(self.progress)
        layout.addWidget(buttons)

        self.btn_find.clicked.connect(self.refresh_tabs)
        self.btn_launch.clicked.connect(self.launch)
        self.btn_connect.clicked.connect(self.connect_tab)
        self.btn_visible.clicked.connect(self.translate_visible)
        self.btn_chapter.clicked.connect(self.translate_chapter)
        self.btn_disconnect.clicked.connect(self.disconnect)
        self.list_tabs.itemDoubleClicked.connect(lambda _item: self.connect_tab())

        self.browser.statusChanged.connect(self.set_status)
        self.browser.failed.connect(lambda message: self.set_status(message, error=True))
        self._chapter_worker: Optional[_ChapterWorker] = None

        self.refresh_tabs()
        self._sync_buttons()

    # -- helpers ----------------------------------------------------------
    def set_status(self, text: str, error: bool = False) -> None:
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet("color: #f87171;" if error else "color: #93a4bf;")

    def _sync_buttons(self) -> None:
        attached = self.browser.info.attached
        self.btn_connect.setEnabled(bool(self.list_tabs.count()) and not attached)
        self.btn_visible.setEnabled(attached)
        self.btn_chapter.setEnabled(attached)
        self.btn_disconnect.setEnabled(attached)

    # -- actions ----------------------------------------------------------
    def refresh_tabs(self) -> None:
        self.list_tabs.clear()
        port = self.controller.browser_port()
        if port is None:
            self.set_status(tr("No browser with remote debugging found."))
            self._sync_buttons()
            return
        try:
            tabs: List[Tab] = list_tabs(port)
        except Exception as exc:
            self.set_status(f"{tr('Cannot list tabs')}: {exc}", error=True)
            self._sync_buttons()
            return
        for tab in tabs:
            item = QListWidgetItem(tab.label())
            item.setData(Qt.UserRole, tab)
            self.list_tabs.addItem(item)
        self.set_status(tr("Debug port {port}: {n} tab(s)").format(port=port, n=len(tabs)))
        self._sync_buttons()

    def launch(self) -> None:
        port = self.controller.launch_browser()
        if port is None:
            self.set_status(tr("Chrome did not start with a debugging port."), error=True)
        else:
            self.set_status(tr("Debug port {port}").format(port=port))
        self.refresh_tabs()

    def connect_tab(self) -> None:
        item = self.list_tabs.currentItem()
        if item is None:
            return
        tab: Tab = item.data(Qt.UserRole)
        if self.controller.connect_browser_tab(tab):
            self.set_status(self.browser.info.line())
        self._sync_buttons()

    def translate_visible(self) -> None:
        self.browser.invalidate()
        self.set_status(self.browser.info.line())

    def translate_chapter(self) -> None:
        if self.browser.tab is None:
            return
        if self._chapter_worker is not None and self._chapter_worker.isRunning():
            return
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.set_status(tr("Scanning the chapter for pages…"))
        worker = _ChapterWorker(self.browser.tab.ws_url, self)
        worker.collected.connect(self._on_chapter_collected)
        self._chapter_worker = worker
        worker.start()

    def _on_chapter_collected(self, panels: list, error: str) -> None:
        self.progress.setRange(0, 100)
        if error:
            self.progress.setVisible(False)
            self.set_status(f"{tr('Chapter scan failed')}: {error}", error=True)
            return
        self.browser.set_chapter(panels)
        total = len(panels)
        self.set_status(tr("Chapter queued: {n} pages").format(n=total))
        self.progress.setValue(0)
        self.progress.setVisible(total > 0)

    def disconnect(self) -> None:
        self.controller.disconnect_browser()
        self.set_status(tr("Not connected"))
        self._sync_buttons()
