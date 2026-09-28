"""DPI / multi-monitor coordinate mapping (§27)."""
from __future__ import annotations

from manga_translator.capture.coordmap import CoordMap
from manga_translator.core.models import Rect, ScreenRegion


class FakeScreen:
    """Minimal QScreen stand-in (geometry in logical px + devicePixelRatio)."""

    def __init__(self, name, x, y, w, h, dpr, primary=False):
        self._name = name
        self._rect = (x, y, w, h)
        self._dpr = dpr
        self._primary = primary

    def name(self):
        return self._name

    def devicePixelRatio(self):
        return self._dpr

    def geometry(self):
        class G:
            def __init__(self, x, y, w, h):
                self._v = (x, y, w, h)

            def x(self):
                return self._v[0]

            def y(self):
                return self._v[1]

            def width(self):
                return self._v[2]

            def height(self):
                return self._v[3]

        return G(*self._rect)

    def isPrimaryScreen(self):
        return self._primary


def _mixed_dpi_map() -> CoordMap:
    # 150% primary 2560x1440 physical, 100% secondary 1080x1920 physical
    screens = [
        FakeScreen(r"\\.\DISPLAY2", 0, 0, 1707, 960, 1.5, primary=True),
        FakeScreen(r"\\.\DISPLAY1", 1707, -473, 1080, 1920, 1.0),
    ]
    natives = [
        {"name": r"\\.\DISPLAY2", "physical": Rect(0, 0, 2560, 1440), "work": Rect(0, 0, 2560, 1400)},
        {"name": r"\\.\DISPLAY1", "physical": Rect(2560, -473, 1080, 1920), "work": Rect(2560, -473, 1080, 1880)},
    ]
    return CoordMap.build(screens, natives)


def test_region_on_150_percent_monitor():
    cmap = _mixed_dpi_map()
    region = ScreenRegion(x=500, y=300, width=1250, height=1000, monitor=r"\\.\DISPLAY2", dpi_scale=1.5)
    logical = cmap.region_to_logical(region)
    # window geometry is snapped to whole logical pixels
    assert abs(logical.x - 500 / 1.5) <= 1.0
    assert abs(logical.y - 300 / 1.5) <= 1.0
    assert abs(logical.w - 1250 / 1.5) <= 1.0
    assert abs(logical.h - 1000 / 1.5) <= 1.0


def test_region_on_secondary_monitor_with_negative_origin():
    cmap = _mixed_dpi_map()
    region = ScreenRegion(x=2600, y=-400, width=1000, height=900, monitor=r"\\.\DISPLAY1", dpi_scale=1.0)
    logical = cmap.region_to_logical(region)
    # DISPLAY1 starts at logical x=1707 (right of the 150% primary) and y=-473,
    # equal to its physical origin because its own scale is 100%.
    assert abs(logical.x - 1747) <= 1.0      # 1707 + (2600-2560)/1.0
    assert abs(logical.y - (-400)) <= 1.0    # -473 + (-400 - -473)
    assert abs(logical.w - 1000) <= 1.0


def test_block_bbox_maps_to_the_same_place_as_the_region():
    """Overlay cards must sit exactly on the OCR source (§27)."""
    cmap = _mixed_dpi_map()
    region = ScreenRegion(x=2560, y=-473, width=1080, height=1920, monitor=r"\\.\DISPLAY1", dpi_scale=1.0)
    origin = cmap.region_to_logical(region)
    # OCR bbox 100 px right / 200 px down inside the capture
    absolute = Rect(100, 200, 300, 80).translated(region.x, region.y)
    logical = cmap.physical_to_logical(absolute)
    relative = logical.translated(-origin.x, -origin.y)
    assert abs(relative.x - 100) < 0.6
    assert abs(relative.y - 200) < 0.6
    assert abs(relative.w - 300) < 0.6


def test_round_trip_is_stable():
    cmap = _mixed_dpi_map()
    rect = Rect(300, 200, 400, 300)
    back = cmap.logical_to_physical(cmap.physical_to_logical(rect))
    assert abs(back.x - rect.x) < 1.0
    assert abs(back.w - rect.w) < 1.0


def test_equal_scaling_monitors_use_a_single_factor():
    screens = [
        FakeScreen("A", 0, 0, 1920, 1080, 1.25, primary=True),
        FakeScreen("B", 1920, 0, 1920, 1080, 1.25),
    ]
    natives = [
        {"name": "A", "physical": Rect(0, 0, 2400, 1350), "work": Rect(0, 0, 2400, 1300)},
        {"name": "B", "physical": Rect(2400, 0, 2400, 1350), "work": Rect(2400, 0, 2400, 1300)},
    ]
    cmap = CoordMap.build(screens, natives)
    region = ScreenRegion(x=3000, y=100, width=500, height=400, monitor="B", dpi_scale=1.25)
    logical = cmap.region_to_logical(region)
    assert abs(logical.x - (1920 + 600 / 1.25)) < 0.6
    assert abs(logical.w - 400) < 0.6


def test_uniform_map_helper():
    cmap = CoordMap.uniform(1.5, Rect(0, 0, 2560, 1440))
    logical = cmap.physical_to_logical(Rect(150, 150, 300, 300))
    assert logical.x == 100 and logical.w == 200
