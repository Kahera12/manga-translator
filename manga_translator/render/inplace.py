"""In-place page rendering: erase the source text, draw the Russian translation.

The overlay path shows translation cards *next to* the bubbles; this module
rewrites the page itself so the comic looks like it was lettered in Russian:

1. every block box is refined to the pixels that really are glyphs. Otsu splits
   the padded box into "ink" and "paper" and the minority side is taken as text,
   so an oval speech bubble keeps its white fill and only the letters vanish.
2. those pixels are erased with ``cv2.inpaint`` (Telea), which rebuilds the
   bubble fill / artwork from the surrounding pixels.
3. the translation is wrapped and drawn inside the same box, shrinking the font
   until it fits, clipping to the box and optionally adding a readability halo.

Rendering is defensive by design: one broken block is logged at debug level and
skipped, a bad block never aborts the whole page.
"""
from __future__ import annotations

import base64
import functools
import importlib
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..core.logging_setup import get_logger
from ..core.models import Block

log = get_logger("render.inplace")

#: JPEG quality used by callers that do not pass one to :func:`encode_inplace`.
DEFAULT_QUALITY = 88

#: Glyph pixels are grown by this much before inpainting (anti-aliased edges).
_GLYPH_GROW_PX = 2
#: A refined glyph mask is only trusted inside this share of the box area.
_MIN_GLYPH_FRACTION = 0.005
_MAX_GLYPH_FRACTION = 0.60
#: Font files probed in order (missing ones are simply skipped).
_FONT_CANDIDATES = ("segoeui.ttf", "segoeuib.ttf", "arial.ttf", "arialbd.ttf", "tahoma.ttf")
#: Defaults that `auto_text_color` is allowed to replace.
_DEFAULT_TEXT_COLOR = (16, 16, 20)
_DEFAULT_OUTLINE_COLOR = (255, 255, 255)
#: Relative paths of DejaVuSans inside the matplotlib / Pillow package dirs.
_DEJAVU_RELATIVE = (
    ("mpl-data", "fonts", "ttf", "DejaVuSans.ttf"),
    ("fonts", "DejaVuSans.ttf"),
    ("DejaVuSans.ttf",),
)
#: mime -> (cv2 extension, canonical mime)
_MIME_TABLE = {
    "image/jpeg": (".jpg", "image/jpeg"),
    "image/jpg": (".jpg", "image/jpeg"),
    "image/png": (".png", "image/png"),
    "image/webp": (".webp", "image/webp"),
    "image/bmp": (".bmp", "image/bmp"),
    "image/tiff": (".tiff", "image/tiff"),
}

_font_warning_logged = False


@dataclass
class InplaceStyle:
    """Knobs for :func:`render_inplace` (sane defaults for a 1x manga page)."""

    font_path: str = ""          # empty -> auto-pick a system font with Cyrillic
    min_font_px: int = 11
    max_font_px: int = 64
    padding: int = 3             # px of breathing room inside the text box
    text_color: tuple = (16, 16, 20)          # RGB
    outline_color: tuple | None = (255, 255, 255)  # thin halo for readability
    outline_px: int = 0
    # Pick dark/light text from the erased background instead of always using
    # `text_color`: black on dark artwork is unreadable.
    auto_text_color: bool = True
    line_spacing: float = 1.12
    align: str = "center"        # "center" | "left"
    inpaint_radius: int = 4
    mask_dilate_px: int = 3
    erode_mask_px: int = 0
    quality: int = 88            # JPEG quality used by encode_inplace


@dataclass(frozen=True)
class _Target:
    """One drawable block, already clipped to the image."""

    text: str
    is_sfx: bool                          # carried for future SFX styling
    mask_box: Tuple[int, int, int, int]   # padded, clipped (x1, y1, x2, y2)
    draw_box: Tuple[int, int, int, int]   # block bbox, clipped


# --------------------------------------------------------------------------- #
# fonts
# --------------------------------------------------------------------------- #
def _windows_font_dir() -> Optional[Path]:
    root = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if not root:
        return None
    candidate = Path(root) / "Fonts"
    return candidate if candidate.is_dir() else None


