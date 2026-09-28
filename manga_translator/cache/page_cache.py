"""Persistent page cache (SQLite) keyed by a perceptual hash (§20-§22).

A → B → A must come back instantly without OCR or a Gemini request, so the key is
a 64-bit pHash (Hamming distance <= threshold) rather than an exact binary hash.
Small differences from anti-aliasing, a 1-2 px scroll offset or the mouse cursor
therefore still hit the cache, while genuinely different pages stay apart.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
import numpy as np

from ..core.logging_setup import get_logger
from ..core.models import Block, Rect, TextSpan
from ..detection.image_similarity import hamming_hex, phash_hex
from ..core.paths import Paths, get_paths

log = get_logger("cache")

#: Bump when the reading/box logic changes so old cache rows stop matching.
#: 1 -> the original screen pipeline, 2 -> vision reading + guarded box snapping,
#: watermark filter and per-panel (exact) cache matching.
CACHE_LOGIC_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    phash        TEXT    NOT NULL,
    region_key   TEXT    NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    blocks       TEXT    NOT NULL,
    model        TEXT    DEFAULT '',
    preview      BLOB,
    bytes        INTEGER DEFAULT 0,
    created_at   REAL    NOT NULL,
    last_used    REAL    NOT NULL,
    hits         INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pages_region ON pages(region_key, last_used DESC);
CREATE INDEX IF NOT EXISTS idx_pages_phash  ON pages(phash);

-- Translation memory: the same bubble text is translated once, ever. This is
-- what makes scrolling/webtoons feel instant - only new lines cost an API call.
CREATE TABLE IF NOT EXISTS translations (
    source       TEXT    NOT NULL,
    target_lang  TEXT    NOT NULL,
    translation  TEXT    NOT NULL,
    model        TEXT    DEFAULT '',
    created_at   REAL    NOT NULL,
    last_used    REAL    NOT NULL,
    hits         INTEGER DEFAULT 0,
    PRIMARY KEY (source, target_lang)
);
CREATE INDEX IF NOT EXISTS idx_translations_used ON translations(last_used DESC);
"""


@dataclass
class CachedPage:
    page_hash: str
    region_key: str
    width: int
    height: int
    blocks: List[Block] = field(default_factory=list)
    model: str = ""
    created_at: float = 0.0
    last_used: float = 0.0
    hits: int = 0
    distance: int = 0
    preview: Optional[bytes] = None


def region_key(x: int, y: int, w: int, h: int) -> str:
    return f"{int(x)},{int(y)},{int(w)},{int(h)}"


