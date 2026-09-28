"""Physical <-> logical coordinate mapping across mixed-DPI multi-monitor setups.

`mss`/Win32 work in physical device pixels, Qt works in logical (DIP) pixels.
On a 150% display those differ by 1.5x, and each monitor has its own factor, so
a single global scale is wrong. Every rect is therefore converted *relative to
the monitor that owns it*:

    logical = monitor.logical.origin + (physical - monitor.physical.origin) / scale
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from ..core.logging_setup import get_logger
from ..core.models import Rect, ScreenRegion

log = get_logger("coordmap")


@dataclass(frozen=True)
class MonitorGeom:
    name: str
    physical: Rect  # Win32 physical rect (GetMonitorInfoW rcMonitor)
    logical: Rect   # Qt logical rect (QScreen.geometry())
    scale: float
    primary: bool = False

    @property
    def physical_bounds(self) -> Rect:
        return self.physical

    def physical_to_logical(self, rect: Rect) -> Rect:
        rel_x = rect.x - self.physical.x
        rel_y = rect.y - self.physical.y
        return Rect(
            self.logical.x + rel_x / self.scale,
            self.logical.y + rel_y / self.scale,
            max(1.0, rect.w / self.scale),
            max(1.0, rect.h / self.scale),
        )

    def logical_to_physical(self, rect: Rect) -> Rect:
        rel_x = rect.x - self.logical.x
        rel_y = rect.y - self.logical.y
        return Rect(
            self.physical.x + rel_x * self.scale,
            self.physical.y + rel_y * self.scale,
            max(1.0, rect.w * self.scale),
            max(1.0, rect.h * self.scale),
        )


class CoordMap:
    def __init__(self, monitors: Sequence[MonitorGeom]):
        self.monitors: List[MonitorGeom] = list(monitors)
        if not self.monitors:
            raise ValueError("CoordMap needs at least one monitor")

    # -- lookup -----------------------------------------------------------
    def monitor_for_rect(self, rect: Rect) -> MonitorGeom:
        best: Optional[MonitorGeom] = None
        best_cov = -1.0
        for mon in self.monitors:
            cov = mon.physical.coverage_of(rect)
            if cov > best_cov:
                best, best_cov = mon, cov
        return best or self.monitors[0]

    def monitor_for_logical_rect(self, rect: Rect) -> MonitorGeom:
        best: Optional[MonitorGeom] = None
        best_cov = -1.0
        for mon in self.monitors:
            cov = mon.logical.coverage_of(rect)
            if cov > best_cov:
                best, best_cov = mon, cov
        return best or self.monitors[0]

    def scale_for_rect(self, rect: Rect) -> float:
        return self.monitor_for_rect(rect).scale

    # -- conversions ------------------------------------------------------
    def physical_to_logical(self, rect: Rect) -> Rect:
        return self.monitor_for_rect(rect).physical_to_logical(rect)

    def logical_to_physical(self, rect: Rect) -> Rect:
        return self.monitor_for_logical_rect(rect).logical_to_physical(rect)

    def region_to_logical(self, region: ScreenRegion) -> Rect:
        """Capture region (absolute physical) -> Qt geometry (absolute logical)."""
        return self.physical_to_logical(region.rect).rounded()

    def region_rect_to_logical(self, rect_in_region_px: Rect, region: ScreenRegion) -> Rect:
        """A bbox inside a capture (region-relative physical px) -> Qt logical px."""
        absolute = rect_in_region_px.translated(region.x, region.y)
        return self.physical_to_logical(absolute)

    # -- factories --------------------------------------------------------
    @staticmethod
    def build(qt_screens: Iterable, win32_monitors: Sequence[dict]) -> "CoordMap":
        """qt_screens: QScreen objects; win32_monitors: dpi.list_monitors() output."""
        screens = list(qt_screens)
        qt: List[tuple] = []  # (name, logical rect, devicePixelRatio)
        for screen in screens:
            geo = screen.geometry()
            qt.append(
                (
                    str(screen.name()),
                    Rect(geo.x(), geo.y(), geo.width(), geo.height()),
                    float(screen.devicePixelRatio()) or 1.0,
                )
            )
        by_name = {m["name"]: m for m in win32_monitors}
        used: set = set()
        monitors: List[MonitorGeom] = []
        for name, logical, dpr in qt:
            native = by_name.get(name)
            if native is None:
                # Qt and Win32 names differ (rare): match by width ratio.
                for cand_name, cand in by_name.items():
                    if cand_name in used:
                        continue
                    if abs(cand["physical"].w / max(1.0, logical.w) - dpr) < 0.28:
                        native = dict(cand, name=cand_name)
                        break
            if native is None:
                log.warning("no native monitor matched %s; assuming 100%%", name)
                monitors.append(MonitorGeom(name, logical, logical, 1.0))
                continue
            used.add(native["name"])
            monitors.append(
                MonitorGeom(
                    name=str(native["name"]),
                    physical=native["physical"],
                    logical=logical,
                    scale=max(0.25, native["physical"].w / max(1.0, logical.w)),
                )
            )
        primary = _pick_primary(monitors, screens)
        monitors = [
            MonitorGeom(m.name, m.physical, m.logical, m.scale, m.name == primary)
            for m in monitors
        ]
        return CoordMap(monitors)

    @staticmethod
    def uniform(scale: float = 1.0, size: Rect = Rect(0, 0, 1920, 1080)) -> "CoordMap":
        """Single-monitor map, used by tests and non-Windows fallbacks."""
        logical = Rect(size.x / scale, size.y / scale, size.w / scale, size.h / scale)
        return CoordMap([MonitorGeom("display", size, logical, scale, True)])

    def describe(self) -> str:
        return ", ".join(
            f"{m.name} {m.physical.w:.0f}x{m.physical.h:.0f}px @ {m.scale * 100:.0f}%"
            for m in self.monitors
        )


def _pick_primary(monitors: List[MonitorGeom], qt_screens: Iterable) -> str:
    screens = list(qt_screens)
    primary_name = ""
    for screen in screens:
        try:
            if screen.isPrimaryScreen():
                primary_name = str(screen.name())
                break
        except Exception:  # pragma: no cover - defensive
            continue
    if primary_name:
        for m in monitors:
            if m.name == primary_name:
                return primary_name
    return monitors[0].name
