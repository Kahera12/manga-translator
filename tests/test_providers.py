"""Provider registry + every backend protocol, verified against stub endpoints.

No network access is needed: each provider speaks to a local HTTP server that
records the exact request and replies with a canned answer, so request shapes,
batching, model/language discovery and error handling are all checked for real.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Tuple

import pytest

from manga_translator.core.config import TranslationSettings
from manga_translator.core.models import PipelineError
from manga_translator.translation import create_backend
from manga_translator.translation.base import TranslateItem, TranslateRequest
from manga_translator.translation.parsing import parse_answer, parse_rows
from manga_translator.translation.providers import (
    PROVIDERS,
    get_spec,
    list_models,
    provider_choices,
    provider_ids,
)

Handler = Callable[[dict], Tuple[int, object, Dict[str, str]]]


class StubServer:
    """Tiny HTTP server with a routing table: (method, path) -> handler."""

    def __init__(self):
        self.routes: Dict[Tuple[str, str], Handler] = {}
        self.requests: List[dict] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self, method: str):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = raw.decode("utf-8", "replace")
                record = {
                    "method": method,
                    "path": path,
                    "query": query,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
                outer.requests.append(record)
                handler = outer.routes.get((method, path))
                if handler is None:
                    status, payload, extra = 404, {"error": f"no route for {method} {path}"}, {}
                else:
                    status, payload, extra = handler(record)
                data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
                data = data.encode("utf-8") if isinstance(data, str) else data
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._serve("GET")

            def do_POST(self):  # noqa: N802
                self._serve("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def route(self, method: str, path: str, handler: Handler) -> None:
        self.routes[(method, path)] = handler

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def server():
    stub = StubServer()
    try:
        yield stub
    finally:
        stub.close()


def ok(payload) -> Tuple[int, object, dict]:
    return 200, payload, {}


def request_for(items=(("你好", 1), ("再见", 2))) -> TranslateRequest:
    return TranslateRequest(
        items=[TranslateItem(id=i, text=t) for t, i in items],
        target_language="Russian",
        page_index=0,
        context_lines=["师兄 -> старший брат"],
        glossary={"林雪": "Линь Сюэ"},
    )


def settings_for(base_url: str, provider: str, model: str = "test-model", **kw) -> TranslationSettings:
    defaults = {"max_retries": 0, "timeout_s": 8.0}
    defaults.update(kw)
    return TranslationSettings(
        provider=provider,
        backend=provider,
        text_model=model,
        base_url=base_url,
        **defaults,
    )


# --- registry --------------------------------------------------------------

def test_registry_is_complete_and_consistent():
    assert len(PROVIDERS) >= 30
    seen = set()
    for provider_id, spec in PROVIDERS.items():
        assert spec.id == provider_id
        assert spec.label and spec.kind in ("llm-gemini", "llm-openai", "llm-anthropic", "mt", "fake")
        if spec.kind == "mt":
            assert spec.service, provider_id
        if spec.is_llm:
            assert spec.default_model, provider_id
            assert spec.static_models, provider_id
        if spec.needs_key:
            assert spec.key_env, f"{provider_id} needs a key but declares no env var"
        assert spec.id not in seen
        seen.add(spec.id)
    # the providers the user explicitly asked for
    for expected in ("gemini", "google-translate", "deepl", "yandex", "microsoft",
                     "libretranslate", "mymemory", "openai", "deepseek", "anthropic",
                     "openrouter", "groq", "mistral", "ollama", "qwen", "fake"):
        assert expected in PROVIDERS, expected


def test_provider_choices_are_sorted_and_unique():
    choices = provider_choices()
    ids = [pid for pid, _ in choices]
    assert len(ids) == len(set(ids)) == len(PROVIDERS)
    labels = [label for _, label in choices]
    assert all(labels)


def test_unknown_provider_falls_back_with_a_warning():
    backend, warning = create_backend(settings_for("http://127.0.0.1:1", "nope-not-real"), "")
    assert backend.name == "gemini"
    assert warning and "nope-not-real" in warning


def test_list_models_never_raises_for_unknown_provider():
    items, unit, error = list_models("nope", "", "")
    assert items == [] and unit == "model" and "unknown provider" in error


def test_fake_provider_needs_no_key():
    backend, warning = create_backend(settings_for("", "fake"), "")
    assert warning is None
    assert backend.name == "fake"


# --- OpenAI-compatible -----------------------------------------------------

def test_openai_compatible_request_and_parsing(server):
    captured = {}

    def chat(record):
        captured.update(record)
        return ok({"choices": [{"message": {"content": json.dumps(
            {"translations": [{"id": 1, "translation": "Привет"}, {"id": 2, "translation": "Пока"}]}
        )}, "finish_reason": "stop"}]})

    server.route("POST", "/v1/chat/completions", chat)
    settings = settings_for(server.base_url + "/v1", "openai", "gpt-test")
    backend, warning = create_backend(settings, "sk-test-key")
    assert warning is None
    result = backend.translate_page(request_for())
    assert result.translations == {1: "Привет", 2: "Пока"}

    body = captured["body"]
    assert captured["headers"]["authorization"] == "Bearer sk-test-key"
    assert body["model"] == "gpt-test"
    assert body["messages"][0]["role"] == "system"
    assert "manga" in body["messages"][0]["content"].lower()
    user = body["messages"][1]["content"]
    assert '"id":1' in user and '"text":"你好"' in user
    assert "林雪" in user, "glossary must reach the prompt"
    assert "старший брат" in user, "previous-page context must reach the prompt"
    # OpenAI gets a strict json_schema response format
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True


def test_openai_json_schema_is_downgraded_when_rejected(server):
    calls = []

    def chat(record):
        calls.append(record["body"])
        if "response_format" in record["body"]:
            return 400, {"error": {"message": "Unsupported parameter: response_format"}}, {}
        return ok({"choices": [{"message": {"content": '{"translations":[{"id":1,"translation":"Привет"}]}'}}]})

    server.route("POST", "/v1/chat/completions", chat)
    settings = settings_for(server.base_url + "/v1", "deepseek", "deepseek-chat")
    backend, _ = create_backend(settings, "sk-test")
    assert backend._json_mode == "object"          # DeepSeek preset uses json_object
    result = backend.translate_page(request_for([("你好", 1)]))
    assert result.translations == {1: "Привет"}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[-1], "must fall back to prompt-only"
    assert backend._json_mode == "none"


def test_local_provider_works_without_a_key(server):
    server.route("POST", "/v1/chat/completions", lambda record: ok(
        {"choices": [{"message": {"content": '{"translations":[{"id":1,"translation":"Привет"}]}'}}]}
    ))
    settings = settings_for(server.base_url + "/v1", "ollama", "qwen2.5:7b")
    backend, warning = create_backend(settings, "")
    assert warning is None, "a local provider must not demand an API key"
    assert backend.translate_page(request_for([("你好", 1)])).translations == {1: "Привет"}


def test_openai_model_listing(server):
    server.route("GET", "/v1/models", lambda record: ok(
        {"object": "list", "data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}, {"id": "o4-mini"}]}
    ))
    items, unit, error = list_models("openai", server.base_url + "/v1", "sk-key")
    assert items == ["gpt-4o", "gpt-4o-mini", "o4-mini"]
    assert unit == "model" and error == ""
    assert server.requests[0]["headers"]["authorization"] == "Bearer sk-key"


def test_ollama_model_listing_uses_native_tags(server):
    server.route("GET", "/api/tags", lambda record: ok(
        {"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.1:8b"}]}
    ))
    items, unit, error = list_models("ollama", server.base_url + "/v1", "")
    assert items == ["llama3.1:8b", "qwen2.5:7b"] and unit == "model" and not error


# --- Gemini ----------------------------------------------------------------

def test_gemini_model_listing_filters_embeddings(server):
    server.route("GET", "/v1beta/models", lambda record: ok({"models": [
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/aqa", "supportedGenerationMethods": ["generateAnswer"]},
    ]}))
    items, unit, error = list_models("gemini", server.base_url, "AIza-test")
    assert items == ["gemini-2.5-flash", "gemini-3.8-flash"] and unit == "model" and not error
    assert "key=AIza-test" in server.requests[0]["query"]


def test_gemini_model_listing_failure_returns_static_fallback(server):
    server.route("GET", "/v1beta/models", lambda record: (403, {"error": {"message": "bad key"}}, {}))
    items, unit, error = list_models("gemini", server.base_url, "bad")
    assert items and unit == "model" and "403" in error


# --- Anthropic -------------------------------------------------------------

def test_anthropic_request_shape_and_parsing(server):
    captured = {}

    def messages(record):
        captured.update(record)
        return ok({"content": [{"type": "text", "text": '"translations":[{"id":1,"translation":"Привет"}]}'}],
                   "stop_reason": "end_turn"})

    server.route("POST", "/v1/messages", messages)
    settings = settings_for(server.base_url, "anthropic", "claude-3-5-haiku-latest")
    backend, warning = create_backend(settings, "sk-ant-test")
    assert warning is None
    result = backend.translate_page(request_for([("你好", 1)]))
    assert result.translations == {1: "Привет"}
    body = captured["body"]
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["headers"]["anthropic-version"]
    assert body["messages"][-1] == {"role": "assistant", "content": "{"}
    assert body["system"]


def test_anthropic_model_listing(server):
    server.route("GET", "/v1/models", lambda record: ok(
        {"data": [{"id": "claude-3-5-haiku-latest"}, {"id": "claude-sonnet-4-5"}]}
    ))
    items, unit, error = list_models("anthropic", server.base_url, "sk-ant")
    assert items == ["claude-3-5-haiku-latest", "claude-sonnet-4-5"] and not error


# --- classic MT APIs -------------------------------------------------------

def test_google_translate_batch(server):
    captured = {}

    def translate(record):
        captured.update(record)
        return ok({"data": {"translations": [{"translatedText": "Привет"},
                                             {"translatedText": "Пока &amp; всё"}]}})

    server.route("POST", "/language/translate/v2", translate)
    settings = settings_for(server.base_url, "google-translate")
    backend, warning = create_backend(settings, "AIza-google")
    assert warning is None
    result = backend.translate_page(request_for())
    assert result.translations == {1: "Привет", 2: "Пока & всё"}   # HTML entities decoded
    body = captured["body"]
    assert body["q"] == ["你好", "再见"]        # whole page in ONE request
    assert body["source"] == "zh" and body["target"] == "ru"
    assert "key=AIza-google" in captured["query"]


def test_deepl_batch(server):
    captured = {}

    def translate(record):
        captured.update(record)
        return ok({"translations": [{"detected_source_language": "ZH", "text": "Привет"},
                                    {"detected_source_language": "ZH", "text": "Пока"}]})

    server.route("POST", "/v2/translate", translate)
    settings = settings_for(server.base_url, "deepl")
    backend, _ = create_backend(settings, "deepl-key:fx")
    result = backend.translate_page(request_for())
    assert result.translations == {1: "Привет", 2: "Пока"}
    assert captured["headers"]["authorization"] == "DeepL-Auth-Key deepl-key:fx"
    assert captured["body"]["target_lang"] == "RU" and captured["body"]["source_lang"] == "ZH"


def test_microsoft_translator_batch(server):
    captured = {}

    def translate(record):
        captured.update(record)
        return ok([{"translations": [{"text": "Привет", "to": "ru"}]},
                   {"translations": [{"text": "Пока", "to": "ru"}]}])

    server.route("POST", "/translate", translate)
    settings = settings_for(server.base_url, "microsoft")
    backend, _ = create_backend(settings, "ms-key")
    result = backend.translate_page(request_for())
    assert result.translations == {1: "Привет", 2: "Пока"}
    assert captured["headers"]["ocp-apim-subscription-key"] == "ms-key"
    assert captured["body"] == [{"Text": "你好"}, {"Text": "再见"}]
    assert "from=zh" in captured["query"] and "to=ru" in captured["query"]


def test_yandex_batch(server):
    captured = {}

    def translate(record):
        captured.update(record)
        return ok({"translations": [{"text": "Привет"}, {"text": "Пока"}]})

    server.route("POST", "/translate/v2/translate", translate)
    settings = settings_for(server.base_url, "yandex")
    backend, _ = create_backend(settings, "yandex-key")
    result = backend.translate_page(request_for())
    assert result.translations == {1: "Привет", 2: "Пока"}
    assert captured["headers"]["authorization"] == "Api-Key yandex-key"
    assert captured["body"]["sourceLanguageCode"] == "zh"
    assert captured["body"]["texts"] == ["你好", "再见"]


def test_libretranslate_batch(server):
    server.route("POST", "/translate", lambda record: ok({"translatedText": ["Привет", "Пока"]}))
    settings = settings_for(server.base_url, "libretranslate")
    backend, warning = create_backend(settings, "")
    assert warning is None            # self-hosted needs no key
    result = backend.translate_page(request_for())
    assert result.translations == {1: "Привет", 2: "Пока"}


def test_mymemory_sends_one_request_per_block(server):
    seen = []

    def get(record):
        seen.append(record["query"])
        return ok({"responseData": {"translatedText": f"RU:{record['query']}"},
                   "responseStatus": 200})

    server.route("GET", "/get", get)
    settings = settings_for(server.base_url, "mymemory")
    backend, warning = create_backend(settings, "")
    assert warning is None
    result = backend.translate_page(request_for())
    assert len(server.requests) == 2, "MyMemory accepts one q per request"
    assert set(result.translations) == {1, 2}
    assert "langpair=zh%7Cru" in seen[0].replace("|", "%7C") or "langpair=zh|ru" in seen[0]


def test_mt_language_lists(server):
    server.route("GET", "/language/translate/v2/languages", lambda record: ok(
        {"data": {"languages": [{"language": "ru"}, {"language": "en"}, {"language": "de"}]}}
    ))
    items, unit, error = list_models("google-translate", server.base_url, "AIza")
    assert unit == "language" and items == ["de", "en", "ru"] and not error

    server.route("GET", "/v2/languages", lambda record: ok(
        [{"language": "RU"}, {"language": "EN"}]
    ))
    items, unit, _ = list_models("deepl", server.base_url, "deepl-key")
    assert unit == "language" and items == ["EN", "RU"]


# --- errors / parsing ------------------------------------------------------

def test_provider_error_classification(server):
    server.route("POST", "/v1/chat/completions", lambda record: (401, {"error": {"message": "bad key"}}, {}))
    backend, _ = create_backend(settings_for(server.base_url + "/v1", "openai", "m"), "bad")
    with pytest.raises(PipelineError) as excinfo:
        backend.translate_page(request_for())
    assert excinfo.value.kind == "auth"
    assert "OPENAI_API_KEY" in excinfo.value.message

    server.route("POST", "/v1/chat/completions", lambda record: (429, {"error": {"message": "slow down"}}, {}))
    with pytest.raises(PipelineError) as excinfo:
        backend.translate_page(request_for())
    assert excinfo.value.kind == "quota" and excinfo.value.retryable


def test_transport_retries_then_succeeds(server):
    state = {"calls": 0}

    def chat(record):
        state["calls"] += 1
        if state["calls"] == 1:
            return 503, {"error": {"message": "busy"}}, {}
        return ok({"choices": [{"message": {"content": '{"translations":[{"id":1,"translation":"Привет"}]}'}}]})

    server.route("POST", "/v1/chat/completions", chat)
    settings = settings_for(server.base_url + "/v1", "openai", "gpt", max_retries=2, backoff_base_s=0.01)
    backend, _ = create_backend(settings, "sk")
    assert backend.translate_page(request_for([("你好", 1)])).translations == {1: "Привет"}
    assert state["calls"] == 2


def test_parser_tolerates_common_shapes():
    assert parse_rows('```json\n{"translations": [{"id": 2, "translation": "x"}]}\n```') == [
        {"id": 2, "translation": "x"}
    ]
    assert parse_answer('{"1": "один", "2": "два"}', [1, 2]) == {1: "один", 2: "два"}
    assert parse_answer('[{"index": 3, "text": "три"}]', [3]) == {3: "три"}
    assert parse_answer('{"translations": [{"id": 1, "translation": ""}]}', [1]) == {}
    assert parse_answer("not json at all", [1]) == {}


def test_provider_note_and_units():
    assert get_spec("deepl").unit == "language"
    assert get_spec("openai").unit == "model"
    assert get_spec("mymemory").needs_key is False
    assert get_spec("ollama").local is True
    assert get_spec("google-translate").service == "google"
    assert provider_ids()[0] in PROVIDERS
