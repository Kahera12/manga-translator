# Implementation report — Real-Time Chinese Manga Translator

Project root: `C:\Users\neuro\.dsh\translator`
Entry point: `main.py` · Package: `manga_translator/` · Tests: `tests/`, `tools/`

---

## Implemented

**Speed pass: the same chapter, 1.75x faster (third round)**

| Measured problem | Fix |
|---|---|
| Reading was fully sequential: one batch at a time, and a **3-40 s vision request blocked the whole queue** | `bridge.parallel_batches` (3 by default): several readers pull batches at once, panels are marked *in flight* so nothing is processed twice; the vision gap pass moved to its **own thread**, so the fast OCR + text queue never waits for it |
| Every panel with zero OCR blocks went to the model, including pure artwork and the site watermark our own filter had just removed | the gap trigger now needs the OCR *detector* to have seen text (`detected_spans > 0`) **and** that text to have been dropped for a debatable reason (two-character minimum, low confidence, a non-CJK guess) - `RecognizeOutcome.suspicious_drops`, fed by counters from `postprocess_blocks` |
| Text requests also paid the reasoning model's thinking time | `reasoning_effort: none` for text as well as vision (dropped automatically if a provider rejects the field) |

A/B on the same 40 cold panels (`tools/bridge_speed.py --panels 40 --clear`, real key):
**22.2 s → 12.7 s** (108 → 188 panels/min, 0.55 → 0.32 s per panel). End to end with the
extension, scrolling the chapter at 600 px/s, the badge stayed at *done == total*
throughout (94/94 → 113/113) instead of trailing the page. The OCR engine itself was
measured (`tools/ocr_speed.py`): two concurrent callers give only 1.4x, so it stays
serialised on one lock while the model requests overlap.

**"Translate everything at once": extension + bridge + vision (second round)**

Goal: translate a whole chapter in one logical pass, anchored to the artwork, in the
user's normal Chrome, with an in-place option — and stop losing bubbles.

