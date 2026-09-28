"""Fast region screen capture (mss / GDI BitBlt of the virtual desktop).

mss is used because it is a single lightweight dependency, grabs a partial
region (no full-desktop copy) and is ~1-3 ms for a page-sized area.

The overlay must not contaminate what we OCR: full-page grabs hide it for the
duration of the grab (`_quiet()`), and preview grabs mask the card rectangles
inside the page-change detector (see detection/page_change.py).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from ..core.logging_setup import get_logger
from ..core.models import PipelineError, ScreenRegion

log = get_logger("capture")

try:  # pragma: no cover - platform dependent
    import mss as _mss
    import mss.exception as _mss_exc
except Exception:  # pragma: no cover
    _mss = None
    _mss_exc = None

# mss >= 10 exposes `MSS`; older releases only have the `mss()` factory.
_MSS_FACTORY = None
if _mss is not None:
    _MSS_FACTORY = getattr(_mss, "MSS", None) or getattr(_mss, "mss", None)

try:
    import cv2 as _cv2
except Exception:  # pragma: no cover
    _cv2 = None


class FrameSource:
    """Where frames come from. Implemented by ScreenCapture and by test sources."""

    def grab(self, region: ScreenRegion) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError

    def grab_preview(self, region: ScreenRegion, max_dim: int = 240) -> np.ndarray:
        raise NotImplementedError

    def desktop_size(self) -> tuple:  # pragma: no cover - interface
        return (0, 0)


@dataclass
class CaptureHooks:
    """Called around a *full page* grab so the overlay can get out of the way."""

    before: Optional[Callable[[], None]] = None
    after: Optional[Callable[[], None]] = None
    settle_ms: int = 25


class ScreenCapture(FrameSource):
    """Thread-safe (one mss session per thread) region grabber."""

    def __init__(self, hooks: Optional[CaptureHooks] = None):
        if _mss is None:
            raise PipelineError(
                "Screen capture library (mss) is not installed. Run: pip install -r requirements.txt",
                kind="dependency",
            )
        self._hooks = hooks or CaptureHooks()
        self._local = threading.local()
        self._desktop = self._query_desktop()

    # -- mss plumbing -----------------------------------------------------
    def _session(self):
        sess = getattr(self._local, "sct", None)
        if sess is None:
            try:
                sess = _MSS_FACTORY()
            except Exception as exc:  # pragma: no cover
                raise PipelineError(
                    f"Cannot start screen capture ({exc.__class__.__name__}).", kind="capture"
                ) from exc
            self._local.sct = sess
        return sess

    def _query_desktop(self):
        try:
            sct = self._session()
            bounds = sct.monitors[0]  # virtual desktop spanning all monitors
            return (int(bounds["width"]), int(bounds["height"]))
        except Exception as exc:  # pragma: no cover
            log.warning("desktop bounds unavailable: %s", exc)
            return (0, 0)

    def desktop_size(self) -> tuple:
        return self._desktop

    def refresh_desktop(self) -> tuple:
        """Call after a monitor/DPI change so clamping stays correct."""
        self._local = threading.local()
        self._desktop = self._query_desktop()
        return self._desktop

    def clamp(self, region: ScreenRegion) -> ScreenRegion:
        w, h = self._desktop
        if w <= 0 or h <= 0:
            return region
        x1 = max(region.x, 0)
        y1 = max(region.y, 0)
        x2 = min(region.x + region.width, w)
        y2 = min(region.y + region.height, h)
        if x2 - x1 < 16 or y2 - y1 < 16:
            raise PipelineError(
                "The selected region is no longer on a connected monitor. Select a new region.",
                kind="region",
            )
        return ScreenRegion(x1, y1, x2 - x1, y2 - y1, region.monitor, region.dpi_scale)

    # -- grabs ------------------------------------------------------------
    def grab(self, region: ScreenRegion) -> np.ndarray:
        """Full resolution BGR uint8 array of the region (physical pixels)."""
        region = self.clamp(region)
        monitor = {"left": region.x, "top": region.y, "width": region.width, "height": region.height}
        before = self._hooks.before
        after = self._hooks.after
        if before:
            before()
            if self._hooks.settle_ms:
                time.sleep(self._hooks.settle_ms / 1000.0)
        try:
            shot = self._session().grab(monitor)
        except Exception as exc:  # mss.ScreenError / MonitorsError / OSError
            exc.__class__.__name__
            raise PipelineError(
                "Screen capture failed - the region may be protected (DRM) or the monitor changed.",
                kind="capture",
            ) from exc
        finally:
            if after:
                after()
        img = np.asarray(shot)  # BGRA
        if img.ndim == 3 and img.shape[2] == 4:
            img = img[:, :, :3]
        if not img.flags["C_CONTIGUOUS"]:
            img = np.ascontiguousarray(img)
        if not img.any():
            raise PipelineError(
                "The captured region is completely black - the app you selected may block "
                "screen capture (hardware DRM).",
                kind="capture",
            )
        return np.ascontiguousarray(img)

    def grab_preview(self, region: ScreenRegion, max_dim: int = 240) -> np.ndarray:
        """Small grayscale preview for cheap change detection."""
        img = self.grab(region)
        return downscale_gray(img, max_dim)


def downscale_gray(img: np.ndarray, max_dim: int = 240) -> np.ndarray:
    """Deterministic small grayscale copy used for similarity + pHash."""
    if img.ndim == 3:
        if _cv2 is not None:
            gray = _cv2.cvtColor(img, _cv2.COLOR_BGR2GRAY)
        else:  # pragma: no cover
            gray = img[:, :, :3].mean(axis=2).astype("uint8")
    else:
        gray = img
    h, w = gray.shape[:2]
    if max(h, w) > max_dim and max_dim >= 8:
        if _cv2 is not None:
            ratio = max_dim / float(max(h, w))
            size = (max(1, int(round(w * ratio))), max(1, int(round(h * ratio))))
            gray = _cv2.resize(gray, size, interpolation=_cv2.INTER_AREA)
        else:  # pragma: no cover
            step_h = max(1, h // max_dim)
            step_w = max(1, w // max_dim)
            gray = gray[::step_h, ::step_w]
    return np.ascontiguousarray(gray)
