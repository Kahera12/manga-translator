"""OCR grouping, confidence filtering and SFX heuristics (§7-§9)."""
from __future__ import annotations

from manga_translator.core.models import Block, Rect
from manga_translator.ocr.filtering import (
    FilterSettings,
    SfxSettings,
    cjk_count,
    cjk_ratio,
    detect_sfx,
    filter_spans,
    traditional_share,
)
from manga_translator.ocr.grouping import GroupSettings, group_spans
from manga_translator.ocr.stub_engine import span


def test_lines_of_one_bubble_are_grouped():
    spans = [
        span("你好，今天", 60, 100, 200, 32),
        span("天气真好。", 60, 140, 180, 32),   # next line of the same bubble
        span("为什么？", 700, 400, 140, 32),     # different bubble
    ]
    blocks = group_spans(spans)
    assert len(blocks) == 2
    texts = [b.text for b in blocks]
    assert "你好，今天天气真好。" in texts
    assert "为什么？" in texts
    joined = [b for b in blocks if "天气" in b.text][0]
    assert len(joined.spans) == 2
    assert joined.direction == "horizontal"


def test_reading_order_is_right_to_left_for_manga():
    spans = [
        span("右", 800, 100, 60, 120),
        span("中", 600, 100, 60, 120),
        span("左", 400, 100, 60, 120),
    ]
    blocks = group_spans(spans, GroupSettings(right_to_left=True))
    assert [b.text for b in blocks] == ["右", "中", "左"]
    assert [b.id for b in blocks] == [1, 2, 3]


def test_vertical_columns_are_grouped_and_marked_vertical():
    spans = [
        span("灵气", 700, 100, 40, 120),
        span("失控", 660, 100, 40, 120),
    ]
    for s in spans:
        s.angle = 90.0
    blocks = group_spans(spans)
    assert len(blocks) == 1
    assert blocks[0].direction == "vertical"
    assert blocks[0].text == "灵气失控"


def test_distant_bubbles_are_not_merged():
    spans = [
        span("你好", 60, 60, 120, 30),
        span("再见", 900, 700, 120, 30),
    ]
    blocks = group_spans(spans)
    assert len(blocks) == 2


def test_low_confidence_is_kept_but_not_translated():
    spans = [
        span("清楚", 60, 60, 120, 30, score=0.95),
        span("模糊", 60, 120, 120, 30, score=0.31),
    ]
    result = filter_spans(spans, FilterSettings(min_confidence=0.55))
    assert [s.text for s in result.accepted] == ["清楚"]
    assert [s.text for s in result.low_confidence] == ["模糊"]


def test_non_chinese_text_is_dropped():
    spans = [span("hello world", 10, 10, 100, 20, score=0.99)]
    result = filter_spans(spans, FilterSettings(min_confidence=0.5))
    assert not result.accepted
    assert result.non_cjk[0].text == "hello world"


def test_cjk_helpers():
    assert cjk_count("你好，世界") == 4
    assert cjk_ratio("你好abc") > 0.3
    assert traditional_share("認識說話") > 0


def test_sfx_detection_heuristics():
    sfx = Block(id=1, text="ドン", bbox=Rect(100, 100, 400, 300), spans=[], confidence=0.9)
    assert detect_sfx(sfx, page_area=1200 * 900, settings=SfxSettings())
    dialogue = Block(id=2, text="你好，今天天气真好。", bbox=Rect(100, 100, 220, 40),
                     spans=[], confidence=0.95)
    assert not detect_sfx(dialogue, page_area=1200 * 900, settings=SfxSettings())
    # ... unless the user explicitly wants SFX translated
    assert not detect_sfx(sfx, page_area=1200 * 900, settings=SfxSettings(translate_sfx=True))
