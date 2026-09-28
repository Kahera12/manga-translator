"""Read page images with a multimodal model instead of a local OCR engine.

Why this exists: a vision model answers the three questions at once - *what is
written*, *what does it mean in Russian*, *where exactly is it on the page* - in a
single request that can carry several pages. That removes the OCR pass, the span
grouping, the "is this dialogue or a sound effect" classifier and the geometric
UI-chrome filter from the critical path, all of which were guessing.

Measured on a real gateway (4 page images in one request): 20.5 s, 2093 prompt
tokens, 4/4 pages read, boxes within 0-1 px on x and 12-15 px on y - the latter
being corrected locally by :mod:`manga_translator.vision.snap`.

The local OCR path stays as a fallback: providers without image support, offline
mode, or a model that answers worse than OCR.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from ..core.config import TranslationSettings
from ..core.logging_setup import get_logger
from ..core.models import Block, PipelineError, Rect
from ..translation.base import CancelCheck, CancelledError
from ..translation.providers import ProviderSpec, resolve_base_url
from .snap import snap_box

log = get_logger("vision.reader")

# A refused image request is remembered per provider+model so the app does not
# pay for the same failure on every page of a chapter.
_BROKEN: Dict[str, str] = {}

SYSTEM_PROMPT = """You are a professional manga translator working from Chinese \
into {target_language}. You are shown page images of one comic chapter, in order.

For every page, read **every** piece of text a reader is meant to understand and \
translate it into {target_language}.

Rules:
- Include dialogue in speech bubbles, narration boxes, inner monologue, handwritten \
notes and stylised/sound-effect lettering.
- Ignore watermarks, site logos, page numbers, chapter numbers, scanlator credits \
and decorative typography that is part of the artwork itself.
- Read the Chinese exactly as printed, including punctuation, then translate \
naturally: Russian must sound like a person speaking, not a literal gloss.
- Keep the translation about as long as the original - it has to fit the bubble.
- Coordinates: `box` is the area of the *printed text* (not the bubble outline) in \
the page's own pixel grid, given as [x, y, w, h].
- Answer with JSON only, no prose, no markdown fences."""

USER_PROMPT = """Pages: {count}. They are panels {first}-{last} of one chapter, in reading order.
If a page has no text, return it with an empty "blocks" list.
"""

ANSWER_SHAPE = """{
  "pages": [
    {"page": <page index as given>,
     "blocks": [{"text": "<original Chinese>", "translation": "<__LANG__>",
                 "box": [x, y, w, h]}]}
  ]
}"""


class VisionUnavailable(RuntimeError):
    """The endpoint cannot read images (or failed hard) - use the OCR path."""


@dataclass
class VisionPage:
    """Result for one page image."""

    key: str
    blocks: List[Block] = field(default_factory=list)
    width: int = 0
    height: int = 0
    elapsed_ms: float = 0.0
    error: str = ""


def encode_panel(image_bgr: np.ndarray, max_side: int = 1280, quality: int = 85) -> str:
    """BGR image -> ``data:image/jpeg;base64,...`` (downscaled to ``max_side``)."""
    img = image_bgr
    height, width = img.shape[:2]
    longest = max(height, width)
    if max_side and longest > max_side:
        ratio = float(max_side) / float(longest)
        img = cv2.resize(img, (max(1, int(width * ratio)), max(1, int(height * ratio))),
                         interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:  # pragma: no cover - cv2 only fails on an empty array
        raise VisionUnavailable("could not encode the page image")
    return "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode("ascii")


def _dump_answer(answer: str) -> None:
    """With ``MT_VISION_DUMP=<file>`` the raw answer is kept for debugging.

    Parsing a model's JSON is the part that breaks in practice, and without the
    raw text a "0 blocks" result is indistinguishable from "empty page".
    """
    import os

    path = os.environ.get("MT_VISION_DUMP", "").strip()
    if not path:
        return
    try:
        from pathlib import Path

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(answer, encoding="utf-8")
        log.info("vision answer dumped to %s (%d chars)", target, len(answer))
    except OSError as exc:  # pragma: no cover - debugging aid
        log.warning("could not dump the vision answer: %s", exc)


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _first_json(text: str):
    """Parse the first JSON object/array in an answer that may carry prose."""
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    start = min((i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no JSON in the answer")
    depth = 0
    opener = cleaned[start]
    closer = "}" if opener == "{" else "]"
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return json.loads(cleaned[start:index + 1])
    raise ValueError("unbalanced JSON in the answer")


def _as_float(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _normalise_box(raw, width: int, height: int) -> Optional[Tuple[float, float, float, float]]:
    """Accept ``[x,y,w,h]`` or ``[x1,y1,x2,y2]`` in pixels, 0-1 or 0-1000 units."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    values = [_as_float(v) for v in raw]
    if any(v is None for v in values):
        return None
    x, y, a, b = (float(v) for v in values)  # type: ignore[arg-type]
    biggest = max(abs(x), abs(y), abs(a), abs(b))
    if biggest <= 1.5:                       # 0..1 fractions
        x, y, a, b = x * width, y * height, a * width, b * height
    elif biggest <= 1000.0:                  # 0..1000 (the documented form)
        x, y, a, b = x / 1000.0 * width, y / 1000.0 * height, a / 1000.0 * width, b / 1000.0 * height
    # [x, y, w, h] unless the last two are clearly an opposite corner
    if a > x and b > y and (a - x) <= width and (b - y) <= height:
        if (a - x) * (b - y) > 0.55 * width * height:
            a, b = a - x, b - y              # looked like a corner, not a size
    if a <= 0 or b <= 0:
        return None
    return max(0.0, x), max(0.0, y), min(float(a), float(width)), min(float(b), float(height))


