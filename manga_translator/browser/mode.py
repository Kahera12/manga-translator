"""Browser mode controller: attach to a tab, keep the overlay in sync with it.

The reader's own page images are translated ahead of time by a background queue;
this controller maps their blocks onto the screen and pushes them to the same
overlay window the screen-region mode uses. No screenshot, and the GUI thread
never waits for OCR or the API.

    region (physical px)  =  browser client rect
    card position         =  panel position in the viewport × devicePixelRatio
                             + block position inside the panel × display scale
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import httpx
import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from ..cache.page_cache import PageCache
from ..core.config import Config
from ..core.logging_setup import get_logger
from ..core.models import Block, Rect, ScreenRegion
from ..glossary.glossary import Glossary
from ..ocr import create_engine
from ..translation import create_backend
from .cdp import CdpConnection, Tab
from .reader import (
    Panel,
    PageState,
    apply_overlay,
    clear_overlay,
    decode_image,
    fetch_panel_bytes,
    page_cards,
    read_state,
)
from .service import PanelQueue, PanelResult, PanelTranslator

log = get_logger("browser.mode")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/154 Safari/537.36")
WATCH_INTERVAL_MS = 400


@dataclass
class BrowserStatus:
    attached: bool = False
    tab_title: str = ""
    panels: int = 0
    visible: int = 0
    ready: int = 0
    translated: int = 0
    processed: int = 0
    failures: int = 0
    pending: int = 0
    last_error: str = ""

    def line(self) -> str:
        if not self.attached:
            return "Браузерный режим не подключён"
        return (
            f"{self.tab_title[:40]} · страниц {self.panels} · на экране {self.visible} · "
            f"готово {self.ready} · в очереди {self.pending}"
            + (f" · сбоев {self.failures}" if self.failures else "")
        )


class BrowserMode(QObject):
    """Qt-side controller (owns the CDP connection and the panel queue)."""

    pageReady = Signal(object, object, object)   # blocks, preview image (BGR), ScreenRegion
    statusChanged = Signal(str)          # human readable status line
    failed = Signal(str)                 # message for the notification
    cardsDrawn = Signal(int)             # number of cards painted in the page

    def __init__(self, config: Config, cache: PageCache, glossary: Glossary,
                 client_rect: Callable[[], Tuple[int, int, int, int]], parent=None):
        super().__init__(parent)
        self.cfg = config
        self.cache = cache
        self.glossary = glossary
        self._client_rect = client_rect
        self.conn: Optional[CdpConnection] = None
        self.tab: Optional[Tab] = None
        self.state = PageState()
        self.panels: List[Panel] = []
        self.results: Dict[str, PanelResult] = {}
        self.translator: Optional[PanelTranslator] = None
        self.queue: Optional[PanelQueue] = None
        self.info = BrowserStatus()
        self._images: Dict[str, np.ndarray] = {}
        self._http = httpx.Client(timeout=httpx.Timeout(20.0, read=40.0),
                                  headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self._engine = None
        self._backend = None
        self._active = False
        self._last_signature: Tuple = ()
        self._timer = QTimer(self)
        self._timer.setInterval(WATCH_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # -- lifecycle --------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    def attach(self, tab: Tab) -> bool:
        self.detach()
        try:
            self.conn = CdpConnection(tab.ws_url)
            self.tab = tab
            self.state = read_state(self.conn)
        except Exception as exc:
            log.warning("attach failed: %s", exc)
            self.conn = None
            self.failed.emit(f"Не удалось подключиться к вкладке: {exc}")
            return False

        if self._engine is None:
            self._engine, warning = create_engine(self.cfg.ocr)
            if warning:
                log.warning("%s", warning)
        if self._backend is None:
            self._backend, _warning = create_backend(self.cfg.translation, self.cfg.api_key())
        self.translator = PanelTranslator(self.cfg, self._engine, self._backend,
                                          self.cache, self.glossary)
        self.info = BrowserStatus(attached=True, tab_title=tab.title,
                                  panels=len(self.state.panels))
        log.info("attached to %r (%d panel images in the DOM)", tab.title[:60],
                 len(self.state.panels))
        self.statusChanged.emit(self.info.line())
        return True

    def start(self) -> None:
        if self.conn is None or self.translator is None:
            return
        self._active = True
        self._images.clear()
        if self.queue is None:
            self.queue = PanelQueue(self.translator, self.fetch_panel_image,
                                    self._on_panel_result, self._on_queue_progress)
        self.queue.start()
        self._last_signature = ()
        self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._active = False
        self._timer.stop()
        if self.queue is not None:
            self.queue.stop()
        if self.conn is not None:
            clear_overlay(self.conn)

    def detach(self) -> None:
        if self.conn is not None:
            clear_overlay(self.conn)
        self.stop()
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        self.tab = None
        self.results.clear()
        self._images.clear()
        self.info = BrowserStatus()
        self.statusChanged.emit(self.info.line())

    # -- fetching ---------------------------------------------------------
    def fetch_panel_image(self, panel: Panel) -> Optional[np.ndarray]:
        cached = self._images.get(panel.key)
        if cached is not None:
            return cached
        data: Optional[bytes] = None
        try:  # the tested CDN serves images directly (CORS: *)
            response = self._http.get(panel.src)
            if response.status_code == 200 and response.content:
                data = response.content
        except Exception as exc:
            log.debug("direct download failed (%s); retrying through the browser", exc)
        if data is None and self.conn is not None:
            data = fetch_panel_bytes(self.conn, panel.src)
        if data is None:
            return None
        try:
            image = decode_image(data)
        except Exception as exc:
            log.warning("panel decode failed: %s", exc)
            return None
        if len(self._images) > 60:
            self._images.clear()
        self._images[panel.key] = image
        return image

    # -- queue callbacks (worker thread) ----------------------------------
    def _on_panel_result(self, result: PanelResult) -> None:
        if result.error:
            self.info.failures += 1
            self.info.last_error = result.error
        elif result.blocks:
            self.results[result.key] = result
        self.info.processed += 1

    def _on_queue_progress(self, processed: int, failures: int) -> None:
        self.info.processed = processed
        self.info.failures = failures
        self.info.pending = self.queue.pending if self.queue else 0

    # -- overlay synchronisation (GUI thread) ------------------------------
    def _tick(self) -> None:
        if not self._active or self.conn is None:
            return
        try:
            self.state = read_state(self.conn)
        except Exception as exc:
            log.warning("reading the tab failed: %s", exc)
            self.failed.emit("Связь с браузером потеряна — подключитесь заново.")
            self.stop()
            return

        visible = self.state.visible()
        self.info.panels = len(self.state.panels)
        self.info.visible = len(visible)
        self.info.ready = len(self.results)
        self.info.pending = self.queue.pending if self.queue else 0

        reported = [
            (panel.key, round(panel.doc_y, 1)) for panel in self.panels
        ]
        signature = (round(self.state.scroll_y, 1), round(self.state.dpr, 3),
                     tuple(sorted(p.key for p in visible)), len(self.results))
        if signature == self._last_signature:
            self.statusChanged.emit(self.info.line())
            return
        self._last_signature = signature

        if self.queue is not None:
            self.queue.push_visible(visible)

        self.draw_cards(visible)
        self.statusChanged.emit(self.info.line())
        if reported:
            log.debug("visible panels: %s", reported[:6])

    def draw_cards(self, visible: Sequence[Panel]) -> int:
        """Paint the translated bubbles inside the page and refresh the preview."""
        if self.conn is None:
            return 0
        cards = page_cards(visible, self.results)
        drawn = 0
        try:
            drawn = apply_overlay(self.conn, cards)
        except Exception as exc:
            log.warning("could not draw the in-page overlay: %s", exc)
        self.info.translated = drawn
        self.cardsDrawn.emit(drawn)
        # preview: the first visible panel that has blocks, with its boxes
        blocks = []
        preview = None
        for panel in visible:
            result = self.results.get(panel.key)
            if result is None:
                continue
            if preview is None and panel.key in self._images:
                preview = self._images[panel.key]
            for block in result.blocks:
                if block.translation and not block.is_sfx:
                    blocks.append(block)
        if preview is None:
            for panel in visible:
                if panel.key in self._images:
                    preview = self._images[panel.key]
                    break
        self.pageReady.emit(blocks, preview, None)
        return drawn

    def screen_blocks(self, visible: Sequence[Panel]) -> Tuple[List[Block], Optional[ScreenRegion]]:
        """Map the visible panels' translated blocks into screen coordinates."""
        rect = self._client_rect() if callable(self._client_rect) else self._client_rect
        if not rect or rect[2] <= 0 or rect[3] <= 0:
            return [], None
        client_x, client_y, client_w, client_h = rect
        region = ScreenRegion(int(client_x), int(client_y), int(client_w), int(client_h),
                              monitor="browser", dpi_scale=1.0)
        dpr = self.state.dpr or 1.0
        blocks: List[Block] = []
        next_id = 1
        margin = 40.0
        for panel in visible:
            result = self.results.get(panel.key)
            if result is None:
                continue
            panel_x = (panel.doc_x - self.state.scroll_x) * dpr
            panel_y = (panel.doc_y - self.state.scroll_y) * dpr
            scale = (panel.view_w / panel.nat_w if panel.nat_w else 1.0) * dpr
            for block in result.blocks:
                if not block.translation or block.is_sfx:
                    continue
                box = Rect(
                    panel_x + block.bbox.x * scale,
                    panel_y + block.bbox.y * scale,
                    max(8.0, block.bbox.w * scale),
                    max(8.0, block.bbox.h * scale),
                )
                # ignore what is scrolled out of sight: a card pinned to the window
                # edge for an off-screen bubble only gets in the way
                if (
                    box.y2 < -margin
                    or box.y > client_h + margin
                    or box.x2 < -margin
                    or box.x > client_w + margin
                ):
                    continue
                blocks.append(
                    Block(id=next_id, text=block.text, bbox=box, spans=list(block.spans),
                          direction=block.direction, confidence=block.confidence,
                          is_sfx=block.is_sfx, translation=block.translation)
                )
                next_id += 1
        return blocks, region

    # -- chapter prefetch -------------------------------------------------
    def set_chapter(self, panels: Sequence[Panel]) -> None:
        self.panels = list(panels)
        self.info.panels = len(self.panels)
        if self.queue is not None:
            self.queue.push_all(self.panels)

    def invalidate(self) -> None:
        """Force a re-render on the next tick (e.g. after a manual request)."""
        self._last_signature = ()
        self._tick()

    def describe(self) -> Dict[str, object]:
        return {
            "attached": self.info.attached,
            "tab": self.info.tab_title,
            "panels": self.info.panels,
            "visible": self.info.visible,
            "ready": self.info.ready,
            "translated": self.info.translated,
            "pending": self.info.pending,
            "failures": self.info.failures,
            "line": self.info.line(),
        }
