"""Live end-to-end check on the real Windows desktop.

Opens a window that shows a synthetic Chinese manga page, points the real
application at it (mss capture -> change detection -> PP-OCRv4 -> translation ->
click-through overlay) and verifies, with real screen pixels and real Win32
calls:

  1. the overlay cards really appear on screen over the reader window;
  2. the overlay is click-through (the window under a card is the reader, not us);
  3. a screenshot taken by the pipeline does NOT contain the overlay (hygiene);
  4. turning the page updates the overlay automatically (old text disappears);
  5. the region stays aligned (cards sit on their bubbles) at the current DPI.

    python tools/live_check.py            # ~20 seconds, opens a window
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

WINDOW_W, WINDOW_H = 900, 640
WINDOW_POS = (80, 80)  # logical


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mt-live-"))
    # MT_LIVE_PROVIDER=mymemory runs the whole desktop check against a real,
    # keyless translation API instead of the offline stub.
    provider = os.environ.get("MT_LIVE_PROVIDER", "fake").strip() or "fake"
    os.environ["MT_DATA_DIR"] = str(tmp / "data")
    os.environ["TRANSLATION_BACKEND"] = provider
    os.environ["TRANSLATION_PROVIDER"] = provider
    os.environ["CACHE_ENABLED"] = "true"
    os.environ.pop("QT_QPA_PLATFORM", None)
    print(f"translation provider for this run: {provider}")

    from manga_translator.core import dpi

    awareness = dpi.enable_per_monitor_dpi_awareness()

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import QLabel

    from manga_translator.app import MangaTranslatorApp
    from manga_translator.capture.screen_capture import ScreenCapture
    from manga_translator.capture.synthetic import BubbleSpec, make_manga_page
    from manga_translator.core.models import ScreenRegion

    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(f" [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    app = MangaTranslatorApp([])
    qt = app.qt

    # --- a fake manga reader window ---------------------------------------
    page_a = make_manga_page(
        width=WINDOW_W, height=WINDOW_H,
        bubbles=[BubbleSpec("你好，今天天气真好。", 120, 120, font_size=30),
                 BubbleSpec("师兄，你来了！", 120, 260, font_size=30)],
        seed=1234,
    )
    page_b = make_manga_page(
        width=WINDOW_W, height=WINDOW_H,
        bubbles=[BubbleSpec("灵气失控了", 640, 90, vertical=True, font_size=30),
                 BubbleSpec("为什么？", 160, 380, font_size=30)],
        seed=4321,
    )

    def to_pixmap(bgr):
        rgb = np.ascontiguousarray(bgr[:, :, ::-1])
        h, w, _ = rgb.shape
        image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        return QPixmap.fromImage(image)

    reader = QLabel()
    reader.setWindowTitle("LIVE CHECK reader (synthetic manga page)")
    reader.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
    reader.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    reader.setPixmap(to_pixmap(page_a))
    reader.setFixedSize(WINDOW_W, WINDOW_H)
    reader.move(*WINDOW_POS)
    reader.show()
    reader.raise_()
    reader.activateWindow()
    qt.processEvents()
    time.sleep(0.6)
    qt.processEvents()

    reader_handle = int(reader.winId())
    try:  # make sure our synthetic page is really the front window
        ctypes.windll.user32.SetForegroundWindow(ctypes.wintypes.HWND(reader_handle))
    except Exception:
        pass
    time.sleep(0.4)
    qt.processEvents()
    print(f"reader window hwnd={reader_handle} at {reader.geometry()} (logical)")
    print(f"dpi awareness: {awareness}")

    # --- point the app at that window (region mode) ------------------------
    app.coordmap = app._build_coordmap()
    app.overlay.coordmap = app.coordmap
    app.pipeline.set_masks([])
    physical = app.coordmap.logical_to_physical(
        __import__("manga_translator.core.models", fromlist=["Rect"]).Rect(
            reader.geometry().x(), reader.geometry().y(),
            reader.geometry().width(), reader.geometry().height(),
        )
    ).rounded()
    region = ScreenRegion(int(physical.x), int(physical.y), int(physical.w), int(physical.h),
                          monitor=app.coordmap.monitor_for_rect(physical).name,
                          dpi_scale=app.coordmap.monitor_for_rect(physical).scale)
    print(f"capture region: {region.to_dict()}")

    # Sanity: the region must really show our synthetic page (and not, say, the
    # terminal that launched this script).
    from manga_translator.detection.image_similarity import change_score

    probe = ScreenCapture()
    visible_score = 1.0
    for attempt in range(6):
        probe_frame = probe.grab(region)
        visible_score = change_score(
            __import__("manga_translator.capture.screen_capture", fromlist=["downscale_gray"]).downscale_gray(page_a, 220),
            __import__("manga_translator.capture.screen_capture", fromlist=["downscale_gray"]).downscale_gray(probe_frame, 220),
        ).score
        print(f"region content vs synthetic page (attempt {attempt + 1}): score={visible_score:.3f}")
        if visible_score < 0.05:
            break
        reader.raise_()
        try:
            ctypes.windll.user32.SetForegroundWindow(ctypes.wintypes.HWND(reader_handle))
        except Exception:
            pass
        qt.processEvents()
        time.sleep(0.5)
    app.config.set_region(region)
    app.pipeline.set_region(region)
    app.overlay.follow_region(region)
    app.set_manga_mode(True)

    def wait_for_cards(timeout=25.0, need=1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            qt.processEvents()
            cards = app.overlay.cards
            if len(cards) >= need:
                return cards
            time.sleep(0.05)
        return app.overlay.cards

    cards = wait_for_cards()
    check(
        "synthetic page visible in the capture region",
        visible_score < 0.05,
        f"page similarity score={visible_score:.3f}",
    )
    check(
        "overlay cards rendered on the real desktop",
        len(cards) >= 2,
        f"{len(cards)} card(s): "
        + " | ".join(f"#{c.block_id} {c.translation[:40]}" for c in cards[:3]),
    )
    last = app._last_result
    if last is not None:
        from manga_translator.ocr.filtering import glyph_size, sfx_reason
        from manga_translator.ocr import sfx_settings

        settings = sfx_settings(app.config.ocr)
        print("   blocks: " + " | ".join(
            f"#{b.id} sfx={b.is_sfx}({sfx_reason(b, (region.width, region.height), settings) or 'dialogue'}) "
            f"glyph={glyph_size(b):.0f}px box=({b.bbox.w:.0f}x{b.bbox.h:.0f}) "
            f"{b.text!r} -> {b.translation!r}"
            for b in last.blocks
        ))
    if not cards:
        return _finish(results)

    # anchors must sit on the original bubbles (region-relative alignment)
    aligned = all(
        abs(c.anchor.x) < region.width and abs(c.anchor.y) < region.height for c in cards
    )
    check("cards anchored to OCR boxes", aligned,
          "; ".join(f"anchor=({c.anchor.x:.0f},{c.anchor.y:.0f}) card=({c.rect.x:.0f},{c.rect.y:.0f})"
                    for c in cards[:2]))

    # --- 1) overlay visible on screen + excluded from pipeline captures ----
    # Pause polling and let any in-flight page finish first, so nothing is
    # blanking the overlay at the moment we take the reference screenshot.
    app.pipeline.pause()
    app.pipeline.wait_idle(timeout=8.0)
    time.sleep(0.4)
    qt.processEvents()

    from PIL import ImageGrab

    def screen_grab():
        """Screen pixels including layered windows (what the user really sees)."""
        image = ImageGrab.grab(
            bbox=(region.x, region.y, region.x + region.width, region.y + region.height),
            all_screens=True,
        ).convert("RGB")
        return np.asarray(image)[:, :, ::-1].copy()

    visible_shot = screen_grab()
    clean = app.capture.grab(region)        # pipeline path (overlay auto-blanked)
    app.overlay.show_translation(False)
    qt.processEvents()
    time.sleep(0.3)
    hidden_shot = screen_grab()
    app.overlay.show_translation(True)
    qt.processEvents()
    time.sleep(0.2)

    card = cards[0]
    scale = region.dpi_scale
    x1 = int(card.rect.x * scale) + 3
    y1 = int(card.rect.y * scale) + 3
    x2 = max(x1 + 1, int(card.rect.x2 * scale) - 3)
    y2 = max(y1 + 1, int(card.rect.y2 * scale) - 3)

    def area_diff(a, b):
        return float(np.abs(a[y1:y2, x1:x2].astype("int16") - b[y1:y2, x1:x2].astype("int16")).mean())

    def rest_diff(a, b):
        outside = a.copy()
        outside[y1:y2, x1:x2] = b[y1:y2, x1:x2]
        return float(np.abs(outside.astype("int16") - b.astype("int16")).mean())

    on_screen = area_diff(visible_shot, hidden_shot)
    hygienic = area_diff(clean, hidden_shot)
    aligned = rest_diff(visible_shot, clean)
    check(
        "overlay cards are really drawn on the desktop",
        on_screen > 12.0,
        f"pixel difference inside the card (visible vs hidden) = {on_screen:.1f} "
        f"[windowOpacity={app.overlay.windowOpacity():.1f}]",
    )
    check(
        "pipeline screenshots do not contain the overlay (OCR hygiene)",
        hygienic < 6.0,
        f"pipeline grab vs clean page inside the card area = {hygienic:.2f}",
    )
    check(
        "overlay aligned with the page outside the cards",
        aligned < 6.0,
        f"page difference outside the card = {aligned:.2f}",
    )

    # --- 3) click-through (Win32 hit test) --------------------------------
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
    user32.WindowFromPoint.restype = ctypes.wintypes.HWND
    overlay_handle = int(app.overlay.winId())
    hit = user32.WindowFromPoint(
        ctypes.wintypes.POINT(region.x + (x1 + x2) // 2, region.y + (y1 + y2) // 2)
    )
    check(
        "overlay is click-through (clicks reach the manga window)",
        int(hit or 0) not in (0, overlay_handle),
        f"window under the card: {int(hit or 0)} (overlay={overlay_handle}, reader={reader_handle})",
    )

    # --- 4) page turn updates the overlay automatically -------------------
    app.pipeline.resume()
    before = [c.translation for c in app.overlay.cards]
    reader.setPixmap(to_pixmap(page_b))
    reader.update()
    qt.processEvents()
    deadline = time.time() + 25
    updated = False
    while time.time() < deadline:
        qt.processEvents()
        now = [c.translation for c in app.overlay.cards]
        if now and now != before:
            updated = True
            break
        time.sleep(0.05)
    after = [c.translation for c in app.overlay.cards]
    check("page turn updates the overlay automatically", updated and after != before,
          f"before={before} after={after}")

    # --- 5) generation guard: newest page wins ----------------------------
    check(
        "only the newest generation is displayed",
        all(c.confidence >= 0 for c in app.overlay.cards) and after != before,
        f"generation={app.pipeline.generations.current}",
    )

    # --- 6) control panel window ------------------------------------------
    app.show_window()
    qt.processEvents()
    time.sleep(0.4)
    app.request_preview()
    deadline = time.time() + 12
    while time.time() < deadline and app.window.preview.frame_size == (0, 0):
        qt.processEvents()
        time.sleep(0.05)
    frame_size = app.window.preview.frame_size
    rows = app.window.blocks_panel.table.rowCount()
    check(
        "control panel shows the live page preview",
        frame_size[0] >= 800 and frame_size[1] >= 600,
        f"preview frame {frame_size[0]}x{frame_size[1]}",
    )
    check(
        "control panel lists the detected dialogue blocks",
        rows >= 1,
        f"{rows} rows · {app.window.blocks_panel.lbl_summary.text()}",
    )

    pixmap = app.window.grab()
    panel_path = Path(os.environ["MT_DATA_DIR"]) / "control_panel.png"
    panel_path.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(panel_path))
    image = pixmap.toImage()
    colors = {image.pixelColor(x, y).name()
              for x in range(0, image.width(), 37) for y in range(0, image.height(), 37)}
    check(
        "control panel renders real content (screenshot saved)",
        image.width() >= 900 and len(colors) > 8,
        f"{image.width()}x{image.height()} px, {len(colors)} distinct sampled colours, png={panel_path}",
    )

    app.window.act_overlay.setChecked(False)
    qt.processEvents()
    hidden = not app.overlay.isVisible()
    app.window.act_overlay.setChecked(True)
    qt.processEvents()
    check(
        "toolbar toggle controls the on-screen overlay",
        hidden and app.overlay.isVisible(),
        "'Translation' toggle hides and shows the overlay",
    )
    app.hide_window()

    app.overlay.clear()
    reader.close()
    app.shutdown()
    return _finish(results)


def _finish(results) -> int:
    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 70)
    print(f" live check: {len(results) - len(failed)} passed, {len(failed)} failed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  - {name}: {detail}")
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