def parse_pages(answer: str, count: int) -> Dict[int, List[dict]]:
    """Answer text -> ``{page index: [block dicts]}`` (page indices as returned)."""
    payload = _first_json(answer)
    if isinstance(payload, dict):
        pages = payload.get("pages")
        if pages is None and isinstance(payload.get("blocks"), list):
            pages = [{"page": 0, "blocks": payload["blocks"]}]
        if pages is None:
            # A dict keyed by page index: {"0": {...}, "1": {...}}
            if all(str(k).lstrip("-").isdigit() for k in payload):
                pages = [{"page": int(k), "blocks": v.get("blocks", []) if isinstance(v, dict) else v}
                         for k, v in payload.items()]
            else:
                pages = []
    elif isinstance(payload, list):
        pages = payload
    else:
        pages = []
    out: Dict[int, List[dict]] = {}
    for position, entry in enumerate(pages if isinstance(pages, list) else []):
        if isinstance(entry, dict):
            index = entry.get("page", entry.get("index", entry.get("page_index", position)))
            blocks = entry.get("blocks") or entry.get("lines") or entry.get("items") or []
        elif isinstance(entry, list):
            index, blocks = position, entry
        else:
            continue
        try:
            number = int(index)
        except (TypeError, ValueError):
            number = position
        if number < 0 or number >= count:
            log.debug("vision answer mentioned page %s (only %d were sent)", number, count)
            continue
        out[number] = [b for b in blocks if isinstance(b, dict)]
    return out