def _package_font_candidates() -> Iterator[str]:
    """DejaVuSans.ttf shipped inside matplotlib or Pillow, if either is here."""
    for module_name in ("matplotlib", "PIL"):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # pragma: no cover - optional dependency
            continue
        location = getattr(module, "__file__", "") or ""
        if not location:
            continue
        root = Path(location).parent
        for relative in _DEJAVU_RELATIVE:
            yield str(root.joinpath(*relative))


def _font_candidates(custom: str) -> Iterator[str]:
    if custom:
        yield custom
    font_dir = _windows_font_dir()
    if font_dir is not None:
        for name in _FONT_CANDIDATES:
            yield str(font_dir / name)
    yield from _package_font_candidates()


@functools.lru_cache(maxsize=128)
def _is_usable_font(path: str) -> bool:
    """True when ``path`` is a font file Pillow can open and use for Cyrillic."""
    try:
        if not path or not Path(path).is_file():
            return False
        font = ImageFont.truetype(path, 16)
        box = font.getbbox("Яж")
        return box is not None and (box[2] - box[0]) > 0
    except Exception:
        return False


@functools.lru_cache(maxsize=64)
def _resolve_font_path(custom: str) -> str:
    for candidate in _font_candidates(custom):
        if _is_usable_font(candidate):
            return candidate
    return ""


def font_for_text(text: str, style: Optional[InplaceStyle] = None) -> str:
    """Path of a font that can render ``text`` (Cyrillic), or ``""``.

    ``""`` means "no usable font file": callers must fall back to
    ``PIL.ImageFont.load_default()``.  This never raises and never returns a
    path that does not exist.  The resolved path is cached per ``font_path``.
    """
    active = style or InplaceStyle()
    path = _resolve_font_path(str(getattr(active, "font_path", "") or ""))
    if not path:
        _warn_missing_font()
    return path


def _warn_missing_font() -> None:
    global _font_warning_logged
    if _font_warning_logged:
        return
    _font_warning_logged = True
    log.warning("no Cyrillic TrueType font found; falling back to the PIL default font")


@functools.lru_cache(maxsize=256)
def _load_font(path: str, size: int) -> ImageFont.ImageFont:
    """Cached ``ImageFont`` for a resolved path (``""`` -> PIL default)."""
    size = max(1, int(size))
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # pragma: no cover - unreadable/corrupt font file
            log.debug("font %s could not be loaded at %dpx", path, size, exc_info=True)
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 9.2 scales the default
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


# --------------------------------------------------------------------------- #
# text measurement
# --------------------------------------------------------------------------- #
def _text_width(font: ImageFont.ImageFont, text: str, size: int) -> float:
    try:
        return float(font.getlength(text))
    except Exception:  # pragma: no cover - exotic font objects
        return float(len(text)) * max(1.0, float(size)) * 0.55


def _line_height(font: ImageFont.ImageFont, size: int, spacing: float) -> float:
    try:
        ascent, descent = font.getmetrics()
        base = float(ascent) + float(descent)
        if base <= 0:
            base = float(size) * 1.2
    except Exception:  # pragma: no cover - exotic font objects
        base = max(1.0, float(size)) * 1.2
    step = float(spacing) if isinstance(spacing, (int, float)) and spacing > 0 else 1.0
    if not math.isfinite(step) or step <= 0:
        step = 1.0
    return base * step


def _break_word(word: str, measure: Callable[[str], float], max_width: float) -> List[str]:
    """Hard-break a single over-wide word (Russian words are long)."""
    chunks: List[str] = []
    current = ""
    for char in word:
        trial = current + char
        if current and measure(trial) > max_width:
            chunks.append(current)
            current = char
        else:
            current = trial
    if current or not chunks:
        chunks.append(current)
    return chunks


