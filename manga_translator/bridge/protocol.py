"""Wire protocol between the browser extension and the local application.

The extension sends *what is on the page* (image URLs and geometry); the app
answers with translations in **image coordinates** so the page can place them
itself. All messages are JSON objects with a ``type`` field.

Extension -> app
    hello        {version, url, title}
    register     {tab, page, panels: [{key, url, width, height, order}]}
    request      {keys: [key, ...], mode: "cards"|"inplace", priority: bool}
    cancel       {keys: [...]}
    status       {}

App -> extension
    welcome      {version, provider, model, target_language, batch_size}
    panel        {key, image_w, image_h, blocks: [...], inplace: {mime, data}}
    progress     {done, total, queued, failed, current}
    error        {message, kind}
    status       {attached, provider, model, queue, memory}

A *block* is::

    {"id": "12:3", "x": 120, "y": 340, "w": 220, "h": 70,
     "text": "原文", "translation": "Перевод", "vertical": false,
     "confidence": 0.97, "sfx": false}
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1
DEFAULT_PORT = 8770


@dataclass
class PanelSpec:
    """One page image as the page sees it."""

    key: str                    # stable identity: content hash (preferred) or URL
    url: str
    width: int = 0              # natural size (pixels of the image file)
    height: int = 0
    order: int = 0              # position in the chapter
    doc_x: float = 0.0          # not used for placement anymore, kept for diagnostics
    doc_y: float = 0.0
    visible: bool = False       # intersects the viewport -> translate first

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "PanelSpec":
        return PanelSpec(
            key=str(data.get("key") or data.get("url") or ""),
            url=str(data.get("url") or ""),
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            order=int(data.get("order") or 0),
            doc_x=float(data.get("doc_x") or 0.0),
            doc_y=float(data.get("doc_y") or 0.0),
            visible=bool(data.get("visible")),
        )


@dataclass
class BlockPayload:
    """A translated dialogue block in *image* coordinates."""

    id: str
    x: float
    y: float
    w: float
    h: float
    text: str = ""
    translation: str = ""
    vertical: bool = False
    confidence: float = 0.0
    sfx: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PanelPayload:
    key: str
    image_w: int
    image_h: int
    blocks: List[BlockPayload] = field(default_factory=list)
    from_cache: bool = False
    inplace: Optional[Dict[str, Any]] = None   # {"mime": "image/webp", "data": "<base64>"}
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "image_w": self.image_w,
            "image_h": self.image_h,
            "blocks": [b.to_dict() for b in self.blocks],
            "from_cache": self.from_cache,
            "inplace": self.inplace,
            "error": self.error,
        }


def encode(message_type: str, **payload: Any) -> str:
    return json.dumps({"type": message_type, **payload}, ensure_ascii=False)


def decode(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"type": "invalid"}
    if not isinstance(data, dict) or "type" not in data:
        return {"type": "invalid"}
    return data
