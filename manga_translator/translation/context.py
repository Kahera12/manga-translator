"""Rolling dialogue context across pages (§14): a bounded window, context only."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Sequence

from ..core.logging_setup import get_logger
from ..core.models import Block

log = get_logger("context")


@dataclass
class ContextLine:
    source: str
    translation: str

    def render(self) -> str:
        return f"{self.source} -> {self.translation}"


class ContextStore:
    """Keeps the last N pages of (source -> translation) lines, never growing."""

    def __init__(self, max_pages: int = 2, max_lines: int = 60):
        self.max_pages = max(0, int(max_pages))
        self.max_lines = max(0, int(max_lines))
        self._pages: Deque[List[ContextLine]] = deque(maxlen=max(1, self.max_pages))

    def configure(self, max_pages: int) -> None:
        self.max_pages = max(0, int(max_pages))
        pages = list(self._pages)[-self.max_pages :] if self.max_pages else []
        self._pages = deque(pages, maxlen=max(1, self.max_pages))

    def add_page(self, blocks: Sequence[Block]) -> None:
        if self.max_pages <= 0:
            return
        lines = [
            ContextLine(source=b.text.strip(), translation=b.translation.strip())
            for b in blocks
            if b.text.strip() and b.translation.strip()
        ]
        if not lines:
            return
        self._pages.append(lines)

    def lines(self) -> List[str]:
        out: List[str] = []
        for page in self._pages:
            out.extend(line.render() for line in page)
        return out[-self.max_lines :] if self.max_lines else out

    def clear(self) -> None:
        self._pages.clear()

    def __len__(self) -> int:
        return sum(len(p) for p in self._pages)
