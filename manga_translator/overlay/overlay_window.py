"""Always-on-top, click-through, translucent overlay window.

* borderless, transparent background, no taskbar entry, never steals focus
* click-through by default (Win32 `WS_EX_TRANSPARENT|WS_EX_LAYERED|WS_EX_NOACTIVATE`)
* aligned with the capture region in Qt logical pixels (mixed-DPI safe)
* can blank itself for the duration of a screenshot so it never contaminates OCR
* never paints on the GUI thread during processing (cards are handed over ready)
"""
from __future__ import annotations

import contextlib
import ctypes
import sys
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import QRect, Qt, QTimer, Slot
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from ..core.config import OverlaySettings
from ..core.logging_setup import get_logger
from ..core.models import Block, Rect, ScreenRegion
from .layout import CardLayout, LayoutSettings, layout_cards
from .widgets import DebugInfo, make_font, measure_factory, paint_cards, paint_debug

log = get_logger("overlay")

IS_WINDOWS = sys.platform == "win32"
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000

_USER32 = ctypes.windll.user32 if IS_WINDOWS else None


def layout_settings(settings: OverlaySettings) -> LayoutSettings:
    return LayoutSettings(
        font_size=float(settings.font_size),
        padding=float(settings.padding),
        max_card_width=float(settings.max_card_width),
        min_card_width=float(settings.min_card_width),
        gap=float(settings.gap),
    )


def block_anchors(
    blocks: Sequence[Block], region: ScreenRegion, coordmap, origin_logical: Rect
) -> Dict[int, Rect]:
    """Block bboxes (region-relative physical px) -> window-relative logical px."""
    anchors: Dict[int, Rect] = {}
    for block in blocks:
        absolute = block.bbox.translated(region.x, region.y)
        logical = coordmap.physical_to_logical(absolute)
        anchors[block.id] = logical.translated(-origin_logical.x, -origin_logical.y)
    return anchors


def card_rects_region_px(
    cards: Sequence[CardLayout],
    region: ScreenRegion,
    coordmap,
) -> List[Rect]:
    """Card rects as capture-region relative physical pixels.

    Used on the GUI thread to hand the monitor thread a snapshot it can turn
    into preview-space masks, so the change detector ignores our own cards.
    """
    rects: List[Rect] = []
    if region.width <= 0 or region.height <= 0:
        return rects
    for card in cards:
        expanded = card.rect.expanded(4.0)
        absolute = coordmap.logical_to_physical(expanded)
        rects.append(
            Rect(absolute.x - region.x, absolute.y - region.y, absolute.w, absolute.h)
        )
    return rects


