"""Shared HTTP transport for every translation backend.

Centralises the boring-but-critical parts so all providers behave the same:
timeouts, limited retries with exponential backoff + jitter, `Retry-After`
support, cancellation between attempts (a stale page must never keep retrying)
and consistent, human-readable error classification.

API keys are always sent in headers (or as a query parameter only where the
provider requires it), never logged.
"""
from __future__ import annotations

import random
import time
from typing import Optional

import httpx

from ..core.config import TranslationSettings
from ..core.logging_setup import get_logger
from ..core.models import PipelineError
from ..core.i18n import tr
from .base import CancelledError, CancelCheck

log = get_logger("transport")

MAX_BACKOFF_S = 8.0
MAX_RETRY_AFTER_S = 20.0

CancelCheckT = Optional[CancelCheck]


class Transport:
    """One httpx client + retry policy per backend instance (worker thread only)."""

    def __init__(
        self,
        settings: TranslationSettings,
        label: str = "api",
        headers: Optional[dict] = None,
        client: Optional[httpx.Client] = None,
        key_hint: str = "",
        model_hint: str = "",
    ):
        self.settings = settings
        self.label = label
        self.key_hint = key_hint
        self.model_hint = model_hint
        self._headers = dict(headers or {})
        self._client = client
        self._owns_client = client is None

    # -- client -----------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(
                    connect=6.0, read=float(self.settings.timeout_s), write=20.0, pool=6.0
                ),
                headers={
                    "User-Agent": "MangaTranslator/0.9 (+https://github.com/)",
                    **self._headers,
                },
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    # -- cancellation -----------------------------------------------------
    @staticmethod
    def raise_if_cancelled(is_cancelled: CancelCheckT) -> None:
        if is_cancelled and is_cancelled():
            raise CancelledError("page generation changed")

    def sleep(self, seconds: float, is_cancelled: CancelCheckT) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.raise_if_cancelled(is_cancelled)
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    # -- requests ---------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        is_cancelled: CancelCheckT = None,
        expect: tuple = (200, 201),
        retries: Optional[int] = None,
        **kwargs,
    ) -> httpx.Response:
        """Perform a request with retries; returns the response on success."""
        attempts = (max(0, int(self.settings.max_retries)) if retries is None else max(0, retries)) + 1
        last: Optional[PipelineError] = None
        for attempt in range(1, attempts + 1):
            self.raise_if_cancelled(is_cancelled)
            retry_after: Optional[float] = None
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                last = PipelineError(
                    tr("{label}: no answer in time (request timed out).").format(
                        label=self.label
                    ),
                    kind="timeout",
                    retryable=True,
                )
                log.warning("%s timeout (attempt %d/%d): %s", self.label, attempt, attempts,
                            exc.__class__.__name__)
            except httpx.HTTPError as exc:
                last = PipelineError(
                    tr("{label}: no connection. Check your internet connection.").format(
                        label=self.label
                    ),
                    kind="network",
                    retryable=True,
                )
                log.warning("%s transport error (attempt %d/%d): %s", self.label, attempt, attempts,
                            exc.__class__.__name__)
            else:
                if response.status_code in expect:
                    return response
                error = self.classify(response)
                if not error.retryable or attempt >= attempts:
                    raise error
                last = error
                retry_after = _retry_after_seconds(response)

            if attempt < attempts:
                delay = retry_after if retry_after is not None else min(
                    MAX_BACKOFF_S, float(self.settings.backoff_base_s) * (2 ** (attempt - 1))
                )
                delay = min(MAX_RETRY_AFTER_S, delay) * random.uniform(0.85, 1.15)
                log.info("%s retry in %.1fs (attempt %d/%d)", self.label, delay, attempt + 1, attempts)
                self.sleep(delay, is_cancelled)
        raise last or PipelineError(
            tr("{label}: request failed.").format(label=self.label), kind="unknown", retryable=True
        )

    def post_json(self, url: str, is_cancelled: CancelCheckT = None, **kwargs) -> dict:
        response = self.request("POST", url, is_cancelled=is_cancelled, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise PipelineError(
                tr("{label} returned a response that is not JSON.").format(label=self.label),
                kind="malformed",
            ) from exc

    def get_json(self, url: str, is_cancelled: CancelCheckT = None, **kwargs) -> dict:
        response = self.request("GET", url, is_cancelled=is_cancelled, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise PipelineError(
                f"{self.label} returned a response that is not JSON.", kind="malformed"
            ) from exc

    # -- errors -----------------------------------------------------------
    @property
    def _key_suffix(self) -> str:
        return tr(" (key: {names})").format(names=self.key_hint) if self.key_hint else ""

    def classify(self, response: httpx.Response) -> PipelineError:
        status = response.status_code
        detail = _error_detail(response)
        label = self.label
        if status in (401, 403):
            return PipelineError(
                tr("{label} rejected the API key (HTTP {status}){hint}. "
                   "Check Settings → Translation.").format(
                    label=label, status=status, hint=self._key_suffix
                ),
                kind="auth",
            )
        if status == 404:
            suffix = ""
            if self.model_hint:
                suffix = tr(
                    " Check the model name ('{model}') or the base URL in Settings → "
                    "Translation; use 'Load models' to list what the key can use."
                ).format(model=self.model_hint)
            return PipelineError(
                tr("{label}: model or endpoint not found (HTTP 404).{hint} {detail}").format(
                    label=label, hint=suffix, detail=detail
                ).strip(),
                kind="model",
            )
        if status == 400:
            lowered = detail.lower()
            if "api key" in lowered or "api_key" in lowered or "unauthorized" in lowered:
                return PipelineError(
                    tr("{label} rejected the API key{hint}: {detail}").format(
                        label=label, hint=self._key_suffix, detail=detail
                    ),
                    kind="auth",
                )
            return PipelineError(
                tr("{label} rejected the request: {detail}").format(label=label, detail=detail),
                kind="request",
            )
        if status == 413:
            return PipelineError(
                tr("{label}: the page had too much text for one request.").format(label=label),
                kind="toolong",
            )
        if status == 429:
            return PipelineError(
                tr("{label}: rate limit / quota reached. The next page will be retried "
                   "automatically.").format(label=label),
                kind="quota",
                retryable=True,
            )
        if status >= 500:
            return PipelineError(
                tr("{label} is temporarily unavailable (HTTP {status}). Retrying shortly.").format(
                    label=label, status=status
                ),
                kind="server",
                retryable=True,
            )
        return PipelineError(
            tr("{label} request failed (HTTP {status}): {detail}").format(
                label=label, status=status, detail=detail
            ),
            kind="http",
        )


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "")[:180]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("detail") or "")[:240]
        if isinstance(error, str):
            return error[:240]
        for key in ("message", "detail", "error_description", "Message"):
            if payload.get(key):
                return str(payload[key])[:240]
    return str(payload)[:180]


def _retry_after_seconds(response: httpx.Response) -> Optional[float]:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def html_unescape(value: str) -> str:
    """Google/LibreTranslate may return HTML entities instead of plain text."""
    import html

    return html.unescape(value or "")
