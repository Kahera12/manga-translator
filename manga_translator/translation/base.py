"""Translation backend abstraction (§10): page-level, structured, cancellable."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence


@dataclass
class TranslateItem:
    id: int
    text: str
    direction: str = "horizontal"   # horizontal | vertical
    confidence: float = 1.0


@dataclass
class TranslateRequest:
    items: Sequence[TranslateItem]
    target_language: str = "Russian"
    page_index: int = 0
    context_lines: Sequence[str] = ()
    glossary: Dict[str, str] = field(default_factory=dict)


@dataclass
class TranslateResult:
    translations: Dict[int, str] = field(default_factory=dict)
    missing_ids: List[int] = field(default_factory=list)
    unexpected_ids: List[int] = field(default_factory=list)
    model: str = ""
    elapsed_ms: float = 0.0
    attempts: int = 0
    raw_preview: str = ""

    def merged(self, other: "TranslateResult") -> "TranslateResult":
        merged = TranslateResult(
            translations={**self.translations, **other.translations},
            model=other.model or self.model,
            elapsed_ms=self.elapsed_ms + other.elapsed_ms,
            attempts=self.attempts + other.attempts,
            raw_preview=other.raw_preview or self.raw_preview,
        )
        requested = set(self.translations) | set(other.missing_ids) | set(other.translations)
        merged.missing_ids = sorted(i for i in requested if i not in merged.translations)
        merged.unexpected_ids = sorted(set(self.unexpected_ids) | set(other.unexpected_ids))
        return merged


CancelCheck = Callable[[], bool]


class CancelledError(RuntimeError):
    """Raised when the caller's generation is stale and work must stop."""


class TranslationBackend:
    name = "base"
    model = ""

    def translate_page(
        self,
        request: TranslateRequest,
        is_cancelled: Optional[CancelCheck] = None,
    ) -> TranslateResult:  # pragma: no cover - interface
        raise NotImplementedError

    def is_configured(self) -> tuple:
        """(ok, human readable reason)"""
        return True, "ok"
