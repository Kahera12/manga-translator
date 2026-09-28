"""Browser mode: panel mapping, queue behaviour, CDP helpers (no live browser)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from manga_translator.browser import cdp as cdp_module
from manga_translator.browser.reader import Panel, page_cards
from manga_translator.browser.service import PanelQueue, PanelResult
from manga_translator.cache.page_cache import settings_fingerprint
from manga_translator.core.config import Config
from manga_translator.core.models import Block, Rect


def make_panel(index: int = 0, doc_x: float = 174, doc_y: float = 650) -> Panel:
    return Panel(
        index=index, src=f"https://cdn.example/panel{index}.jpg?sign=x",
        doc_x=doc_x, doc_y=doc_y, view_x=doc_x, view_y=doc_y,
        view_w=860, view_h=672, nat_w=1280, nat_h=1000,
    )


def block(block_id: int, text: str, translation: str, x: float, y: float,
          w: float = 200, h: float = 60, sfx: bool = False) -> Block:
    return Block(id=block_id, text=text, bbox=Rect(x, y, w, h), translation=translation,
                 is_sfx=sfx, confidence=0.95)


class _Result:
    def __init__(self, blocks):
        self.blocks = blocks
        self.error = ""


# --- panel -> document mapping --------------------------------------------

def test_page_cards_map_image_coordinates_to_the_document():
    panel = make_panel(doc_x=174, doc_y=1000)
    result = _Result([block(1, "我想", "Я хочу…", 100, 200)])
    cards = page_cards([panel], {panel.key: result})
    assert len(cards) == 1
    card = cards[0]
    scale = 860 / 1280
    # x is centred on the bubble, y is the bubble top
    expected_x = 174 + 100 * scale - max(0.0, (card["w"] - 200 * scale) / 2.0)
    assert card["x"] == pytest.approx(round(expected_x, 1), abs=0.2)
    assert card["y"] == pytest.approx(1000 + 200 * scale, abs=0.1)
    assert card["above"] is True
    assert card["source"] == "我想"
    assert card["text"] == "Я хочу…"


def test_page_cards_skip_sfx_and_untranslated():
    panel = make_panel()
    result = _Result([
        block(1, "ドン", "", 10, 10, sfx=True),
        block(2, "待翻译", "", 20, 20),
        block(3, "好", "Хорошо", 30, 30),
    ])
    cards = page_cards([panel], {panel.key: result})
    assert [c["id"] for c in cards] == [3]


def test_page_cards_put_the_card_below_near_the_document_top():
    panel = make_panel(doc_y=20)
    result = _Result([block(1, "上", "Верх", 10, 10)])
    cards = page_cards([panel], {panel.key: result})
    assert cards[0]["above"] is False


def test_page_cards_ignore_panels_without_results():
    panel = make_panel()
    assert page_cards([panel], {}) == []


def test_page_cards_width_is_bounded():
    panel = make_panel()
    narrow = _Result([block(1, "短", "Коротко", 0, 0, w=20, h=20)])
    wide = _Result([block(1, "很长的一句话", "Очень длинная строка перевода", 0, 0, w=1200, h=80)])
    assert page_cards([panel], {panel.key: narrow})[0]["w"] == 120
    assert page_cards([panel], {panel.key: wide})[0]["w"] == 420


# --- queue -----------------------------------------------------------------

class _FakeTranslator:
    def __init__(self, fail_keys=()):
        self.processed = []
        self.fail_keys = set(fail_keys)

    def process(self, panel, image, is_cancelled=None):
        self.processed.append(panel.key)
        if panel.key in self.fail_keys:
            raise RuntimeError("boom")
        return PanelResult(key=panel.key, image_hash="h", blocks=[
            block(1, "原文", "Перевод", 0, 0)
        ], panel=panel)


def test_panel_queue_processes_visible_panels_first():
    translator = _FakeTranslator()
    seen = []
    queue = PanelQueue(translator, lambda panel: np.zeros((10, 10, 3), "uint8"),
                       lambda result: seen.append(result.key))
    queue.push_all([make_panel(i) for i in range(6)])
    queue.push_visible([make_panel(5)])
    queue.start()
    try:
        import time

        deadline = time.time() + 5
        while time.time() < deadline and len(seen) < 6:
            time.sleep(0.05)
    finally:
        queue.stop()
    assert len(seen) == 6
    assert seen[0].endswith("panel5.jpg?sign=x") or "panel5" in seen[0], seen
    assert queue.pending == 0


def test_panel_queue_does_not_process_a_panel_twice():
    translator = _FakeTranslator()
    queue = PanelQueue(translator, lambda panel: np.zeros((10, 10, 3), "uint8"), lambda r: None)
    panel = make_panel(1)
    queue.push_visible([panel])
    queue.push_all([panel])
    queue.start()
    try:
        import time

        time.sleep(0.6)
    finally:
        queue.stop()
    assert translator.processed.count(panel.key) == 1


def test_panel_queue_survives_failures():
    translator = _FakeTranslator(fail_keys={make_panel(2).key})
    results = []
    queue = PanelQueue(translator, lambda panel: np.zeros((10, 10, 3), "uint8"),
                       lambda result: results.append(result))
    queue.push_all([make_panel(1), make_panel(2), make_panel(3)])
    queue.start()
    try:
        import time

        deadline = time.time() + 5
        while time.time() < deadline and len(results) < 3:
            time.sleep(0.05)
    finally:
        queue.stop()
    assert len(results) == 3
    assert any(r.error for r in results)
    assert queue.failures >= 1


def test_panel_queue_reports_download_failures():
    results = []
    queue = PanelQueue(_FakeTranslator(), lambda panel: None, lambda result: results.append(result))
    queue.push_visible([make_panel(1)])
    queue.start()
    try:
        import time

        deadline = time.time() + 5
        while time.time() < deadline and not results:
            time.sleep(0.05)
    finally:
        queue.stop()
    assert results and "download failed" in results[0].error


# --- cdp helpers ------------------------------------------------------------

def test_list_tabs_parses_the_debug_endpoint(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return [
                {"type": "page", "id": "1", "title": "chapter", "url": "https://x/1",
                 "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/1"},
                {"type": "service_worker", "id": "2", "url": "sw.js",
                 "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/2"},
                {"type": "page", "id": "3", "title": "no ws", "url": "https://x/2"},
            ]

    monkeypatch.setattr(cdp_module.httpx, "get", lambda *a, **k: Response())
    tabs = cdp_module.list_tabs(1234)
    assert [t.id for t in tabs] == ["1"]
    assert tabs[0].label().startswith("chapter")


def test_discover_port_reads_devtools_active_port(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("5555\n/devtools/browser/abc\n", encoding="utf-8")
    monkeypatch.setattr(cdp_module, "_is_alive", lambda port, timeout=1.5: port == 5555)
    assert cdp_module.discover_port(profile) == 5555


def test_discover_port_reports_nothing_when_dead(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("not-a-port\n", encoding="utf-8")
    monkeypatch.setattr(cdp_module, "_is_alive", lambda port, timeout=1.5: False)
    assert cdp_module.discover_port(profile, ports=()) is None


def test_browser_executable_detection():
    executable = cdp_module.find_browser_executable()
    assert executable is None or Path(executable).exists()


# --- cache fingerprint ------------------------------------------------------

def test_settings_fingerprint_changes_with_ocr_settings(tmp_path):
    paths = Config.load.__self__ if False else None  # keep the import list honest
    cfg = Config()
    original = settings_fingerprint(cfg)
    cfg.ocr.translate_sfx_in_browser = not cfg.ocr.translate_sfx_in_browser
    assert settings_fingerprint(cfg) != original
    cfg2 = Config()
    assert settings_fingerprint(cfg2) == original
    cfg2.translation.text_model = "another-model"
    assert settings_fingerprint(cfg2) != original
