"""Offline self-test harness: verifies every pipeline stage on this machine.

    python main.py --selftest
    python tools/selftest.py

Runs against synthetic Chinese manga pages (no GUI interaction, no API key) and
prints a PASS/FAIL table plus the artifacts it produced. The real OCR engine,
the real capture stack and the real overlay painting code are exercised; only
the LLM backend is replaced by the offline stub (or the real API when a key is
present and the network is reachable).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    artifacts: List[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", skipped: bool = False) -> None:
        self.checks.append(Check(name, bool(ok), detail, skipped))

    @property
    def failed(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and not c.skipped]


class _StubGemini:
    """Minimal stand-in for the Gemini endpoint (request-format verification)."""

    def __init__(self):
        self.requests = []
        self.headers = []
        self.get_requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                outer.requests.append((self.path, body))
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                rows = [
                    {"id": item["id"], "translation": f"RU:{item['text']}"}
                    for item in _items_from_prompt(body)
                ]
                # Mirror the real Gemini envelope: the JSON answer lives in
                # candidates[0].content.parts[0].text
                payload = json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"text": json.dumps({"translations": rows}, ensure_ascii=False)}
                                    ]
                                },
                                "finishReason": "STOP",
                            }
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                """Model listing for the OpenAI-compatible and Gemini shapes."""
                path = self.path.split("?")[0]
                outer.get_requests.append(path)
                if path.endswith("/v1/models") or path.endswith("/models") and "v1beta" not in path:
                    payload = json.dumps(
                        {"object": "list", "data": [{"id": "stub-model-small"}, {"id": "stub-model-large"}]}
                    ).encode("utf-8")
                elif "v1beta/models" in path:
                    payload = json.dumps(
                        {"models": [
                            {"name": "models/gemini-3.8-flash",
                             "supportedGenerationMethods": ["generateContent"]},
                            {"name": "models/text-embedding-004",
                             "supportedGenerationMethods": ["embedContent"]},
                        ]}
                    ).encode("utf-8")
                else:
                    payload = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _items_from_prompt(body: dict) -> List[dict]:
    """Recover the block list the backend put into the prompt (for echoing back)."""
    import re

    text = body["contents"][0]["parts"][0]["text"]
    match = re.search(r'\[\{"id":.*?\}\]', text, re.DOTALL)
    if not match:
        return [{"id": 1, "text": "?"}]
    try:
        return json.loads(match.group(0))
    except ValueError:
        return [{"id": 1, "text": "?"}]


def run_selftest(verbose: bool = True) -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="mt-selftest-"))
    os.environ["MT_DATA_DIR"] = str(tmp / "data")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["TRANSLATION_BACKEND"] = "fake"
    os.environ["CACHE_ENABLED"] = "true"

    report = Report()
    _check_environment(report, tmp)
    _check_ocr(report, tmp)
    _check_providers(report, tmp)
    _check_pipeline(report, tmp)
    _check_capture(report, tmp)
    _check_gemini(report, tmp)
    _check_overlay(report, tmp)
    _check_control_panel(report, tmp)
    _check_stale(report, tmp)

    _print_report(report, verbose, tmp)
    return not report.failed


# --- checks ----------------------------------------------------------------


def _check_environment(report: Report, tmp: Path) -> None:
    try:
        import mss  # noqa: F401
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import PySide6  # noqa: F401
        import rapidocr_onnxruntime  # noqa: F401
        import httpx  # noqa: F401

        report.add("dependencies", True, "mss, opencv, numpy, PySide6, rapidocr, httpx")
    except Exception as exc:
        report.add("dependencies", False, f"{exc.__class__.__name__}: {exc}")

    from manga_translator.core import dpi

    try:
        mode = dpi.enable_per_monitor_dpi_awareness()
        monitors = dpi.list_monitors()
        detail = f"{mode}; " + ", ".join(
            f"{m['name']} {m['physical'].w:.0f}x{m['physical'].h:.0f}" for m in monitors
        )
        report.add("dpi / monitors", bool(monitors) or sys.platform != "win32", detail)
    except Exception as exc:
        report.add("dpi / monitors", False, f"{exc.__class__.__name__}: {exc}")

    from manga_translator.capture.synthetic import find_cjk_font, make_manga_page

    font = find_cjk_font()
    try:
        page = make_manga_page(seed=1)
        report.add("synthetic CJK page", font is not None and page.shape[0] > 100, f"font={font}")
    except Exception as exc:
        report.add("synthetic CJK page", False, f"{exc.__class__.__name__}: {exc}")


def _check_ocr(report: Report, tmp: Path) -> None:
    pass

    from manga_translator.capture.synthetic import BubbleSpec, make_manga_page
    from manga_translator.ocr import create_engine
    from manga_translator.core.config import OcrSettings

    page = make_manga_page(
        width=900,
        height=520,
        bubbles=[
            BubbleSpec("你好，今天天气真好。", 60, 60),
            BubbleSpec("我是林雪，很高認識你！", 60, 200),
            BubbleSpec("灵气失控了", 700, 60, vertical=True, font_size=28),
        ],
        seed=7,
        panels=2,
    )
    engine, _warning = create_engine(OcrSettings(engine="rapid", min_confidence=0.5))
    try:
        started = time.perf_counter()
        engine.warmup()
        spans = engine.recognize(page)
        elapsed = time.perf_counter() - started
    except Exception as exc:
        report.add("OCR engine", False, f"{exc.__class__.__name__}: {exc}")
        return

    texts = [s.text for s in spans]
    joined = "".join(texts)
    report.add(
        "OCR engine (PP-OCRv4)",
        len(spans) >= 2,
        f"{len(spans)} spans in {elapsed * 1000:.0f} ms: {texts}",
    )
    report.add(
        "OCR simplified Chinese",
        "今天天气真好" in joined,
        f"got {joined!r}",
    )

    # Traditional Chinese gets its own clean page (PP-OCRv4 has the traditional
    # glyphs in its dictionary, but small text over artwork is harder for it).
    traditional_target = "這是一個傳統中文測試"
    traditional_page = make_manga_page(
        width=900, height=220,
        bubbles=[BubbleSpec(traditional_target, 60, 60, font_size=44)],
        seed=11, panels=0, noise=0,
    )
    traditional_spans = engine.recognize(traditional_page)
    traditional_text = "".join(s.text for s in traditional_spans)
    hits = sum(1 for ch in "這傳統測試" if ch in traditional_text)
    report.add(
        "OCR traditional Chinese",
        hits >= 4,
        f"read {traditional_text!r} (expected {traditional_target!r}, {hits}/5 key glyphs)",
    )
    report.add(
        "OCR vertical column",
        any(s.is_vertical and len(s.text) >= 3 for s in spans),
        f"vertical spans: {[s.text for s in spans if s.is_vertical]}",
    )

    # grouping: two lines of one bubble -> one block
    from manga_translator.ocr.grouping import group_spans

    blocks = group_spans(spans, page_area=float(page.shape[0] * page.shape[1]))
    report.add("block grouping", len(blocks) >= 1, f"{len(blocks)} block(s): {[b.text for b in blocks]}")


def _check_providers(report: Report, tmp: Path) -> None:
    """Provider registry, backend factory and live model discovery."""
    from manga_translator.core.config import TranslationSettings
    from manga_translator.translation import create_backend
    from manga_translator.translation.base import TranslateItem, TranslateRequest
    from manga_translator.translation.providers import PROVIDERS, list_models

    problems = []
    for provider_id, spec in PROVIDERS.items():
        if spec.id != provider_id or not spec.label:
            problems.append(provider_id)
        if spec.kind == "mt" and not spec.service:
            problems.append(f"{provider_id}:no-service")
        if spec.is_llm and not (spec.default_model and spec.static_models):
            problems.append(f"{provider_id}:no-model")
        if spec.needs_key and not spec.key_env:
            problems.append(f"{provider_id}:no-key-env")
    report.add(
        "provider registry",
        len(PROVIDERS) >= 30 and not problems,
        f"{len(PROVIDERS)} providers"
        + (f", problems: {problems[:4]}" if problems else " (LLM + classic MT + local servers)"),
    )

    expected_kinds = {
        "gemini": "GeminiBackend",
        "openai": "OpenAiCompatBackend",
        "deepseek": "OpenAiCompatBackend",
        "anthropic": "AnthropicBackend",
        "deepl": "MtBatchBackend",
        "google-translate": "MtBatchBackend",
        "mymemory": "MtBatchBackend",
        "fake": "FakeTranslator",
    }
    built = {}
    for provider, class_name in expected_kinds.items():
        settings = TranslationSettings(provider=provider, backend=provider, base_url="")
        backend, _warning = create_backend(settings, "")
        built[provider] = type(backend).__name__
    bad = {p: built[p] for p, want in expected_kinds.items() if built[p] != want}
    report.add(
        "backend factory",
        not bad,
        ", ".join(f"{p}->{c}" for p, c in built.items()) if not bad else f"unexpected: {bad}",
    )

    # Model discovery against a local stub endpoint (no key needed).
    stub = _StubGemini()
    try:
        from manga_translator.translation.providers import resolve_base_url

        items, unit, error = list_models("openai", stub.base_url + "/v1", "sk-selftest")
        report.add(
            "model list from the API key (stub endpoint)",
            unit == "model" and len(items) >= 2 and not error,
            f"{len(items)} models, error={error!r}",
        )
        # a single configured base_url must not leak between providers
        leaked = resolve_base_url("deepl", "https://api.openai.com/v1")
        report.add(
            "endpoint follows the selected provider",
            leaked.startswith("https://api.deepl.com") or leaked.startswith("https://api-free.deepl.com"),
            f"deepl endpoint resolved to {leaked}",
        )
    finally:
        stub.close()

    # Real network call to a provider that needs no key (MyMemory).
    try:
        settings = TranslationSettings(provider="mymemory", backend="mymemory", base_url="")
        backend, _ = create_backend(settings, "")
        result = backend.translate_page(
            TranslateRequest(items=[TranslateItem(id=1, text="你好")], target_language="Russian")
        )
        text = (result.translations.get(1) or "").strip()
        report.add(
            "live translation via a keyless provider (MyMemory)",
            bool(text),
            f"'你好' -> {text!r}",
        )
    except Exception as exc:
        report.add(
            "live translation via a keyless provider (MyMemory)",
            False,
            f"{exc.__class__.__name__}: {exc} (needs internet)",
            skipped=True,
        )


def _check_pipeline(report: Report, tmp: Path) -> None:
    from manga_translator.cache.page_cache import PageCache
    from manga_translator.capture.synthetic import BubbleSpec, MutableFrameSource, make_manga_page
    from manga_translator.core.config import Config
    from manga_translator.core.models import ScreenRegion
    from manga_translator.core.paths import get_paths
    from manga_translator.glossary.glossary import Glossary
    from manga_translator.ocr import create_engine
    from manga_translator.pipeline.orchestrator import PagePipeline, RecordingPublisher
    from manga_translator.translation.fake import FakeTranslator

    pages = {
        "A": make_manga_page(bubbles=[BubbleSpec("你好，今天天气真好。", 90, 90),
                                      BubbleSpec("师兄，你来了！", 90, 240)], seed=101),
        "B": make_manga_page(bubbles=[BubbleSpec("灵气失控了", 800, 80, vertical=True, font_size=26),
                                      BubbleSpec("为什么？", 120, 300)], seed=202),
        "C": make_manga_page(bubbles=[BubbleSpec("林雪，等一下！", 160, 400)], seed=303),
        "D": make_manga_page(bubbles=[BubbleSpec("不可能，这不可能！", 200, 200)], seed=505),
        "blank": make_manga_page(bubbles=[], seed=404, noise=14),
    }

    paths = get_paths().ensure()
    cfg = Config.load(paths)
    cfg.general.poll_interval_ms = 60
    cfg.general.stable_duration_ms = 120
    cfg.general.preview_max_dim = 220
    cfg.translation.backend = "fake"
    cfg.cache.enabled = True
    cfg.ocr.translate_sfx = False

    engine, _ = create_engine(cfg.ocr)
    engine.warmup()
    source = MutableFrameSource(pages["A"])
    cache = PageCache(paths, max_mb=cfg.cache.max_mb, hamming_threshold=cfg.cache.hamming_threshold)
    publisher = RecordingPublisher()
    backend = FakeTranslator()
    pipeline = PagePipeline(
        cfg, source, backend, cache, Glossary(paths.glossary), publisher=publisher, engine=engine
    )
    region = ScreenRegion(0, 0, pages["A"].shape[1], pages["A"].shape[0], "SELFTEST", 1.0)
    pipeline.start()
    pipeline.set_region(region)

    try:
        first = publisher.wait_for_page(timeout=25)
        ok = first is not None and any(b.translation for b in first.blocks)
        report.add(
            "scenario A: page turn -> translation",
            ok,
            f"{len(first.blocks) if first else 0} blocks, first={first.blocks[0].text if first and first.blocks else '-'}"
            if first
            else "no page produced",
        )
        if not first:
            return

        # scenario B: fast switching keeps only the latest generation
        source.set_frame(pages["B"])
        time.sleep(0.15)
        source.set_frame(pages["C"])
        result_c = None
        deadline = time.time() + 25
        while time.time() < deadline:
            if publisher.pages:
                candidate = publisher.pages[-1][0]
                if any("林雪" in b.text for b in candidate.blocks):
                    result_c = candidate
                    break
            time.sleep(0.05)
        generations = [r.generation for r, _ in publisher.pages]
        report.add(
            "scenario B: stale generations discarded",
            result_c is not None and generations == sorted(generations),
            f"generations={generations}, stale_discarded={pipeline.stats.stale_discarded}",
        )

        # scenario C: cache round trip
        source.set_frame(pages["A"])
        cached = None
        deadline = time.time() + 20
        while time.time() < deadline:
            if len(publisher.pages) > 2 and publisher.pages[-1][0].from_cache:
                cached = publisher.pages[-1][0]
                break
            time.sleep(0.05)
        report.add(
            "scenario C: A -> B -> A served from cache",
            cached is not None,
            f"cache hits={pipeline.stats.cache_hits}",
        )

        # scenario E: no text
        source.set_frame(pages["blank"])
        before_errors = len(publisher.errors)
        blank = None
        deadline = time.time() + 20
        while time.time() < deadline:
            if publisher.pages and not publisher.pages[-1][0].blocks:
                blank = publisher.pages[-1][0]
                break
            time.sleep(0.05)
        report.add(
            "scenario E: page without text",
            blank is not None and len(publisher.errors) == before_errors,
            "no crash, no translation request" if blank else "no empty page produced",
        )

        # manual refresh
        backend.calls_before = getattr(backend, "calls", 0)
        source.set_frame(pages["B"])
        time.sleep(0.4)
        generation = pipeline.refresh(force=True)
        refreshed = None
        deadline = time.time() + 20
        while time.time() < deadline:
            if publisher.pages and publisher.pages[-1][1].kind == "manual":
                refreshed = publisher.pages[-1][0]
                break
            time.sleep(0.05)
        report.add(
            "manual refresh",
            refreshed is not None and refreshed.generation == generation,
            f"generation={generation}",
        )

        # scenario F: API offline (a page that was never translated before, so the
        # cache cannot hide the failure)
        backend.fail_times = 4
        source.set_frame(pages["D"])
        errors_before = len(publisher.errors)
        deadline = time.time() + 20
        while time.time() < deadline and len(publisher.errors) == errors_before:
            time.sleep(0.05)
        offline_ok = len(publisher.errors) > errors_before
        backend.fail_times = 0
        report.add(
            "scenario F: API offline -> readable error, app alive",
            offline_ok and "Traceback" not in (publisher.errors[-1][1] if publisher.errors else ""),
            publisher.errors[-1][1] if publisher.errors else "no error reported",
        )
    finally:
        pipeline.stop(timeout=2.0)
        cache.close()


def _check_capture(report: Report, tmp: Path) -> None:
    """Real screen capture + overlay hygiene (skipped when there is no desktop)."""
    from manga_translator.capture.screen_capture import CaptureHooks, ScreenCapture
    from manga_translator.core import dpi
    from manga_translator.core.models import ScreenRegion

    monitors = dpi.list_monitors()
    if not monitors or os.environ.get("QT_QPA_PLATFORM") == "offscreen" and not monitors:
        report.add("screen capture", False, "no monitor available", skipped=True)
        return
    physical = monitors[0]["physical"]
    width = int(min(600, physical.w - 20))
    height = int(min(400, physical.h - 20))
    region = ScreenRegion(
        x=int(physical.x) + 10, y=int(physical.y) + 10, width=width, height=height,
        monitor=monitors[0]["name"], dpi_scale=dpi.get_monitor_scale(physical.x + 10, physical.y + 10),
    )

    quiet_seen = {"during": 0, "after": 0}
    state = {"flag": False}

    def _before():
        state["flag"] = True

    def _after():
        quiet_seen["during"] += 1 if state["flag"] else 0
        state["flag"] = False

    capture = ScreenCapture(CaptureHooks(before=_before, after=_after, settle_ms=0))
    try:
        frame = capture.grab(region)
        preview = capture.grab_preview(region, 200)
        ok = frame.ndim == 3 and frame.shape[2] == 3 and frame.any()
        report.add(
            "screen capture (mss)",
            ok and quiet_seen["during"] >= 1,
            f"{frame.shape[1]}x{frame.shape[0]} px, preview {preview.shape}, "
            f"overlay-quiet hooks fired during {quiet_seen['during']} grab(s)",
        )
        from manga_translator.detection.image_similarity import change_score

        second = capture.grab(region)
        score = change_score(capture.grab_preview(region, 200), preview).score
        report.add(
            "capture stability / change detection on real screen",
            second.any(),
            f"change score of two consecutive grabs = {score:.4f}",
        )
    except Exception as exc:
        report.add("screen capture (mss)", False, f"{exc.__class__.__name__}: {exc}")


def _check_gemini(report: Report, tmp: Path) -> None:
    """Verify the exact HTTP request we send to Gemini (schema, key, payload)."""
    from manga_translator.core.config import TranslationSettings
    from manga_translator.translation.base import TranslateItem, TranslateRequest
    from manga_translator.translation.gemini import GeminiBackend
    from manga_translator.translation.prompts import RESPONSE_SCHEMA

    stub = _StubGemini()
    key = "AIzaSySELFTEST_KEY_0123456789"
    try:
        settings = TranslationSettings(
            text_model="gemini-3.8-flash",
            target_language="Russian",
            base_url=stub.base_url,
            max_retries=0,
        )
        backend = GeminiBackend(settings, key)
        request = TranslateRequest(
            items=[TranslateItem(id=1, text="你好"), TranslateItem(id=2, text="再见", direction="vertical")],
            target_language="Russian",
            page_index=2,
            context_lines=["师兄 -> старший брат по школе"],
            glossary={"林雪": "Линь Сюэ"},
        )
        result = backend.translate_page(request)
        path, body = stub.requests[0]
        headers = stub.headers[0]
        ok = (
            path == "/v1beta/models/gemini-3.8-flash:generateContent"
            and headers.get("x-goog-api-key") == key
            and body["generationConfig"]["responseSchema"] == RESPONSE_SCHEMA
            and body["generationConfig"]["responseMimeType"] == "application/json"
            and "林雪" in body["contents"][0]["parts"][0]["text"]
            and result.translations.get(1) == "RU:你好"
        )
        report.add(
            "Gemini request format / structured output",
            ok,
            f"{path}; schema ok; key in header; parsed {result.translations}",
        )
    except Exception as exc:
        report.add("Gemini request format", False, f"{exc.__class__.__name__}: {exc}")
    finally:
        stub.close()

    # Real endpoint, only when a key is configured and reachable.
    key_real = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key_real:
        report.add("Gemini live call", False, "no GEMINI_API_KEY in the environment", skipped=True)
        return
    try:
        pass

        from manga_translator.core.config import TranslationSettings as TS

        backend = GeminiBackend(TS(timeout_s=20.0, max_retries=1), key_real)
        result = backend.translate_page(
            TranslateRequest(items=[TranslateItem(id=1, text="你好，今天天气真好。")])
        )
        report.add("Gemini live call", bool(result.translations), str(result.translations))
    except Exception as exc:
        report.add("Gemini live call", False, f"{exc.__class__.__name__}: {exc}")


def _check_overlay(report: Report, tmp: Path) -> None:
    """Render the real overlay widget with real pipeline output (offscreen)."""
    try:
        from PySide6.QtWidgets import QApplication

        from manga_translator.capture.coordmap import CoordMap
        from manga_translator.core.config import OverlaySettings
        from manga_translator.core.models import Block, Rect, ScreenRegion, TextSpan
        from manga_translator.overlay.overlay_window import OverlayWindow, layout_settings
        from manga_translator.overlay.widgets import DebugInfo, render_overlay_image

        QApplication.instance() or QApplication([])
        coordmap = CoordMap.uniform(1.0, Rect(0, 0, 1920, 1080))
        settings = OverlaySettings(show_debug_hud=True, font_size=13.0)
        overlay = OverlayWindow(settings, coordmap)
        region = ScreenRegion(0, 0, 800, 600, "SELFTEST", 1.0)
        overlay.follow_region(region)
        overlay.resize(800, 600)

        quad = (60.0, 60.0, 320.0, 60.0, 320.0, 100.0, 60.0, 100.0)
        blocks = [
            Block(id=1, text="你好，今天天气真好。", bbox=Rect(60, 60, 260, 40),
                  spans=[TextSpan("你好，今天天气真好。", quad, 0.96)],
                  translation="Привет! Сегодня отличная погода, не так ли?"),
            Block(id=2, text="师兄，你来了！", bbox=Rect(420, 320, 180, 40),
                  spans=[TextSpan("师兄，你来了！", quad, 0.94)],
                  translation="Старший брат, ты пришёл!"),
        ]
        cards = overlay.set_page(blocks, region, DebugInfo(generation=7, page_hash="deadbeef",
                                                           change_score=0.31, block_count=2))
        image = render_overlay_image((800, 600), cards, layout_settings(settings),
                                     debug=DebugInfo(generation=7, page_hash="deadbeef"))
        path = Path(os.environ["MT_DATA_DIR"]) / "overlay_render.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(str(path))

        # every card must stay inside the window and near its anchor
        inside = all(
            0 <= c.rect.x and 0 <= c.rect.y and c.rect.x2 <= 800 and c.rect.y2 <= 600 for c in cards
        )
        near = all(abs(c.rect.cy - c.anchor.cy) < 300 for c in cards)
        painted = image.width() == 800 and image.height() == 600
        report.add(
            "overlay rendering (offscreen)",
            len(cards) == 2 and inside and near and painted,
            f"{len(cards)} cards, inside={inside}, near anchors={near}, png={path}",
        )
        report.artifacts.append(str(path))

        # Pixel-level check: transparent where there is no card, and actual text
        # pixels inside each card (proves the painter draws readable content).
        from PIL import Image

        png = Image.open(path).convert("RGBA")
        pixels = png.load()
        transparent = opaque = 0
        for y in range(0, png.height, 7):
            for x in range(0, png.width, 7):
                alpha = pixels[x, y][3]
                if alpha < 20:
                    transparent += 1
                elif alpha > 120:
                    opaque += 1
        text_pixels = 0
        for card in cards:
            rect = card.rect
            for y in range(int(rect.y) + 2, max(int(rect.y) + 3, int(rect.y2) - 2)):
                for x in range(int(rect.x) + 2, max(int(rect.x) + 3, int(rect.x2) - 2)):
                    r, g, b, a = pixels[x, y]
                    if a > 180 and r > 225 and g > 225 and b > 225:
                        text_pixels += 1
        report.add(
            "overlay card content / transparency",
            transparent > 50 and opaque > 50 and text_pixels > 50,
            f"transparent samples={transparent}, card samples={opaque}, text pixels={text_pixels}",
        )
    except Exception as exc:
        report.add("overlay rendering (offscreen)", False, f"{exc.__class__.__name__}: {exc}")


def _check_control_panel(report: Report, tmp: Path) -> None:
    """Build the real control-panel window offscreen and check it renders."""
    try:
        from PySide6.QtWidgets import QApplication

        from manga_translator.core.config import Config
        from manga_translator.core.models import Block, PageResult, Rect, ScreenRegion, TextSpan
        from manga_translator.core.paths import get_paths
        from manga_translator.capture.synthetic import BubbleSpec, make_manga_page
        from manga_translator.pipeline.orchestrator import PageJob
        from manga_translator.ui.main_window import MainWindow

        class _StubPipeline:
            def latest_frame(self):
                return frame, REGION

            def detector_status(self):
                return {"state": "idle", "score": 0.03, "changed_fraction": 0.002,
                        "hamming": 1, "stable_for_ms": 420, "page_hash": "selftest"}

            def last_result(self):
                return result

            def request_preview(self):
                return None

        class _StubController:
            def __init__(self):
                self.config = Config.load(get_paths())
                self.paths = get_paths()
                self.pipeline = _StubPipeline()

            def describe(self):
                return {
                    "manga_mode": True,
                    "engine": "RapidOCR PP-OCRv4 (offline)",
                    "backend": "fake",
                    "model": "fake-model",
                    "generation": 1,
                    "displays": "selftest 900x640px @ 100%",
                    "region": REGION.to_dict(),
                    "detector": self.pipeline.detector_status(),
                    "cache": {"entries": 0, "bytes": 0, "hits": 0, "enabled": 1},
                    "stats": {"pages_processed": 1, "cache_hits": 0, "stale_discarded": 0, "errors": 0},
                    "last_page": {"total_ms": 500.0, "ocr_ms": 120.0, "translate_ms": 380.0,
                                  "blocks": 2, "from_cache": False},
                }

            def window_list(self):
                return []

            def hotkey_mapping(self):
                return {"Toggle Manga Mode": "ctrl+alt+m"}

        QApplication.instance() or QApplication([])
        frame = make_manga_page(
            width=900, height=640,
            bubbles=[BubbleSpec("你好，今天天气真好。", 120, 120, font_size=30)],
            seed=77,
        )
        REGION = ScreenRegion(0, 0, 900, 640, "SELFTEST", 1.0)
        result = PageResult(
            generation=1,
            page_hash="selftest",
            blocks=[
                Block(
                    id=1,
                    text="你好，今天天气真好。",
                    bbox=Rect(120, 120, 300, 44),
                    spans=[TextSpan("你好，今天天气真好。",
                                    (120, 120, 420, 120, 420, 164, 120, 164), 0.97)],
                    translation="Привет! Сегодня отличная погода.",
                    confidence=0.97,
                )
            ],
            ocr_ms=120.0,
            translate_ms=380.0,
            total_ms=500.0,
        )

        window = MainWindow(_StubController())
        window.resize(1000, 700)
        window.show()
        window.set_page(result, PageJob(generation=1, region=REGION, kind="changed"), [])
        QApplication.processEvents()

        rows = window.blocks_panel.table.rowCount()
        frame_size = window.preview.frame_size
        pixmap = window.grab()
        image = pixmap.toImage()
        colors = {
            image.pixelColor(x, y).name()
            for x in range(0, image.width(), 41)
            for y in range(0, image.height(), 41)
        }
        path = Path(os.environ["MT_DATA_DIR"]) / "control_panel.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(path))
        report.artifacts.append(str(path))
        report.add(
            "control panel window (offscreen)",
            rows == 1 and frame_size == (900, 640) and len(colors) > 8,
            f"{rows} block row(s), preview {frame_size[0]}x{frame_size[1]}, "
            f"{len(colors)} distinct colours, png={path}",
        )
        window.deleteLater()
    except Exception as exc:
        report.add("control panel window (offscreen)", False, f"{exc.__class__.__name__}: {exc}")


def _check_stale(report: Report, tmp: Path) -> None:
    """Explicit stale-generation test with a slow backend."""
    from manga_translator.cache.page_cache import PageCache
    from manga_translator.capture.synthetic import BubbleSpec, MutableFrameSource, make_manga_page
    from manga_translator.core.config import Config
    from manga_translator.core.models import ScreenRegion
    from manga_translator.core.paths import get_paths
    from manga_translator.glossary.glossary import Glossary
    from manga_translator.ocr.stub_engine import HashKeyedOcrEngine, span
    from manga_translator.pipeline.orchestrator import PagePipeline, RecordingPublisher
    from manga_translator.translation.fake import FakeTranslator

    page_a = make_manga_page(bubbles=[BubbleSpec("你好", 100, 100)], seed=501)
    page_b = make_manga_page(bubbles=[BubbleSpec("再见", 300, 300)], seed=502)
    engine = HashKeyedOcrEngine(
        {
            HashKeyedOcrEngine.key(page_a): [span("你好", 100, 100, 120, 34)],
            HashKeyedOcrEngine.key(page_b): [span("再见", 300, 300, 120, 34)],
        },
        delay_s=0.3,
    )
    backend = FakeTranslator(delay_s=0.3)
    paths = get_paths().ensure()
    cfg = Config.load(paths)
    cfg.general.poll_interval_ms = 40
    cfg.general.stable_duration_ms = 80
    cfg.cache.enabled = False
    source = MutableFrameSource(page_a)
    publisher = RecordingPublisher()
    pipeline = PagePipeline(
        cfg, source, backend, PageCache(paths, enabled=False), Glossary(paths.glossary),
        publisher=publisher, engine=engine,
    )
    pipeline.start()
    pipeline.set_region(ScreenRegion(0, 0, page_a.shape[1], page_a.shape[0], "SELFTEST", 1.0))
    try:
        source.set_frame(page_b)   # change the page while A is still being processed
        time.sleep(0.35)
        source.set_frame(page_a)
        deadline = time.time() + 20
        while time.time() < deadline and not publisher.pages:
            time.sleep(0.05)
        time.sleep(1.0)
        generations = [r.generation for r, _ in publisher.pages]
        last = publisher.pages[-1][0] if publisher.pages else None
        report.add(
            "stale result cancellation",
            bool(publisher.pages) and generations == sorted(generations),
            f"published generations={generations}, discarded={pipeline.stats.stale_discarded}, "
            f"last='{last.blocks[0].text if last and last.blocks else '-'}'",
        )
    finally:
        pipeline.stop(timeout=2.0)


# --- reporting -------------------------------------------------------------


def _print_report(report: Report, verbose: bool, tmp: Path) -> None:
    width = max(len(c.name) for c in report.checks) + 2
    print()
    print("=" * (width + 60))
    print(" Manga Translator self-test")
    print("=" * (width + 60))
    for check in report.checks:
        status = "SKIP" if check.skipped else ("PASS" if check.ok else "FAIL")
        print(f" [{status}] {check.name:<{width}} {check.detail}")
    print("-" * (width + 60))
    passed = sum(1 for c in report.checks if c.ok)
    skipped = sum(1 for c in report.checks if c.skipped)
    failed = len(report.failed)
    print(f" {passed} passed, {failed} failed, {skipped} skipped")
    if report.artifacts:
        print(" artifacts:")
        for artifact in report.artifacts:
            print(f"   - {artifact}")
    print(f" temp dir: {tmp}")
    print("=" * (width + 60))
    if verbose and report.failed:
        print("\nFailed checks:")
        for check in report.failed:
            print(f"  - {check.name}: {check.detail}")


if __name__ == "__main__":
    sys.exit(0 if run_selftest() else 1)
