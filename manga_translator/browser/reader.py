"""Read a comic/reader page through the browser instead of the screen.

The DOM gives us what a screenshot cannot: the *original* page images (full
resolution, no site UI, no scaling artefacts) and their exact position, so the
translator can work on clean artwork and place every card precisely on its
bubble.

Everything here is pure data collection; OCR/translation lives in `service.py`.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..core.logging_setup import get_logger
from .cdp import CdpConnection, CdpError

log = get_logger("browser.reader")

# Images big enough to be a comic page rather than an icon/avatar.
MIN_PANEL_SIDE = 500

JS_COLLECT = """
(() => {
  const minSide = __MT_MIN_SIDE__;
  const out = [];
  let index = 0;
  for (const img of document.images) {
    const nw = img.naturalWidth || 0, nh = img.naturalHeight || 0;
    if (nw < minSide || nh < minSide) continue;
    const style = getComputedStyle(img);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') === 0)
      continue;
    const r = img.getBoundingClientRect();
    const src = img.currentSrc || img.src || '';
    if (!src) continue;
    out.push({
      index: index++,
      src: src,
      view_x: r.x, view_y: r.y, view_w: r.width, view_h: r.height,
      doc_x: r.x + window.scrollX, doc_y: r.y + window.scrollY,
      nat_w: nw, nat_h: nh,
      complete: !!img.complete,
    });
  }
  return JSON.stringify({
    panels: out,
    dpr: window.devicePixelRatio || 1,
    scroll_x: window.scrollX || 0,
    scroll_y: window.scrollY || 0,
    viewport_w: window.innerWidth, viewport_h: window.innerHeight,
    doc_h: document.documentElement.scrollHeight,
    url: location.href,
    title: document.title,
  });
})()
"""

JS_SCROLL_TO = "window.scrollTo(__MT_X__, __MT_Y__); true;"

# Cards are injected *into the page*: their position is then exact by
# construction (document coordinates, no window offset, no DPI maths) and they
# scroll with the artwork. `translateY(-100%)` puts a card just above its bubble
# without the app having to know the card's height.
JS_OVERLAY = """
(() => {
  const payload = __MT_PAYLOAD__;
  const root_id = '__mt_overlay';
  let root = document.getElementById(root_id);
  if (!root) {
    root = document.createElement('div');
    root.id = root_id;
    root.style.cssText = 'position:absolute;top:0;left:0;width:0;height:0;'
      + 'z-index:2147483000;pointer-events:none;';
    (document.body || document.documentElement).appendChild(root);
  }
  while (root.firstChild) root.removeChild(root.firstChild);
  for (const item of payload) {
    const card = document.createElement('div');
    card.textContent = item.text;
    card.setAttribute('data-mt-block', String(item.id));
    card.style.cssText =
      'position:absolute;left:' + item.x + 'px;top:' + item.y + 'px;'
      + 'max-width:' + item.w + 'px;min-width:60px;'
      + (item.above ? 'transform:translateY(-100%) translateY(-6px);' : '')
      + 'background:rgba(9,14,24,0.88);color:#f8fafc;'
      + 'font:600 14px/1.3 "Segoe UI",Roboto,sans-serif;'
      + 'padding:6px 8px;border-radius:7px;'
      + 'border:1px solid rgba(125,211,252,0.75);'
      + 'box-shadow:0 3px 10px rgba(0,0,0,0.5);'
      + 'white-space:pre-wrap;word-break:break-word;text-align:left;'
      + 'pointer-events:none;';
    root.appendChild(card);
  }
  return payload.length;
})()
"""

JS_CLEAR_OVERLAY = """
(() => {
  const root = document.getElementById('__mt_overlay');
  if (root) root.remove();
  return true;
})()
"""

# Images can be several MB; base64 keeps the transfer simple and survives CORS.
JS_FETCH = """
(async () => {
  const response = await fetch(__MT_SRC__, {credentials: 'include'});
  if (!response.ok) return JSON.stringify({error: 'HTTP ' + response.status});
  const buffer = await response.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return JSON.stringify({data: btoa(binary), size: bytes.length});
})()
"""


@dataclass
class Panel:
    """One page image of the reader."""

    index: int
    src: str
    doc_x: float
    doc_y: float
    view_x: float
    view_y: float
    view_w: float
    view_h: float
    nat_w: int
    nat_h: int
    complete: bool = True

    @property
    def key(self) -> str:
        """Stable identity for the cache (the image URL without its signature)."""
        return self.src.split("?")[0]

    @property
    def display_scale(self) -> float:
        return (self.view_w / self.nat_w) if self.nat_w else 1.0

    def view_rect(self, scroll_x: float, scroll_y: float) -> Tuple[float, float, float, float]:
        """Where this panel currently is in the viewport (CSS pixels)."""
        return (self.doc_x - scroll_x, self.doc_y - scroll_y, self.view_w, self.view_h)


@dataclass
class PageState:
    panels: List[Panel] = field(default_factory=list)
    dpr: float = 1.0
    scroll_x: float = 0.0
    scroll_y: float = 0.0
    viewport_w: float = 0.0
    viewport_h: float = 0.0
    doc_h: float = 0.0
    url: str = ""
    title: str = ""

    def visible(self, margin: float = 120.0) -> List[Panel]:
        """Panels intersecting the viewport (plus a margin above/below)."""
        out = []
        for panel in self.panels:
            x, y, w, h = panel.view_rect(self.scroll_x, self.scroll_y)
            if y + h < -margin or y > self.viewport_h + margin:
                continue
            if x + w < -margin or x > self.viewport_w + margin:
                continue
            out.append(panel)
        return out


def read_state(conn: CdpConnection, min_side: int = MIN_PANEL_SIDE) -> PageState:
    """Current panels + scroll/viewport of the attached tab."""
    raw = conn.evaluate(JS_COLLECT.replace("__MT_MIN_SIDE__", str(int(min_side))))
    if not raw:
        return PageState()
    import json

    data = json.loads(raw)
    panels = [
        Panel(
            index=int(item.get("index", 0)),
            src=str(item.get("src", "")),
            doc_x=float(item.get("doc_x", 0.0)),
            doc_y=float(item.get("doc_y", 0.0)),
            view_x=float(item.get("view_x", 0.0)),
            view_y=float(item.get("view_y", 0.0)),
            view_w=float(item.get("view_w", 0.0)),
            view_h=float(item.get("view_h", 0.0)),
            nat_w=int(item.get("nat_w", 0)),
            nat_h=int(item.get("nat_h", 0)),
            complete=bool(item.get("complete", True)),
        )
        for item in data.get("panels", [])
    ]
    return PageState(
        panels=panels,
        dpr=float(data.get("dpr", 1.0) or 1.0),
        scroll_x=float(data.get("scroll_x", 0.0)),
        scroll_y=float(data.get("scroll_y", 0.0)),
        viewport_w=float(data.get("viewport_w", 0.0)),
        viewport_h=float(data.get("viewport_h", 0.0)),
        doc_h=float(data.get("doc_h", 0.0)),
        url=str(data.get("url", "")),
        title=str(data.get("title", "")),
    )


def collect_chapter(
    conn: CdpConnection,
    step_ratio: float = 0.85,
    max_steps: int = 400,
    settle_s: float = 0.35,
    progress=None,
) -> List[Panel]:
    """Scroll through the whole page to load every panel, then restore the view.

    Readers lazy-load images as they approach the viewport, so walking the
    document is the reliable way to enumerate a chapter. The original scroll
    position is restored at the end.
    """
    original = conn.evaluate("JSON.stringify({x: window.scrollX, y: window.scrollY})")
    import json

    try:
        start = json.loads(original or "{}")
    except ValueError:
        start = {}
    known: Dict[str, Panel] = {}
    state = read_state(conn)
    for panel in state.panels:
        known.setdefault(panel.key, panel)

    step = max(240, int(state.viewport_h * step_ratio)) if state.viewport_h else 800
    doc_h = state.doc_h or 0
    y = 0
    steps = 0
    while steps < max_steps:
        steps += 1
        conn.evaluate(JS_SCROLL_TO.replace("__MT_X__", "0").replace("__MT_Y__", str(int(y))))
        time.sleep(settle_s)
        state = read_state(conn)
        for panel in state.panels:
            known.setdefault(panel.key, panel)
        doc_h = max(doc_h, state.doc_h)
        if progress is not None:
            progress(steps, y, doc_h, len(known))
        y += step
        if y > doc_h + step:
            break

    conn.evaluate(
        JS_SCROLL_TO.replace("__MT_X__", str(int(start.get("x", 0))))
        .replace("__MT_Y__", str(int(start.get("y", 0))))
    )
    time.sleep(0.3)
    panels = sorted(known.values(), key=lambda p: (round(p.doc_y, 1), p.doc_x))
    for position, panel in enumerate(panels):
        panel.index = position
    log.info("chapter contains %d panel(s) (%d px tall)", len(panels), int(doc_h))
    return panels


def fetch_panel_bytes(conn: CdpConnection, src: str, timeout_s: float = 60.0) -> Optional[bytes]:
    """Download an image *through the browser* (cookies/referer/hotlink protection)."""
    import json

    expression = JS_FETCH.replace("__MT_SRC__", json.dumps(src))
    try:
        raw = conn.evaluate(expression, await_promise=True, timeout=timeout_s)
    except CdpError as exc:
        log.warning("in-browser fetch failed: %s", exc)
        return None
    if not raw:
        return None
    payload = json.loads(raw)
    if payload.get("error"):
        log.warning("in-browser fetch returned %s for %s", payload["error"], src[:80])
        return None
    data = payload.get("data")
    if not data:
        return None
    try:
        return base64.b64decode(data)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not decode panel image: %s", exc)
        return None


def decode_image(data: bytes):
    """Bytes -> BGR numpy array (via PIL + numpy, both already required)."""
    import io

    import numpy as np
    from PIL import Image

    image = Image.open(io.BytesIO(data)).convert("RGB")
    return np.asarray(image)[:, :, ::-1].copy()


def page_cards(panels: Sequence["Panel"], results: Dict[str, object],
               min_width: float = 120.0, max_width: float = 420.0) -> List[dict]:
    """Translated blocks -> absolutely positioned cards in *document* coordinates.

    ``results`` maps a panel key to an object with ``.blocks`` (image coordinates).
    """

    cards: List[dict] = []
    for panel in panels:
        result = results.get(panel.key)
        if result is None:
            continue
        scale = (panel.view_w / panel.nat_w) if panel.nat_w else 1.0
        for block in getattr(result, "blocks", []):
            if not block.translation or block.is_sfx:
                continue
            width = min(max_width, max(min_width, block.bbox.w * scale * 1.5))
            doc_x = panel.doc_x + block.bbox.x * scale
            doc_y = panel.doc_y + block.bbox.y * scale
            # a little above the bubble unless that would leave the document
            above = doc_y > 90
            cards.append({
                "id": block.id,
                "text": block.translation,
                "x": round(doc_x - max(0.0, (width - block.bbox.w * scale) / 2.0), 1),
                "y": round(doc_y, 1),
                "w": round(width, 1),
                "above": above,
                "source": block.text,
            })
    return cards


def apply_overlay(conn: CdpConnection, cards: Sequence[dict]) -> int:
    """Draw (or refresh) the translation cards inside the page."""
    import json

    payload = json.dumps(list(cards), ensure_ascii=False)
    script = JS_OVERLAY.replace("__MT_PAYLOAD__", payload)
    return int(conn.evaluate(script) or 0)


def clear_overlay(conn: CdpConnection) -> None:
    try:
        conn.evaluate(JS_CLEAR_OVERLAY)
    except Exception as exc:  # pragma: no cover - tab may be gone
        log.debug("could not clear the in-page overlay: %s", exc)
