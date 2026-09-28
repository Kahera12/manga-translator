"""Qt painting for overlay cards and the debug HUD.

Kept apart from the window so the exact same painting code can be rendered into
an offscreen QImage — that is how overlay rendering is verified in the tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QImage, QPainter, QPen

from ..core.models import Rect
from .layout import CardLayout, LayoutSettings


@dataclass
class DebugInfo:
    generation: int = 0
    page_hash: str = ""
    change_score: float = 0.0
    state: str = "idle"
    stage: str = ""
    from_cache: bool = False
    ocr_ms: float = 0.0
    translate_ms: float = 0.0
    block_count: int = 0
    extra: List[str] = field(default_factory=list)


def qrectf(rect: Rect) -> QRectF:
    return QRectF(rect.x, rect.y, rect.w, rect.h)


def make_font(family: str, size: float, bold: bool = False) -> QFont:
    font = QFont(family)
    font.setPointSizeF(max(6.0, size))
    font.setBold(bold)
    font.setStyleStrategy(QFont.PreferAntialias)
    return font


def measure_factory(font: QFont):
    metrics = QFontMetricsF(font)

    def measure(text: str) -> float:
        return metrics.horizontalAdvance(text)

    return measure


def paint_cards(
    painter: QPainter,
    cards: Sequence[CardLayout],
    settings: LayoutSettings,
    font_family: str = "Segoe UI",
    text_color: str = "#ffffff",
    bg_color: str = "#10131a",
    accent_color: str = "#7dd3fc",
    opacity: float = 0.82,
) -> None:
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.TextAntialiasing, True)
    for card in cards:
        if card.is_empty:
            continue
        body = qrectf(card.rect)
        bg = QColor(bg_color)
        bg.setAlphaF(max(0.05, min(1.0, opacity)))
        painter.setPen(QPen(QColor(accent_color).lighter(115), 1.0))
        painter.setBrush(QBrush(bg))
        radius = min(8.0, max(4.0, settings.padding * 0.8))
        painter.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        accent = QColor(accent_color)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(accent))
        bar = QRectF(body.left() + 2.0, body.top() + radius, 2.5, max(4.0, body.height() - 2 * radius))
        painter.drawRoundedRect(bar, 1.2, 1.2)

        font = make_font(font_family, card.font_size)
        painter.setFont(font)
        painter.setPen(QPen(QColor(text_color)))
        baseline = body.top() + settings.padding
        for line in card.lines:
            baseline += card.line_height
            painter.drawText(
                QPointF(body.left() + settings.padding, baseline - card.line_height * 0.22),
                line,
            )


def paint_debug(
    painter: QPainter,
    cards: Sequence[CardLayout],
    info: DebugInfo,
    font_family: str = "Consolas",
) -> None:
    painter.setRenderHint(QPainter.Antialiasing, True)
    for card in cards:
        anchor = qrectf(card.anchor)
        color = QColor("#f97316") if not card.covered else QColor("#22c55e")
        pen = QPen(color, 1.0)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(anchor)
        painter.setFont(make_font(font_family, 8.0))
        painter.setPen(QPen(color))
        label = f"#{card.block_id} {card.confidence:.2f} {card.direction[:3]}"
        painter.drawText(QPointF(anchor.left(), max(9.0, anchor.top() - 2.0)), label)

    header = [
        f"gen {info.generation}  {info.state}  {info.stage}".strip(),
        f"hash {info.page_hash or '-'}  score {info.change_score:.3f}",
        f"blocks {info.block_count}  ocr {info.ocr_ms:.0f}ms  mt {info.translate_ms:.0f}ms"
        + ("  CACHE" if info.from_cache else ""),
    ] + list(info.extra)
    font = make_font(font_family, 9.0)
    painter.setFont(font)
    metrics = QFontMetricsF(font)
    width = max(metrics.horizontalAdvance(line) for line in header) + 12.0
    height = (metrics.height() + 2.0) * len(header) + 8.0
    box = QRectF(6.0, 6.0, width, height)
    painter.setPen(QPen(QColor("#38bdf8"), 1.0))
    painter.setBrush(QBrush(QColor(8, 12, 20, 210)))
    painter.drawRoundedRect(box, 5.0, 5.0)
    painter.setPen(QPen(QColor("#e2e8f0")))
    y = box.top() + 5.0 + metrics.ascent() + 1.0
    for line in header:
        painter.drawText(QPointF(box.left() + 6.0, y), line)
        y += metrics.height() + 2.0


def render_overlay_image(
    size: tuple,
    cards: Sequence[CardLayout],
    settings: LayoutSettings,
    debug: Optional[DebugInfo] = None,
    font_family: str = "Segoe UI",
    text_color: str = "#ffffff",
    bg_color: str = "#10131a",
    accent_color: str = "#7dd3fc",
    opacity: float = 0.82,
) -> QImage:
    """Offscreen render used by tests and for saving debug screenshots."""
    image = QImage(int(size[0]), int(size[1]), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        paint_cards(painter, cards, settings, font_family, text_color, bg_color, accent_color, opacity)
        if debug is not None:
            paint_debug(painter, cards, debug, font_family)
    finally:
        painter.end()
    return image