def _wrap_text(text: str, measure: Callable[[str], float], max_width: float) -> List[str]:
    """Greedy word wrap; words wider than the box are broken by characters."""
    lines: List[str] = []
    for paragraph in str(text).split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            if measure(word) > max_width:
                if current:
                    lines.append(current)
                    current = ""
                chunks = _break_word(word, measure, max_width)
                lines.extend(chunks[:-1])
                current = chunks[-1] if chunks else ""
                continue
            trial = f"{current} {word}" if current else word
            if measure(trial) <= max_width:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        elif not lines:
            lines.append("")
    if not lines:
        lines = [""]
    return lines


def _lines_fit(
    lines: Sequence[str],
    font: ImageFont.ImageFont,
    size: int,
    style: InplaceStyle,
    avail_w: float,
    avail_h: float,
) -> bool:
    if not lines:
        return True
    if _line_height(font, size, style.line_spacing) * len(lines) > avail_h:
        return False
    return max(_text_width(font, line, size) for line in lines) <= avail_w


def _fit_text(
    text: str,
    font_path: str,
    style: InplaceStyle,
    avail_w: float,
    avail_h: float,
) -> Tuple[int, List[str], ImageFont.ImageFont]:
    """Largest size in [min_font_px, max_font_px] whose wrapped text fits.

    When even ``min_font_px`` overflows, the minimum size is returned and the
    caller clips the drawing to the box (better a cut word than no translation).
    """
    smallest = max(1, int(style.min_font_px))
    largest = max(smallest, int(style.max_font_px))
    best: Optional[Tuple[int, List[str], ImageFont.ImageFont]] = None
    low, high = smallest, largest
    while low <= high:
        size = (low + high) // 2
        font = _load_font(font_path, size)
        measure = lambda value, f=font, z=size: _text_width(f, value, z)  # noqa: E731
        lines = _wrap_text(text, measure, avail_w)
        if _lines_fit(lines, font, size, style, avail_w, avail_h):
            best = (size, lines, font)
            low = size + 1
        else:
            high = size - 1
    if best is None:
        font = _load_font(font_path, smallest)
        measure = lambda value, f=font, z=smallest: _text_width(f, value, z)  # noqa: E731
        best = (smallest, _wrap_text(text, measure, avail_w), font)
    return best


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def _clip_box(
    x1: float, y1: float, x2: float, y2: float, width: int, height: int
) -> Optional[Tuple[int, int, int, int]]:
    """Round a float box to integer pixels, clipped to the image; None if empty."""
    ix1 = int(max(0, min(width, math.floor(x1))))
    iy1 = int(max(0, min(height, math.floor(y1))))
    ix2 = int(max(0, min(width, math.ceil(x2))))
    iy2 = int(max(0, min(height, math.ceil(y2))))
    if ix2 <= ix1 or iy2 <= iy1:
        return None
    return ix1, iy1, ix2, iy2


def _rect_kernel(radius: int) -> np.ndarray:
    size = 2 * max(0, int(radius)) + 1
    return np.ones((size, size), dtype=np.uint8)


def _inpaint(image_bgr: np.ndarray, mask: np.ndarray, radius: int) -> np.ndarray:
    """Telea inpaint, restricted to the masked area's bounding box.

    ``cv2.inpaint`` costs the same for a 40x60 text box as for a 4K page, so the
    work is limited to the region that actually contains mask pixels (plus a
    margin, so the reconstruction still sees real neighbours).  The image itself
    is never resized.
    """
    height, width = image_bgr.shape[:2]
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return np.array(image_bgr, copy=True, order="C")
    margin = radius * 2 + 8
    y1 = int(max(0, rows[0] - margin))
    y2 = int(min(height, rows[-1] + 1 + margin))
    x1 = int(max(0, cols[0] - margin))
    x2 = int(min(width, cols[-1] + 1 + margin))
    if (x2 - x1) * (y2 - y1) >= height * width:
        return cv2.inpaint(image_bgr, mask, radius, cv2.INPAINT_TELEA)
    patch = image_bgr[y1:y2, x1:x2]
    result = np.array(image_bgr, copy=True, order="C")
    result[y1:y2, x1:x2] = cv2.inpaint(patch, mask[y1:y2, x1:x2], radius, cv2.INPAINT_TELEA)
    return result


