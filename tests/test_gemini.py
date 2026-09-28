"""Gemini REST backend: request format, structured parsing, retries, errors.

A local HTTP server stands in for the Gemini endpoint, so request formatting and
response handling are verified for real - without needing an API key.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from manga_translator.core.config import TranslationSettings
from manga_translator.core.models import PipelineError
from manga_translator.translation.base import (
    CancelledError,
    TranslateItem,
    TranslateRequest,
)
from manga_translator.translation.gemini import GeminiBackend
from manga_translator.translation.prompts import RESPONSE_SCHEMA

API_KEY = "AIzaSyTESTKEY_0123456789_do_not_log"


class StubGemini:
    """Scripted HTTP endpoint that records what the backend actually sent."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests = []
        self.headers = []
        self.delay_s = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    outer.requests.append((self.path, json.loads(raw.decode("utf-8"))))
                except ValueError:
                    outer.requests.append((self.path, {}))
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                if outer.delay_s:
                    time.sleep(outer.delay_s)
                status, payload, extra = (
                    outer.script.pop(0) if outer.script else (200, _ok([{"id": 1, "translation": "x"}]), {})
                )
                body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
                body = body.encode("utf-8") if isinstance(body, str) else body
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _ok(rows):
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": json.dumps({"translations": rows})}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"totalTokenCount": 42},
    }


def _error(status_text, message):
    return {"error": {"code": 400, "status": status_text, "message": message}}


def make_backend(server: StubGemini, **overrides) -> GeminiBackend:
    settings = TranslationSettings(
        backend="gemini",
        text_model="gemini-3.8-flash",
        target_language="Russian",
        base_url=server.base_url,
        timeout_s=overrides.pop("timeout_s", 5.0),
        max_retries=overrides.pop("max_retries", 0),
        backoff_base_s=overrides.pop("backoff_base_s", 0.01),
        max_blocks_per_request=overrides.pop("max_blocks_per_request", 40),
        **overrides,
    )
    return GeminiBackend(settings, API_KEY)


def request_with(items, **kwargs) -> TranslateRequest:
    return TranslateRequest(
        items=[TranslateItem(id=i, text=t, direction="horizontal") for i, t in items],
        target_language="Russian",
        page_index=0,
        context_lines=["你好 -> Привет"],
        glossary={"林雪": "Линь Сюэ"},
        **kwargs,
    )


@pytest.fixture()
def server():
    stub = StubGemini()
    try:
        yield stub
    finally:
        stub.close()


def test_request_format_and_structured_output(server):
    server.script = [(200, _ok([{"id": 1, "translation": "Привет"}, {"id": 2, "translation": "Пока"}]), {})]
    backend = make_backend(server)
    result = backend.translate_page(request_with([(1, "你好"), (2, "再见")]))

    assert result.translations == {1: "Привет", 2: "Пока"}
    assert result.missing_ids == []
    assert result.model == "gemini-3.8-flash"

    path, body = server.requests[0]
    assert path == "/v1beta/models/gemini-3.8-flash:generateContent"
    assert "key" not in path.lower()

    # the API key travels in the header only
    assert server.headers[0]["x-goog-api-key"] == API_KEY
    assert API_KEY not in json.dumps(body)

    # structured output requested
    gen = body["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseSchema"] == RESPONSE_SCHEMA
    assert gen["responseSchema"]["properties"]["translations"]["items"]["required"] == [
        "id",
        "translation",
    ]
    assert 0.0 <= gen["temperature"] <= 1.0

    # system prompt + page context + glossary + payload all present
    system = body["systemInstruction"]["parts"][0]["text"]
    assert "manga" in system.lower() and "Russian" in system
    user = body["contents"][0]["parts"][0]["text"]
    assert "林雪" in user                      # glossary
    assert "Привет" in user                    # previous page context
    assert '"id":1' in user and '"text":"你好"' in user
    assert '"id":2' in user


def test_bare_array_and_fenced_json_are_tolerated(server):
    fenced = '```json\n[{"id": 1, "translation": "Привет"}]\n```'
    server.script = [(200, {"candidates": [{"content": {"parts": [{"text": fenced}]}}]}, {})]
    backend = make_backend(server)
    result = backend.translate_page(request_with([(1, "你好")]))
    assert result.translations == {1: "Привет"}


def test_id_keyed_object_is_tolerated(server):
    text = json.dumps({"1": "Привет", "2": "Пока"})
    server.script = [(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]}, {})]
    backend = make_backend(server)
    result = backend.translate_page(request_with([(1, "你好"), (2, "再见")]))
    assert result.translations == {1: "Привет", 2: "Пока"}


def test_retry_on_rate_limit_then_success(server):
    server.script = [
        (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}, {}),
        (200, _ok([{"id": 1, "translation": "Привет"}]), {}),
    ]
    backend = make_backend(server, max_retries=2)
    result = backend.translate_page(request_with([(1, "你好")]))
    assert result.translations == {1: "Привет"}
    assert len(server.requests) == 2


