"""User-editable glossary (§15): JSON file, prompt injection + deterministic
post-correction so names and terms stay consistent across pages."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Iterable, Optional

from ..core.logging_setup import get_logger
from ..ocr.filtering import cjk_count

log = get_logger("glossary")

SAMPLE_GLOSSARY: Dict[str, str] = {
    "林雪": "Линь Сюэ",
    "灵气": "духовная ци",
    "师兄": "старший брат по школе",
}


class Glossary:
    def __init__(self, path: Path, entries: Optional[Dict[str, str]] = None):
        self.path = Path(path)
        self._entries: Dict[str, str] = dict(entries or {})
        self._lock = threading.RLock()

    # -- io ---------------------------------------------------------------
    def load(self, create_sample: bool = False) -> "Glossary":
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._entries = {str(k): str(v) for k, v in data.items()}
                elif isinstance(data, list):
                    self._entries = {
                        str(item["source"]): str(item["target"])
                        for item in data
                        if isinstance(item, dict) and "source" in item and "target" in item
                    }
                log.info("glossary loaded: %d entries", len(self._entries))
            elif create_sample:
                self._entries = dict(SAMPLE_GLOSSARY)
                self.save()
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("glossary unreadable (%s); keeping %d entries", exc.__class__.__name__, len(self._entries))
        return self

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(self._entries, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                tmp.replace(self.path)
            except OSError as exc:
                log.warning("could not save glossary: %s", exc)

    # -- api --------------------------------------------------------------
    def as_dict(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, source: str, target: str) -> None:
        source, target = source.strip(), target.strip()
        if not source or not target:
            return
        with self._lock:
            self._entries[source] = target

    def remove(self, source: str) -> None:
        with self._lock:
            self._entries.pop(source, None)

    def replace_all(self, entries: Dict[str, str]) -> None:
        with self._lock:
            self._entries = {str(k).strip(): str(v).strip() for k, v in entries.items() if k and v}

    def relevant(self, texts: Iterable[str], limit: int = 60) -> Dict[str, str]:
        """Only the terms that actually appear on this page (keeps prompts small)."""
        haystack = "\n".join(texts)
        with self._lock:
            hits = {src: dst for src, dst in self._entries.items() if src and src in haystack}
        return dict(list(hits.items())[:limit])

    def enforce(self, source_text: str, translation: str) -> str:
        """Replace source terms the model left untranslated inside the output."""
        if not translation:
            return translation
        out = translation
        with self._lock:
            items = list(self._entries.items())
        for src, dst in items:
            if not src or not dst:
                continue
            if src in source_text and src in out:
                out = out.replace(src, dst)
        # Any leftover CJK run means the model echoed the source: flag it visibly
        # instead of silently showing Chinese to the user.
        if cjk_count(out) and cjk_count(source_text):
            remaining = "".join(ch for ch in out if cjk_count(ch))
            if remaining.strip():
                log.debug("translation still contains CJK: %r", remaining[:40])
        return out
