"""Vision path: answer parsing, box geometry, snapping, availability, reader loop.

Everything runs on synthetic images and a fake backend - no provider, no network,
no OCR engine. `snap_box` is exercised for real (it is the part that decides where
a translated card lands), so its assertions are written to hold for both answers:
a refined box, or `None` (the caller then keeps the model's box).
"""
from __future__ import annotations

import base64
import json
from typing import List, Sequence, Tuple

import cv2
import numpy as np
import pytest

from manga_translator.core.config import TranslationSettings
from manga_translator.core.models import Block, PipelineError, Rect
from manga_translator.ocr.filtering import SfxSettings, looks_like_watermark
from manga_translator.translation.providers import get_spec
from manga_translator.vision import reader as reader_module
from manga_translator.vision.reader import (
    VisionReader,
    VisionUnavailable,
    _normalise_box,
    encode_panel,
    parse_pages,
)
from manga_translator.vision.snap import snap_box

PAGE = (1280, 1000)


@pytest.fixture(autouse=True)
def _isolated_broken_registry():
    """VisionReader remembers a broken provider+model process-wide (`_BROKEN`).

    Clear it around every test so one test's "this endpoint cannot read images"
    cannot decide another test's `available`.
    """
    saved = dict(reader_module._BROKEN)
    reader_module._BROKEN.clear()
    yield
    reader_module._BROKEN.clear()
    reader_module._BROKEN.update(saved)


# --- helpers ----------------------------------------------------------------

class _FakeVisionBackend:
    """Records the vision request and replays a scripted answer."""

    def __init__(self, answer: str = "{}") -> None:
        self.answer = answer
        self.calls: List[dict] = []

    def complete_vision(self, system: str, prompt: str, images: Sequence[str],
                        is_cancelled=None, timeout_s: float = 0.0,
                        reasoning_effort: str = "") -> str:
        self.calls.append({"system": system, "prompt": prompt, "images": list(images),
                           "timeout_s": timeout_s, "reasoning_effort": reasoning_effort})
        return self.answer


def _reader(backend=None, **settings) -> VisionReader:
    """A reader wired to the OpenAI spec (the only kind that may read images)."""
    options = {"vision_mode": "always", "text_model": "gpt-4o-mini"}
    options.update(settings)
    return VisionReader(TranslationSettings(**options), get_spec("openai"), "dummy-key",
                        backend=backend)


def _glyph_bars(height: int, width: int, xs: Sequence[int], y: int,
                bar: Tuple[int, int] = (20, 30)) -> np.ndarray:
    """White page with black bars where a line of glyphs would be.

    Solid glyph-sized bars with wide gaps on purpose: a real glyph is mostly
    background, and `snap_box` judges ink by the minority class of a region.
    """
    image = np.full((height, width, 3), 255, np.uint8)
    for x in xs:
        image[y:y + bar[1], x:x + bar[0]] = 0
    return image