def test_quota_error_after_all_retries(server):
    server.script = [
        (429, {"error": {"message": "Quota exceeded"}}, {}),
        (429, {"error": {"message": "Quota exceeded"}}, {}),
    ]
    backend = make_backend(server, max_retries=1)
    with pytest.raises(PipelineError) as excinfo:
        backend.translate_page(request_with([(1, "你好")]))
    assert excinfo.value.kind == "quota"
    assert excinfo.value.retryable


def test_invalid_api_key():
    stub = StubGemini([(400, _error("INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."), {})])
    try:
        backend = make_backend(stub)
        with pytest.raises(PipelineError) as excinfo:
            backend.translate_page(request_with([(1, "你好")]))
        assert excinfo.value.kind == "auth"
        assert "GEMINI_API_KEY" in excinfo.value.message
    finally:
        stub.close()


def test_missing_api_key_is_reported_clearly():
    settings = TranslationSettings(base_url="http://127.0.0.1:1")
    backend = GeminiBackend(settings, "")
    ok, reason = backend.is_configured()
    assert not ok and "GEMINI_API_KEY" in reason
    with pytest.raises(PipelineError) as excinfo:
        backend.translate_page(request_with([(1, "你好")]))
    assert excinfo.value.kind == "auth"


def test_model_not_found():
    stub = StubGemini([(404, _error("NOT_FOUND", "models/gemini-3.8-flash is not found"), {})])
    try:
        backend = make_backend(stub)
        with pytest.raises(PipelineError) as excinfo:
            backend.translate_page(request_with([(1, "你好")]))
        assert excinfo.value.kind == "model"
        assert "gemini-3.8-flash" in excinfo.value.message
        assert "model name" in excinfo.value.message.lower()
    finally:
        stub.close()


def test_missing_ids_trigger_a_follow_up_request(server):
    server.script = [
        (200, _ok([{"id": 1, "translation": "Привет"}]), {}),
        (200, _ok([{"id": 2, "translation": "Пока"}]), {}),
    ]
    backend = make_backend(server)
    result = backend.translate_page(request_with([(1, "你好"), (2, "再见")]))
    assert result.translations == {1: "Привет", 2: "Пока"}
    assert len(server.requests) == 2
    second_prompt = server.requests[1][1]["contents"][0]["parts"][0]["text"]
    assert "再见" in second_prompt


def test_unparseable_answers_raise_malformed(server):
    server.script = [
        (200, {"candidates": [{"content": {"parts": [{"text": "I am sorry, I cannot."}]}}]}, {}),
        (200, {"candidates": [{"content": {"parts": [{"text": "still not json"}]}}]}, {}),
    ]
    backend = make_backend(server)
    with pytest.raises(PipelineError) as excinfo:
        backend.translate_page(request_with([(1, "你好")]))
    assert excinfo.value.kind == "malformed"
    assert len(server.requests) == 2  # one repair attempt


def test_safety_block_is_reported(server):
    server.script = [(200, {"promptFeedback": {"blockReason": "SAFETY"}}, {})]
    backend = make_backend(server)
    with pytest.raises(PipelineError) as excinfo:
        backend.translate_page(request_with([(1, "你好")]))
    assert excinfo.value.kind == "blocked"


def test_timeout_is_retryable_and_does_not_hang():
    stub = StubGemini()
    stub.delay_s = 1.5
    try:
        backend = make_backend(stub, timeout_s=0.25, max_retries=0)
        started = time.perf_counter()
        with pytest.raises(PipelineError) as excinfo:
            backend.translate_page(request_with([(1, "你好")]))
        assert time.perf_counter() - started < 1.4
        assert excinfo.value.kind in ("timeout", "network")
        assert excinfo.value.retryable
    finally:
        stub.close()


def test_cancellation_stops_before_sending():
    stub = StubGemini()
    try:
        backend = make_backend(stub)
        with pytest.raises(CancelledError):
            backend.translate_page(request_with([(1, "你好")]), lambda: True)
        assert not stub.requests
    finally:
        stub.close()


def test_large_pages_are_split_into_several_requests(server):
    server.script = [
        (200, _ok([{"id": 1, "translation": "a"}, {"id": 2, "translation": "b"}]), {}),
        (200, _ok([{"id": 3, "translation": "c"}]), {}),
    ]
    backend = make_backend(server, max_blocks_per_request=2)
    result = backend.translate_page(request_with([(1, "一"), (2, "二"), (3, "三")]))
    assert result.translations == {1: "a", 2: "b", 3: "c"}
    assert len(server.requests) == 2


def test_api_key_is_not_written_to_logs(server, caplog):
    import logging

    server.script = [(500, {"error": {"message": f"server exploded with {API_KEY}"}}, {})]
    backend = make_backend(server, max_retries=0)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PipelineError):
            backend.translate_page(request_with([(1, "你好")]))
    for record in caplog.records:
        assert API_KEY not in record.getMessage()