def settings_fingerprint(config) -> str:
    """Short hash of the settings that change OCR/translation output.

    It is part of every cache key: after changing e.g. TRANSLATE_SFX or the
    confidence threshold, previously cached pages no longer match and are
    recomputed instead of serving stale results.

    ``CACHE_LOGIC_VERSION`` is bumped whenever the *reading* logic changes (box
    refinement, filters, vision prompt), because cached boxes from the old logic
    are then wrong even though the settings look identical.
    """
    import hashlib

    parts = [
        f"v{CACHE_LOGIC_VERSION}",
        config.ocr.engine,
        f"{config.ocr.min_confidence:.3f}",
        f"{config.ocr.translate_sfx}",
        f"{getattr(config.ocr, 'translate_sfx_in_browser', True)}",
        f"{config.ocr.sfx_min_glyph:.0f}",
        f"{config.ocr.sfx_median_factor:.2f}",
        f"{config.ocr.vertical_pass}",
        f"{config.ocr.drop_ui_chrome}",
        f"{config.ocr.suppress_static_text}",
        config.translation.target_language,
        config.translation.text_model,
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def block_to_dict(block: Block) -> dict:
    return {
        "id": block.id,
        "text": block.text,
        "translation": block.translation,
        "bbox": [block.bbox.x, block.bbox.y, block.bbox.w, block.bbox.h],
        "direction": block.direction,
        "confidence": round(float(block.confidence), 4),
        "is_sfx": bool(block.is_sfx),
        "spans": [
            {
                "text": s.text,
                "quad": list(s.quad),
                "confidence": round(float(s.confidence), 4),
                "angle": round(float(s.angle), 2),
            }
            for s in block.spans
        ],
    }


def block_from_dict(data: dict) -> Block:
    bbox = data.get("bbox") or [0, 0, 1, 1]
    spans = [
        TextSpan(
            text=str(s.get("text", "")),
            quad=tuple(float(v) for v in (s.get("quad") or (0,) * 8))[:8],
            confidence=float(s.get("confidence", 0.0)),
            angle=float(s.get("angle", 0.0)),
        )
        for s in data.get("spans", [])
    ]
    return Block(
        id=int(data.get("id", 0)),
        text=str(data.get("text", "")),
        bbox=Rect(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
        spans=spans,
        direction=str(data.get("direction", "horizontal")),
        confidence=float(data.get("confidence", 0.0)),
        is_sfx=bool(data.get("is_sfx", False)),
        translation=str(data.get("translation", "")),
    )


class PageCache:
    def __init__(
        self,
        paths: Optional[Paths] = None,
        max_mb: int = 1000,
        hamming_threshold: int = 6,
        max_age_days: int = 45,
        enabled: bool = True,
    ):
        self.paths = (paths or get_paths()).ensure()
        self.db_path: Path = self.paths.cache_db
        self.max_mb = max(8, int(max_mb))
        self.hamming_threshold = max(0, int(hamming_threshold))
        self.max_age_days = max(0, int(max_age_days))
        self.enabled = enabled
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._broken = False

    # -- connection -------------------------------------------------------
    def _connect(self) -> Optional[sqlite3.Connection]:
        if not self.enabled or self._broken:
            return None
        if self._conn is not None:
            return self._conn
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
            conn.executescript(SCHEMA)
            conn.commit()
            self._conn = conn
        except sqlite3.Error as exc:
            log.error("cache unavailable (%s); running without cache", exc)
            self._broken = True
            return None
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    # -- keys -------------------------------------------------------------
    @staticmethod
    def hash_frame(bgr: np.ndarray) -> str:
        import cv2

        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY) if bgr.ndim == 3 else bgr
        return phash_hex(gray)

    # -- lookup -----------------------------------------------------------
    def find(
        self,
        page_hash: str,
        region: Optional[str] = None,
        threshold: Optional[int] = None,
    ) -> Optional[CachedPage]:
        conn = self._connect()
        if conn is None:
            return None
        limit = self.hamming_threshold if threshold is None else threshold
        with self._lock:
            try:
                if region:
                    rows = conn.execute(
                        "SELECT phash, region_key, width, height, blocks, model, preview,"
                        " created_at, last_used, hits FROM pages WHERE region_key = ?"
                        " ORDER BY last_used DESC LIMIT 4000",
                        (region,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT phash, region_key, width, height, blocks, model, preview,"
                        " created_at, last_used, hits FROM pages ORDER BY last_used DESC LIMIT 4000"
                    ).fetchall()
            except sqlite3.Error as exc:
                log.error("cache lookup failed (%s); continuing without cache", exc)
                return None
        best: Optional[Tuple[int, tuple]] = None
        for row in rows:
            distance = hamming_hex(page_hash, row[0])
            if distance <= limit and (best is None or distance < best[0]):
                best = (distance, row)
                if distance == 0:
                    break
        if best is None:
            return None
        distance, row = best
        try:
            blocks = [block_from_dict(b) for b in json.loads(row[4])]
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            log.warning("cache row %s is corrupt (%s); ignoring", row[0], exc.__class__.__name__)
            return None
        page = CachedPage(
            page_hash=row[0],
            region_key=row[1],
            width=row[2],
            height=row[3],
            blocks=blocks,
            model=row[5] or "",
            preview=row[6],
            created_at=float(row[7] or 0.0),
            last_used=float(row[8] or 0.0),
            hits=int(row[9] or 0),
            distance=distance,
        )
        log.info("cache HIT (distance=%d, %d blocks, %.0f min old)",
                 distance, len(blocks), (time.time() - page.created_at) / 60.0)
        return page

    def touch(self, page_hash: str) -> None:
        conn = self._connect()
        if conn is None:
            return
        with self._lock:
            try:
                conn.execute(
                    "UPDATE pages SET hits = hits + 1, last_used = ? WHERE phash = ?",
                    (time.time(), page_hash),
                )
                conn.commit()
            except sqlite3.Error:
                pass

    # -- store ------------------------------------------------------------
    def store(
        self,
        page_hash: str,
        region: str,
        width: int,
        height: int,
        blocks: Sequence[Block],
        model: str = "",
        preview_bgr: Optional[np.ndarray] = None,
    ) -> None:
        conn = self._connect()
        if conn is None:
            return
        payload = json.dumps([block_to_dict(b) for b in blocks], ensure_ascii=False)
        blob = _encode_preview(preview_bgr) if preview_bgr is not None else None
        size = len(payload.encode("utf-8")) + (len(blob) if blob else 0)
        now = time.time()
        with self._lock:
            try:
                existing = conn.execute(
                    "SELECT id, phash FROM pages WHERE region_key = ? ORDER BY last_used DESC LIMIT 2000",
                    (region,),
                ).fetchall()
                duplicate_id = None
                for row_id, row_hash in existing:
                    if hamming_hex(page_hash, row_hash) <= self.hamming_threshold:
                        duplicate_id = row_id
                        break
                if duplicate_id is not None:
                    conn.execute(
                        "UPDATE pages SET blocks = ?, model = ?, preview = ?, bytes = ?,"
                        " last_used = ?, hits = hits + 1 WHERE id = ?",
                        (payload, model, blob, size, now, duplicate_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO pages (phash, region_key, width, height, blocks, model,"
                        " preview, bytes, created_at, last_used, hits)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                        (page_hash, region, width, height, payload, model, blob, size, now, now),
                    )
                conn.commit()
            except sqlite3.Error as exc:
                log.error("cache store failed: %s", exc)
                return
            self._prune(force=False)
            log.info("cache STORE %s (%d blocks, %.1f KB)", page_hash, len(blocks), size / 1024.0)

    # -- maintenance ------------------------------------------------------
    def _prune(self, force: bool = False) -> Dict[str, int]:
        conn = self._connect()
        if conn is None:
            return {"removed": 0}
        removed = 0
        with self._lock:
            try:
                if self.max_age_days:
                    cutoff = time.time() - self.max_age_days * 86400
                    cur = conn.execute("DELETE FROM pages WHERE last_used < ?", (cutoff,))
                    removed += cur.rowcount or 0
                total = conn.execute("SELECT COALESCE(SUM(bytes),0) FROM pages").fetchone()[0] or 0
                budget = self.max_mb * 1024 * 1024
                while total > budget:
                    row = conn.execute(
                        "SELECT id, bytes FROM pages ORDER BY last_used ASC LIMIT 1"
                    ).fetchone()
                    if row is None:
                        break
                    conn.execute("DELETE FROM pages WHERE id = ?", (row[0],))
                    total -= int(row[1] or 0)
                    removed += 1
                conn.commit()
                if removed:
                    log.info("cache pruned: %d entries removed", removed)
            except sqlite3.Error as exc:
                log.error("cache prune failed: %s", exc)
        return {"removed": removed}

    def clear(self) -> Dict[str, int]:
        conn = self._connect()
        if conn is None:
            # Nothing usable: recreate the file so "Clear cache" still works.
            try:
                if self.db_path.exists():
                    self.db_path.unlink()
                self._broken = False
            except OSError as exc:
                log.warning("could not delete cache file: %s", exc)
            return {"removed": 0}
        with self._lock:
            try:
                count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] or 0
                lines = conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0] or 0
                conn.execute("DELETE FROM pages")
                conn.execute("DELETE FROM translations")
                conn.commit()
                conn.execute("VACUUM")
                log.info("cache cleared (%d pages, %d translated lines)", count, lines)
                return {"removed": int(count), "lines": int(lines)}
            except sqlite3.Error as exc:
                log.error("cache clear failed: %s", exc)
                return {"removed": 0, "lines": 0}

    def stats(self) -> Dict[str, float]:
        conn = self._connect()
        if conn is None:
            return {"entries": 0, "bytes": 0, "hits": 0, "enabled": float(self.enabled)}
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(bytes),0), COALESCE(SUM(hits),0) FROM pages"
                ).fetchone()
            except sqlite3.Error:
                return {"entries": 0, "bytes": 0, "hits": 0, "enabled": float(self.enabled)}
        return {
            "entries": int(row[0] or 0),
            "bytes": int(row[1] or 0),
            "hits": int(row[2] or 0),
            "enabled": float(self.enabled),
        }

    def configure(self, enabled: bool, max_mb: int, hamming_threshold: int) -> None:
        with self._lock:
            self.enabled = enabled
            self.max_mb = max(8, int(max_mb))
            self.hamming_threshold = max(0, int(hamming_threshold))
            if not enabled:
                self.close()

    # -- translation memory ----------------------------------------------
    @staticmethod
    def normalise_text(text: str) -> str:
        """Key used for the translation memory (whitespace/punctuation stable)."""
        return " ".join((text or "").split())

    def get_translations(self, texts: Sequence[str], target_lang: str) -> Dict[str, str]:
        """Cached translations for the given source texts (missing ones omitted)."""
        conn = self._connect()
        if conn is None:
            return {}
        keys = [self.normalise_text(t) for t in texts]
        keys = [k for k in keys if k]
        if not keys:
            return {}
        out: Dict[str, str] = {}
        with self._lock:
            try:
                chunk_size = 400
                for start in range(0, len(keys), chunk_size):
                    chunk = keys[start : start + chunk_size]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = conn.execute(
                        f"SELECT source, translation FROM translations "
                        f"WHERE target_lang = ? AND source IN ({placeholders})",
                        (target_lang, *chunk),
                    ).fetchall()
                    for source, translation in rows:
                        out[source] = translation
                if out:
                    conn.execute(
                        f"UPDATE translations SET hits = hits + 1, last_used = ? "
                        f"WHERE target_lang = ? AND source IN "
                        f"({','.join('?' for _ in out)})",
                        (time.time(), target_lang, *out.keys()),
                    )
                    conn.commit()
            except sqlite3.Error as exc:
                log.debug("translation memory lookup failed: %s", exc)
                return {}
        if out:
            log.info("translation memory: %d/%d lines already translated", len(out), len(keys))
        return out

    def store_translations(
        self, pairs: Sequence[Tuple[str, str]], target_lang: str, model: str = ""
    ) -> None:
        conn = self._connect()
        if conn is None:
            return
        rows = [
            (self.normalise_text(source), target_lang, translation.strip(), model, time.time())
            for source, translation in pairs
            if self.normalise_text(source) and (translation or "").strip()
        ]
        if not rows:
            return
        with self._lock:
            try:
                conn.executemany(
                    "INSERT INTO translations (source, target_lang, translation, model,"
                    " created_at, last_used, hits) VALUES (?,?,?,?,?,?,0)"
                    " ON CONFLICT(source, target_lang) DO UPDATE SET"
                    " translation = excluded.translation, model = excluded.model,"
                    " last_used = excluded.last_used",
                    [(s, lang, tr, mdl, now, now) for s, lang, tr, mdl, now in rows],
                )
                conn.commit()
                log.debug("translation memory: stored %d lines", len(rows))
            except sqlite3.Error as exc:
                log.warning("could not store translations: %s", exc)

    def translation_memory_stats(self) -> Dict[str, int]:
        conn = self._connect()
        if conn is None:
            return {"lines": 0, "hits": 0}
        with self._lock:
            try:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(hits),0) FROM translations"
                ).fetchone()
            except sqlite3.Error:
                return {"lines": 0, "hits": 0}
        return {"lines": int(row[0] or 0), "hits": int(row[1] or 0)}


def _encode_preview(bgr: np.ndarray, max_dim: int = 480, quality: int = 70) -> Optional[bytes]:
    """Small JPEG kept for debugging / cache inspection (not for OCR)."""
    try:
        import cv2

        img = bgr
        h, w = img.shape[:2]
        if max(h, w) > max_dim:
            ratio = max_dim / float(max(h, w))
            img = cv2.resize(img, (max(1, int(w * ratio)), max(1, int(h * ratio))),
                             interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None
    except Exception:  # pragma: no cover - never break the pipeline for a preview
        return None