def _rgba(color) -> Tuple[int, int, int, int]:
    try:
        values = tuple(int(max(0, min(255, round(float(component))))) for component in color)
    except (TypeError, ValueError):
        return 0, 0, 0, 255
    if len(values) >= 4:
        return values[0], values[1], values[2], values[3]
    if len(values) == 3:
        return values[0], values[1], values[2], 255
    return 0, 0, 0, 255


def _outline_offsets(radius: int) -> Tuple[Tuple[int, int], ...]:
    r = max(1, int(radius))
    return ((-r, 0), (r, 0), (0, -r), (0, r), (-r, -r), (r, -r), (-r, r), (r, r))


def _as_bgr_copy(image_bgr) -> np.ndarray:
    """Always a fresh, contiguous, 3-channel uint8 BGR array (never the input)."""
    array = np.asarray(image_bgr)
    if array.ndim not in (2, 3):
        raise ValueError(f"expected a 2-D or 3-D image array, got shape {array.shape}")
    if np.issubdtype(array.dtype, np.floating) and array.size:
        peak = float(np.nanmax(array)) if np.isfinite(array).any() else 0.0
        if peak <= 1.0:
            array = array * 255.0
    if array.dtype != np.uint8:
        array = np.clip(np.nan_to_num(array), 0, 255).astype(np.uint8)
    if array.ndim == 2:
        array = cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    elif array.shape[2] == 1:
        array = cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    elif array.shape[2] == 4:
        array = cv2.cvtColor(array, cv2.COLOR_BGRA2BGR)
    elif array.shape[2] != 3:
        raise ValueError(f"expected 1, 3 or 4 channels, got {array.shape[2]}")
    return np.array(array, copy=True, order="C")


# --------------------------------------------------------------------------- #
# masking
# --------------------------------------------------------------------------- #
def _glyph_mask(crop: np.ndarray) -> Optional[np.ndarray]:
    """Boolean mask of the *letters* inside a padded box, or None.

    Otsu splits the crop into two classes; the minority class is the text and
    the majority class is the (dominant) background the bubble is filled with.
    None means "no trustworthy refinement" -> the caller inpaints the plain box.
    """
    if crop.size < 4:
        return None
    _, binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = binary > 0
    ink_count = int(ink.sum())
    total = int(ink.size)
    if ink_count == 0 or ink_count == total:
        return None  # a flat box: Otsu found no split at all
    glyph = ink if ink_count * 2 <= total else ~ink
    fraction = int(glyph.sum()) / float(total)
    if fraction < _MIN_GLYPH_FRACTION or fraction > _MAX_GLYPH_FRACTION:
        return None
    return glyph


def _refine_box(
    gray: np.ndarray, box: Tuple[int, int, int, int]
) -> Optional[Tuple[Tuple[int, int, int, int], np.ndarray]]:
    """(box, local glyph mask) when the refinement is trustworthy, else None."""
    x1, y1, x2, y2 = box
    glyph = _glyph_mask(gray[y1:y2, x1:x2])
    if glyph is None:
        return None
    return box, glyph


def _build_inpaint_mask(
    image_bgr: np.ndarray, targets: Sequence[_Target], style: InplaceStyle
) -> np.ndarray:
    """Union of per-block masks: refined glyph pixels, or the plain box."""
    height, width = image_bgr.shape[:2]
    plain = np.zeros((height, width), dtype=np.uint8)
    glyphs = np.zeros((height, width), dtype=np.uint8)
    gray: Optional[np.ndarray] = None
    have_glyphs = False

    for target in targets:
        x1, y1, x2, y2 = target.mask_box
        try:
            if gray is None:
                gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
            refined = _refine_box(gray, target.mask_box)
        except Exception:  # pragma: no cover - defensive
            log.debug("glyph refinement failed, using the plain box", exc_info=True)
            refined = None
        if refined is None:
            plain[y1:y2, x1:x2] = 255
            continue
        _, glyph = refined
        glyphs[y1:y2, x1:x2][glyph] = 255
        have_glyphs = True

    if have_glyphs:
        # Grow the letters a little: anti-aliased edges survive the threshold.
        glyphs = np.maximum(glyphs, cv2.dilate(glyphs, _rect_kernel(_GLYPH_GROW_PX)))
    mask = np.maximum(plain, glyphs)

    dilate_px = max(0, int(style.mask_dilate_px))
    if dilate_px:
        mask = cv2.dilate(mask, _rect_kernel(dilate_px))
    erode_px = max(0, int(style.erode_mask_px))
    if erode_px and mask.any():
        eroded = cv2.erode(mask, _rect_kernel(erode_px))
        mask = eroded if eroded.any() else mask
    return mask


