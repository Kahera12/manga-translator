"""Classic machine-translation APIs (one request per page, batch of texts).

Google Cloud Translation v2, DeepL, Yandex Translate, Microsoft Translator,
LibreTranslate and MyMemory. These engines have no notion of page context or a
glossary prompt, but they are fast, cheap and need no model choice — the panel
shows their supported target languages instead of a model list.

The whole page is still sent as **one** request (a batch of ``q``/``text``
values) whenever the API supports batching, and the results are mapped back to
the block ids positionally.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

from ..core.config import TranslationSettings
from ..core.logging_setup import get_logger
from ..core.models import PipelineError
from .base import (
    CancelCheck,
    TranslateRequest,
    TranslateResult,
    TranslationBackend,
)
from ..core.i18n import tr
from .providers import ProviderSpec, resolve_base_url
from .transport import Transport, html_unescape

log = get_logger("mt_api")

LANG_NAMES = {
    "russian": "ru", "русский": "ru", "english": "en", "английский": "en",
    "chinese": "zh", "китайский": "zh", "simplified chinese": "zh", "traditional chinese": "zh-hant",
    "japanese": "ja", "korean": "ko", "german": "de", "french": "fr", "spanish": "es",
    "italian": "it", "portuguese": "pt", "polish": "pl", "ukrainian": "uk", "turkish": "tr",
    "arabic": "ar", "hindi": "hi", "thai": "th", "vietnamese": "vi", "indonesian": "id",
    "dutch": "nl", "swedish": "sv", "czech": "cs", "hebrew": "he",
}

SOURCE_LANGUAGE = "zh"          # manga is Chinese; kept configurable via settings if needed
MYMEMORY_LIMIT = 480            # bytes per query accepted by the free endpoint


def to_iso(language: str, upper: bool = False) -> str:
    """'Russian' -> 'ru' / 'RU'; already-ISO input passes through."""
    key = (language or "").strip().lower()
    code = LANG_NAMES.get(key) or key or "ru"
    if code in ("zh-hans", "zh-cn"):
        code = "zh"
    if code in ("zh-hant", "zh-tw"):
        code = "zh-tw" if upper else "zh-hant"
    return code.upper() if upper else code


def source_iso(upper: bool = False) -> str:
    return SOURCE_LANGUAGE.upper() if upper else SOURCE_LANGUAGE


class MtBatchBackend(TranslationBackend):
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
        self.service = spec.service
        self.model = (settings.text_model or spec.default_model).strip()
        self._api_key = (api_key or "").strip()
        self.transport = Transport(
            settings,
            label=spec.label,
            client=client,
            key_hint=" / ".join(spec.key_env),
        )
        self._target = "ru"

    # -- configuration ----------------------------------------------------
    def is_configured(self) -> Tuple[bool, str]:
        if self.spec.needs_key and not self._api_key:
            env = " or ".join(self.spec.key_env) or "the API key"
            return False, tr(
                "{label} needs a key: set {env} in .env / the environment "
                "or paste it in Settings → Translation."
            ).format(label=self.spec.label, env=env)
        return True, "ok"

    def close(self) -> None:
        self.transport.close()

    @property
    def base(self) -> str:
        return resolve_base_url(self.spec.id, self.settings.base_url)

    # -- api --------------------------------------------------------------
    def translate_page(
        self, request: TranslateRequest, is_cancelled: Optional[CancelCheck] = None
    ) -> TranslateResult:
        ok, reason = self.is_configured()
        if not ok:
            raise PipelineError(reason, kind="auth")
        items = list(request.items)
        started = time.perf_counter()
        if not items:
            return TranslateResult(model=self.model)
        self._target = to_iso(request.target_language)
        if self.service == "mymemory":
            table = self._mymemory(items, is_cancelled)
        else:
            table = self._batch(items, is_cancelled)
        if not table:
            raise PipelineError(
                tr("{label} returned no translation for this page.").format(label=self.spec.label),
                kind="empty",
                retryable=True,
            )
        wanted = [it.id for it in items]
        return TranslateResult(
            translations=table,
            missing_ids=[i for i in wanted if i not in table],
            model=self.model or self.service,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    # -- per-service adapters ---------------------------------------------
    def _batch(self, items: Sequence, is_cancelled: Optional[CancelCheck]) -> Dict[int, str]:
        texts = [it.text for it in items]
        if self.service == "google":
            payload = self.transport.post_json(
                f"{self.base}/language/translate/v2",
                is_cancelled=is_cancelled,
                params={"key": self._api_key},
                json={"q": texts, "source": source_iso(), "target": self._target, "format": "text"},
            )
            rows = (payload.get("data") or {}).get("translations") or []
            values = [html_unescape(str(r.get("translatedText", ""))) for r in rows]
        elif self.service == "deepl":
            payload = self.transport.post_json(
                f"{self.base}/v2/translate",
                is_cancelled=is_cancelled,
                headers={"Authorization": f"DeepL-Auth-Key {self._api_key}"},
                json={
                    "text": texts,
                    "source_lang": source_iso(upper=True),
                    "target_lang": to_iso(self._target, upper=True),
                },
            )
            values = [str(r.get("text", "")) for r in payload.get("translations") or []]
        elif self.service == "yandex":
            body: Dict[str, object] = {
                "texts": texts,
                "sourceLanguageCode": source_iso(),
                "targetLanguageCode": self._target,
            }
            folder = self.settings.extra.get("yandex_folder_id", "") if hasattr(self.settings, "extra") else ""
            if folder:
                body["folderId"] = folder
            payload = self.transport.post_json(
                f"{self.base}/translate/v2/translate",
                is_cancelled=is_cancelled,
                headers={"Authorization": f"Api-Key {self._api_key}"},
                json=body,
            )
            values = [str(r.get("text", "")) for r in payload.get("translations") or []]
        elif self.service == "microsoft":
            payload = self.transport.request(
                "POST",
                f"{self.base}/translate",
                is_cancelled=is_cancelled,
                params={"api-version": "3.0", "from": source_iso(), "to": self._target},
                headers={"Ocp-Apim-Subscription-Key": self._api_key,
                         "Content-Type": "application/json"},
                json=[{"Text": text} for text in texts],
            ).json()
            values = [
                str((item.get("translations") or [{}])[0].get("text", ""))
                for item in (payload if isinstance(payload, list) else [])
            ]
        elif self.service == "libretranslate":
            body = {"q": texts, "source": source_iso(), "target": self._target, "format": "text"}
            if self._api_key:
                body["api_key"] = self._api_key
            payload = self.transport.post_json(
                f"{self.base}/translate", is_cancelled=is_cancelled, json=body
            )
            translated = payload.get("translatedText")
            values = [str(v) for v in translated] if isinstance(translated, list) else [str(translated or "")]
        else:  # pragma: no cover - registry guards this
            raise PipelineError(f"unsupported translation service '{self.service}'", kind="request")

        if len(values) != len(items):
            log.warning("%s returned %d values for %d blocks", self.name, len(values), len(items))
        table: Dict[int, str] = {}
        for item, value in zip(items, values):
            value = (value or "").strip()
            if value:
                table[item.id] = value
        return table

    def _mymemory(self, items: Sequence, is_cancelled: Optional[CancelCheck]) -> Dict[int, str]:
        """MyMemory takes one query per request (free tier), so blocks go serially."""
        table: Dict[int, str] = {}
        pair = f"{source_iso()}|{self._target}"
        for item in items:
            self.transport.raise_if_cancelled(is_cancelled)
            for chunk_index, chunk in enumerate(_chunks(item.text, MYMEMORY_LIMIT)):
                params = {"q": chunk, "langpair": pair}
                if self._api_key:
                    params["key"] = self._api_key
                payload = self.transport.get_json(
                    f"{self.base}/get", is_cancelled=is_cancelled, params=params
                )
                text = str((payload.get("responseData") or {}).get("translatedText", "")).strip()
                if not text:
                    continue
                table[item.id] = (table.get(item.id, "") + " " + html_unescape(text)).strip() \
                    if chunk_index else html_unescape(text)
        return table


def _chunks(text: str, limit: int) -> List[str]:
    text = (text or "").strip()
    if len(text.encode("utf-8")) <= limit:
        return [text] if text else []
    out: List[str] = []
    current = ""
    for char in text:
        if len((current + char).encode("utf-8")) > limit:
            out.append(current)
            current = char
        else:
            current += char
    if current:
        out.append(current)
    return out
