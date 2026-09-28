"""Generation counter (§5): every detected page gets a unique id.

All OCR / translation / overlay work belongs to a generation. Anything that
finishes late for an older generation is discarded instead of being drawn over a
newer page - the mandatory "stale results never appear on a newer page" rule.
"""
from __future__ import annotations

import threading
from typing import Callable, Tuple


class GenerationCounter:
    def __init__(self, initial: int = 0):
        self._value = int(initial)
        self._lock = threading.Lock()

    @property
    def current(self) -> int:
        with self._lock:
            return self._value

    def bump(self) -> int:
        """Invalidate everything older and return the new generation id."""
        with self._lock:
            self._value += 1
            return self._value

    def is_stale(self, generation: int) -> bool:
        with self._lock:
            return int(generation) != self._value

    def cancellable(self, generation: int) -> Callable[[], bool]:
        return lambda: self.is_stale(generation)

    def snapshot(self) -> Tuple[int, Callable[[int], bool]]:
        with self._lock:
            value = self._value
        return value, self.is_stale