class OverlayWindow(QWidget):
    def __init__(self, settings: OverlaySettings, coordmap, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.coordmap = coordmap
        self._cards: List[CardLayout] = []
        self._debug: Optional[DebugInfo] = None
        self._toast: Optional[tuple] = None
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.clear_toast)
        self._suppress_paint = 0
        self._region: Optional[ScreenRegion] = None
        self._origin_logical = Rect(0, 0, 1, 1)
        self._click_through = bool(settings.click_through)
        self._font_family = QApplication.font().family() if QApplication.instance() else "Segoe UI"
        self._measurer = None

        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, self._click_through)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowTitle("Manga Translator overlay")

    # -- native window behaviour -----------------------------------------
    def showEvent(self, event):  # noqa: N802 (Qt naming)
        super().showEvent(event)
        self._apply_native_style()

    def _apply_native_style(self) -> None:
        if not IS_WINDOWS:
            return
        try:
            hwnd = int(self.winId())
            user32 = _USER32
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_long.restype = ctypes.c_ssize_t
            set_long.restype = ctypes.c_ssize_t
            style = get_long(hwnd, GWL_EXSTYLE)
            style = (style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
            if self._click_through:
                style |= WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            set_long(hwnd, GWL_EXSTYLE, style)
            # Re-assert top-most without activating.
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        except (OSError, ValueError) as exc:  # pragma: no cover
            log.debug("native overlay style failed: %s", exc.__class__.__name__)

    def set_click_through(self, enabled: bool) -> None:
        self._click_through = bool(enabled)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, self._click_through)
        self._apply_native_style()

    # -- geometry ---------------------------------------------------------
    def follow_region(self, region: ScreenRegion) -> bool:
        """Place the overlay exactly over the capture region (logical px)."""
        self._region = region
        try:
            logical = self.coordmap.region_to_logical(region)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("cannot map region to logical coordinates: %s", exc)
            return False
        self._origin_logical = logical
        self.setGeometry(QRect(int(logical.x), int(logical.y), int(logical.w), int(logical.h)))
        if self._cards:
            self._cards = []  # coordinates changed; stale cards must not be shown
        return True

    @property
    def origin_logical(self) -> Rect:
        return self._origin_logical

    # -- content ----------------------------------------------------------
    def _measurer_for(self, font_size: float):
        return measure_factory(make_font(self._font_family, font_size))

    def set_page(
        self,
        blocks: Sequence[Block],
        region: ScreenRegion,
        debug: Optional[DebugInfo] = None,
    ) -> List[CardLayout]:
        """Recompute and display cards for a page (call on the GUI thread only)."""
        if self._region is None or self._region != region:
            self.follow_region(region)
        layout = layout_settings(self.settings)
        anchors = block_anchors(blocks, region, self.coordmap, self._origin_logical)
        mode = (self.settings.mode or "translation").lower()
        prepared: List[Block] = []
        for block in blocks:
            if mode == "original":
                shown = block.text
            elif mode == "both":
                shown = f"{block.translation}\n{block.text}" if block.translation else block.text
            else:
                shown = block.translation
            if not shown or not shown.strip():
                continue
            prepared.append(
                Block(
                    id=block.id,
                    text=shown,
                    bbox=block.bbox,
                    spans=block.spans,
                    direction=block.direction,
                    confidence=block.confidence,
                    is_sfx=block.is_sfx,
                    translation=shown,
                )
            )
        cards = layout_cards(
            prepared,
            display_size=(max(1.0, self.width()), max(1.0, self.height())),
            anchors=anchors,
            measure=self._measurer_for(layout.font_size),
            settings=layout,
        )
        self._cards = cards
        self._debug = debug
        self.update()
        return cards

    def clear(self, reason: str = "manual") -> None:
        if self._cards or self._debug:
            log.debug("overlay cleared (%s)", reason)
        self._cards = []
        self._debug = None
        self.update()

    def set_debug(self, debug: Optional[DebugInfo]) -> None:
        self._debug = debug
        self.update()

    # -- toasts (small readable notifications without the tray) -----------
    def show_toast(self, text: str, level: str = "info", duration_ms: int = 3500) -> None:
        self._toast = (str(text)[:220], str(level))
        self._toast_timer.start(max(500, int(duration_ms)))
        self.update()

    def clear_toast(self) -> None:
        self._toast = None
        self.update()

    @property
    def cards(self) -> List[CardLayout]:
        return list(self._cards)

    def card_rects_window(self) -> List[Rect]:
        return [c.rect for c in self._cards]

    # -- capture hygiene --------------------------------------------------
    @contextlib.contextmanager
    def quiet(self):
        """Blank the overlay for the duration of a screenshot (re-entrant)."""
        self._enter_quiet()
        try:
            yield
        finally:
            self._exit_quiet()

    @Slot()
    def request_quiet(self) -> None:
        """GUI-thread entry point for the capture hooks (worker thread calls it)."""
        self._enter_quiet()

    @Slot()
    def request_unquiet(self) -> None:
        self._exit_quiet()

    def _enter_quiet(self) -> None:
        self._suppress_paint += 1
        if self._suppress_paint == 1:
            self.setWindowOpacity(0.0)
            self.repaint()

    def _exit_quiet(self) -> None:
        self._suppress_paint = max(0, self._suppress_paint - 1)
        if self._suppress_paint == 0:
            self.setWindowOpacity(1.0)
            self.update()

    @property
    def is_quiet(self) -> bool:
        return self._suppress_paint > 0

    # -- show / hide ------------------------------------------------------
    def show_translation(self, visible: bool) -> None:
        self.setVisible(bool(visible))
        if visible:
            self.raise_()
            self._apply_native_style()

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802
        if self._suppress_paint or not self.isVisible():
            return
        painter = QPainter(self)
        try:
            settings = self.settings
            paint_cards(
                painter,
                self._cards,
                layout_settings(settings),
                font_family=self._font_family,
                text_color=settings.text_color,
                bg_color=settings.bg_color,
                accent_color=settings.accent_color,
                opacity=settings.opacity,
            )
            if settings.show_debug_hud and self._debug is not None:
                paint_debug(painter, self._cards, self._debug, font_family="Consolas")
            if self._toast is not None:
                self._paint_toast(painter, *self._toast)
        finally:
            painter.end()

    def _paint_toast(self, painter: QPainter, text: str, level: str) -> None:
        from PySide6.QtCore import QRectF  # local import keeps the top tidy

        font = make_font(self._font_family, max(10.0, self.settings.font_size))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = min(self.width() - 16.0, metrics.horizontalAdvance(text) + 20.0)
        height = metrics.height() + 12.0
        box = QRectF((self.width() - width) / 2.0, self.height() - height - 10.0, width, height)
        color = {"error": "#b91c1c", "warn": "#b45309", "warning": "#b45309"}.get(level, "#0f172a")
        painter.setPen(QPen(QColor("#38bdf8"), 1.0))
        painter.setBrush(QBrush(QColor(color).lighter(190)))
        painter.drawRoundedRect(box, 6.0, 6.0)
        painter.setPen(QPen(QColor("#0b1220")))
        painter.drawText(box, Qt.AlignCenter, metrics.elidedText(text, Qt.ElideRight, int(width - 12)))