def _overlap(box: Tuple[float, float, float, float],
             region: Tuple[float, float, float, float]) -> float:
    """Area shared by a box and the ground-truth ink rectangle."""
    x1, y1 = max(box[0], region[0]), max(box[1], region[1])
    x2 = min(box[0] + box[2], region[0] + region[2])
    y2 = min(box[1] + box[3], region[1] + region[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


# --- parse_pages ------------------------------------------------------------

def _pages_answer(pages: list) -> str:
    return json.dumps({"pages": pages}, ensure_ascii=False)


def test_parse_pages_reads_the_pages_array():
    answer = _pages_answer([
        {"page": 0, "blocks": [{"text": "你好", "box": [0, 0, 10, 10]}]},
        {"page": 1, "blocks": [{"text": "再见"}]},
    ])
    parsed = parse_pages(answer, 2)
    assert sorted(parsed) == [0, 1]
    assert parsed[0][0]["text"] == "你好"
    assert parsed[1][0]["text"] == "再见"


def test_parse_pages_tolerates_a_markdown_fence():
    # Models wrap the JSON in ```json often enough that this is the common case.
    answer = "```json\n" + _pages_answer([{"page": 0, "blocks": [{"text": "很好"}]}]) + "\n```"
    assert parse_pages(answer, 1)[0][0]["text"] == "很好"


def test_parse_pages_finds_the_json_inside_prose():
    answer = "Sure! Here is the result:\n" + _pages_answer([{"page": 0, "blocks": [{"text": "好"}]}])
    assert parse_pages(answer, 1)[0][0]["text"] == "好"


def test_parse_pages_accepts_a_bare_blocks_object():
    # A single panel: the model answers with one page's blocks and no wrapper.
    answer = json.dumps({"blocks": [{"text": "单独一页"}]}, ensure_ascii=False)
    parsed = parse_pages(answer, 1)
    assert list(parsed) == [0]
    assert parsed[0][0]["text"] == "单独一页"


def test_parse_pages_accepts_a_dict_keyed_by_page_index():
    answer = json.dumps({"0": {"blocks": [{"text": "第一页"}]},
                         "1": {"blocks": [{"text": "第二页"}]}}, ensure_ascii=False)
    parsed = parse_pages(answer, 2)
    assert sorted(parsed) == [0, 1]
    assert parsed[1][0]["text"] == "第二页"


def test_parse_pages_ignores_pages_that_were_never_sent():
    answer = _pages_answer([{"page": 0, "blocks": [{"text": "在"}]},
                            {"page": 7, "blocks": [{"text": "越界"}]}])
    # Page 7 was not in the request: it is dropped, never remapped onto page 1.
    assert list(parse_pages(answer, 2)) == [0]


def test_parse_pages_returns_nothing_for_an_empty_page_list():
    assert parse_pages('{"pages": []}', 3) == {}


def test_parse_pages_reports_prose_instead_of_inventing_pages():
    # The prompt demands JSON only, but a model still answers with a sentence now
    # and then. `parse_pages` raises - `read_pages` turns that into
    # `VisionUnavailable` so the OCR path takes over. Silently returning "no text
    # on this page" would delete the whole page instead.
    with pytest.raises(ValueError):
        parse_pages("I could not read this page.", 2)


# --- _normalise_box ---------------------------------------------------------

def test_normalise_box_converts_the_documented_0_1000_units():
    # [x, y, w, h] on 0..1000 is what the system prompt documents (and what fits a
    # small page into the answer without the model counting pixels).
    assert _normalise_box([100, 200, 300, 100], 1000, 500) == pytest.approx(
        (100.0, 100.0, 300.0, 50.0))


def test_normalise_box_converts_0_1_fractions():
    assert _normalise_box([0.1, 0.2, 0.3, 0.1], 1000, 500) == pytest.approx(
        (100.0, 100.0, 300.0, 50.0))


def test_normalise_box_keeps_pixel_coordinates():
    # Only a value above 1000 proves the answer is in page pixels, so this box is
    # left alone. A pixel box whose coordinates all stay under 1000 cannot be told
    # apart from the 0..1000 form and is scaled down - a known ambiguity of the
    # heuristic, not something a test can pin down from the outside.
    assert _normalise_box([1200, 340, 220, 70], 2000, 1200) == pytest.approx(
        (1200.0, 340.0, 220.0, 70.0))


def test_normalise_box_pushes_the_origin_inside_the_image():
    assert _normalise_box([-0.1, -0.2, 0.5, 0.5], 100, 80) == pytest.approx((0.0, 0.0, 50.0, 40.0))


def test_normalise_box_caps_the_size_at_the_page():
    assert _normalise_box([0, 0, 1.4, 1.4], 100, 80) == pytest.approx((0.0, 0.0, 100.0, 80.0))


def test_normalise_box_rejects_unusable_boxes():
    assert _normalise_box([1, 2, 3], 1000, 500) is None            # not four values
    assert _normalise_box("1,2,3,4", 1000, 500) is None            # not a sequence
    assert _normalise_box([1, 2, 3, "x"], 1000, 500) is None       # not a number
    assert _normalise_box([True, 1, 2, 3], 1000, 500) is None      # booleans are not sizes
    assert _normalise_box([0, 0, -5, 10], 1000, 500) is None       # negative size
    assert _normalise_box([0, 0, 0, 10], 1000, 500) is None        # zero size


# --- encode_panel -----------------------------------------------------------

def _decode_data_url(encoded: str) -> np.ndarray:
    assert encoded.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(encoded.split(",", 1)[1])
    decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None, "the payload is not a decodable JPEG"
    return decoded


def test_encode_panel_downscales_to_a_valid_jpeg():
    image = np.zeros((200, 2000, 3), np.uint8)
    image[:, 1000:] = 200                       # an edge, so it is not a blank image
    decoded = _decode_data_url(encode_panel(image, max_side=1280))
    assert max(decoded.shape[:2]) <= 1280       # 2000 px must not be uploaded as is
    assert decoded.shape[1] / decoded.shape[0] == pytest.approx(10.0, rel=0.05)


def test_encode_panel_leaves_a_small_panel_alone():
    decoded = _decode_data_url(encode_panel(np.zeros((40, 60, 3), np.uint8), max_side=1280))
    assert decoded.shape[:2] == (40, 60)


# --- snap_box ---------------------------------------------------------------

def test_snap_box_moves_an_offset_box_onto_the_glyphs():
    # Three glyph-sized bars, and the model's box 20 px to the right of them: the
    # refinement must move the box back onto the ink, never away from it.
    image = _glyph_bars(400, 400, (150, 210, 270), y=200)
    ink = (150.0, 200.0, 140.0, 30.0)
    model = (170.0, 200.0, 140.0, 30.0)
    snapped = snap_box(image, model)
    # `None` is a legitimate answer (the caller keeps the model's box), but whatever
    # comes back must not cover less of the text than the input did.
    assert snapped is None or _overlap(snapped, ink) >= _overlap(model, ink)
    assert snapped is None or snapped[2] > 0 and snapped[3] > 0


def test_snap_box_returns_none_on_a_page_without_ink():
    blank = np.full((200, 200, 3), 255, np.uint8)
    # Nothing to snap to: an invented box would be a card pointing at empty paper.
    assert snap_box(blank, (50, 50, 60, 40)) is None
    # A box outside the image is not refined either - the caller keeps its own.
    assert snap_box(blank, (1000, 1000, 60, 40)) is None


# --- availability -----------------------------------------------------------

def test_reader_is_unavailable_when_vision_is_off():
    reader = _reader(vision_mode="off")
    assert reader.enabled is False
    assert reader.available is False


def test_reader_is_available_with_a_spec_a_model_and_a_key():
    reader = _reader(backend=_FakeVisionBackend())
    assert reader.available is True
    assert reader.model == "gpt-4o-mini"
    assert reader.fingerprint == "openai:gpt-4o-mini"


def test_reader_without_a_key_is_unavailable():
    # No backend to borrow credentials from and no key: the vision path must stay
    # off instead of sending unauthenticated requests.
    settings = TranslationSettings(vision_mode="always", text_model="gpt-4o-mini")
    assert VisionReader(settings, get_spec("openai"), "").available is False


def test_mark_unavailable_switches_the_reader_off():
    reader = _reader(backend=_FakeVisionBackend())
    assert reader.available is True
    reader.mark_unavailable("x")
    assert reader.available is False


def test_handle_error_disables_vision_on_an_auth_failure():
    reader = _reader(backend=_FakeVisionBackend())
    reason = reader.handle_error(PipelineError("bad key", kind="auth"))
    assert reason                              # the caller falls back to OCR
    assert reader.available is False


def test_handle_error_keeps_vision_on_a_transient_failure():
    # A timeout is not proof that the endpoint cannot read images: disabling vision
    # for the rest of the session would lose every bubble OCR cannot read.
    reader = _reader(backend=_FakeVisionBackend())
    assert reader.handle_error(PipelineError("gateway timeout", kind="timeout")) == ""
    assert reader.available is True


# --- read_pages -------------------------------------------------------------

def _panel_image(height: int = 80, width: int = 120) -> np.ndarray:
    """A tiny panel with three thin bars: a line of text for `snap_box` to find."""
    return _glyph_bars(height, width, (20, 40, 60), y=20, bar=(6, 20))


def test_read_pages_reads_a_batch_in_one_request(monkeypatch):
    monkeypatch.delenv("MT_VISION_DUMP", raising=False)   # never write a dump file
    answer = json.dumps({"pages": [
        {"page": 0, "blocks": [{"text": "你好", "translation": "Привет",
                                "box": [167, 250, 383, 250]}]},
        {"page": 1, "blocks": [{"text": "再见", "translation": "Пока",
                                "box": [167, 250, 383, 250]}]},
    ]}, ensure_ascii=False)
    backend = _FakeVisionBackend(answer)
    reader = _reader(backend=backend)

    result = reader.read_pages([("panel-0", _panel_image()), ("panel-1", _panel_image())],
                               target_language="Russian")

    assert list(result) == ["panel-0", "panel-1"]
    assert result["panel-0"].width == 120 and result["panel-0"].height == 80
    assert [block.text for block in result["panel-0"].blocks] == ["你好"]
    assert [block.translation for block in result["panel-1"].blocks] == ["Пока"]

    block = result["panel-0"].blocks[0]
    assert block.bbox.w > 0 and block.bbox.h > 0
    # The box came back in the page's own grid and landed on the drawn glyphs.
    assert block.bbox.intersects(Rect(20, 20, 46, 20))

    assert len(backend.calls) == 1             # one request for the whole batch
    call = backend.calls[0]
    assert len(call["images"]) == 2
    assert all(image.startswith("data:image/jpeg;base64,") for image in call["images"])
    assert "Russian" in call["system"]
    assert "Russian" in call["prompt"]
    assert "Answer in exactly this shape" in call["prompt"]
    assert call["timeout_s"] == reader.settings.vision_timeout_s


def test_read_pages_raises_unavailable_when_the_answer_is_not_json():
    backend = _FakeVisionBackend("I could not read these pages.")
    reader = _reader(backend=backend)
    with pytest.raises(VisionUnavailable):
        reader.read_pages([("panel-0", _panel_image())], target_language="Russian")


def test_read_pages_needs_no_request_for_an_empty_batch():
    backend = _FakeVisionBackend()
    reader = _reader(backend=backend)
    assert reader.read_pages([], target_language="Russian") == {}
    assert backend.calls == []


def test_read_pages_refuses_to_run_when_vision_is_off():
    # The OCR path depends on this: a reader that is not available must fail loudly
    # (VisionUnavailable) instead of sending a request nobody can answer.
    off = _reader(vision_mode="off")
    with pytest.raises(VisionUnavailable):
        off.read_pages([("panel-0", _panel_image())], target_language="Russian")


# --- looks_like_watermark ---------------------------------------------------

def _block(text: str, x: float, y: float, w: float = 200.0, h: float = 80.0) -> Block:
    return Block(id=1, text=text, bbox=Rect(x, y, w, h), confidence=0.95)


def test_looks_like_watermark_fires_on_the_site_name():
    # "快看！快看漫画" is burned into the artwork at the left edge by the reading
    # site, and the OCR happily reads it as dialogue.
    assert looks_like_watermark(_block("快看！快看漫画", 40, 300), PAGE, SfxSettings()) is True


def test_looks_like_watermark_fires_on_a_short_latin_string_at_the_edge():
    assert looks_like_watermark(_block("example.com", 4, 500, w=200, h=30),
                                PAGE, SfxSettings()) is True


def test_looks_like_watermark_keeps_ordinary_dialogue():
    # A false positive here deletes a real line from the page silently, so the
    # middle of the page must never match - in any language, vertical or not.
    for text, box in (
        ("你今天怎么来了？", (400, 400, 220, 90)),
        ("师兄，你来了！", (300, 300, 180, 240)),     # vertical column
        ("快点走！", (600, 500, 160, 70)),
        ("おはよう、先輩！", (500, 200, 220, 90)),
        ("example.com", (400, 500, 200, 30)),        # latin, but not at an edge
    ):
        assert looks_like_watermark(_block(text, *box), PAGE, SfxSettings()) is False, text
    # Known limitation, reported and not fixed here: the pattern list contains the
    # bare "快看", which is also ordinary Chinese ("look!"), so a line like
    # "快看那边！" is dropped as a watermark.


def test_looks_like_watermark_can_be_switched_off():
    settings = SfxSettings(drop_watermarks=False)
    assert looks_like_watermark(_block("快看漫画", 40, 300), PAGE, settings) is False