class VisionReader:
    """Batch reader: several page images -> translated, positioned blocks."""

    def __init__(self, settings: TranslationSettings, spec: ProviderSpec, api_key: str,
                 backend=None):
        self.settings = settings
        self.spec = spec
        self.model = (settings.text_model or spec.default_model).strip()
        self._api_key = (api_key or "").strip()
        self._backend = backend          # an OpenAiCompatBackend, when available
        self._client = None
        self.fingerprint = f"{spec.id}:{self.model}"
        self._unavailable = _BROKEN.get(self.fingerprint, "")

    # -- availability -----------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.settings.vision_enabled() and self.spec.kind == "llm-openai"

    @property
    def available(self) -> bool:
        return self.enabled and not self._unavailable and self._model()

    def mark_unavailable(self, reason: str) -> None:
        self._unavailable = reason
        _BROKEN[self.fingerprint] = reason
        log.warning("vision reader disabled for %s: %s", self.fingerprint, reason)

    def _model(self) -> bool:
        if not self.model:
            self.mark_unavailable("no model selected")
            return False
        if self.spec.needs_key and not self._api_key and self._backend is None:
            self.mark_unavailable("no API key")
            return False
        return True

    def _endpoint(self) -> str:
        base = resolve_base_url(self.spec.id, self.settings.base_url)
        return f"{base}/chat/completions"

    # -- reading ----------------------------------------------------------
    def read_pages(
        self,
        pages: Sequence[Tuple[str, np.ndarray]],
        target_language: str = "Russian",
        glossary: Sequence[str] = (),
        chapter_note: str = "",
        is_cancelled: Optional[CancelCheck] = None,
    ) -> Dict[str, VisionPage]:
        """Read a batch of panels in ONE request. Returns ``{panel key: VisionPage}``."""
        if not pages:
            return {}
        if not self.available:
            raise VisionUnavailable(self._unavailable or "vision is off")

        started = time.perf_counter()
        system = SYSTEM_PROMPT.format(target_language=target_language)
        parts: List[str] = [USER_PROMPT.format(
            count=len(pages), first=0, last=len(pages) - 1)]
        if glossary:
            parts.append("Glossary (use these readings):\n" + "\n".join(f"- {g}" for g in glossary))
        if chapter_note:
            parts.append(chapter_note)
        parts.append("Answer in exactly this shape:\n"
                     + ANSWER_SHAPE.replace("__LANG__", target_language))

        prompt_text = "\n\n".join(parts)
        ordered = list(pages)
        if self._backend is not None:
            answer = self._backend.complete_vision(
                system, prompt_text, [encode_panel(img, self.settings.vision_max_side,
                                                   self.settings.vision_quality)
                                      for _key, img in ordered],
                is_cancelled=is_cancelled, timeout_s=float(self.settings.vision_timeout_s),
                reasoning_effort=str(getattr(self.settings, "vision_reasoning_effort", "") or ""),
            )
        else:  # pragma: no cover - the app always has a backend
            raise VisionUnavailable("no backend for vision requests")

        if is_cancelled and is_cancelled():
            raise CancelledError("stale page")
        elapsed = (time.perf_counter() - started) * 1000.0
        _dump_answer(answer)
        try:
            parsed = parse_pages(answer, len(ordered))
        except ValueError as exc:
            log.warning("vision answer is not JSON (%s); first 300 chars: %s", exc, answer[:300])
            raise VisionUnavailable("the model did not answer with JSON") from exc

        out: Dict[str, VisionPage] = {}
        for index, (key, image) in enumerate(ordered):
            height, width = image.shape[:2]
            page = VisionPage(key=key, width=width, height=height, elapsed_ms=elapsed)
            raw_blocks = parsed.get(index, [])
            for number, raw in enumerate(raw_blocks, start=1):
                text = str(raw.get("text") or raw.get("original") or raw.get("source") or "").strip()
                translation = str(
                    raw.get("translation") or raw.get("ru") or raw.get("translated") or ""
                ).strip()
                if not text and not translation:
                    continue
                box = _normalise_box(raw.get("box") or raw.get("bbox") or raw.get("rect"),
                                     width, height)
                if box is None:
                    log.debug("page %s block %r has no usable box; skipping", key[-16:], text[:20])
                    continue
                refined = snap_box(image, box)
                if refined is not None:
                    box = refined
                page.blocks.append(Block(
                    id=number,
                    text=text,
                    bbox=Rect(box[0], box[1], box[2], box[3]),
                    direction="vertical" if raw.get("vertical") else "horizontal",
                    confidence=float(_as_float(raw.get("confidence")) or 0.9),
                    is_sfx=False,
                    translation=translation,
                ))
            out[key] = page
        log.info("vision read %d page(s) in %.0f ms: %s", len(ordered), elapsed,
                 ", ".join(f"{len(out[k].blocks)}b" for k, _ in ordered))
        return out

    # -- error handling ---------------------------------------------------
    def handle_error(self, exc: Exception) -> str:
        """Decide whether a failed vision call should disable the vision path.

        Returns a short reason when vision is now considered unusable for this
        provider+model (the caller then falls back to OCR).
        """
        if isinstance(exc, VisionUnavailable):
            self.mark_unavailable(str(exc)[:120])
            return str(exc)[:120]
        if isinstance(exc, PipelineError):
            if exc.kind in ("auth", "model", "request", "http", "blocked"):
                self.mark_unavailable(f"{exc.kind}: {exc.message[:90]}")
                return exc.message[:120]
            return ""
        if isinstance(exc, CancelledError):
            return ""
        text = str(exc).lower()
        if "image" in text or "vision" in text or "multimodal" in text:
            self.mark_unavailable(str(exc)[:120])
            return str(exc)[:120]
        return ""
