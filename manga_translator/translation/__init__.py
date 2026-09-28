"""Page-level translation backends and the provider factory."""
from __future__ import annotations

from typing import Optional, Tuple

from ..core.config import TranslationSettings
from ..core.logging_setup import get_logger
from .base import (
    CancelledError,
    TranslateItem,
    TranslateRequest,
    TranslateResult,
    TranslationBackend,
)
from .context import ContextLine, ContextStore
from .providers import PROVIDERS, get_spec, list_models, provider_choices, provider_ids

log = get_logger("translation")


def create_backend(
    settings: TranslationSettings, api_key: str = ""
) -> Tuple[TranslationBackend, Optional[str]]:
    """Build the backend for the configured provider.

    Returns ``(backend, warning)``; unknown providers fall back to Gemini with a
    warning instead of failing, and a missing key is reported as a warning so the
    app can still start and explain what to do.
    """
    provider = settings.active_provider()
    spec = get_spec(provider)
    warning: Optional[str] = None

    if spec is None:
        warning = f"Unknown provider '{provider}'; using Google Gemini."
        log.warning(warning)
        provider, spec = "gemini", PROVIDERS["gemini"]

    # providers listed in the registry but not installed/available
    if spec.kind == "fake":
        from .fake import FakeTranslator

        return FakeTranslator(), None

    if spec.kind == "llm-gemini":
        from .gemini import GeminiBackend

        backend = GeminiBackend(settings, api_key)
    elif spec.kind == "llm-openai":
        from .openai_compat import OpenAiCompatBackend

        backend = OpenAiCompatBackend(settings, spec, api_key)
    elif spec.kind == "llm-anthropic":
        from .anthropic import AnthropicBackend

        backend = AnthropicBackend(settings, spec, api_key)
    elif spec.kind == "mt":
        from .mt_apis import MtBatchBackend

        backend = MtBatchBackend(settings, spec, api_key)
    else:  # pragma: no cover - registry is exhaustive
        raise ValueError(f"unsupported provider kind '{spec.kind}'")

    ok, reason = backend.is_configured()
    if not ok and warning is None:
        warning = reason
    log.info("translation backend: %s (model=%s)", spec.label, getattr(backend, "model", "?"))
    return backend, warning


__all__ = [
    "TranslationBackend",
    "TranslateItem",
    "TranslateRequest",
    "TranslateResult",
    "ContextStore",
    "ContextLine",
    "CancelledError",
    "create_backend",
    "PROVIDERS",
    "get_spec",
    "provider_ids",
    "provider_choices",
    "list_models",
]
