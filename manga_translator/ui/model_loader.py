"""Background workers for the settings dialog.

Querying a provider for its model list (or running a one-line test translation)
must never block the GUI thread, so both run in a QThread and report back with
signals.
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from ..core.config import Config
from ..core.logging_setup import get_logger
from ..translation.providers import list_models
from ..translation.base import TranslateItem, TranslateRequest

log = get_logger("ui.models")


class ModelListWorker(QThread):
    """`GET /models` (or `/languages`) for the selected provider + key."""

    loaded = Signal(list, str, str)      # items, unit ("model"/"language"), error

    def __init__(self, provider: str, base_url: str, api_key: str, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key

    def run(self) -> None:  # pragma: no cover - exercised via the live check/tests
        try:
            items, unit, error = list_models(self.provider, self.base_url, self.api_key)
        except Exception as exc:
            items, unit, error = [], "model", f"{exc.__class__.__name__}: {exc}"
        self.loaded.emit(list(items), unit, error)


class TestWorker(QThread):
    """One tiny translation to prove the key/model/endpoint actually work."""

    done = Signal(bool, str)

    def __init__(self, config: Config, provider: str, base_url: str, api_key: str,
                 model: str, target: str, parent=None):
        super().__init__(parent)
        self.config = config
        self.provider = provider
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.target = target

    def run(self) -> None:  # pragma: no cover - exercised live
        from ..translation import create_backend

        settings = self.config.translation
        previous = (
            settings.provider, settings.backend, settings.base_url,
            settings.text_model, settings.target_language,
        )
        settings.provider = self.provider
        settings.backend = self.provider
        settings.base_url = self.base_url or settings.base_url
        settings.text_model = self.model or settings.text_model
        settings.target_language = self.target or settings.target_language
        settings.max_retries = 0
        settings.timeout_s = min(settings.timeout_s, 20.0)
        try:
            backend, warning = create_backend(settings, self.api_key)
            if warning:
                self.done.emit(False, warning)
                return
            result = backend.translate_page(
                TranslateRequest(
                    items=[TranslateItem(id=1, text="你好，今天天气真好。")],
                    target_language=settings.target_language,
                )
            )
            text = (result.translations.get(1) or "").strip()
            if text:
                self.done.emit(True, f"OK: {text}")
            else:
                self.done.emit(False, "the provider returned no translation")
        except Exception as exc:
            self.done.emit(False, f"{exc.__class__.__name__}: {exc}")
        finally:
            (
                settings.provider, settings.backend, settings.base_url,
                settings.text_model, settings.target_language,
            ) = previous
            backend = None
