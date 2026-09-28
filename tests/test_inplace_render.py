"""In-place rendering: glyph-aware erasing, wrapping, drawing and encoding.

All pages here are synthetic (white paper + drawn shapes), so every assertion is
about pixels this test drew itself -- no OCR, no GUI, no network.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import ImageFont

from manga_translator.core.models import Block, Rect
from manga_translator.render import (
    InplaceStyle,
    encode_inplace,
    font_for_text,
    render_inplace,
)

PAGE_W, PAGE_H = 600, 400

# One speech bubble with six "Chinese characters" (solid black rectangles).
BUBBLE_BOX = Rect(210, 150, 180, 90)
BUBBLE_GLYPHS = (
    (225, 160, 265, 182), (275, 160, 315, 182), (325, 160, 365, 182),
    (225, 200, 265, 222), (275, 200, 315, 222), (325, 200, 365, 222),
)
# A region far from every box: must stay pixel-identical.
FAR_REGION = (slice(320, 400), slice(440, 600))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _page_with_bubble() -> np.ndarray:
    """White page, black-bordered white ellipse, black Chinese-ish text."""
    page = np.full((PAGE_H, PAGE_W, 3), 255, np.uint8)
    cv2.ellipse(page, (300, 195), (150, 105), 0, 0, 360, (0, 0, 0), 3)
    for x1, y1, x2, y2 in BUBBLE_GLYPHS:
        cv2.rectangle(page, (x1, y1), (x2, y2), (0, 0, 0), -1)
    return page


def _region(rect: Rect, grow: int = 0) -> tuple:
    x1 = max(0, int(rect.x) - grow)
    y1 = max(0, int(rect.y) - grow)
    x2 = min(PAGE_W, int(rect.x2) + grow)
    y2 = min(PAGE_H, int(rect.y2) + grow)
    return slice(y1, y2), slice(x1, x2)


def _union(*regions: tuple) -> np.ndarray:
    mask = np.zeros((PAGE_H, PAGE_W), dtype=bool)
    for region in regions:
        mask[region] = True
    return mask


def _changed(before: np.ndarray, after: np.ndarray, threshold: int = 2) -> np.ndarray:
    delta = np.abs(after.astype(np.int16) - before.astype(np.int16)).max(axis=2)
    return delta > threshold


def _block(translation: str = "Привет, мир!") -> Block:
    return Block(id=0, text="你好世界", bbox=BUBBLE_BOX, translation=translation)


# --------------------------------------------------------------------------- #
# 1. erasing + drawing
# --------------------------------------------------------------------------- #
def test_erases_the_old_text_and_leaves_the_rest_of_the_page_alone():
    page = _page_with_bubble()
    before = page.copy()
    out = render_inplace(page, [_block()])

    assert out.shape == page.shape
    # Background far from every box is untouched, so the page is not smeared.
    assert np.array_equal(before[FAR_REGION], out[FAR_REGION])
    # The bubble outline survives: only the letters were replaced.
    assert np.array_equal(before[86:96, 292:308], out[86:96, 292:308])
    # The pixels that used to be the Chinese characters did change.
    old_text = _changed(before, out)[152:230, 212:388]
    assert old_text.mean() > 0.1
    assert _changed(before, out)[160:183, 225:366].mean() > 0.2


def test_refinement_erases_only_the_letters_not_the_bubble_fill():
    """The important part: a dotted (screentone) bubble fill must survive.

    A 1px font keeps the *drawn* translation out of the measured area, so what
    is measured is purely the erasing step.
    """
    page = np.full((PAGE_H, PAGE_W, 3), 255, np.uint8)
    cv2.ellipse(page, (300, 195), (180, 130), 0, 0, 360, (0, 0, 0), 3)
    for gy in range(165, 240, 12):
        for gx in range(230, 375, 12):
            cv2.circle(page, (gx, gy), 3, (170, 170, 170), -1)
    glyphs = ((240, 170, 270, 186), (300, 170, 330, 186), (240, 205, 270, 221), (300, 205, 330, 221))
    for x1, y1, x2, y2 in glyphs:
        cv2.rectangle(page, (x1, y1), (x2, y2), (0, 0, 0), -1)
    before = page.copy()
    box = Rect(225, 160, 150, 76)
    style = InplaceStyle(min_font_px=1, max_font_px=1, padding=3, mask_dilate_px=3)

    out = render_inplace(page, [Block(id=0, text="字", bbox=box, translation="Ой!")], style)
    changed = _changed(before, out)

    for x1, y1, x2, y2 in glyphs:  # every character is gone
        assert changed[y1:y2, x1:x2].mean() > 0.9
    # Everything else in the box -- the dots and the paper -- is bit-identical,
    # except the few px of the drawn 1px text at the centre.
    letters = _union(*[_region(Rect(*rect), 6) for rect in glyphs])
    centre = _union(_region(Rect(box.cx - 12, box.cy - 12, 24, 24)))
    unexpected = changed & _union(_region(box, 3)) & ~letters & ~centre
    assert unexpected.sum() == 0, f"{int(unexpected.sum())} background px were erased"


def test_untrustworthy_refinement_inpaints_the_whole_box():
    """A mask that covers <0.5% of the box is not trusted: the plain box is used."""
    page = np.full((PAGE_H, PAGE_W, 3), 255, np.uint8)
    for gy in range(160, 245, 12):
        for gx in range(224, 380, 12):
            cv2.circle(page, (gx, gy), 3, (170, 170, 170), -1)
    cv2.rectangle(page, (296, 194), (302, 200), (0, 0, 0), -1)  # 0.28% of the padded box
    before = page.copy()
    box = Rect(225, 160, 150, 76)
    style = InplaceStyle(min_font_px=1, max_font_px=1, padding=3)

    out = render_inplace(page, [Block(id=0, text="字", bbox=box, translation="Ой!")], style)
    delta = np.abs(out.astype(np.int16) - before.astype(np.int16)).max(axis=2)
    changed = delta > 2

    # The whole padded box was inpainted, so the screentone far from the glyph
    # is gone as well -- the opposite of the refinement case above.
    assert changed[_region(box, 3)].mean() > 0.4
    assert delta[230:235, 342:347].mean() > 10, "a far dot should have been erased"
    assert np.array_equal(before[FAR_REGION], out[FAR_REGION])


def test_draws_the_outline_halo():
    """White text with a black halo: only the halo can produce dark pixels."""
    box = Rect(120, 150, 260, 70)
    page = np.full((PAGE_H, PAGE_W, 3), 255, np.uint8)
    block = Block(id=0, text="字", bbox=box, translation="Я!")
    style = InplaceStyle(text_color=(255, 255, 255), outline_color=(0, 0, 0), outline_px=2)

    out = render_inplace(page, [block], style)
    drawn = _changed(page, out)
    assert drawn[_region(box)].any(), "the outline should have drawn dark pixels"
    assert not drawn[FAR_REGION].any()

    halo_free = render_inplace(page, [block], InplaceStyle(text_color=(255, 255, 255), outline_px=0))
    assert not _changed(page, halo_free).any(), "white text without a halo is invisible on white paper"


def test_left_alignment_starts_at_the_right_hand_padding():
    box = Rect(120, 150, 260, 70)
    page = np.full((PAGE_H, PAGE_W, 3), 255, np.uint8)
    block = Block(id=0, text="字", bbox=box, translation="Я!")
    style = InplaceStyle(padding=4)

    centred = cv2.cvtColor(render_inplace(page, [block], style), cv2.COLOR_BGR2GRAY)
    aligned = cv2.cvtColor(
        render_inplace(page, [block], InplaceStyle(padding=4, align="left")), cv2.COLOR_BGR2GRAY
    )
    rows, cols = _region(box)
    centre_x = np.flatnonzero((centred[rows, cols] < 128).any(axis=0))
    left_x = np.flatnonzero((aligned[rows, cols] < 128).any(axis=0))
    assert centre_x.size and left_x.size
    assert left_x[0] < centre_x[0]
    assert left_x[0] <= style.padding + 2


# --------------------------------------------------------------------------- #
# 2. shape, dtype, purity
# --------------------------------------------------------------------------- #
def test_returns_a_new_array_with_the_same_shape_and_dtype():
    page = _page_with_bubble()
    snapshot = page.copy()
    out = render_inplace(page, [_block()])

    assert isinstance(out, np.ndarray)
    assert out is not page
    assert out.shape == page.shape
    assert out.dtype == np.uint8
    assert np.array_equal(page, snapshot), "the caller's image must not be mutated"


# --------------------------------------------------------------------------- #
# 3. empty translation
# --------------------------------------------------------------------------- #
def test_block_without_a_translation_is_ignored():
    page = _page_with_bubble()
    out = render_inplace(page, [Block(id=0, text="你好世界", bbox=BUBBLE_BOX, translation="")])
    assert np.array_equal(out, page)

    blank = render_inplace(page, [Block(id=0, text="你好", bbox=BUBBLE_BOX, translation="   ")])
    assert np.array_equal(blank, page)

    # In a mixed page only the translated bubble is touched.
    other = Rect(40, 320, 100, 50)
    mixed = render_inplace(
        page,
        [
            Block(id=0, text="你好世界", bbox=BUBBLE_BOX, translation=""),
            Block(id=1, text="再见", bbox=other, translation="Пока!"),
        ],
    )
    assert np.array_equal(page[_region(BUBBLE_BOX)], mixed[_region(BUBBLE_BOX)])
    assert _changed(page, mixed)[_region(other)].any()
    assert np.array_equal(page[FAR_REGION], mixed[FAR_REGION])


# --------------------------------------------------------------------------- #
# 4. degenerate geometry
# --------------------------------------------------------------------------- #
def test_degenerate_blocks_never_raise():
    page = _page_with_bubble()
    before = page.copy()
    out = render_inplace(
        page,
        [
            Block(id=0, text="a", bbox=Rect(-120, -80, 60, 40), translation="Привет"),
            Block(id=1, text="b", bbox=Rect(900, 700, 200, 100), translation="Пока"),
            Block(id=2, text="c", bbox=Rect(-40, 360, 120, 90), translation="На границе"),
            Block(id=3, text="d", bbox=Rect(10, 10, 0, 0), translation="Пусто"),
            Block(id=4, text="e", bbox=Rect(300, 300, 4, 3), translation="Крошка"),
            Block(id=5, text="f", bbox=Rect(560, -60, 90, 120), translation="Сверху"),
        ],
    )
    assert out.shape == page.shape
    assert out.dtype == np.uint8
    assert np.isfinite(out.astype(np.float32)).all()
    # Blocks that fall outside the page must not touch anything.
    assert np.array_equal(before[FAR_REGION], out[FAR_REGION])

    covering = render_inplace(
        page, [Block(id=6, text="g", bbox=Rect(-50, -50, 700, 500), translation="Всё")]
    )
    assert covering.shape == page.shape and covering.dtype == np.uint8


# --------------------------------------------------------------------------- #
# 5. encoding
# --------------------------------------------------------------------------- #
def test_encode_inplace_returns_a_decodable_jpeg():
    page = _page_with_bubble()
    snapshot = page.copy()
    mime, data = encode_inplace(page)

    assert mime == "image/jpeg"
    assert isinstance(data, str)
    assert data.isascii()
    assert not data.startswith("data:")
    raw = base64.b64decode(data)
    decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape == page.shape
    assert np.array_equal(page, snapshot), "encoding must not mutate the page"

    png_mime, png_data = encode_inplace(page, "image/png")
    assert png_mime == "image/png"
    png = cv2.imdecode(np.frombuffer(base64.b64decode(png_data), np.uint8), cv2.IMREAD_COLOR)
    assert png.shape == page.shape

    assert len(encode_inplace(page, "image/jpeg", 20)[1]) <= len(data)
    assert cv2.imdecode(
        np.frombuffer(base64.b64decode(encode_inplace(page, "image/webp")[1]), np.uint8),
        cv2.IMREAD_COLOR,
    ).shape == page.shape


# --------------------------------------------------------------------------- #
# 6. fonts
# --------------------------------------------------------------------------- #
def test_font_for_text_returns_an_existing_path_or_empty():
    path = font_for_text("Привет, ёжик! Щука, объём.")
    assert isinstance(path, str)
    assert path == "" or Path(path).is_file()

    # A configured font that does not exist degrades instead of raising.
    missing = font_for_text("Привет", InplaceStyle(font_path="C:/definitely/missing/font.ttf"))
    assert missing == "" or Path(missing).is_file()
    # ... and the resolved file really renders Cyrillic.
    if missing:
        assert ImageFont.truetype(missing, 18).getbbox("Привет")[2] > 0

    assert font_for_text("") == font_for_text("", None)
    assert isinstance(font_for_text("日本語", InplaceStyle(min_font_px=1)), str)


def test_degrades_to_the_default_font_when_no_font_file_exists(monkeypatch, caplog):
    """A machine without any Cyrillic TrueType font must still render, warning once."""
    import logging

    import manga_translator.render.inplace as inplace_module

    monkeypatch.setattr(inplace_module, "_font_candidates", lambda custom: iter(()))
    monkeypatch.setattr(inplace_module, "_font_warning_logged", False)
    inplace_module._resolve_font_path.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="mt.render.inplace"):
            assert inplace_module.font_for_text("Привет") == ""
            assert inplace_module.font_for_text("Привет") == ""
        assert len([r for r in caplog.records if "font" in r.getMessage()]) == 1

        page = np.full((PAGE_H, PAGE_W, 3), 255, np.uint8)
        box = Rect(200, 170, 200, 60)
        out = render_inplace(page, [Block(id=0, text="字", bbox=box, translation="Привет!")])
        assert out.shape == page.shape and out.dtype == np.uint8
        assert (cv2.cvtColor(out, cv2.COLOR_BGR2GRAY) < 128).sum() > 0
    finally:
        inplace_module._resolve_font_path.cache_clear()


# --------------------------------------------------------------------------- #
# 7. long text stays inside its box
# --------------------------------------------------------------------------- #
def test_long_translation_stays_inside_its_box():
    page = np.full((PAGE_H, PAGE_W, 3), 255, np.uint8)
    box = Rect(220, 160, 120, 60)
    sentence = "Это очень длинный перевод, который не помещается в маленький бокс целиком. "
    text = (sentence * 5)[:300]
    assert len(text) == 300

    out = render_inplace(page, [Block(id=0, text="字" * 25, bbox=box, translation=text)])
    assert out.shape == page.shape and out.dtype == np.uint8

    gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
    assert (gray[_region(box)] < 200).sum() > 0, "something should have been drawn"
    tolerance = 2
    outside = np.ones((PAGE_H, PAGE_W), dtype=bool)
    outside[_region(box, tolerance)] = False
    assert (gray[outside] < 200).sum() == 0, "the text bled outside the box"

    # A tiny box (smaller than the minimum font size) is clipped, not fatal.
    tiny = render_inplace(page, [Block(id=1, text="字", bbox=Rect(20, 20, 9, 7), translation="Привет")])
    assert tiny.shape == page.shape and tiny.dtype == np.uint8


# --------------------------------------------------------------------------- #
# 8. no blocks
# --------------------------------------------------------------------------- #
def test_zero_blocks_returns_an_image_equal_to_the_input():
    page = _page_with_bubble()
    out = render_inplace(page, [])
    assert np.array_equal(out, page)
    assert out is not page

    untranslated = [Block(id=0, text="你好世界", bbox=BUBBLE_BOX), Block(id=1, text="再见", bbox=Rect(40, 320, 100, 50))]
    assert np.array_equal(render_inplace(page, untranslated), page)


# --------------------------------------------------------------------------- #
# performance
# --------------------------------------------------------------------------- #
def test_thirty_blocks_on_a_1280x1000_page_are_fast():
    rng = np.random.default_rng(3)
    page = rng.integers(180, 256, size=(1000, 1280, 3), dtype=np.uint8)
    blocks = []
    for index in range(30):
        x = 40 + (index % 6) * 200
        y = 40 + (index // 6) * 190
        for row in range(3):
            cv2.rectangle(page, (x + 25, y + 40 + row * 22), (x + 135, y + 56 + row * 22), (0, 0, 0), -1)
        blocks.append(
            Block(
                id=index,
                text="你好世界" * 3,
                bbox=Rect(x + 18, y + 32, 124, 70),
                translation="Привет, как дела? Всё хорошо, спасибо большое!",
            )
        )

    render_inplace(page, blocks[:1])  # warm the font cache
    started = time.perf_counter()
    out = render_inplace(page, blocks)
    elapsed = time.perf_counter() - started
    assert out.shape == page.shape
    assert elapsed < 2.0, f"30 blocks took {elapsed:.2f}s"
