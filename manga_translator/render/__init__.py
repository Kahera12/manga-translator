"""In-place page rendering: rewrite the page itself instead of overlaying cards."""

from .inplace import (
    DEFAULT_QUALITY,
    InplaceStyle,
    encode_inplace,
    font_for_text,
    render_inplace,
)

__all__ = [
    "InplaceStyle",
    "DEFAULT_QUALITY",
    "render_inplace",
    "encode_inplace",
    "font_for_text",
]
