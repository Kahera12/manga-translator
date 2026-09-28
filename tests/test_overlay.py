"""Overlay layout + rendering (§16-§18).  Runs headless (offscreen Qt)."""
from __future__ import annotations

import pytest

from manga_translator.core.models import Block, Rect, TextSpan
from manga_translator.overlay.layout import (
    LayoutSettings,
    cards_bounding_boxes,
    layout_cards,
    wrap_text,
)
from manga_translator.overlay.widgets import DebugInfo, render_overlay_image


def measure(text: str, per_char: float = 7.0) -> float:
    """Deterministic stand-in for QFontMetrics."""
    return len(text) * per_char


def block(block_id: int, translation: str, x, y, w, h, text="原文") -> Block:
    quad = (x, y, x + w, y, x + w, y + h, x, y + h)
    return Block(
        id=block_id,
        text=text,
        bbox=Rect(x, y, w, h),
        spans=[TextSpan(text=text, quad=quad, confidence=0.95)],
        translation=translation,
    )


def test_wrap_keeps_text_inside_the_card():
    lines, truncated = wrap_text("Привет! Сегодня отличная погода, не так ли?", measure, 140)
    assert not truncated
    assert len(lines) > 1
    assert all(measure(line) <= 140 for line in lines)
    assert " ".join(lines).split() == "Привет! Сегодня отличная погода, не так ли?".split()


def test_wrap_hard_splits_cjk_without_spaces():
    text = "你好，今天天气真好，我是林雪，很高兴认识你！"
    lines, _ = wrap_text(text, measure, 70)
    assert len(lines) > 1
    assert all(measure(line) <= 70 for line in lines)
    assert "".join(lines) == text


def test_wrap_honours_explicit_newlines_and_truncation():
    lines, _ = wrap_text("первая строка\nвторая строка", measure, 200)
    assert lines == ["первая строка", "вторая строка"]
    many, truncated = wrap_text(" ".join(["слово"] * 40), measure, 100, max_lines=3)
    assert truncated and len(many) == 3
    assert many[-1].endswith("...")


def test_cards_are_placed_near_their_anchor_and_inside_the_window():
    blocks = [
        block(1, "Привет! Сегодня отличная погода.", 100, 100, 200, 40),
        block(2, "Старший брат, ты пришёл!", 700, 600, 180, 40),
    ]
    anchors = {1: Rect(100, 100, 200, 40), 2: Rect(700, 600, 180, 40)}
    cards = layout_cards(blocks, (1000, 800), anchors, measure, LayoutSettings())
    assert len(cards) == 2
    for card in cards:
        assert card.rect.x >= 0 and card.rect.y >= 0
        assert card.rect.x2 <= 1000 and card.rect.y2 <= 800
        assert abs(card.rect.cx - card.anchor.cx) < 260
        assert card.lines


def test_cards_do_not_stack_on_top_of_each_other():
    blocks = [
        block(1, "Первая реплика героя здесь.", 300, 300, 160, 36),
        block(2, "Вторая реплика героя здесь.", 300, 340, 160, 36),
    ]
    anchors = {1: Rect(300, 300, 160, 36), 2: Rect(300, 340, 160, 36)}
    cards = layout_cards(blocks, (900, 700), anchors, measure, LayoutSettings())
    assert len(cards) == 2
    first, second = (c.rect for c in cards)
    overlap = first.intersection(second)
    assert overlap is None or overlap.area / min(first.area, second.area) <= 0.25


def test_cards_prefer_not_to_cover_the_original_bubble():
    blocks = [block(1, "Короткая реплика.", 400, 500, 180, 40)]
    anchors = {1: Rect(400, 500, 180, 40)}
    cards = layout_cards(blocks, (1000, 900), anchors, measure, LayoutSettings())
    assert cards and not cards[0].covered, "there was room above the bubble"

    # No room at all: the card may cover the bubble instead of leaving the screen.
    tight = [block(1, "Реплика.", 0, 0, 1000, 900)]
    tight_cards = layout_cards(tight, (1000, 900), {1: Rect(0, 0, 1000, 900)}, measure,
                               LayoutSettings(max_card_width=300))
    assert tight_cards and tight_cards[0].covered


def test_cards_bounding_boxes_are_usable_as_masks():
    blocks = [block(1, "Текст перевода.", 200, 200, 160, 40)]
    cards = layout_cards(blocks, (800, 600), {1: Rect(200, 200, 160, 40)}, measure, LayoutSettings())
    boxes = cards_bounding_boxes(cards, margin=4.0)
    assert len(boxes) == 1
    assert boxes[0].w > cards[0].rect.w


def test_offscreen_render_contains_cards_and_text():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    blocks = [
        block(1, "Привет! Сегодня отличная погода, не так ли?", 60, 60, 260, 40),
        block(2, "Старший брат, ты пришёл!", 420, 320, 180, 40),
    ]
    anchors = {1: Rect(60, 60, 260, 40), 2: Rect(420, 320, 180, 40)}
    cards = layout_cards(blocks, (800, 600), anchors, measure, LayoutSettings())
    image = render_overlay_image(
        (800, 600), cards, LayoutSettings(), debug=DebugInfo(generation=3, page_hash="abc", block_count=2)
    )
    assert image.width() == 800 and image.height() == 600

    transparent = 0
    text_pixels = 0
    for y in range(0, 600, 5):
        for x in range(0, 800, 5):
            color = image.pixelColor(x, y)
            if color.alpha() < 20:
                transparent += 1
            elif color.red() > 220 and color.green() > 220 and color.blue() > 220:
                text_pixels += 1
    assert transparent > 100, "background must stay transparent"
    assert text_pixels > 20, "text must actually be painted"
