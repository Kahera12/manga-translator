"""Page change detection + stabilization state machine.

Loop fed by the monitor thread (every ``poll_interval_ms``):

    IDLE --(score >= threshold)--> CHANGING --(score < threshold for
    ``stable_duration_ms``)--> PAGE_READY --> IDLE

While CHANGING, intermediate scroll frames are *never* processed; only the
frame that finally stabilizes produces a page event. If the view never settles
(e.g. an animated background) we force one best-effort event after
``max_wait_ms`` so the user is not stuck without a translation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from ..core.logging_setup import get_logger
from ..core.models import Rect
from .image_similarity import ChangeReport, SimilarityConfig, change_score

log = get_logger("page_change")

STATE_IDLE = "idle"
STATE_CHANGING = "changing"


@dataclass
class DetectorSettings:
    change_threshold: float = 0.075
    stable_ratio: float = 0.45        # must drop below threshold*ratio to count as stable
    stable_duration_ms: int = 350
    min_unstable_frames: int = 1      # ignore single-frame blips (tooltips, carets)
    max_wait_ms: int = 4000
    preview_max_dim: int = 240


@dataclass
class PageEvent:
    kind: str            # "first" | "changed" | "forced"
    frame: np.ndarray    # the stabilized preview frame
    report: ChangeReport
    changed_after_ms: int
    stable_for_ms: int
    peak_score: float = 0.0   # strongest change seen while the page was moving


@dataclass
class DetectorStatus:
    state: str = STATE_IDLE
    score: float = 0.0
    changed_fraction: float = 0.0
    hamming: int = 0
    stable_for_ms: int = 0
    page_hash: str = ""
    region_coverage_masked: float = 0.0


class PageChangeDetector:
    """Feed preview frames in, get page events out. Not thread safe (one thread)."""

    def __init__(self, settings: DetectorSettings, sim: Optional[SimilarityConfig] = None):
        self.s = settings
        self.sim = sim or SimilarityConfig()
        self.state = STATE_IDLE
        self._baseline: Optional[np.ndarray] = None
        self._last_frame: Optional[np.ndarray] = None
        self._last_change_ts: Optional[float] = None
        self._changed_since: Optional[float] = None
        self._unstable_frames = 0
        self._first_pending = True
        self._peak_score = 0.0
        self.status = DetectorStatus()
        self.page_hash = ""

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def now_ms() -> float:
        return time.monotonic() * 1000.0

    def reset(self, reason: str = "manual") -> None:
        log.debug("detector reset (%s)", reason)
        self._baseline = None
        self._last_frame = None
        self._changed_since = None
        self._unstable_frames = 0
        self._first_pending = True
        self.state = STATE_IDLE
        self.status.state = STATE_IDLE
        self.status.stable_for_ms = 0

    @property
    def stable_threshold(self) -> float:
        return self.s.change_threshold * self.s.stable_ratio

    # -- main entry point -------------------------------------------------
    def feed(
        self,
        frame: np.ndarray,
        masks: Sequence[Rect] = (),
        now: Optional[float] = None,
    ) -> Optional[PageEvent]:
        """``masks``: rects in *frame* pixel space that must be ignored."""
        now = self.now_ms() if now is None else now

        if self._baseline is None:
            # First frame: enter CHANGING and wait for the view to settle, exactly
            # like a page turn. Enabling Manga Mode on an already open page
            # therefore still produces a page (kind="first") once it is stable.
            self._baseline = frame
            self._last_frame = frame
            self._changed_since = now
            self._last_change_ts = now
            self._unstable_frames = 1
            self.state = STATE_CHANGING
            self.status.state = STATE_CHANGING
            self.status.score = 1.0
            return None

        report = change_score(self._last_frame, frame, self.sim, masks)
        self._last_frame = frame
        self.status.score = report.score
        self.status.changed_fraction = report.changed_fraction
        self.status.hamming = report.hamming
        self.status.region_coverage_masked = report.mask_coverage

        if report.mask_coverage > 0.92:
            # Our own cards cover the whole page: no reliable signal.
            self.status.stable_for_ms = self._stable_for(now)
            return None

        if self.state == STATE_IDLE:
            if report.score >= self.s.change_threshold:
                self.state = STATE_CHANGING
                self.status.state = STATE_CHANGING
                self._changed_since = now
                self._unstable_frames = 1
                self._last_change_ts = now
                self._peak_score = report.score
            else:
                self.status.stable_for_ms = 0
            return None

        # CHANGING
        if report.score >= self.stable_threshold:
            self._last_change_ts = now
            self._unstable_frames += 1
            self._peak_score = max(self._peak_score, report.score)
            self.status.stable_for_ms = 0
            # Safety valve: the view never settles (animation, video, GIF) - hand
            # over one best-effort page instead of never translating anything.
            if self._changed_for(now) >= self.s.max_wait_ms:
                return self._emit("forced", frame, report, now, 0)
            return None

        stable_for = self._stable_for(now)
        self.status.stable_for_ms = stable_for

        if (
            self._unstable_frames >= self.s.min_unstable_frames
            and stable_for >= self.s.stable_duration_ms
        ):
            return self._emit("changed", frame, report, now, stable_for)

        if self._changed_for(now) >= self.s.max_wait_ms:
            return self._emit("forced", frame, report, now, stable_for)

        return None

    def _stable_for(self, now: float) -> int:
        if self._last_change_ts is None:
            return 0
        return max(0, int(now - self._last_change_ts))

    def _changed_for(self, now: float) -> int:
        if self._changed_since is None:
            return 0
        return max(0, int(now - self._changed_since))

    def _emit(self, kind, frame, report, now, stable_for) -> PageEvent:
        changed_after = self._changed_for(now)
        if kind == "changed" and self._first_pending:
            kind = "first"
        self._first_pending = False
        self.state = STATE_IDLE
        self.status.state = STATE_IDLE
        self.status.page_hash = report.hash_b
        self.page_hash = report.hash_b
        self._baseline = frame
        self._changed_since = None
        self._unstable_frames = 0
        self.status.stable_for_ms = stable_for
        log.debug(
            "page event %s (score=%.3f peak=%.3f changed=%.3f ham=%d after=%dms stable=%dms)",
            kind, report.score, self._peak_score, report.changed_fraction, report.hamming,
            changed_after, stable_for,
        )
        event = PageEvent(
            kind=kind,
            frame=frame,
            report=report,
            changed_after_ms=changed_after,
            stable_for_ms=stable_for,
            peak_score=self._peak_score,
        )
        self._peak_score = 0.0
        return event
