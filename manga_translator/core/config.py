"""Persistent settings: JSON file in %LOCALAPPDATA% + .env / process env overrides.

The API key is never written to the repo. It is read from (in order)
process env `GEMINI_API_KEY` -> `<project>/.env` -> config.json
(LOCALAPPDATA, machine-local). Its value is never logged.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import threading
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .logging_setup import get_logger
from .models import ScreenRegion
from .paths import Paths, get_paths

log = get_logger("config")

GEMINI_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"


@dataclass
class GeneralSettings:
    manga_mode_enabled: bool = False
    region: Optional[dict] = None
    language: str = "ru"                 # UI language ("ru" | "en")
    poll_interval_ms: int = 150
    stable_duration_ms: int = 350
    change_threshold: float = 0.075      # 0..1 combined change score
    preview_max_dim: int = 240           # preview downscale for change detection
    min_unstable_frames: int = 1
    max_wait_ms: int = 4000              # force-process a page even if unstable
    debug_mode: bool = False


@dataclass
class OcrSettings:
    engine: str = "rapid"                # rapid | paddle | stub
    min_confidence: float = 0.55
    keep_low_confidence: bool = True     # keep for debug overlay, never translated
    simplified_chinese: bool = True
    traditional_chinese: bool = True
    vertical_pass: bool = True           # re-recognise tall CJK columns rotated
    max_ocr_side: int = 1600             # downscale very large captures for speed
    group_gap_ratio: float = 0.85        # gap / line-height to merge lines
    group_size_ratio: float = 0.55       # max box-height difference to merge
    drop_blocks_without_cjk: bool = True
    drop_ui_chrome: bool = True          # never translate site titles/breadcrumbs
    suppress_static_text: bool = True    # drop site menus that never move
    translate_sfx: bool = False          # TRANSLATE_SFX (screen capture mode)
    translate_sfx_in_browser: bool = True  # panels are clean: translate it all
    sfx_min_area_ratio: float = 0.16     # huge box -> sound effect
    sfx_min_glyph: float = 90.0          # glyph size in physical px -> stylised SFX
    sfx_median_factor: float = 2.2       # SFX = much bigger than the page's own text


@dataclass
class TranslationSettings:
    provider: str = "gemini"             # see translation/providers.py (gemini | openai | deepl | ...)
    backend: str = "gemini"              # legacy alias of `provider` (kept in sync on load)
    text_model: str = GEMINI_DEFAULT_MODEL
    target_language: str = "Russian"
    context_pages: int = 2
    glossary_path: str = ""
    api_key: str = ""                    # legacy single key (Gemini); env vars win
    api_key_env: str = "GEMINI_API_KEY"  # legacy alias of the provider's first env name
    api_keys: Dict[str, str] = field(default_factory=dict)   # provider id -> key
    base_url: str = GEMINI_DEFAULT_BASE_URL
    timeout_s: float = 60.0              # slow gateways/reasoning models need room
    max_retries: int = 1
    backoff_base_s: float = 0.6
    temperature: float = 0.25
    max_blocks_per_request: int = 80
    # structured output: auto (try schema, drop it when slow/rejected) | schema |
    # object | off. Reasoning models on gateways are much faster without it.
    response_mode: str = "auto"
    # Vision: let a multimodal model read the page images itself (text + boxes +
    # translation in one request).
    #   "auto"   - local OCR reads every panel at once (0.4 s each) and the vision
    #              model is asked only about the panels OCR found nothing on, so
    #              missed bubbles are fixed without paying 30-70 s for every page;
    #   "always" - vision reads everything (best quality, slowest with a
    #              reasoning model: measured 25-70 s per panel on a real gateway);
    #   "off"    - OCR only.
    vision_mode: str = "auto"
    vision_panels_per_request: int = 8   # page images per vision request
    vision_max_side: int = 1280          # images are downscaled to this side
    vision_quality: int = 85             # JPEG quality of the images we upload
    vision_timeout_s: float = 180.0      # a vision answer is much slower than text
    vision_max_gap_panels: int = 24      # cap on panels sent to vision in "auto"
    # Reasoning models "think" for a minute before writing any JSON; on a real
    # gateway "none" made a 5-page read 3x faster with the same result.
    vision_reasoning_effort: str = "none"   # none | low | medium | high | "" (default)
    # The same knob for *text* requests: they carry short dialogue lines, so the
    # thinking time buys little and costs seconds on every batch.
    reasoning_effort: str = "none"          # none | low | medium | high | "" (default)
    # provider-specific extras (e.g. yandex folder id, azure api-version)
    extra: Dict[str, str] = field(default_factory=dict)

    def vision_enabled(self) -> bool:
        return (self.vision_mode or "auto").strip().lower() in (
            "auto", "on", "always", "true", "1")

    def vision_only(self) -> bool:
        """True when vision should read every page instead of only filling OCR gaps."""
        return (self.vision_mode or "auto").strip().lower() in ("on", "always", "true", "1")

    def active_provider(self) -> str:
        return ((self.provider or self.backend or "gemini").strip().lower()) or "gemini"


@dataclass
class OverlaySettings:
    font_size: float = 13.0
    opacity: float = 0.82                # card background alpha 0..1
    padding: float = 8.0
    max_card_width: int = 360            # logical px
    min_card_width: int = 90
    mode: str = "translation"            # translation | original | both
    click_through: bool = True
    visible: bool = True
    gap: int = 6                         # gap between bbox and card (logical px)
    text_color: str = "#ffffff"
    bg_color: str = "#10131a"
    accent_color: str = "#7dd3fc"
    show_debug_hud: bool = False


@dataclass
class CacheSettings:
    enabled: bool = True
    max_mb: int = 1000
    max_age_days: int = 45
    hamming_threshold: int = 6           # pHash bits allowed to differ -> same page
    prune_interval_s: float = 300.0


@dataclass
class HotkeySettings:
    enabled: bool = True
    toggle_manga_mode: str = "ctrl+alt+m"
    toggle_overlay: str = "ctrl+alt+h"
    refresh_page: str = "ctrl+alt+r"
    select_region: str = "ctrl+alt+s"

    def as_pynput(self) -> dict:
        """`ctrl+alt+m` -> `<ctrl>+<alt>+m` (pynput GlobalHotKeys syntax)."""
        out = {}
        for f in fields(self):
            if f.name == "enabled":
                continue
            spec: str = getattr(self, f.name)
            parts = [p.strip() for p in spec.replace(" ", "").split("+") if p.strip()]
            out[f.name] = "+".join(
                f"<{p}>" if p.lower() in {"ctrl", "alt", "shift", "cmd", "win"} else p.upper()[-1:]
                for p in parts
            )
        return out


@dataclass
class BridgeSettings:
    """The WebSocket bridge the browser extension talks to."""

    enabled: bool = True
    autostart: bool = True           # start with the app, no button needed
    port: int = 8770
    mode: str = "cards"              # cards | inplace (the extension can override)
    whole_chapter: bool = True       # translate every panel the page knows about
    prefetch_ahead: int = 12         # panels to translate beyond the viewport
    # Reading is dominated by waiting for the model, so several batches are read at
    # once (OCR itself is serialised - it is fast and shares one engine).
    parallel_batches: int = 3
    # Panels where OCR found no text but the *detector* did see something are
    # re-read by the model in a background worker, so the fast queue never waits
    # for a 3-40 s vision request.
    gap_pass: bool = True


@dataclass
class Config:
    general: GeneralSettings = field(default_factory=GeneralSettings)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    translation: TranslationSettings = field(default_factory=TranslationSettings)
    overlay: OverlaySettings = field(default_factory=OverlaySettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    hotkeys: HotkeySettings = field(default_factory=HotkeySettings)
    bridge: BridgeSettings = field(default_factory=BridgeSettings)

    _paths: Paths = field(default_factory=get_paths, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    # -- io ---------------------------------------------------------------
    @classmethod
    def load(cls, paths: Optional[Paths] = None) -> "Config":
        paths = paths or get_paths()
        cfg = cls(_paths=paths)
        if paths.config.exists():
            try:
                data = json.loads(paths.config.read_text(encoding="utf-8"))
                cfg._merge(data)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("config.json unreadable (%s); using defaults", exc.__class__.__name__)
        # keep the legacy `backend` alias in sync with `provider`
        translation = cfg.translation
        legacy = (translation.backend or "").strip().lower()
        if legacy and legacy != "gemini" and (translation.provider or "gemini") == "gemini":
            translation.provider = legacy
        translation.backend = translation.active_provider()
        if translation.timeout_s < 30.0:
            # LLM gateways regularly need 10-30 s for a whole page; too small a
            # timeout is the most common reason "nothing ever appears".
            translation.timeout_s = 30.0
        cfg.apply_env_overrides()
        return cfg

    def save(self) -> None:
        with self._lock:
            data = self.to_dict(keep_paths=True)
            try:
                self._paths.config.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._paths.config.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self._paths.config)
            except OSError as exc:
                log.warning("could not save config: %s", exc)

    def to_dict(self, keep_paths: bool = False, redact_key: bool = True) -> dict:
        data: dict[str, Any] = {}
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            section = getattr(self, f.name)
            if not is_dataclass(section):
                continue
            d = dataclasses.asdict(section)
            if f.name == "translation":
                d["backend"] = section.active_provider()   # legacy alias stays in sync
                if redact_key:
                    d["api_key"] = "***" if d.get("api_key") else ""
                    d["api_keys"] = {
                        key: ("***" if value else "") for key, value in (d.get("api_keys") or {}).items()
                    }
            data[f.name] = d
        if keep_paths:
            data.setdefault("translation", {})["api_key"] = self.translation.api_key
            data["translation"]["api_keys"] = dict(self.translation.api_keys)
        return data

    def _merge(self, data: dict) -> None:
        for f in fields(self):
            if f.name.startswith("_"):
                continue
            section = getattr(self, f.name)
            if not is_dataclass(section) or f.name not in data:
                continue
            patch = data[f.name]
            if not isinstance(patch, dict):
                continue
            valid = {x.name for x in fields(section)}
            clean = {k: v for k, v in patch.items() if k in valid}
            for k, v in clean.items():
                current = getattr(section, k)
                try:
                    if isinstance(current, bool):
                        setattr(section, k, bool(v))
                    elif isinstance(current, int) and not isinstance(v, bool):
                        setattr(section, k, int(v))
                    elif isinstance(current, float):
                        setattr(section, k, float(v))
                    elif current is None or isinstance(current, (str, dict, list)):
                        setattr(section, k, v)
                    else:
                        setattr(section, k, v)
                except (TypeError, ValueError):
                    log.warning("ignoring bad config value %s=%r", k, v)

    def apply_env_overrides(self) -> None:
        env = os.environ
        if env.get("UI_LANGUAGE") or env.get("MT_LANGUAGE"):
            self.general.language = (env.get("UI_LANGUAGE") or env["MT_LANGUAGE"]).strip().lower()
        if env.get("TRANSLATION_PROVIDER"):
            self.translation.provider = env["TRANSLATION_PROVIDER"].strip().lower()
        if env.get("TRANSLATION_BACKEND"):
            self.translation.provider = env["TRANSLATION_BACKEND"].strip().lower()
        self.translation.backend = self.translation.provider
        if env.get("TEXT_MODEL"):
            self.translation.text_model = env["TEXT_MODEL"]
        if env.get("TARGET_LANGUAGE"):
            self.translation.target_language = env["TARGET_LANGUAGE"]
        base = env.get("TRANSLATION_BASE_URL") or env.get("GEMINI_BASE_URL")
        if base:
            self.translation.base_url = base.rstrip("/")
        for name, provider in (
            ("GEMINI_API_KEY", "gemini"),
            ("OPENAI_API_KEY", "openai"),
            ("DEEPSEEK_API_KEY", "deepseek"),
            ("ANTHROPIC_API_KEY", "anthropic"),
            ("OPENROUTER_API_KEY", "openrouter"),
            ("DEEPL_API_KEY", "deepl"),
        ):
            if env.get(name):
                self.translation.api_keys.setdefault(provider, env[name])
        if env.get("OCR_ENGINE"):
            self.ocr.engine = env["OCR_ENGINE"].strip().lower()
        if env.get("TRANSLATE_SFX"):
            self.ocr.translate_sfx = env["TRANSLATE_SFX"].strip().lower() in ("1", "true", "yes")
        if env.get("VISION_MODE"):
            self.translation.vision_mode = env["VISION_MODE"].strip().lower()
        if env.get("REASONING_EFFORT"):
            self.translation.reasoning_effort = env["REASONING_EFFORT"].strip().lower()
        if env.get("BRIDGE_WORKERS"):
            try:
                self.bridge.parallel_batches = max(1, int(env["BRIDGE_WORKERS"]))
            except ValueError:
                pass
        if env.get("BRIDGE_GAP_PASS"):
            self.bridge.gap_pass = env["BRIDGE_GAP_PASS"].strip().lower() in ("1", "true", "yes")
        if env.get("BRIDGE_PORT"):
            try:
                self.bridge.port = int(env["BRIDGE_PORT"])
            except ValueError:
                pass
        if env.get("DEBUG_MODE"):
            self.general.debug_mode = env["DEBUG_MODE"].strip().lower() in ("1", "true", "yes")
        if env.get("CACHE_ENABLED"):
            self.cache.enabled = env["CACHE_ENABLED"].strip().lower() in ("1", "true", "yes")
        for name, target in (
            ("POLL_INTERVAL_MS", "poll_interval_ms"),
            ("STABLE_DURATION_MS", "stable_duration_ms"),
            ("CONTEXT_PAGES", "context_pages"),
            ("CACHE_MAX_MB", "max_mb"),
        ):
            if env.get(name):
                try:
                    value = int(env[name])
                except ValueError:
                    continue
                if name == "CONTEXT_PAGES":
                    self.translation.context_pages = value
                elif name == "CACHE_MAX_MB":
                    self.cache.max_mb = value
                else:
                    setattr(self.general, target, value)
        if env.get("OCR_MIN_CONFIDENCE"):
            try:
                self.ocr.min_confidence = float(env["OCR_MIN_CONFIDENCE"])
            except ValueError:
                pass
        if env.get("CHANGE_THRESHOLD"):
            try:
                self.general.change_threshold = float(env["CHANGE_THRESHOLD"])
            except ValueError:
                pass

    # -- helpers ----------------------------------------------------------
    def snapshot(self) -> "Config":
        """Cheap deep copy for worker threads (no locking surprises)."""
        with self._lock:
            new = copy.copy(self)
            new.general = copy.copy(self.general)
            new.ocr = copy.copy(self.ocr)
            new.translation = copy.copy(self.translation)
            new.overlay = copy.copy(self.overlay)
            new.cache = copy.copy(self.cache)
            new.hotkeys = copy.copy(self.hotkeys)
            return new

    def region(self) -> Optional[ScreenRegion]:
        return ScreenRegion.from_dict(self.general.region) if self.general.region else None

    def set_region(self, region: ScreenRegion) -> None:
        self.general.region = region.to_dict()

    # -- provider keys ----------------------------------------------------
    def key_env_names(self, provider: Optional[str] = None) -> list:
        """Environment-variable names that may hold this provider's key."""
        provider = (provider or self.translation.active_provider()).strip().lower()
        try:
            from ..translation.providers import get_spec

            spec = get_spec(provider)
        except Exception:  # pragma: no cover - import cycle safety
            spec = None
        names = list(spec.key_env) if spec else []
        if not names:
            names = [self.translation.api_key_env or "GEMINI_API_KEY"]
        return names

    def api_key(self, provider: Optional[str] = None) -> str:
        """Key for a provider: environment → .env → machine-local config."""
        from dotenv import dotenv_values

        provider = (provider or self.translation.active_provider()).strip().lower()
        names = self.key_env_names(provider)
        for name in names:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        stored = (self.translation.api_keys.get(provider) or "").strip()
        if stored:
            return stored
        if provider == "gemini":
            legacy = (self.translation.api_key or "").strip()
            if legacy:
                return legacy
        for path in (self._paths.env_file, Path(__file__).resolve().parents[2] / ".env"):
            if not path or not path.exists():
                continue
            try:
                data = dotenv_values(path)
            except OSError:
                continue
            for name in names:
                value = (data.get(name) or "").strip()
                if value:
                    return value
        return ""

    def set_api_key(self, provider: str, key: str) -> None:
        provider = (provider or "").strip().lower()
        key = (key or "").strip()
        if not provider:
            return
        if key:
            self.translation.api_keys[provider] = key
        else:
            self.translation.api_keys.pop(provider, None)
        if provider == "gemini":
            self.translation.api_key = key

    def api_key_source(self, provider: Optional[str] = None) -> str:
        provider = (provider or self.translation.active_provider()).strip().lower()
        for name in self.key_env_names(provider):
            if os.environ.get(name, "").strip():
                return f"env {name}"
        if (self.translation.api_keys.get(provider) or "").strip():
            return "config"
        if provider == "gemini" and (self.translation.api_key or "").strip():
            return "config"
        for path in (self._paths.env_file, Path(__file__).resolve().parents[2] / ".env"):
            if path and path.exists():
                try:
                    from dotenv import dotenv_values

                    data = dotenv_values(path)
                except OSError:
                    continue
                for name in self.key_env_names(provider):
                    if (data.get(name) or "").strip():
                        return f".env ({name})"
        return ""

    def api_key_status(self, provider: Optional[str] = None) -> str:
        provider = (provider or self.translation.active_provider()).strip().lower()
        try:
            from ..translation.providers import get_spec

            spec = get_spec(provider)
            label = spec.label if spec else provider
            optional = spec is not None and not spec.needs_key
        except Exception:  # pragma: no cover
            label, optional = provider, False
        key = self.api_key(provider)
        if not key:
            if optional:
                return f"{label}: no key needed"
            names = " / ".join(self.key_env_names(provider))
            return f"not set (put it in .env as {names})"
        return f"ok ({self.api_key_source(provider) or 'config'}, …{key[-4:]})"

    def glossary_file(self) -> Path:
        raw = (self.translation.glossary_path or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self._paths.root / path
            return path
        return self._paths.glossary
