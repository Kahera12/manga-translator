"""Page preview widget: last (or live) capture + OCR boxes + overlay cards.

Everything is drawn in *region-relative physical pixels* and mapped to the
widget through a single fit-to-widget transform, so what you see here matches
what the overlay draws on screen.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.logging_setup import get_logger
from ..core.models import Block, Rect
from ..overlay.widgets import DebugInfo
from . import theme
from ..core.i18n import tr

log = get_logger("ui.preview")


def bgr_to_qimage(bgr: np.ndarray) -> QImage:
    """numpy BGR (as captured by mss) -> QImage without copying twice."""
    array = bgr
    if array.ndim == 2:
        array = np.repeat(array[:, :, None], 3, axis=2)
    if array.shape[2] == 4:
        array = array[:, :, :3]
    array = np.ascontiguousarray(array)
    height, width = array.shape[:2]
    image = QImage(array.data, width, height, 3 * width, QImage.Format_BGR888)
    return image.copy()  # detach from the numpy buffer


def bbox_to_qrect(rect: Rect, scale: float, offset: QPointF) -> QRectF:
    return QRectF(
        offset.x() + rect.x * scale,
        offset.y() + rect.y * scale,
        max(1.0, rect.w * scale),
        max(1.0, rect.h * scale),
    )


class PagePreviewWidget(QWidget):
    """Shows the captured page with detection/overlay annotations."""

    doubleClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setToolTip(tr("Captured region — green = translated, amber = SFX/untranslated,\n"
                           "dashed blue = where the overlay card is drawn (double-click to refresh)"))
        self._pixmap: Optional[QPixmap] = None
        self._frame_size = (0, 0)
        self._blocks: List[Block] = []
        self._cards: List[Rect] = []
        self._show_translation = True
        self._debug: Optional[DebugInfo] = None
        self._placeholder = tr("No region selected yet")
        self._stale = False

    # -- content ----------------------------------------------------------
    def set_frame(self, bgr: Optional[np.ndarray]) -> None:
        if bgr is None:
            self._pixmap = None
            self._frame_size = (0, 0)
        else:
            self._pixmap = QPixmap.fromImage(bgr_to_qimage(bgr))
            self._frame_size = (self._pixmap.width(), self._pixmap.height())
        self.update()

    def set_analysis(
        self,
        blocks: Sequence[Block] = (),
        cards: Sequence[Rect] = (),
        show_translation: bool = True,
        debug: Optional[DebugInfo] = None,
    ) -> None:
        self._blocks = list(blocks)
        self._cards = list(cards)
        self._show_translation = bool(show_translation)
        self._debug = debug
        self.update()

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    def set_stale(self, stale: bool) -> None:
        """Dim the frame when it no longer matches the page on screen."""
        self._stale = bool(stale)
        self.update()

    def clear_analysis(self) -> None:
        self._blocks = []
        self._cards = []
        self._debug = None
        self.update()

    @property
    def frame_size(self) -> tuple:
        return self._frame_size

    # -- geometry ---------------------------------------------------------
    def image_rect(self) -> QRectF:
        """Where the frame is painted inside the widget (keeps aspect ratio)."""
        if self._pixmap is None or self._frame_size[0] <= 0:
            return QRectF()
        avail_w = max(1.0, self.width() - 8.0)
        avail_h = max(1.0, self.height() - 8.0)
        sx = avail_w / self._frame_size[0]
        sy = avail_h / self._frame_size[1]
        scale = min(sx, sy)
        width = self._frame_size[0] * scale
        height = self._frame_size[1] * scale
        return QRectF((self.width() - width) / 2.0, (self.height() - height) / 2.0, width, height)

    def _transform(self) -> tuple:
        target = self.image_rect()
        if target.isEmpty() or self._frame_size[0] <= 0:
            return 1.0, QPointF(0, 0)
        scale = target.width() / float(self._frame_size[0])
        return scale, target.topLeft()

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.fillRect(self.rect(), QColor("#0b0f18"))

            if self._pixmap is None:
                painter.setPen(QPen(QColor(theme.TEXT_DIM)))
                painter.drawText(self.rect(), Qt.AlignCenter, self._placeholder)
                return

            target = self.image_rect()
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
            if self._stale:
                painter.fillRect(target, QColor(0, 0, 0, 90))

            scale, offset = self._transform()
            painter.setClipRect(target)

            # OCR blocks
            for block in self._blocks:
                translated = bool(block.translation)
                if block.is_sfx:
                    color = QColor(theme.SFX)
                elif translated:
                    color = QColor(theme.DETECT)
                else:
                    color = QColor(theme.TEXT_DIM)
                painter.setPen(QPen(color, 1.4))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(bbox_to_qrect(block.bbox, scale, offset))
                label = f"#{block.id}"
                if self._debug is not None:
                    label += f" {block.confidence:.2f}"
                painter.setFont(QFont("Segoe UI", 8))
                painter.setPen(QPen(color))
                box = bbox_to_qrect(block.bbox, scale, offset)
                painter.drawText(QPointF(box.left() + 2, max(10.0, box.top() - 2)), label)

            # overlay cards (what the on-screen overlay will draw)
            if self._show_translation:
                for rect in self._cards:
                    card = bbox_to_qrect(rect, scale, offset)
                    painter.setPen(QPen(QColor(theme.CARD), 1.2, Qt.DashLine))
                    painter.setBrush(QBrush(QColor(16, 19, 26, 150)))
                    painter.drawRoundedRect(card, 4, 4)

            # translation preview inside the block boxes
            if self._show_translation:
                font = QFont("Segoe UI", 8)
                painter.setFont(font)
                metrics = QFontMetricsF(font)
                for block in self._blocks:
                    text = (block.translation or "").strip()
                    if not text:
                        continue
                    box = bbox_to_qrect(block.bbox, scale, offset)
                    if box.width() < 24 or box.height() < 10:
                        continue
                    painter.fillRect(box, QColor(8, 12, 20, 200))
                    painter.setPen(QPen(QColor(theme.TEXT)))
                    painter.drawText(
                        box.adjusted(2, 1, -2, -1),
                        Qt.AlignLeft | Qt.AlignVCenter,
                        metrics.elidedText(text.replace("\n", " "), Qt.ElideRight, int(box.width() - 4)),
                    )

            self._paint_hud(painter, target)
        finally:
            painter.end()

    def _paint_hud(self, painter: QPainter, target: QRectF) -> None:
        info = self._debug
        if info is None:
            return
        lines = [
            f"gen {info.generation} · {info.state} {info.stage}".strip(),
            f"hash {info.page_hash or '-'} · change {info.change_score:.3f}",
            f"blocks {info.block_count} · ocr {info.ocr_ms:.0f}ms · mt {info.translate_ms:.0f}ms"
            + (" · CACHE" if info.from_cache else ""),
        ]
        font = QFont("Consolas", 8)
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        width = max(metrics.horizontalAdvance(line) for line in lines) + 12
        height = (metrics.height() + 1) * len(lines) + 8
        box = QRectF(target.left() + 6, target.top() + 6, width, height)
        painter.setPen(QPen(QColor(theme.ACCENT), 1.0))
        painter.setBrush(QBrush(QColor(8, 12, 20, 215)))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QPen(QColor(theme.TEXT)))
        y = box.top() + 4 + metrics.ascent()
        for line in lines:
            painter.drawText(QPointF(box.left() + 6, y), line)
            y += metrics.height() + 1

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.doubleClicked.emit()
