"""Shared parsing helpers for structured model answers.

Every LLM backend asks for the same shape:

    {"translations": [{"id": 1, "translation": "..."}]}

Models are not always obedient, so the parser tolerates markdown fences, a bare
array, an object wrapper under a different key, or an ``{"1": "..."}`` map.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Sequence

_FENCE = re.compile(r"^\s*```(?:json)?|```\s*$", re.IGNORECASE)
_WRAPPER_KEYS = ("translations", "items", "results", "data", "blocks", "output")
_ID_KEYS = ("id", "index", "block_id", "blockId", "block")
_TEXT_KEYS = ("translation", "text", "ru", "target", "output", "translatedText")


def strip_fences(text: str) -> str:
    return _FENCE.sub("", (text or "").strip()).strip()


def parse_rows(text: str) -> List[dict]:
    """Best-effort extraction of the translation rows from a model answer."""
    cleaned = strip_fences(text)
    if not cleaned:
        return []
    payload = None
    try:
        payload = json.loads(cleaned)
    except ValueError:
        match = re.search(r"[\[{].*[\]}]", cleaned, re.DOTALL)
        if match:
            try:
                payload = json.loads(match.group(0))
            except ValueError:
                return []
    if isinstance(payload, dict):
        for key in _WRAPPER_KEYS:
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            if payload and all(str(k).isdigit() for k in payload.keys()):
                return [{"id": int(k), "translation": v} for k, v in payload.items()]
            return []
    if not isinstance(payload, list):
        return []
    rows: List[dict] = []
    for row in payload:
        if isinstance(row, dict):
            rows.append(_normalise_row(row))
    return rows


def _normalise_row(row: dict) -> dict:
    out = dict(row)
    if "id" not in out:
        for key in _ID_KEYS:
            if key in out:
                out["id"] = out[key]
                break
    if "translation" not in out:
        for key in _TEXT_KEYS:
            if key in out:
                out["translation"] = out[key]
                break
    return out


def rows_to_table(rows: Sequence[dict], allowed: Sequence[int]) -> Dict[int, str]:
    """Keep only requested ids, ignore duplicates and empty values."""
    table: Dict[int, str] = {}
    allow = set(int(i) for i in allowed)
    for row in rows:
        try:
            rid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        value = row.get("translation")
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or rid not in allow:
            continue
        table.setdefault(rid, value)
    return table


def parse_answer(text: str, allowed: Sequence[int]) -> Dict[int, str]:
    return rows_to_table(parse_rows(text), allowed)


def json_only_note() -> str:
    return ("Your previous answer could not be parsed as the requested JSON. "
            "Reply again with ONLY the JSON object, no prose, no markdown fences.")
