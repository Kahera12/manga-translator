"""Manga-specific prompts + the structured-output schema.

The whole page is sent in one request (§12) so the model sees page context and
can keep voices consistent, and the reply is constrained by a JSON schema so no
free-form parsing is needed (§12/§13).
"""
from __future__ import annotations

import json
from typing import Iterable, Sequence

# Object root (not a bare array): the most widely supported schema shape.
RESPONSE_SCHEMA = {
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
                "propertyOrdering": ["id", "translation"],
            },
        }
    },
    "required": ["translations"],
}

SYSTEM_PROMPT = """You are a professional manga translator working from Chinese \
(Simplified and Traditional) into {target_language}.

You translate ONE page of a comic at a time. Every dialogue block of the page is \
sent together so you can keep the scene coherent.

Rules:
- Translate every block you are given, and only those blocks. Never invent, merge, \
split, reorder or silently drop a block; return one entry per provided id.
- Preserve meaning, tone and register. Keep each character's voice distinct \
(rough, polite, childish, sarcastic, archaic...).
- Preserve slang, jokes, honorifics and casual phrasing; do not sanitise them.
- Prefer natural, idiomatic {target_language} over literal word-for-word \
translation. Avoid stiff, bookish or bureaucratic wording.
- Keep translations compact - about as long as the original - so they fit near \
the original speech bubble. Do not add stage directions.
- Keep character names, place names and terminology consistent with the glossary \
and with the previously translated lines.
- Text may be horizontal or vertical; a vertical block is a CJK column and its \
characters read top to bottom.
- Keep punctuation appropriate for {target_language}; use "..." for hesitation \
and "!" for shouting.
- Never output the Chinese source, romanisation, translator notes, explanations \
or commentary.
- Answer with JSON only, matching the requested schema exactly."""


def build_system_prompt(target_language: str = "Russian") -> str:
    return SYSTEM_PROMPT.format(target_language=target_language)


def build_user_prompt(
    blocks: Sequence[dict],
    target_language: str = "Russian",
    page_index: int = 0,
    context_lines: Iterable[str] = (),
    glossary: dict = None,
    retry_note: str = "",
) -> str:
    """blocks: [{"id": 1, "text": "...", "direction": "horizontal"}]"""
    parts = []
    parts.append(
        f"Translate page {page_index + 1} of this Chinese comic into {target_language}."
    )

    glossary = glossary or {}
    if glossary:
        lines = "\n".join(f"{src} = {dst}" for src, dst in list(glossary.items())[:200])
        parts.append(
            "Glossary (use these exact renderings whenever the source term appears):\n" + lines
        )

    ctx = [line for line in context_lines if line.strip()]
    if ctx:
        parts.append(
            "Previously translated lines (context only - do NOT translate them again):\n"
            + "\n".join(ctx[-30:])
        )

    payload = [
        {
            "id": int(b["id"]),
            "text": b["text"],
            **({"direction": b["direction"]} if b.get("direction") == "vertical" else {}),
        }
        for b in blocks
    ]
    parts.append(
        "Dialogue blocks of the current page, in manga reading order:\n"
        + json.dumps(payload, ensure_ascii=False, indent=None, separators=(",", ":"))
    )
    parts.append(
        "Return JSON: {\"translations\":[{\"id\":<same id>,\"translation\":\"<"
        + target_language
        + " text>\"}]} with exactly one entry per id above."
    )
    if retry_note:
        parts.append(retry_note)
    return "\n\n".join(parts)


REPAIR_NOTE = (
    "Your previous answer could not be parsed as the requested JSON. "
    "Reply again with ONLY the JSON object, no prose, no markdown fences."
)

MISSING_NOTE = (
    "Some ids were missing from your previous answer. Translate the ids listed "
    "below and return the same JSON shape."
)


def repair_prompt(blocks: Sequence[dict], target_language: str, missing_ids: Sequence[int]) -> str:
    payload = [b for b in blocks if int(b["id"]) in set(missing_ids)]
    return (
        "The following dialogue blocks still need translation. "
        f"Translate them into {target_language} and return them as JSON with the "
        "same ids:\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )
