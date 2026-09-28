"""OpenAI-compatible chat-completions backend.

Covers OpenAI, DeepSeek, OpenRouter, Groq, Mistral, Together, Fireworks,
DeepInfra, Cerebras, xAI, Perplexity, NVIDIA NIM, SambaNova, GitHub Models,
Moonshot, Zhipu, Qwen/DashScope, SiliconFlow, Azure OpenAI and the local servers
(Ollama, LM Studio, llama.cpp, vLLM, LocalAI) — they all speak
``POST {base}/chat/completions``.

Structured output is requested with ``response_format`` when the provider
supports it (``json_schema`` → strict, else ``json_object``) and automatically
downgraded if the provider rejects it, so a plain prompt-only mode always works.
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

import httpx

from ..core.config import TranslationSettings
from ..core.logging_setup import get_logger
from ..core.models import PipelineError
from .base import (
    CancelCheck,
    CancelledError,
    TranslateRequest,
    TranslateResult,
    TranslationBackend,
)
from ..core.i18n import tr
from .parsing import json_only_note, parse_answer, strip_fences
from .prompts import build_system_prompt, build_user_prompt, repair_prompt
from .providers import ProviderSpec, resolve_base_url
from .transport import Transport

log = get_logger("openai_compat")

OPENAI_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "manga_page_translation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "translations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "translation": {"type": "string"},
                        },
                        "required": ["id", "translation"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["translations"],
            "additionalProperties": False,
        },
    },
}

JSON_OBJECT_FORMAT = {"type": "json_object"}


class OpenAiCompatBackend(TranslationBackend):
    def __init__(
        self,
        settings: TranslationSettings,
        spec: ProviderSpec,
        api_key: str,
        client: Optional[httpx.Client] = None,
    ):
        self.settings = settings
        self.spec = spec
        self.name = spec.id
        self.label = spec.label      # used in error messages built in this module
        self.model = (settings.text_model or spec.default_model).strip()
        self._api_key = (api_key or "").strip()
        headers = dict(spec.extra_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self.transport = Transport(
            settings,
            label=spec.label,
            headers=headers,
            client=client,
            key_hint=" / ".join(spec.key_env),
            model_hint=self.model,
        )
        self._json_mode = self._initial_json_mode(settings, spec)
        self._response_mode = (getattr(settings, "response_mode", "auto") or "auto").strip().lower()
        if self._response_mode not in ("auto", "schema", "object", "off"):
            self._response_mode = "auto"
        self._slow_responses = 0
        self._stream_supported = True
        self._effort_supported = True   # dropped automatically if the API rejects it

    @staticmethod
    def _initial_json_mode(settings, spec) -> str:
        """Structured-output mode to start with.

        ``auto`` (default) starts from the provider preset, but a gateway or a
        reasoning model can be several times slower with ``response_format``, so
        the first slow answer downgrades it (see ``_complete``).
        """
        requested = (getattr(settings, "response_mode", "auto") or "auto").strip().lower()
        if requested in ("schema", "object", "off"):
            return {"schema": "schema", "object": "object", "off": "none"}[requested]
        return spec.json_mode if spec.json_mode in ("schema", "object", "none") else "object"

    # -- configuration ----------------------------------------------------
    def is_configured(self) -> Tuple[bool, str]:
        if self.spec.needs_key and not self._api_key:
            env = " or ".join(self.spec.key_env) or "the API key"
            return False, tr(
                "{label} needs a key: set {env} in .env / the environment "
                "or paste it in Settings → Translation."
            ).format(label=self.spec.label, env=env)
        if not self.model:
            return False, tr("{label}: no model selected.").format(label=self.spec.label)
        return True, "ok"

    def close(self) -> None:
        self.transport.close()

    @property
    def endpoint(self) -> str:
        base = resolve_base_url(self.spec.id, self.settings.base_url)
        return f"{base}/chat/completions"

    # -- api --------------------------------------------------------------
    def translate_page(
        self, request: TranslateRequest, is_cancelled: Optional[CancelCheck] = None
    ) -> TranslateResult:
        ok, reason = self.is_configured()
        if not ok:
            raise PipelineError(reason, kind="auth")
        limit = max(1, int(self.settings.max_blocks_per_request))
        items = list(request.items)
        if not items:
            return TranslateResult(model=self.model)
        result = TranslateResult(model=self.model)
        for start in range(0, len(items), limit):
            chunk = TranslateRequest(
                items=items[start : start + limit],
                target_language=request.target_language,
                page_index=request.page_index,
                context_lines=request.context_lines,
                glossary=request.glossary,
            )
            part = self._translate_chunk(chunk, is_cancelled)
            result = part if not result.translations else result.merged(part)
        return result

    def _translate_chunk(
        self, request: TranslateRequest, is_cancelled: Optional[CancelCheck]
    ) -> TranslateResult:
        started = time.perf_counter()
        blocks = [{"id": it.id, "text": it.text, "direction": it.direction} for it in request.items]
        wanted = [b["id"] for b in blocks]
        system = build_system_prompt(request.target_language)

        def ask(prompt: str) -> Dict[int, str]:
            text = self._complete(system, prompt, is_cancelled)
            return parse_answer(text, wanted)

        prompt = build_user_prompt(
            blocks,
            target_language=request.target_language,
            page_index=request.page_index,
            context_lines=request.context_lines,
            glossary=request.glossary,
        )
        table = ask(prompt)
        if not table:
            table = ask(prompt + "\n\n" + json_only_note())

        missing = [i for i in wanted if i not in table]
        if missing:
            log.warning("%s omitted %d/%d ids; asking again", self.name, len(missing), len(wanted))
            table.update(ask(repair_prompt(blocks, request.target_language, missing)))

        if not table:
            raise PipelineError(
                tr("{label} returned no usable translation for this page. "
                   "Try another model or check the log.").format(label=self.spec.label),
                kind="malformed",
                retryable=True,
            )
        return TranslateResult(
            translations=table,
            missing_ids=[i for i in wanted if i not in table],
            unexpected_ids=[i for i in table if i not in wanted],
            model=self.model,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    def _complete(self, system: str, prompt: str, is_cancelled: Optional[CancelCheck],
                  images: Optional[List[str]] = None,
                  timeout_s: float = 0.0,
                  extra_body: Optional[Dict[str, object]] = None) -> str:
        """One answer, with retries and two automatic downgrades.

        Attempt order (each step is only taken when the previous one failed for
        the matching reason):

        1. `stream` + the provider's structured-output mode  ← fastest in practice
        2. drop `response_format` (gateway/reasoning models that are slow with it,
           or that reject it outright)
        3. drop `stream` (endpoints without SSE support, or plain JSON proxies)

        Retryable transport errors (429/5xx/timeout/network) are retried here so
        the streaming path gets the same backoff policy as the plain one.

        ``images`` (data URLs) turn the request into a multimodal one; those are
        several times slower, so ``timeout_s`` can widen the budget per attempt.
        ``extra_body`` carries provider-specific knobs (e.g. ``reasoning_effort``).
        """
        budget = float(timeout_s or self.settings.timeout_s)
        user_content: object = prompt
        if images:
            user_content = [{"type": "text", "text": prompt}]
            user_content.extend(  # type: ignore[union-attr]
                {"type": "image_url", "image_url": {"url": url}} for url in images
            )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        retries = max(0, int(self.settings.max_retries))
        attempt = 0
        while True:
            attempt += 1
            body: Dict[str, object] = {
                "model": self.model,
                "temperature": float(self.settings.temperature),
                "messages": messages,
            }
            # A multimodal answer has its own shape (per-page blocks with boxes),
            # so the text schema must never be attached: forcing it made a real
            # gateway answer in the old {"translations":[...]} form and every page
            # came back empty.
            if not images:
                if self._json_mode == "schema":
                    body["response_format"] = OPENAI_RESPONSE_FORMAT
                elif self._json_mode == "object":
                    body["response_format"] = JSON_OBJECT_FORMAT
                effort = str(getattr(self.settings, "reasoning_effort", "") or "").strip()
                if effort and self._effort_supported:
                    # A reasoning model can spend 10-100 s thinking before writing a
                    # two-line dialogue translation; "none" removes that wait.
                    body["reasoning_effort"] = effort
            if extra_body:
                body.update(extra_body)
            streamed = self._stream_enabled()
            if streamed:
                body["stream"] = True

            started = time.perf_counter()
            try:
                text = (
                    self._stream_completion(body, is_cancelled, timeout_s=budget)
                    if streamed
                    else _extract_content(
                        self.transport.post_json(
                            self.endpoint, is_cancelled=is_cancelled, json=body, retries=0,
                            timeout=_request_timeout(budget),
                        )
                    )
                )
            except CancelledError:
                raise
            except PipelineError as exc:
                detail = exc.message.lower()
                if exc.retryable and attempt <= retries:
                    delay = min(6.0, float(self.settings.backoff_base_s) * (2 ** (attempt - 1)))
                    log.info("%s retry in %.1fs after %s (%s)", self.name, delay, exc.kind, exc.message[:80])
                    self.transport.sleep(delay, is_cancelled)
                    continue
                structured = ("response_format" in detail or "json_schema" in detail
                              or "json_object" in detail)
                if self._effort_supported and not images and "reasoning_effort" in detail:
                    # Older providers reject the field with HTTP 400: drop it once.
                    log.info("%s rejects reasoning_effort; dropping it", self.name)
                    self._effort_supported = False
                    continue
                if self._json_mode != "none" and structured and exc.kind in ("request", "http"):
                    log.info("%s rejects %s structured output; falling back to prompt-only JSON",
                             self.name, self._json_mode)
                    self._json_mode = "none"
                    continue
                if streamed and exc.kind in ("request", "http", "malformed", "empty",
                                             "timeout", "network"):
                    log.info("%s cannot stream (%s); falling back to a normal request",
                             self.name, exc.kind)
                    self._stream_supported = False
                    continue
                raise
            self._note_latency(time.perf_counter() - started)
            return text

    def complete_vision(self, system: str, prompt: str, images: List[str],
                        is_cancelled: Optional[CancelCheck] = None,
                        timeout_s: float = 0.0,
                        reasoning_effort: str = "") -> str:
        """One multimodal answer: ``images`` are data URLs (``data:image/jpeg;base64,…``).

        Used by the vision reader, which sends several page images at once; a
        slow vision answer is normal, hence the explicit ``timeout_s``.

        ``reasoning_effort`` is passed straight through when set. Measured on a
        real gateway with a reasoning model, ``"none"`` cut a 5-page read from
        128 s to 38 s with the same number of blocks found - the model was
        spending a minute "thinking" before writing any JSON.
        """
        ok, reason = self.is_configured()
        if not ok:
            raise PipelineError(reason, kind="auth")
        if not images:
            raise PipelineError(tr("No image to read."), kind="request")
        extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else None
        return self._complete(system, prompt, is_cancelled, images=images,
                              timeout_s=timeout_s, extra_body=extra)

    # -- streaming --------------------------------------------------------
    def _stream_enabled(self) -> bool:
        return bool(getattr(self, "_stream_supported", True))

    def _stream_completion(self, body: dict, is_cancelled: Optional[CancelCheck],
                           timeout_s: float = 0.0) -> str:
        """Read the answer as a server-sent-event stream.

        Many gateways buffer a whole non-streaming completion (measured on a real
        one: 15-17 s non-streaming vs 5 s streaming for the identical request), and
        streaming also lets a stale page abort mid-answer instead of burning a
        full request.
        """
        chunks: List[str] = []
        reasoning_chars = 0
        budget = float(timeout_s or self.settings.timeout_s)
        deadline = time.monotonic() + budget
        try:
            with self.transport.client.stream(
                "POST", self.endpoint, json=body, timeout=_request_timeout(budget)
            ) as response:
                if response.status_code != 200:
                    response.read()
                    raise self.transport.classify(response)
                content_type = (response.headers.get("content-type") or "").lower()
                if "event-stream" not in content_type:
                    # The endpoint ignored `stream` and answered with plain JSON.
                    response.read()
                    try:
                        return _extract_content(response.json())
                    except ValueError as exc:
                        raise PipelineError(
                            tr("{label} returned a response that is not JSON.").format(
                                label=self.label
                            ),
                            kind="malformed",
                        ) from exc
                for line in response.iter_lines():
                    if is_cancelled and is_cancelled():
                        raise CancelledError("page generation changed")
                    if time.monotonic() > deadline:
                        raise PipelineError(
                            tr("{label}: no answer in time (request timed out).").format(
                                label=self.label
                            ),
                            kind="timeout",
                            retryable=True,
                        )
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        continue
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if isinstance(piece, str):
                        chunks.append(piece)
                    reasoning = delta.get("reasoning_content")
                    if isinstance(reasoning, str):
                        reasoning_chars += len(reasoning)
                    finish = choices[0].get("finish_reason")
                    if finish and finish not in ("stop", "null"):
                        log.debug("%s stream finished with %s", self.name, finish)
        except PipelineError:
            raise
        except CancelledError:
            raise
        except httpx.TimeoutException as exc:
            # A stalled stream must reach `_complete` as a *classified* error,
            # otherwise it escapes the retry/fallback ladder and the page is lost.
            log.warning("%s stream timed out after %.0fs: %s", self.name,
                        float(self.settings.timeout_s), exc.__class__.__name__)
            raise PipelineError(
                tr("{label}: no answer in time (request timed out).").format(label=self.label),
                kind="timeout",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("%s stream transport error: %s", self.name, exc.__class__.__name__)
            raise PipelineError(
                tr("{label}: no connection. Check your internet connection.").format(
                    label=self.label
                ),
                kind="network",
                retryable=True,
            ) from exc
        text = "".join(chunks)
        if reasoning_chars:
            log.debug("%s produced %d characters of reasoning", self.name, reasoning_chars)
        if not text.strip():
            raise PipelineError(tr("The provider returned an empty answer."), kind="empty",
                                retryable=True)
        return text

    def _note_latency(self, elapsed_s: float) -> None:
        """Downgrade structured output when the endpoint is slow with it.

        Slow gateways/reasoning models can take 3-4x longer when a
        ``response_format`` is attached; the prompt already asks for JSON and the
        parser tolerates prose, so dropping it is a safe, large speed-up.
        """
        if self._response_mode != "auto" or self._json_mode == "none":
            return
        threshold = max(8.0, float(self.settings.timeout_s) * 0.35)
        if elapsed_s >= threshold:
            self._slow_responses += 1
            if self._slow_responses >= 1:
                log.info(
                    "%s answered in %.1fs with response_format=%s; switching to prompt-only JSON",
                    self.name, elapsed_s, self._json_mode,
                )
                self._json_mode = "none"
        else:
            self._slow_responses = 0


def _request_timeout(read_s: float) -> httpx.Timeout:
    """Per-request timeout: image requests need a much longer read window."""
    return httpx.Timeout(connect=8.0, read=max(10.0, float(read_s)), write=60.0, pool=8.0)


def _extract_content(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        error = payload.get("error")
        if isinstance(error, dict):
            raise PipelineError(str(error.get("message", "provider error")), kind="request")
        raise PipelineError(tr("The provider returned no answer."), kind="empty", retryable=True)
    choice = choices[0] or {}
    finish = str(choice.get("finish_reason") or "").lower()
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some gateways return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = strip_fences(content or "")
    if not text:
        if finish in ("content_filter", "safety"):
            raise PipelineError(tr("The provider blocked this page (content filter)."), kind="blocked")
        if finish == "length":
            raise PipelineError(
                tr("The page had too much text for one answer; it will be split."),
                kind="toolong",
                retryable=True,
            )
        raise PipelineError(tr("The provider returned an empty answer."), kind="empty", retryable=True)
    return text