# --------------------------------------------------------------------------- #
# drawing
# --------------------------------------------------------------------------- #
def _draw_target(
    canvas: Image.Image,
    target: _Target,
    style: InplaceStyle,
    font_path: str,
    background_bgr: Optional[np.ndarray] = None,
) -> None:
    x1, y1, x2, y2 = target.draw_box
    box_w, box_h = x2 - x1, y2 - y1
    if box_w <= 0 or box_h <= 0:
        return

    padding = max(0, int(style.padding))
    avail_w = float(max(1, box_w - 2 * padding))
    avail_h = float(max(1, box_h - 2 * padding))
    size, lines, font = _fit_text(target.text, font_path, style, avail_w, avail_h)
    if not lines:
        return

    # Draw on a box-sized layer and paste it: the layer is its own clip region,
    # so nothing can bleed outside the bubble even when the text overflows.
    layer = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(layer)
    text_rgb, outline_rgb = _readable_colors(style, background_bgr, target.draw_box)
    fill = _rgba(text_rgb)
    outline = _rgba(outline_rgb) if outline_rgb is not None else None
    outline_px = max(0, int(style.outline_px))
    left_aligned = str(style.align or "").strip().lower() == "left"

    line_h = _line_height(font, size, style.line_spacing)
    y = (box_h - line_h * len(lines)) / 2.0
    for line in lines:
        if line:
            width = _text_width(font, line, size)
            x = float(padding) if left_aligned else (box_w - width) / 2.0
            if outline is not None and outline_px > 0:
                for dx, dy in _outline_offsets(outline_px):
                    ldraw.text((x + dx, y + dy), line, font=font, fill=outline)
            ldraw.text((x, y), line, font=font, fill=fill)
        y += line_h

    canvas.paste(layer, (x1, y1), layer)


def _readable_colors(style: InplaceStyle, background_bgr: Optional[np.ndarray],
                     box: Tuple[int, int, int, int]) -> Tuple[tuple, Optional[tuple]]:
    """Pick a text colour that can be read on the *erased* background.

    A webtoon page is not always a white bubble: sound effects sit on dark red or
    black artwork, and black letters there would be invisible. When
    ``style.auto_text_color`` is on, the median brightness inside the box decides
    between dark text with a light halo and light text with a dark halo.
    """
    if not getattr(style, "auto_text_color", True) or background_bgr is None:
        return style.text_color, style.outline_color
    # Explicit colours are a contract: only pick automatically when the caller left
    # the defaults in place.
    if tuple(style.text_color) != _DEFAULT_TEXT_COLOR or \
            (style.outline_color is not None
             and tuple(style.outline_color) != _DEFAULT_OUTLINE_COLOR):
        return style.text_color, style.outline_color
    x1, y1, x2, y2 = box
    patch = background_bgr[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
    if patch.size == 0:
        return style.text_color, style.outline_color
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY) if patch.ndim == 3 else patch
    brightness = float(np.median(gray))
    if brightness < 110.0:
        return (250, 250, 252), (12, 12, 16)
    return (16, 16, 20), (255, 255, 255)


