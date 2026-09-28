"""Page cache: perceptual identity, A→B→A reuse, size cap, clear (§20-§22)."""
from __future__ import annotations

import sqlite3

import numpy as np

from conftest import page_one, page_two
from manga_translator.cache.page_cache import PageCache, block_from_dict, block_to_dict, region_key
from manga_translator.core.models import Block, Rect, TextSpan


def make_blocks(texts=("你好", "再见")) -> list:
    blocks = []
    for index, text in enumerate(texts, start=1):
        quad = (10.0, 10.0 * index, 120.0, 10.0 * index, 120.0, 40.0 * index, 10.0, 40.0 * index)
        blocks.append(
            Block(
                id=index,
                text=text,
                bbox=Rect(10, 10 * index, 110, 30),
                spans=[TextSpan(text=text, quad=quad, confidence=0.93)],
                direction="horizontal",
                confidence=0.93,
                translation=f"RU {text}",
            )
        )
    return blocks


def test_block_round_trip():
    blocks = make_blocks()
    restored = [block_from_dict(block_to_dict(b)) for b in blocks]
    assert restored[0].text == blocks[0].text
    assert restored[0].translation == blocks[0].translation
    assert restored[0].bbox == blocks[0].bbox
    assert restored[0].spans[0].confidence == blocks[0].spans[0].confidence
    assert restored[0].spans[0].quad == blocks[0].spans[0].quad


def test_store_and_exact_lookup(tmp_paths):
    cache = PageCache(tmp_paths, max_mb=64, hamming_threshold=6)
    page = page_one()
    digest = PageCache.hash_frame(page)
    cache.store(digest, region_key(0, 0, 1200, 900), 1200, 900, make_blocks(), model="test-model",
                preview_bgr=page)
    found = cache.find(digest, region_key(0, 0, 1200, 900))
    assert found is not None
    assert found.distance == 0
    assert len(found.blocks) == 2
    assert found.model == "test-model"
    assert found.preview and len(found.preview) > 100
    assert cache.stats()["entries"] == 1


def test_fuzzy_lookup_accepts_tiny_differences(tmp_paths):
    cache = PageCache(tmp_paths, hamming_threshold=6)
    page = page_one()
    cache.store(PageCache.hash_frame(page), "reg", 1200, 900, make_blocks())
    nudged = np.roll(page, 2, axis=1)  # like a 2 px scroll offset
    found = cache.find(PageCache.hash_frame(nudged), "reg")
    assert found is not None
    assert found.distance <= 6
    assert cache.find(PageCache.hash_frame(nudged), "other-region") is None


def test_different_pages_do_not_collide(tmp_paths):
    cache = PageCache(tmp_paths, hamming_threshold=6)
    cache.store(PageCache.hash_frame(page_one()), "reg", 1200, 900, make_blocks(["一"]))
    assert cache.find(PageCache.hash_frame(page_two()), "reg") is None


def test_navigating_back_to_a_previous_page_hits_the_cache(tmp_paths):
    """Scenario C: A -> B -> A."""
    cache = PageCache(tmp_paths, hamming_threshold=6)
    a, b = page_one(), page_two()
    ha, hb = PageCache.hash_frame(a), PageCache.hash_frame(b)
    cache.store(ha, "reg", 1200, 900, make_blocks(["甲"]))
    cache.store(hb, "reg", 1200, 900, make_blocks(["乙"]))
    again = cache.find(ha, "reg")
    assert again is not None
    assert again.blocks[0].text == "甲"
    assert cache.stats()["entries"] == 2


def test_restoring_the_same_page_updates_instead_of_duplicating(tmp_paths):
    cache = PageCache(tmp_paths, hamming_threshold=6)
    digest = PageCache.hash_frame(page_one())
    cache.store(digest, "reg", 1200, 900, make_blocks(["甲"]))
    cache.store(digest, "reg", 1200, 900, make_blocks(["甲", "乙"]))
    assert cache.stats()["entries"] == 1
    assert len(cache.find(digest, "reg").blocks) == 2


def test_size_cap_prunes_oldest_entries(tmp_paths):
    cache = PageCache(tmp_paths, max_mb=8)  # 8 MB minimum
    for index in range(6):
        page = np.full((400, 400, 3), 255, dtype="uint8")
        page[10:30, 10 + index * 20 : 30 + index * 20] = 0
        cache.store(PageCache.hash_frame(page), "reg", 400, 400, make_blocks(), preview_bgr=page)
    # Force a tiny budget by hand to exercise the eviction loop.
    cache.max_mb = 0
    cache._prune(force=True)
    assert cache.stats()["entries"] == 0


def test_clear_removes_everything(tmp_paths):
    cache = PageCache(tmp_paths)
    cache.store(PageCache.hash_frame(page_one()), "reg", 1200, 900, make_blocks())
    result = cache.clear()
    assert result["removed"] == 1
    assert cache.stats()["entries"] == 0


def test_corrupt_rows_are_ignored_not_fatal(tmp_paths):
    cache = PageCache(tmp_paths)
    digest = PageCache.hash_frame(page_one())
    cache.store(digest, "reg", 1200, 900, make_blocks())
    conn = sqlite3.connect(tmp_paths.cache_db)
    conn.execute("UPDATE pages SET blocks = ?", ("{not json",))
    conn.commit()
    conn.close()
    cache.close()
    reopened = PageCache(tmp_paths)
    assert reopened.find(digest, "reg") is None  # no exception, no crash


def test_broken_cache_file_is_survivable(tmp_paths):
    tmp_paths.cache_db.parent.mkdir(parents=True, exist_ok=True)
    tmp_paths.cache_db.write_text("not a database at all", encoding="utf-8")
    cache = PageCache(tmp_paths)
    assert cache.find("0000000000000000", "reg") is None
    assert cache.stats()["entries"] == 0
    cache.clear()
    assert cache.stats()["entries"] == 0


def test_disabled_cache_never_touches_disk(tmp_paths):
    cache = PageCache(tmp_paths, enabled=False)
    digest = PageCache.hash_frame(page_one())
    cache.store(digest, "reg", 1200, 900, make_blocks())
    assert cache.find(digest, "reg") is None
    assert not tmp_paths.cache_db.exists()
