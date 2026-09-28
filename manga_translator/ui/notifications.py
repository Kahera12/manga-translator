"""Small readable notifications (§25): tray balloon + in-overlay toast.

Never shows a traceback; technical detail goes to the log file.
"""
from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QSystemTrayIcon

from ..core.logging_setup import get_logger

log = get_logger("notify")

ICON_INFO = QSystemTrayIcon.Information
ICON_WARN = QSystemTrayIcon.Warning
ICON_ERROR = QSystemTrayIcon.Critical


class Notifier(QObject):
    def __init__(self, parent=None, min_interval_s: float = 4.0):
        super().__init__(parent)
        self._tray: Optional[QSystemTrayIcon] = None
        self._overlay = None
        self._last: dict = {}
        self._min_interval = min_interval_s

    def attach(self, tray: Optional[QSystemTrayIcon] = None, overlay=None) -> None:
        self._tray = tray
        self._overlay = overlay

    def _throttle(self, key: str) -> bool:
        now = time.time()
        if now - self._last.get(key, 0.0) < self._min_interval:
            return False
        self._last[key] = now
        return True

    def info(self, message: str, title: str = "Manga Translator", toast: bool = True) -> None:
        log.info("notify: %s", message)
        self._show(title, message, ICON_INFO, toast, key=f"i:{message}")

    def warn(self, message: str, title: str = "Manga Translator", toast: bool = True) -> None:
        log.warning("notify: %s", message)
        self._show(title, message, ICON_WARN, toast, key=f"w:{message}")

    def error(self, message: str, title: str = "Manga Translator", toast: bool = True) -> None:
        log.error("notify: %s", message)
        self._show(title, message, ICON_ERROR, toast, key=f"e:{message}")

    def _show(self, title: str, message: str, icon, toast: bool, key: str) -> None:
        if not self._throttle(key):
            return
        shown = False
        overlay = self._overlay
        if toast and overlay is not None:
            try:
                if overlay.isVisible():
                    overlay.show_toast(message, _level_name(icon))
                    shown = True
            except Exception:  # pragma: no cover - overlay already gone
                shown = False
        if not shown and self._tray is not None:
            self._tray.showMessage(title, message, icon, 4000)


def _level_name(icon) -> str:
    if icon == ICON_ERROR:
        return "error"
    if icon == ICON_WARN:
        return "warn"
    return "info"
