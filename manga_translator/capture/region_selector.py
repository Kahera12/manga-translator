"""Fullscreen drag-to-select region picker (DPI and multi-monitor aware).

One window per screen (each with its own device pixel ratio), so the selection
is exact on 100% / 125% / 150% displays and on secondary monitors with a
different scale. The window's logical rectangle is converted back to physical
device pixels through the CoordMap, which is the space `mss` captures in.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..core.logging_setup import get_logger
from ..core.models import Rect, ScreenRegion
from .coordmap import CoordMap

log = get_logger("region_selector")

MIN_SIZE = 24


class _SelectorWindow(QWidget):
    finished = Signal(object)   # ScreenRegion
    cancelled = Signal()

    def __init__(self, screen, coordmap: CoordMap):
        super().__init__()
        self.screen = screen
        self.coordmap = coordmap
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        geo = screen.geometry()
        self.setGeometry(geo)
        self.setWindowTitle("Select the manga area - drag a rectangle (Esc to cancel)")

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 110))
        rect = self.selection_rect()
        if rect is not None and rect.width() > 0 and rect.height() > 0:
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(rect, Qt.transparent)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setPen(QPen(QColor("#38bdf8"), 2))
            painter.drawRect(rect)
            painter.setPen(QPen(QColor("#e2e8f0")))
            painter.drawText(
                rect.adjusted(4, 4, 0, 0),
                Qt.AlignTop | Qt.AlignLeft,
                f"{rect.width()} x {rect.height()}",
            )
        painter.setPen(QPen(QColor("#f8fafc")))
        painter.drawText(
            self.rect().adjusted(0, 24, 0, 0),
            Qt.AlignHCenter | Qt.AlignTop,
            "Drag a rectangle over the manga page  ·  Esc = cancel",
        )
        painter.end()

    # -- selection --------------------------------------------------------
    def selection_rect(self) -> Optional[QRect]:
        if self._origin is None or self._current is None:
            return None
        return QRect(self._origin, self._current).normalized().intersected(self.rect())

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() != Qt.LeftButton or self._origin is None:
            return
        self._current = event.position().toPoint()
        rect = self.selection_rect()
        self._origin = None
        self._current = None
        if rect is None or rect.width() < MIN_SIZE or rect.height() < MIN_SIZE:
            log.info("selection too small (%.0fx%.0f), ignored",
                     rect.width() if rect else 0, rect.height() if rect else 0)
            return
        self.finished.emit(self.to_region(rect))

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()

    def to_region(self, rect: QRect) -> ScreenRegion:
        """Window-local logical rect -> absolute physical ScreenRegion."""
        geo = self.screen.geometry()
        logical = Rect(
            geo.x() + rect.x(),
            geo.y() + rect.y(),
            rect.width(),
            rect.height(),
        )
        monitor = self.coordmap.monitor_for_logical_rect(logical)
        physical = monitor.logical_to_physical(logical).rounded()
        return ScreenRegion(
            x=int(physical.x),
            y=int(physical.y),
            width=int(physical.w),
            height=int(physical.h),
            monitor=monitor.name,
            dpi_scale=monitor.scale,
        )


class RegionSelector(QWidget):
    """Controller that shows one picker window per screen."""

    selected = Signal(object)     # ScreenRegion
    cancelled = Signal()

    def __init__(self, coordmap: CoordMap, parent=None):
        super().__init__(parent)
        self.coordmap = coordmap
        self._windows: List[_SelectorWindow] = []

    @property
    def active(self) -> bool:
        return bool(self._windows)

    def start(self) -> None:
        if self._windows:
            return
        screens = QGuiApplication.screens()
        if not screens:
            self.cancelled.emit()
            return
        for screen in screens:
            window = _SelectorWindow(screen, self.coordmap)
            window.finished.connect(self._on_finished)
            window.cancelled.connect(self._on_cancelled)
            window.showFullScreen()
            window.raise_()
            window.activateWindow()
            self._windows.append(window)
        log.info("region selector shown on %d screen(s)", len(self._windows))

    def cancel(self) -> None:
        windows, self._windows = self._windows, []
        for window in windows:
            window.close()
        self.cancelled.emit()

    def _close_all(self) -> None:
        windows, self._windows = self._windows, []
        for window in windows:
            window.close()

    def _on_finished(self, region: ScreenRegion) -> None:
        self._close_all()
        log.info("region selected: %s", region.to_dict())
        self.selected.emit(region)

    def _on_cancelled(self) -> None:
        self._close_all()
        log.info("region selection cancelled")
        self.cancelled.emit()
