"""Vision path: let a multimodal model read the pages (text + boxes + Russian)."""
from .reader import VisionPage, VisionReader, VisionUnavailable, encode_panel, parse_pages
from .snap import ink_mask, snap_box, snap_boxes

__all__ = [
    "VisionPage",
    "VisionReader",
    "VisionUnavailable",
    "encode_panel",
    "ink_mask",
    "parse_pages",
    "snap_box",
    "snap_boxes",
]
