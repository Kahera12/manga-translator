"""Bridge: wire protocol, panel queue behaviour and the local WebSocket server.

Nothing here touches the network, the cache DB, the OCR engine or a provider: the
session is driven directly (`_take_batch` / `_mark`), never through its worker
thread, and the server test only binds a loopback port and stops it again.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, List

import numpy as np
import pytest

from manga_translator.bridge.protocol import (
    PROTOCOL_VERSION,
    BlockPayload,
    PanelPayload,
    PanelSpec,
    decode,
    encode,
)
from manga_translator.bridge.server import PORT_RANGE, BridgeServer, port_is_free
from manga_translator.bridge.service import MAX_PANEL_ATTEMPTS, BridgeSession
from manga_translator.cache.page_cache import PageCache
from manga_translator.core.config import Config
from manga_translator.glossary.glossary import Glossary
from manga_translator.ocr.stub_engine import StubOcrEngine, span
from manga_translator.translation.base import TranslateRequest, TranslateResult


# --- protocol ---------------------------------------------------------------

def test_encode_decode_round_trip():
    payload = {"key": "https://cdn.example/1.jpg", "image_w": 800, "image_h": 1000,
               "blocks": [{"id": "0:1", "x": 10.0, "y": 20.0}]}
    raw = encode("panel", **payload)
    assert json.loads(raw)["type"] == "panel"
    assert decode(raw) == {"type": "panel", **payload}


def test_decode_reports_garbage_as_invalid():
    assert decode("not json") == {"type": "invalid"}
    assert decode('{"no":"type"}') == {"type": "invalid"}
    # A JSON array is not a message either - the extension must never be answered
    # with something that has no `type`.
    assert decode('[{"type": "panel"}]') == {"type": "invalid"}


def test_panel_payload_survives_the_wire():
    payload = PanelPayload("k", 1280, 1000, [BlockPayload("0:1", 1.0, 2.0, 3.0, 4.0, text="原文")])
    assert decode(encode("panel", **payload.to_dict()))["blocks"] == payload.to_dict()["blocks"]


# --- PanelSpec --------------------------------------------------------------

def test_panel_spec_defaults_for_missing_fields():
    panel = PanelSpec.from_dict({"key": "k"})
    assert panel == PanelSpec(key="k", url="")
    assert (panel.width, panel.height, panel.order, panel.doc_x, panel.doc_y) == (0, 0, 0, 0.0, 0.0)
    assert panel.visible is False


def test_panel_spec_uses_the_url_as_key_when_the_key_is_missing():
    # Older extension builds send only the image URL; the URL is the identity then.
    panel = PanelSpec.from_dict({"url": "https://cdn.example/a.jpg"})
    assert panel.key == "https://cdn.example/a.jpg"
    assert panel.url == "https://cdn.example/a.jpg"


def test_panel_spec_reads_the_geometry_and_round_trips():
    panel = PanelSpec.from_dict({"key": "k", "url": "https://cdn.example/a.jpg",
                                 "width": 1280, "height": 1000, "order": 3,
                                 "doc_x": 1.5, "doc_y": 2.5, "visible": True})
    assert (panel.width, panel.height, panel.order) == (1280, 1000, 3)
    assert panel.visible is True
    assert panel.to_dict() == {
        "key": "k", "url": "https://cdn.example/a.jpg", "width": 1280, "height": 1000,
        "order": 3, "doc_x": 1.5, "doc_y": 2.5, "visible": True,
    }
    assert PanelSpec.from_dict(panel.to_dict()) == panel


# --- payloads ---------------------------------------------------------------

def test_panel_payload_has_the_documented_wire_shape():
    block = BlockPayload(id="0:1", x=120.0, y=340.0, w=220.0, h=70.0, text="原文",
                         translation="Перевод", vertical=False, confidence=0.97, sfx=False)
    wire = PanelPayload("k", 1280, 1000, [block]).to_dict()
    assert set(wire) == {"key", "image_w", "image_h", "blocks", "from_cache", "inplace", "error"}
    assert (wire["key"], wire["image_w"], wire["image_h"]) == ("k", 1280, 1000)
    assert wire["from_cache"] is False and wire["inplace"] is None and wire["error"] == ""
    assert wire["blocks"] == [{
        "id": "0:1", "x": 120.0, "y": 340.0, "w": 220.0, "h": 70.0, "text": "原文",
        "translation": "Перевод", "vertical": False, "confidence": 0.97, "sfx": False,
    }]


def test_panel_payload_without_blocks_is_empty_not_shared():
    # A default_factory, not a shared list: two empty payloads must not be the same
    # object, or one panel's blocks would leak into the next.
    first = PanelPayload("a", 10, 20).to_dict()
    second = PanelPayload("b", 30, 40).to_dict()
    assert first["blocks"] == [] and second["blocks"] == []


# --- session ----------------------------------------------------------------

class _FakeBackend:
    """Stands in for the translation backend: records the request, translates all."""

    model = "fake-model"

    def __init__(self) -> None:
        self.requests: List[TranslateRequest] = []

    def translate_page(self, request: TranslateRequest, is_cancelled=None) -> TranslateResult:
        self.requests.append(request)
        return TranslateResult(translations={item.id: f"RU:{item.text}" for item in request.items})


def _spec(key: str, order: int = 0, visible: bool = False) -> PanelSpec:
    return PanelSpec(key=key, url=f"https://cdn.example/{key}.jpg", width=1280, height=1000,
                     order=order, visible=visible)


@pytest.fixture()
def build_session(tmp_paths):
    """Factory for a session that is driven by hand: no thread, engine or network."""

    def build(send: Callable[[str], None] | None = None, **translation_settings):
        cfg = Config.load(tmp_paths)
        cfg.translation.provider = "fake"      # no provider lookup, no key read
        cfg.translation.vision_mode = "off"    # ...and no vision reader is built
        for name, value in translation_settings.items():
            setattr(cfg.translation, name, value)
        messages: List[dict] = []
        session = BridgeSession(
            cfg,
            PageCache(tmp_paths, enabled=False),   # enabled=False: the DB is never opened
            Glossary(tmp_paths.glossary),
            _FakeBackend(),
            send=send or (lambda text: messages.append(json.loads(text))),
            # Panels are never downloaded: the stub keeps `_image_for` off the CDN
            # even if a test ever drives the worker by accident.
            fetch_image=lambda _spec: np.zeros((40, 30, 3), np.uint8),
        )
        return session, session.backend, messages

    return build


def _finishes_within(call: Callable[[], None], timeout: float = 3.0) -> bool:
    """True when `call` returned; False when it is stuck (used to catch a deadlock)."""
    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive()


def test_register_queues_panels_and_puts_visible_ones_first(build_session):
    session, _backend, _messages = build_session()
    session.register([_spec("a", order=0), _spec("b", order=1), _spec("c", order=2, visible=True)])
    assert session.pending == 3
    assert session.status.panels == 3
    # The panel the reader is looking at is emitted before the rest of the chapter.
    assert [panel.key for panel in session._take_batch()] == ["c", "a", "b"]


def test_visible_panels_keep_their_reading_order(build_session):
    session, _backend, _messages = build_session()
    session.register([_spec("c", order=2, visible=True), _spec("a", order=0, visible=True),
                      _spec("b", order=1)])
    assert [panel.key for panel in session._take_batch()] == ["a", "c", "b"]


def test_a_batch_never_exceeds_the_configured_panel_count(build_session):
    session, _backend, _messages = build_session(vision_panels_per_request=2)
    session.register([_spec("a", order=0), _spec("b", order=1), _spec("c", order=2, visible=True)])
    batch = session._take_batch()
    assert len(batch) == session.cfg.translation.vision_panels_per_request == 2
    assert batch[0].key == "c"          # the visible panel is first even when capped


def test_mark_returns_instead_of_deadlocking(build_session):
    # Regression: a finished panel used to deadlock the worker for good (the lock
    # was not reentrant), so the session stopped translating after the first panel.
    session, _backend, _messages = build_session()
    session.register([_spec("k")])
    assert _finishes_within(lambda: session._mark("k", True))
    assert "k" in session._done


def test_mark_is_safe_while_the_session_lock_is_held(build_session):
    # The same invariant from the other side: reporting progress from inside a
    # locked section must work, which is exactly what the RLock in the session is for.
    session, _backend, _messages = build_session()
    session.register([_spec("k")])

    def mark_under_lock() -> None:
        with session._lock:
            session._mark("k", True)

    assert _finishes_within(mark_under_lock)


def test_a_failed_panel_is_retried_before_it_is_given_up_on(build_session):
    session, _backend, _messages = build_session()
    session.register([_spec("k")])
    assert MAX_PANEL_ATTEMPTS == 3          # retried twice, the third failure gives up
    session._mark("k", False)
    # Not done: it went back into the queue with a backoff instead of vanishing.
    assert "k" not in session._done
    assert session._retry_at["k"] > time.time()
    assert session.pending == 0
    assert session._take_batch() == []
    first_backoff = session._retry_at["k"]
    session._mark("k", False)
    assert session._retry_at["k"] > first_backoff      # the backoff grows per attempt

    session._mark("k", False)                          # MAX_PANEL_ATTEMPTS: give up
    assert "k" in session._done
    assert session.status.failed == 1
    assert session.status.processed == 1


def test_request_re_emits_a_finished_panel(build_session):
    session, _backend, _messages = build_session()
    session.register([_spec("k")])
    session._mark("k", True)
    assert "k" in session._done and session.pending == 0
    # The extension asks again (e.g. after a reconnect): the panel must come back.
    session.request(["k"], mode="inplace")
    assert "k" not in session._done
    assert session.mode == "inplace" and session.status.mode == "inplace"
    assert [panel.key for panel in session._take_batch()] == ["k"]


def test_cancel_drops_a_panel_from_the_queue(build_session):
    session, _backend, _messages = build_session()
    session.register([_spec("a"), _spec("b"), _spec("c")])
    session.cancel(["b"])
    assert "b" not in session._panels and "b" not in session._order
    assert session.pending == 2
    assert [panel.key for panel in session._take_batch()] == ["a", "c"]


def test_cancel_of_an_unknown_panel_is_harmless(build_session):
    session, _backend, _messages = build_session()
    session.register([_spec("a")])
    session.cancel([])
    session.cancel(["never-registered"])
    assert session.pending == 1


def test_the_ocr_path_translates_a_batch_and_marks_it_done(build_session):
    # The whole path, minus the worker thread: scripted OCR -> fake backend -> wire.
    session, backend, messages = build_session()
    session._engine_getter = lambda: StubOcrEngine(
        script=[[span("你好，今天天气真好。", 30, 60, 140, 40)]])
    panel = _spec("k")
    session.register([panel])
    image = np.full((300, 200, 3), 255, np.uint8)
    image[60:100, 30:170] = 0
    session._read_ocr([(panel, image)])

    assert [item.text for item in backend.requests[0].items] == ["你好，今天天气真好。"]
    emitted = next(message for message in messages if message["type"] == "panel")
    assert emitted["key"] == "k"
    assert emitted["blocks"][0]["text"] == "你好，今天天气真好。"
    assert emitted["blocks"][0]["translation"] == "RU:你好，今天天气真好。"
    assert "k" in session._done          # the panel left the queue
    assert session.status.blocks == 1


def test_hello_emits_a_welcome_with_the_session_settings(build_session):
    session, backend, messages = build_session(vision_panels_per_request=4)
    session.hello("https://site.example/ch/1", "Chapter 1")
    assert session.status.connected is True
    assert session.status.page_url == "https://site.example/ch/1"

    assert len(messages) == 1
    welcome = messages[0]
    assert welcome["type"] == "welcome"
    assert welcome["version"] == PROTOCOL_VERSION
    assert welcome["provider"] == "fake"
    assert welcome["model"] == backend.model
    assert welcome["target_language"] == session.cfg.translation.target_language
    assert welcome["batch_size"] == 4
    assert welcome["vision"] is False        # these sessions are built with vision off


# --- server -----------------------------------------------------------------

def _free_port_block(start: int = 39_000, span: int = 200) -> int:
    """A loopback port whose successors are free too.

    `BridgeServer.start` walks up to `PORT_RANGE` ports forward when one is busy,
    which would change the port in `status()["url"]`; asking for a free block keeps
    the assertion about the URL honest.
    """
    for candidate in range(start, start + span):
        if all(port_is_free(candidate + offset) for offset in range(PORT_RANGE + 1)):
            return candidate
    pytest.skip("no free loopback port block available")


def test_server_starts_and_stops_on_the_requested_port(build_session):
    port = _free_port_block()
    server = BridgeServer(lambda send: build_session(send=send)[0], port=port)
    try:
        assert server.start() is True
        status = server.status()
        assert status["running"] is True
        assert status["port"] == port
        assert status["url"] == f"ws://127.0.0.1:{port}/ws"
        assert status["clients"] == 0          # no client is connected in this test
        assert port_is_free(port) is False     # something really listens there now
    finally:
        server.stop()
    assert server.running is False
    assert server.status()["running"] is False
    assert port_is_free(port) is True      # the listener is really gone, not leaked