| Measured problem | Fix |
|---|---|
| **Bubbles went missing.** The log showed panels dying for good: `The read operation timed out` (an httpx timeout escaping the SSE path, so no retry and no fallback) and `'OpenAiCompatBackend' object has no attribute 'label'` | `self.label` now exists; the streaming request wraps `httpx` errors into classified `PipelineError`s so the retry/fallback ladder applies; a failed panel is **retried** (3 attempts, backoff) instead of being marked done |
| **The bridge worker froze after the first panel** (first panel answered in 2.2 s, then nothing for 300 s) | `_mark()` reported the queue depth through `pending`, which re-acquired the same non-reentrant `Lock` — a deadlock. Now an `RLock` plus an inline computation, with a regression test |
| The extension registered panels and the app translated them, but **nothing was ever drawn** | the background delivered app messages with `chrome.tabs.sendMessage`, while the content script only listens on its runtime port: the translations were dropped silently. Port first, `tabs.sendMessage` as a fallback, plus a `runtime.onMessage` listener on the content side |
| **One request per panel**, 13-25 s each (measured in the user's log) | batching: local OCR reads a whole batch at ~0.4 s per panel and the batch's lines go to the model in **one** request with `panel:block` ids; panels are emitted as soon as their batch lands |
| OCR **misses whole bubbles** (measured on real panels: panel 01 = 0 of 3 found; 5 blocks by OCR vs 9 by vision on the same 5 panels) while a vision answer took 25-70 s per panel | `VISION_MODE=auto` (default): OCR reads everything now, and the **vision model is asked only about the panels OCR found nothing on** (gap pass). `reasoning_effort=none` cut a 5-page vision read from **128 s to 38 s** (measured). The gateway allows only `gpt-6-astra`, so the model choice is not ours to make |
| Stale/foreign blocks in in-place renders (boxes ~80 px off the glyphs, a neighbouring panel's text drawn into a bubble) | `snap_box` gets an **IoU guard** (a refinement may not move sideways), panels are cached by **exact** image hash (a tolerant pHash match can land on the adjacent webtoon panel), empty cached pages are re-read, and `CACHE_LOGIC_VERSION` invalidates rows produced by older logic |
| The site's watermark was OCR'd as dialogue («快看！快看漫画» → «Скорее! Смотри комикс!») | `looks_like_watermark()` (explicit site/promo patterns plus short latin strings pinned to the very edge); deliberately conservative, because a false positive deletes a real line |
| In-place lettering was black on dark artwork | `auto_text_color`: the colour comes from the brightness of the erased background (explicit caller colours still win) |
| The gateway ignored images on the first attempt (every page came back with **0 blocks**) | `response_format` (the *text* JSON schema) was attached to vision requests, so the model answered in the old `{"translations":[…]}` shape. Vision requests never carry the text schema now |

Live verification on `kuaikanmanhua.com/webs/comic-next/185055` (Chrome 154, real key):

* the extension drew **26 cards** on the real page, texts in Russian (`На помощь!`,
  `Спасите меня!`, `Почему именно я?`, `Ха-а…`), positions in **% of the image box**
  with the `translateY(-100%)` anchor;
* **anti-flicker, measured**: 6 cards, 8 s idle → 0 moved, 0 texts changed,
  0 disappeared; the badge went `2/2 → 40/100` as the chapter filled in;
* **in-place mode**: 5 layers on the page, and the app's own before/after dumps show
  Chinese replaced by Russian inside the bubbles with the artwork and bubble
  outlines intact;
* app side without a browser: 8 fresh panels read and translated in **6.1 s**
  (OCR plus one batched text request), every id matched back to its panel.

Chrome 137+ ignores `--load-extension`, and `Extensions.loadUnpacked` returns an id
without installing anything when the browser was started with
`--disable-extensions-except`; `tools/extension_install_probe.py` records the four
variants tried and which one works (load over CDP, no `--load-extension` flags).

**Fixes after live testing on kuaikanmanhua.com (real key, real chapter)**

Measured problems and what was done about them:

| Measured problem | Fix |
|---|---|
| A page took **22-42 s** (25 s timeout + retry) on the user's gateway; the model is a reasoning model and the gateway buffers non-streaming answers | **SSE streaming** in the OpenAI-compatible backend: the same request dropped to **2.5-3.0 s** (measured: 15-17 s non-streaming vs 5 s streaming for an identical call). Cancellation now aborts mid-answer instead of burning a request; retries/backoff work in the streaming path too |
| `response_format` made the gateway 3-4x slower | `response_mode = auto`: start with the provider default, drop it automatically when an answer is slow or rejected (JSON parsing is tolerant) |
| Reader UI (page title, breadcrumbs, language menu) was OCR'd and translated | two filters: geometric **site-chrome** detection (wide, short, at the top/bottom edge) and a **static-text tracker** (same text at the same spot across pages, only near the edges, only when the content really moved) |
| On webtoons *all* text was classified as SFX (giant glyphs) → nothing translated | SFX is now **page-relative** (much bigger than this page's own median glyph, plus an absolute and a region-share floor); panels in browser mode translate big stylised text by default |
| Single-glyph OCR noise from artwork ("口", "十") became cards | blocks shorter than 2 characters are dropped |
| Scrolling invalidated in-flight translations → nothing appeared | **translation memory** (SQLite, per target language): repeated lines are instant, only new text is sent; partial results are published immediately; the overlay is no longer cleared at the start of a page |
| Cached pages could serve results produced with older settings | cache keys now include a **settings fingerprint** (OCR engine, confidence, SFX flags, target language, model) |
| Screen capture cannot produce clean pages | **browser mode** (Chrome DevTools Protocol): read the page images from the tab, OCR at native resolution, translate ahead, and inject the cards *into the page* |

**Browser mode (new)**

* `browser/cdp.py` – minimal DevTools client (Chrome 136+ requires `--remote-debugging-port`
  together with a non-default `--user-data-dir`; the port is read from
  `DevToolsActivePort`), tab listing, launch helper.
* `browser/reader.py` – DOM panel discovery (scroll-collect through the lazy-loaded
  chapter), image download (direct CDN fetch, with an in-browser fetch fallback),
  card construction and **in-page overlay injection**.
* `browser/service.py` – panel OCR/translation on top of the existing engines, cache
  and translation memory, plus a **priority queue** (visible panels first, the rest
  of the chapter in the background).
* `browser/mode.py` / `browser/dialog.py` – tab controller (tracks scroll every
  400 ms, draws cards, reports status) and the "Browser mode…" dialog
  (open Chrome with debugging → find tabs → connect → translate visible / whole chapter).
* Cards are DOM elements in document coordinates: alignment is exact by
  construction, they scroll with the artwork and ignore window position, DPI and
  browser chrome (the previous screen-mapped approach was 85 px off because the
  window client area is taller than the web viewport).

**Localisation (Russian interface, English available)**

* `core/i18n.py`: gettext-style catalogue keyed by the English source string
  (~260 entries), `tr()` with `str.format` templates so word order stays natural,
  `set_language()` / `available_languages()`; unknown strings fall back to English
  instead of showing placeholders.
* The whole user-visible surface is localised: control panel (toolbar, menus,
  panels, tables, buttons, tooltips, placeholders), tray menu and tooltip,
  settings/glossary/statistics dialogs, notifications, pipeline status messages
  and every provider/transport error text. Tray and window expose `retranslate()`,
  so switching the language in `Settings → General → Interface language`
  (or `UI_LANGUAGE=ru|en`) re-labels the running app immediately.
* Russian ships as the default (`GeneralSettings.language = "ru"`).
* `tests/test_i18n.py` walks the package AST and fails if any `tr("...")` string
  lacks a Russian entry or if a template's `{placeholders}` differ between
  languages, so the catalogue cannot silently rot.

**Translation providers (32) + model discovery from the API key**

* Registry (`translation/providers.py`): every provider is a `ProviderSpec` with
  kind, endpoint, key env vars, default model, curated fallback models and
  capabilities. LLM: Gemini, OpenAI, DeepSeek, Anthropic, OpenRouter, Mistral,
  Groq, Together, Fireworks, DeepInfra, Cerebras, xAI, Perplexity, NVIDIA NIM,
  SambaNova, GitHub Models, Moonshot, Zhipu, Qwen/DashScope, SiliconFlow,
  Azure OpenAI, Ollama, LM Studio, llama.cpp, vLLM/LocalAI. Classic MT: Google
  Cloud Translation v2, DeepL, Yandex, Microsoft Translator, LibreTranslate,
  MyMemory (no key) + the offline `fake` stub.
* Backends: `openai_compat.py` (chat-completions, strict `json_schema` →
  automatic downgrade to `json_object` → prompt-only), `anthropic.py` (Messages
  API + assistant prefill), `gemini.py` (responseSchema), `mt_apis.py` (batched
  `q`/`text`/`texts` requests, per-service adapters, HTML-entity decoding,
  per-block MyMemory with chunking), all sharing `transport.py` (timeouts,
  limited retries with backoff + jitter, `Retry-After`, cancellation between
  attempts, consistent auth/quota/network/model error messages with the exact env
  var to set) and `parsing.py` (tolerant JSON parsing).
* **Model list from the key**: `list_models()` queries `GET /models`
  (OpenAI-compatible), `/v1beta/models` (Gemini, filtered to `generateContent`),
  `/v1/models` (Anthropic), `/api/tags` (Ollama) and the MT providers'
  `/languages` endpoints, with the curated static list as a fallback plus the
  reason it was used. Runs in a background `QThread` in Settings → Translation
  ("Load models" / "Load languages"), together with a **Test connection** button
  that translates one line before you commit to a provider.
* Per-provider keys: `api_keys[provider]` in the local config, resolved
  env var → `.env` → config, with the provider's env var names shown in the UI;
  keys are never logged or displayed (last 4 characters only).
* One `base_url` in the config is resolved per provider
  (`resolve_base_url`), so switching provider (or `TRANSLATION_PROVIDER`) can
  never leave another provider's endpoint behind; custom endpoints are kept.

**GUI (control panel)**

* `MainWindow` (`ui/main_window.py`): toolbar (Manga Mode, Translation toggle,
  Select Region, Select Window, Refresh Page, Settings, Glossary, Hide to tray),
  full menu bar (File / Run / View / Tools / Help), dark stylesheet
  (`ui/theme.py`), status bar with the current pipeline message and busy
  indicator, `Ctrl+W` hides to tray, `Ctrl+Q` exits. Closing the window hides it —
  the tray keeps translating.
* Live page preview (`ui/preview.py`): the captured frame drawn to fit, green
  boxes = translated blocks, amber = SFX/skipped, dashed blue = the exact card
  rectangles the overlay draws, optional translations inside the boxes, and a HUD
  with generation / page hash / change score / timings / CACHE marker. The frame is
  refreshed once a second by the worker thread (no OCR, no API call) and right
  after every processed page; double-click forces a refresh.
* Panels (`ui/panels.py`): status (mode, region, displays, engine, translator,
  generation, detector state + change score, page hash, last-page timings, cache
  usage, counters), dialogue-blocks table (#, original, translation, confidence,
  kind, box) with copy-to-clipboard and save-as-text, log tail with auto-refresh,
  and the active hotkey list.
* The window is a pure view: it emits signals that the app maps onto the same
  handlers the tray menu uses, so both entry points stay consistent. It can never
  block translation work — everything expensive stays on the pipeline threads.
* New public app API used by the GUI: `describe()`, `window_list()`,
  `hotkey_mapping()`, `request_preview()`, `refresh_page()`, `set_overlay_visible()`,
  `open_settings()`, `open_glossary()`, `clear_cache()`, `show_window()`,
  `hide_window()`, `run(show_window=...)`; `main.py` gained `--tray-only`.

**MVP (all acceptance items)**

* Region selection (drag a rectangle, one picker window per screen, Esc cancels),
  stored with monitor name and DPI scale; optional window mode that follows a
  browser/reader window.
* Continuous monitoring of the region every 150 ms with a cheap similarity metric
  (changed-pixel fraction + mean difference + 64-bit pHash) — cursor moves, hover
  highlights and 1–2 px scroll deltas do not trigger a page.
* Automatic page-change detection with debounce/stabilization: nothing is OCR'd
  while the page moves; only the frame that stays stable for 350 ms is processed
  (with a 4 s safety valve for never-settling pages).
* Final-frame capture via `mss` (physical pixels, Per-Monitor-V2 DPI aware).
* Chinese OCR with RapidOCR (PaddleOCR **PP-OCRv4** det+cls+rec on ONNX Runtime,
  models bundled in the wheel → fully offline), simplified + traditional,
  horizontal + vertical, plus a vertical re-recognition pass for CJK columns.
* OCR results carry text + quad + bbox + confidence in capture-relative pixels.
* Line grouping into dialogue blocks (gap/overlap/alignment/height heuristics,
  right-to-left manga reading order, component boxes preserved).
* Page-level translation: **one** Gemini request per page with the whole page,
  page context, a rolling window of previous lines and the user glossary.
* Structured output: `responseSchema` (`{translations:[{id,translation}]}`),
  strict validation, one repair attempt, follow-up request for missing ids,
  tolerant parsing of fences / bare arrays / id-keyed maps.
* Always-on-top, borderless, translucent, **click-through** overlay whose cards
  sit on/near the original bubbles; hides with Manga Mode, toggle to show/hide.
* Old cards are cleared on every new page; stale results can never be drawn
  (generation ids + cancellation checks between stages and inside retry sleeps).
* Page cache (SQLite + pHash, fuzzy match, LRU size cap, `Clear Cache`);
  `A → B → A` returns instantly with no OCR and no API call.
* Manual refresh forces capture → OCR → translation → overlay.
* Confidence filtering, optional low-confidence retention for debugging.
* SFX heuristic (`TRANSLATE_SFX=false` by default): glyph size, box area,
  extreme aspect ratio, rotated lettering, isolated short low-confidence text.
* Error handling without tracebacks: missing/invalid key, no network, timeout,
  quota/rate limit, malformed answer, blocked answer, OCR failure, capture
  failure, corrupt cache, disconnected monitor, DPI change. One retry with
  backoff for temporary failures, cancelled as soon as the page changes.
* Tray app: enable/disable Manga Mode, select region, select window, refresh,
  show/hide translation, settings, glossary editor, statistics, clear cache, open
  logs, exit. No main window required.
* Settings dialog (General / OCR / Translation / Overlay / Cache / Hotkeys) plus
  a glossary editor; JSON config + `.env` overrides; key never logged or stored
  in the project.
* Logging to `%LOCALAPPDATA%\MangaTranslator\logs\app.log` (rotating) with secret
  redaction; debug mode with OCR boxes, confidences, block ids, page hash,
  change score, generation id and PNG dumps.
* Global hotkeys via pynput (configurable, default `Ctrl+Alt+M/H/R/S`).
* PyInstaller packaging spec; a real build was produced and started successfully.

**Deliberately not built (Phase 2 per the spec):** bubble segmentation for
inpainting, redraw/typesetting, chapter export mode, browser extension.

---

## Files created/changed

```text
main.py                                  entry point (DPI awareness, CLI, fatal errors)
requirements.txt                          pinned dependencies
.env.example                              key/model/tuning template
.gitignore
README.md                                 usage, settings, architecture, limitations
REPORT.md                                 this report
packaging/manga_translator.spec           PyInstaller spec (windowed, models included)

manga_translator/__init__.py
manga_translator/app.py                   tray+overlay+hotkeys+pipeline wiring
manga_translator/core/{__init__,config,dpi,i18n,logging_setup,models,paths}.py
manga_translator/capture/{__init__,screen_capture,coordmap,region_selector,window_capture,synthetic}.py
manga_translator/detection/{__init__,image_similarity,page_change}.py
manga_translator/ocr/{__init__,base,rapid_engine,paddle_engine,stub_engine,grouping,filtering}.py
manga_translator/translation/{__init__,base,providers,transport,parsing,gemini,
                              openai_compat,anthropic,mt_apis,prompts,context,fake}.py
manga_translator/pipeline/{__init__,generation,orchestrator}.py
manga_translator/overlay/{__init__,layout,widgets,overlay_window}.py
manga_translator/cache/{__init__,page_cache}.py
manga_translator/glossary/{__init__,glossary}.py
manga_translator/browser/{__init__,cdp,reader,service,mode,win,dialog}.py
manga_translator/ui/{__init__,main_window,preview,panels,theme,model_loader,tray,
                    settings,notifications,hotkeys,bridge}.py

tests/conftest.py, test_detection.py, test_grouping.py, test_coordmap.py,
tests/test_cache.py, test_gemini.py, test_providers.py, test_pipeline.py,
tests/test_overlay.py, test_config_glossary.py, test_app_wiring.py,
tests/test_gui_window.py, test_i18n.py, test_browser_mode.py

tools/selftest.py                         offline end-to-end verification (26 checks)
tools/live_check.py                       live desktop verification (13 checks)
tools/live_provider_check.py              real translation via a keyless provider
tools/browser_live.py                     browser mode on the live page
tools/browser_align_check.py              cards vs. bubbles, verified by OCR
tools/mapping_test.py, tools/page_analyze.py   coordinate/OCR diagnostics
```

---

## Dependencies

```text
mss==10.2.0                 fast region capture (GDI BitBlt of the virtual desktop)
numpy==2.3.1
pillow==11.3.0
opencv-python==5.0.0.93
rapidocr-onnxruntime==1.4.4 PP-OCRv4 det+cls+rec on ONNX Runtime (offline, models bundled)
PySide6==6.11.2             overlay window, tray, dialogs
httpx==0.28.1               Gemini REST calls
python-dotenv==1.0.0        .env handling
pynput==1.7.7               global hotkeys
pytest (dev)                test suite
pyinstaller 6.22.3 (dev)    packaging
```

Gemini is called over REST (key in the `x-goog-api-key` header) instead of the
`google-genai` SDK: one dependency less, no `pydantic` version conflict with other
tools installed on this machine, and direct control over `responseSchema`,
timeouts and backoff. All of them were installed and verified on Python 3.11.0.

---

## Run

```powershell
cd C:\Users\neuro\.dsh\translator
pip install -r requirements.txt
copy .env.example .env      # then put GEMINI_API_KEY=... into .env

python main.py                    # control panel + tray + overlay
python main.py --tray-only        # no window
python main.py --select-region    # start by picking the region
python main.py --manga-mode       # start with Manga Mode on
python main.py --debug            # verbose logs + debug HUD
python main.py --selftest         # offline self-test (exit code 0/1)
python -m pytest tests -q         # 119 tests
python tools\live_check.py        # live desktop check (opens windows, ~25 s)
```

---

## Configuration

* `%LOCALAPPDATA%\MangaTranslator\config.json` — all settings (also editable in
  the Settings dialog).
* `.env` next to `main.py` — `GEMINI_API_KEY`, `TEXT_MODEL`, `TARGET_LANGUAGE`,
  `TRANSLATION_BACKEND`, `OCR_ENGINE`, `OCR_MIN_CONFIDENCE`, `POLL_INTERVAL_MS`,
  `STABLE_DURATION_MS`, `CHANGE_THRESHOLD`, `TRANSLATE_SFX`, `CONTEXT_PAGES`,
  `CACHE_ENABLED`, `CACHE_MAX_MB`, `DEBUG_MODE`. Environment wins over JSON.
* `%LOCALAPPDATA%\MangaTranslator\glossary.json` — term glossary (app → Glossary).
* `MT_DATA_DIR` relocates all of the above (used by the tests/self-test).
* Provider: any of the 32 in **Settings → Translation** (`TRANSLATION_PROVIDER`),
  with **Load models** pulling the list straight from your API key. Keys come from
  the provider's env var (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
  `ANTHROPIC_API_KEY`, `DEEPL_API_KEY`, `YANDEX_API_KEY`, `AZURE_TRANSLATOR_KEY`, …),
  from `.env`, or from the key field in Settings (stored in the local config).

Key settings: `POLL_INTERVAL_MS=150`, `STABLE_DURATION_MS=350`,
`CHANGE_THRESHOLD=0.075`, `OCR_MIN_CONFIDENCE=0.55`, `TRANSLATE_SFX=false`,
`CONTEXT_PAGES=2`, `CACHE_ENABLED=true`, `CACHE_MAX_MB=1000`, `TEXT_MODEL=gemini-3.8-flash`.

---

## Hotkeys

| Action | Default |
|---|---|
| Toggle Manga Mode | `Ctrl+Alt+M` |
| Show / hide translation | `Ctrl+Alt+H` |
| Refresh page | `Ctrl+Alt+R` |
| Select region | `Ctrl+Alt+S` |

---

## Verification performed on this machine

| What | How | Result |
|---|---|---|
| Unit/integration tests | `python -m pytest tests -q` | **237 passed** (incl. bridge protocol/queue/server, vision parser + box snapping, in-place renderer, watermark rule) |
| Extension in a real Chrome | `tools/extension_live.py` on the live chapter | 26 cards drawn in the page with Russian text, positions in % of the image box, badge `40/100`; anti-flicker: 6 cards over 8 s idle → 0 moved / 0 re-texted / 0 lost (`tools/extension_modes.py`) |
| Whole chapter, one pass, live | `tools/extension_watch.py` scrolling the real chapter for 11 min | badge **2/113 → 109/113**, cards 6 → 54 and holders 5 → 109 **monotonically** (no wipe, no rebuild), zero console errors or exceptions; the reader removed panels above the viewport as it went and the drawn cards survived |
| In-place mode | `tools/extension_modes.py` + the app's own before/after dumps | Chinese letters erased, Russian lettered inside the bubble, artwork and bubble outlines intact; text colour chosen from the background |
| Bridge without a browser | `tools/bridge_client.py --from-browser --limit 8` | 8 fresh panels read and translated in **6.1 s**, every `panel:block` id matched back |
| Vision vs OCR | `tools/vision_vs_ocr.py` on saved panels | OCR 5 blocks / 1.8 s vs vision 9 blocks / 127 s; one panel: OCR 0 of 3 bubbles, vision 3 of 3 |
| Streaming latency | live gateway, identical request | 15-17 s non-streaming vs **2.5-3.0 s streaming**; a real page now completes in ~3 s |
| Browser mode alignment | cards from the DOM vs. OCR of an actual screenshot | card bottom 6 px above the bubble text it translates (`browser_align_check.py`) |
| Browser mode end-to-end | `tools/browser_live.py` on the live chapter | attach, 25 panels discovered, cards drawn in the page, page turn → new cards in 0.4-4.4 s, 0 failures |
| Russian interface | 11 localisation tests + live window check | every `tr()` string translated, placeholders consistent, window/tray re-label on switch, live panel shows «2 блоков · переведено 2», title «Manga Translator — панель управления» |
| Provider registry & protocols | stub HTTP endpoints recording every request | all 32 providers consistent; OpenAI/Anthropic/Gemini/Google/DeepL/Yandex/Microsoft/LibreTranslate/MyMemory request shapes, batching, JSON-schema downgrade, auth/quota classification, model + language listing |
| Model list from the key | local stub `/v1/models`, `/v1beta/models`, `/api/tags` | parsed and filtered correctly (embeddings excluded); graceful static fallback with reason |
| Live provider (no key) | real network call via MyMemory | `你好` → `привет`; DeepL without a key reports "no API key set" instead of failing |
| Control panel (offscreen) | build the real window, feed a page, render to PNG | 17 GUI tests + selftest check pass; blocks table, preview frame, toolbar signals, log tail, provider switching in Settings |
| Control panel (live desktop) | `tools/live_check.py` opens the real window | live frame 900×640 shown, 2 block rows, window screenshot saved (`runtime/artifacts/control_panel.png`), toolbar toggle hides/shows the overlay |
| Full flow with a REAL translation API | `MT_LIVE_PROVIDER=mymemory python tools\live_check.py` | 13/13: Chinese page on screen → OCR → real Russian on the overlay («Здравствуйте, это прекрасный день.», «Старший подмастерье-брат, ты здесь!»), page turn → «Аура вышла из-под контроля» / «Почему?» |
| Change detection & debounce | synthetic pages, deterministic clock / real pipeline | intermediate scroll frames never processed; cursor-sized change ignored; masked overlay areas ignored |
| DPI / multi-monitor mapping | 150 %+100 % mixed map, negative origins, round-trips | exact within 1 logical px |
| OCR quality | real PP-OCRv4 on synthetic pages | simplified 3/3 lines exact, vertical column read, traditional 4/5 key glyphs |
| Gemini request format | local stub HTTP endpoint (real request/response cycle) | correct URL, key in header only, `responseMimeType`/`responseSchema` verified, 429 retry, 404/400/auth classification, timeout, missing-id repair, malformed handling, key never logged |
| Scenarios A–F | orchestrator with scripted OCR + fake backend, then again with real OCR (`tools/selftest.py`) | all pass || Stale generation cancellation | slow backend + rapid page switching | late results discarded, published generations monotonic |
| Cache A→B→A | real pipeline + SQLite cache | second visit served from cache, no OCR/API call |
| Overlay rendering | offscreen render + pixel analysis | cards inside the window, text pixels present, transparent background |
| Overlay on the real desktop | `tools/live_check.py` with a real window | cards drawn (pixel delta 102 when hidden), aligned outside cards, click-through confirmed by Win32 hit test |
| Capture hygiene | compare screen pixels with overlay visible vs the pipeline's own grab | pipeline grab contains no overlay (delta 0.00 inside the card area) |
| Page turn on the real desktop | swap the page image in the window | new translations replace the old ones automatically |
| Real screen capture | `mss` grab of a 600×400 region | non-black, hooks fire, consecutive frames differ by 0.0004 |
| Packaged executable | `pyinstaller packaging/manga_translator.spec` + start | built (336 MB folder, 14 MB exe) and started; tray/hotkeys/log confirmed |

**Not verifiable here:** a live translation through a *cloud LLM* (no
`GEMINI_API_KEY` / `OPENAI_API_KEY` / other key on this machine) — the
request/response path of every provider is covered by the stub-endpoint tests, the
Gemini path additionally by `tools/selftest.py`, and a real network call is
verified through the keyless MyMemory provider. Local servers (Ollama, LM Studio,
LibreTranslate) were not running on this machine, so they are verified by protocol
tests only.

---

## Known limitations

* Cloud providers need a key and network; keyless options are MyMemory, a local
  LLM server (Ollama / LM Studio / llama.cpp / vLLM), LibreTranslate and the
  offline `fake` stub.
* Default model name `gemini-3.8-flash` follows the task spec — if the key does
  not expose it, press **Load models** in Settings and pick one from the live list.
* Classic MT providers (Google Translate, DeepL, Yandex, Microsoft,
  LibreTranslate, MyMemory) have no page context: they translate the page as a
  batch of texts, so tone/voice consistency across bubbles is limited (the
  glossary is still enforced on the result).
* Structured output: `json_schema` where supported, automatic downgrade to
  `json_object` or prompt-only JSON elsewhere; a provider that ignores all of it
  still works thanks to the tolerant parser, at the cost of an extra repair call.
* Traditional Chinese: PP-OCRv4 occasionally returns a simplified glyph
  (measured: 4/5 key glyphs correct on the synthetic page).
* Vertical/stylised lettering: vertical columns are re-read rotated; extreme
  rotation accuracy drops (treated as SFX by default).
* SFX detection is heuristic and conservative by design (`TRANSLATE_SFX=true`
  translates everything).
* Region selection is clamped to one screen; window mode captures the window's
  screen rectangle (an occluded window shows what is on top of it).
* DRM-protected content cannot be captured; the app reports it instead of
  failing silently.
* Cache identity is a perceptual hash — visually near-identical pages can share
  an entry (tolerance configurable in Settings → Cache).