def _collect_targets(blocks, width: int, height: int, padding: int) -> List[_Target]:
    """Readable blocks clipped to the image; degenerate ones are skipped."""
    targets: List[_Target] = []
    for index, block in enumerate(blocks or ()):
        try:
            text = str(getattr(block, "translation", "") or "").strip()
            if not text:
                continue  # nothing to draw (also covers "translation shorter than 1 char")
            bbox = getattr(block, "bbox", None)
            if bbox is None:
                log.debug("block %s skipped: no bbox", index)
                continue
            x = float(bbox.x)
            y = float(bbox.y)
            w = float(bbox.w)
            h = float(bbox.h)
            if not all(math.isfinite(value) for value in (x, y, w, h)) or w <= 0 or h <= 0:
                log.debug("block %s skipped: degenerate box %sx%s", index, w, h)
                continue
            draw_box = _clip_box(x, y, x + w, y + h, width, height)
            mask_box = _clip_box(
                x - padding, y - padding, x + w + padding, y + h + padding, width, height
            )
            if draw_box is None or mask_box is None:
                log.debug("block %s skipped: box outside the %dx%d image", index, width, height)
                continue
            targets.append(
                _Target(
                    text=text,
                    is_sfx=bool(getattr(block, "is_sfx", False)),
                    mask_box=mask_box,
                    draw_box=draw_box,
                )
            )
        except Exception:
            log.debug("block %s skipped: unreadable geometry", index, exc_info=True)
    return targets


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def render_inplace(
    image_bgr: np.ndarray,
    blocks: Sequence[Block],
    style: Optional[InplaceStyle] = None,
) -> np.ndarray:
    """Erase the original text of ``blocks`` and draw ``.translation`` instead.

    ``image_bgr`` is a BGR uint8 page (OpenCV convention); the caller's array is
    never modified -- a new BGR uint8 array of the same shape is returned.
    Blocks without a translation, with a zero-size box or fully outside the
    image are skipped (debug log), never raised.
    """
    active = style or InplaceStyle()
    work = _as_bgr_copy(image_bgr)
    height, width = work.shape[:2]

    targets = _collect_targets(blocks, width, height, max(0, int(active.padding)))
    if not targets:
        log.debug("inplace render: no drawable block")
        return work

    mask = _build_inpaint_mask(work, targets, active)
    if mask.any():
        radius = max(1, int(active.inpaint_radius))
        try:
            work = _inpaint(work, mask, radius)
        except Exception:
            log.debug("inpainting failed; drawing on the untouched page", exc_info=True)

    font_path = font_for_text(" ".join(target.text for target in targets), active)
    rgb = cv2.cvtColor(work, cv2.COLOR_BGR2RGB)
    canvas = Image.fromarray(rgb)
    for index, target in enumerate(targets):
        try:
            _draw_target(canvas, target, active, font_path, background_bgr=work)
        except Exception:
            log.debug("block %s could not be drawn", index, exc_info=True)
    return cv2.cvtColor(np.array(canvas), cv2.COLOR_RGB2BGR)


def encode_inplace(
    image_bgr: np.ndarray,
    mime: str = "image/jpeg",
    quality: int = DEFAULT_QUALITY,
) -> Tuple[str, str]:
    """Encode a page for the extension: ``(mime, base64 ascii)``.

    No ``data:`` prefix -- the extension turns the payload into a Blob itself.
    ``InplaceStyle.quality`` is the value typically passed here.
    """
    extension, out_mime = _MIME_TABLE.get(str(mime).strip().lower(), (".jpg", "image/jpeg"))
    level = int(quality) if isinstance(quality, (int, float)) else DEFAULT_QUALITY
    level = int(max(1, min(100, level)))
    params: List[int] = []
    if extension == ".jpg":
        params = [int(cv2.IMWRITE_JPEG_QUALITY), level]
    elif extension == ".webp":
        params = [int(cv2.IMWRITE_WEBP_QUALITY), level]

    image = _as_bgr_copy(image_bgr)
    ok, buffer = cv2.imencode(extension, image, params)
    if not ok:
        raise RuntimeError(f"could not encode the page as {out_mime}")
    return out_mime, base64.b64encode(buffer.tobytes()).decode("ascii")


__all__ = [
    "InplaceStyle",
    "DEFAULT_QUALITY",
    "encode_inplace",
    "font_for_text",
    "render_inplace",
]
