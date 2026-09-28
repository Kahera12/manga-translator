# Real-Time Chinese Manga Translator (Windows)

Translates Chinese manga/comic pages **on screen** into **Russian**, automatically,
as you read: pick the page area once, enable Manga Mode, and every page turn is
detected, OCR'd, translated as a whole page and shown as a click-through overlay
right on top of the original speech bubbles. A **control panel window** shows the
live page, the OCR/translation results and all the knobs; the tray keeps it running
when the window is closed.

```text
open manga in browser / reader
        ↓
select the page region once          (toolbar / tray, or Ctrl+Alt+S)
        ↓
enable Manga Mode                    (toolbar / tray, or Ctrl+Alt+M)
        ↓
turn / scroll the page
        ↓
change detected → page stabilized    (150 ms polling, 350 ms stabilization)
        ↓
PP-OCRv4 detects the Chinese text    (local, offline, simplified + traditional)
        ↓
lines are grouped into dialogue blocks, ordered right-to-left
        ↓
the whole page is translated in ONE Gemini request (structured JSON)
        ↓
Russian cards appear over the bubbles (click-through, always on top)
        ↓
turning the page replaces the cards automatically; revisiting a page is instant (cache)
```

---

## 1. What it does

* **Control panel window** – live preview of the captured page with OCR boxes and
  the exact card placement, status panel (mode, region, DPI, engine, detector
  state/score, timings, cache), table of detected blocks with original +
  translation, log tail, controls/hotkey list, dark theme. Closing it hides to the
  tray; `--tray-only` starts without it.
* **Region mode (MVP)** – drag a rectangle over the manga area; the region is
  stored with monitor + DPI information.
* **Window mode (bonus)** – `Select Window…` can follow a browser/reader window
  and re-align itself when that window moves or resizes.
* **Page change detection** – downscaled preview every 150 ms, similarity =
  changed-pixel fraction + mean difference + perceptual hash. Cursor moves,
  hover highlights and 1–2 px scroll deltas do **not** trigger a new page.
* **Debounce / stabilization** – while the page is moving nothing is OCR'd; only
  the frame that stays stable for 350 ms is processed.
* **Generation IDs** – every page gets a generation; late OCR/translation results
  for an older page are discarded and never drawn over a newer page.
* **OCR** – RapidOCR (PaddleOCR PP-OCRv4 det+cls+rec on ONNX Runtime), fully
  offline, simplified + traditional Chinese, horizontal and vertical columns,
  with a dedicated vertical re-recognition pass.
* **Grouping** – OCR lines that belong to one bubble become one translation
  block (gap/overlap/alignment/height heuristics, manga reading order).
* **Translation** – one request per page with page context, a rolling window of
  previously translated lines and a user glossary; schema-constrained JSON output.
  32 providers (Gemini, OpenAI, DeepSeek, Claude, OpenRouter, Groq, Mistral,
  Ollama, LM Studio, … + Google Translate, DeepL, Yandex, Microsoft,
  LibreTranslate, MyMemory); the model list is pulled from your API key.
* **Overlay** – borderless, translucent, always-on-top, **click-through** card
  overlay aligned to the capture region; never contaminates screenshots.
* **Cache** – SQLite + perceptual hash; `A → B → A` returns instantly without OCR
  or API calls; size cap and `Clear Cache`.
* **Tray app** – enable/disable Manga Mode, select region/window, refresh,
  show/hide translation, settings, glossary, statistics, clear cache, open the
  control panel, exit; configurable global hotkeys, optional debug HUD.
* **Browser mode** – reads the page images from a Chrome tab (CDP) instead of the
  screen: better OCR, whole-chapter prefetch, cards drawn inside the page.
* **Fast by design** – streaming answers (a slow gateway went from 25 s to 2.5 s
  per page), a translation memory that makes repeated bubbles instant, and cache
  keys tied to the OCR/translation settings so stale results can never be served.
* **Interface language** – ships in **Russian** (default) with English available;
  switch in `Settings → General → Interface language` (or `UI_LANGUAGE=ru|en`).
  The whole GUI, tray menu, notifications, status messages and error texts are
  localised, and changing the language re-labels the open window immediately.

---

## 2. System requirements

| | |
|---|---|
| OS | Windows 10 1607+ / Windows 11 (Per-Monitor-V2 DPI aware, multi-monitor) |
| Python | **3.10 – 3.11** (tested on 3.11.0) |
| RAM | ~1.5 GB free (ONNX Runtime + Qt) |
| Disk | ~700 MB for dependencies (PySide6 + onnxruntime + models) |
| Network | only for the Gemini API (OCR and the overlay are fully offline) |
| Other | a Gemini API key for real translation; `fake` backend works offline for testing |

---

## 3. Install

```powershell
cd C:\Users\neuro\.dsh\translator
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Dependencies (pinned in `requirements.txt`): `mss`, `numpy`, `pillow`,
`opencv-python`, `rapidocr-onnxruntime` (bundled PP-OCRv4 models, no download
needed), `PySide6`, `httpx`, `python-dotenv`, `pynput`.

---

## 4. Translation providers and the API key

**32 providers** are built in, in two families:

* **LLM (page-level JSON, keeps context/tone/glossary):** Google Gemini, OpenAI,
  DeepSeek, Anthropic Claude, OpenRouter, Mistral, Groq, Together, Fireworks,
  DeepInfra, Cerebras, xAI Grok, Perplexity, NVIDIA NIM, SambaNova, GitHub Models,
  Moonshot Kimi, Zhipu GLM, Alibaba Qwen (DashScope), SiliconFlow, Azure OpenAI,
  plus **local servers**: Ollama, LM Studio, llama.cpp, vLLM/LocalAI.
* **Classic MT (one batched request per page, no context):** Google Cloud
  Translation v2, DeepL, Yandex Translate, Microsoft Translator (Azure),
  LibreTranslate (self-hosted) — and **MyMemory**, which needs no key at all.
* Plus an offline `fake` stub for testing without network.

Pick the provider in **Settings → Translation** (or `.env` →
`TRANSLATION_PROVIDER=...`). Keys are per provider and resolved in this order:

1. the provider's environment variable (e.g. `GEMINI_API_KEY`, `OPENAI_API_KEY`,
   `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPL_API_KEY`, `AZURE_TRANSLATOR_KEY`,
   `YANDEX_API_KEY`, … see `.env.example` for the full list),
2. `.env` next to `main.py` (git-ignored),
3. the "API key" field in Settings → Translation (stored in
   `%LOCALAPPDATA%\MangaTranslator\config.json`, never in the project).

```text
GEMINI_API_KEY=...        # Google Gemini
OPENAI_API_KEY=...        # OpenAI
DEEPSEEK_API_KEY=...      # DeepSeek
ANTHROPIC_API_KEY=...     # Claude
DEEPL_API_KEY=...         # DeepL (…:fx = free tier)
TRANSLATION_PROVIDER=deepseek
TEXT_MODEL=deepseek-chat
TARGET_LANGUAGE=Russian
```

### The model list comes from your key

In Settings → Translation press **Load models**: the app asks the provider
(`GET /models`, `GET /v1beta/models`, `GET /api/tags`, `GET /v1/models`, …) which
models **your** key can actually use and fills the drop-down. For classic MT
providers the same button loads the supported **languages** (`/languages`) and
fills the target-language field. If there is no key, no network or the provider
has no listing endpoint, the curated built-in list is used and the reason is
shown next to the button. **Test connection** then translates one test line to
prove the key/model/endpoint combination works before you start reading.

Keys are never logged and never written into the project directory; log output is
redacted automatically, and the status line only ever shows the last 4 characters.

Providers that need no key at all: `ollama`, `lmstudio`, `llamacpp`, `vllm`,
`libretranslate` (if your server is open), `mymemory` and `fake`. MyMemory is a
quick way to smoke-test the whole pipeline (OCR → text → Russian overlay) without
registering anywhere.

---

## 5. Run

```powershell
python main.py                 # control panel + tray + overlay
python main.py --tray-only     # no window, tray only
python main.py --select-region # start and immediately ask for the region
python main.py --manga-mode    # start with Manga Mode enabled
python main.py --debug         # verbose logging + debug HUD
python main.py --selftest      # offline end-to-end self-test (no GUI, no key)
```

The app lives in the **system tray** too: closing the control panel hides it,
`Open control panel` in the tray menu brings it back, `Exit` shuts everything down
(`Ctrl+Q` in the window, or `Ctrl+C` in the console when started from a terminal).

---

## 6. The control panel

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Manga Mode │ Translation │ Select Region │ Select Window │ Refresh │ Settings │
├──────────────────────────────────┬───────────────────────────────────────────┤
│ Live page preview                │ Status                                    │
│  · the captured frame            │  Manga Mode · Region · Displays · Engine  │
│  · green boxes = translated      │  Translator · Generation · Detector       │
│    amber = SFX / skipped         │  Change score · Page hash · Last page     │
│  · dashed blue = overlay card    │  Cache · Counters                         │
│  · HUD: hash, change score,      │                                           │
│    timings, generation, CACHE    │ Dialogue blocks                           │
│  (double-click = refresh page)   │  #  Original (zh)  Translation (ru)  …    │
│                                  │  [Copy translations] [Save page text…]    │
├──────────────────────────────────┴───────────────────────────────────────────┤
│ Log │ Controls │ About                                                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

* **Preview** – always shows what the pipeline sees right now: the frame is
  refreshed once a second by the worker thread (no OCR, ~10 ms) while Manga Mode
  is on, and immediately after every processed page. Boxes and card rectangles use
  exactly the same coordinates as the on-screen overlay, so the panel doubles as a
  layout debugger. Double-click the preview to force a refresh.
* **Status** – mode, region (size, position, monitor, DPI), displays, OCR engine,
  translator + model, generation, detector state and change score, page hash,
  timings of the last page (OCR / translation / total, cache hit), cache usage and
  the page/hit/stale/error counters.
* **Dialogue blocks** – one row per block: id, original Chinese, Russian
  translation, OCR confidence, kind (dialogue / SFX / skipped) and the pixel box.
  `Copy translations` puts the page into the clipboard, `Save page text…` writes a
  UTF-8 text file with original + translation.
* **Log** – tail of `%LOCALAPPDATA%\MangaTranslator\logs\app.log` with
  auto-refresh, so problems can be diagnosed without leaving the app.
* **Controls** – the active global hotkeys, and the About tab with the versions,
  engine/backend in use and the data folder.

Reference screenshot taken by `tools/live_check.py` on a real desktop:
`runtime/artifacts/control_panel.png`.

---

## 7. Browser mode (recommended for webtoons)

Screen capture reads a *picture of your screen*, so the reader's own UI ends up in
the OCR and every page turn costs a fresh API call. Browser mode instead reads the
page images straight from the tab through the Chrome DevTools Protocol:

```text
DOM panels (1280x1000 each, no site UI)  ->  OCR at native resolution
        ↓                                        ↓
   one request per panel                cards injected INTO the page
        ↓                                        ↓
   queue keeps the next pages ready      they scroll with the artwork
```

* **Better OCR** – the original page image at full resolution instead of a
  downscaled screenshot with the reader's menus in it.
* **Faster** – panels are translated ahead of the scroll position, so turning the
  page shows the translation immediately (page cache + translation memory).
* **Exact placement** – cards are DOM elements placed in *document* coordinates,
  so they sit on their bubble, follow the scroll and are not affected by window
  position, DPI or browser chrome.
* **Whole chapter** – "Translate the whole chapter" walks the page, collects every
  panel and translates them in the background while you read the first one.

How to use it: tray → **Browser mode…** → **Open Chrome with debugging** (the app
starts Chrome/Edge with remote debugging on its own profile — Chrome 136+ refuses
debugging on the default profile) → open your chapter there → **Find tabs** →
select the tab → **Connect**. Then read normally.

Notes:

* The browser started this way is a separate profile: your normal browsing is
  untouched, but you must open the manga in *that* window.
* Screen-free mode does not need the overlay window at all; the screen-region
  pipeline is paused while browser mode is active.
* Tested on `kuaikanmanhua.com` (Chrome 154, panels of 1280x1000, ~150 per
  chapter): alignment verified against a screenshot — cards land 6 px above the
  bubble they translate.

---

## 7b. Browser extension + local bridge (works in your normal Chrome)

Browser mode above needs a Chrome started with remote debugging. The extension
does not: it runs in the Chrome you already use and talks to the app over a local
WebSocket.

```text
extension (content script)                     app (bridge session)
  page images ──► register {key,url,size} ──►  queue: what is on screen first
  cards / in-place image ◄── panel {blocks} ◄── read in batches, one request per
                                                several panels + translation memory
```

Start it: the app starts the bridge automatically (panel **Browser extension** →
`ws://127.0.0.1:8770/ws`), then load the extension once:

1. `chrome://extensions` → enable **Developer mode** → **Load unpacked** →
   pick the `extension` folder of this project;
2. open a chapter and press **Ctrl+Shift+Y** (or use the extension's popup) to
   translate the page.

Two rendering modes:

* **Cards** – a translated card next to each bubble, anchored *inside* the page
  element that holds the image and positioned in **percentages**, updated by an
  incremental DOM diff (a card is created once, only its text changes). Nothing is
  rebuilt on scroll, so nothing flickers and cards never disappear when a panel
  leaves the viewport. Measured on a live chapter: 6 cards over 8 s idle →
  0 moved, 0 re-texted, 0 disappeared.
* **In place** – the app erases the Chinese letters from the panel and letters the
  Russian into the same bubble (`cv2.inpaint` + Pillow) and the extension draws the
  finished image over the panel. The text colour is picked from the erased
  background, so a dark sound effect gets light letters.

How a chapter is read (the "translate everything at once" part):

* every panel the page reports is queued exactly once, visible panels first;
* local OCR reads the whole batch at ~0.4 s per panel and the lines of the batch go
  to the model in **one request** with ids `panel:block`;
* panels where OCR found *no* text (measured: 1 panel in 5 of a real chapter —
  exactly the bubbles that used to go missing) are re-read by a **vision model**
  that returns text, translation and box per page, up to
  `VISION_PANELS_PER_REQUEST` pages per request;
* `VISION_MODE=always` makes the vision model read every page (better boxes; in-place
  mode uses it automatically), `VISION_MODE=off` keeps everything local;
* results are cached twice — the finished panel by exact image hash and every line in
  the translation memory — so scrolling back is instant and free.

Notes:

* Chrome 137+ ignores `--load-extension` on the command line, so the extension is
  loaded through `chrome://extensions` (or the CDP `Extensions.loadUnpacked`, which
  is what `tools/extension_live.py` uses).
* The bridge listens on the loopback interface only.
* `tools/bridge_client.py` speaks the same protocol without a browser — the fastest
  way to check the app side alone.

---

## 8. Select a region

* Tray → **Select Region…**, or press `Ctrl+Alt+S`.
* Every screen dims; drag a rectangle over the manga page and release.
* The selection is converted to physical device pixels using the DPI scale of the
  monitor it was made on, so it stays correct at 100 %, 125 %, 150 % and on
  secondary monitors with a different scale. `Esc` cancels.
* The region (position, size, monitor, DPI scale) is saved and restored on the
  next start.

`Select Window…` lists visible windows (browsers, readers, viewers) and makes the
region follow the chosen window as long as it lives.

---

## 9. Enable Manga Mode

Tray → **Enable Manga Mode** (icon turns blue), or `Ctrl+Alt+M`.

While it is on the app polls the region, detects page turns and keeps the overlay
in sync. Turning it off hides the overlay and stops all detection immediately
(in-flight work is cancelled by generation id). The state is remembered between
runs.

---

## 10. Refresh / show-hide

| Action | Tray | Hotkey |
|---|---|---|
| Toggle Manga Mode | Enable/Disable Manga Mode | `Ctrl+Alt+M` |
| Show / hide translation | Show/Hide translation | `Ctrl+Alt+H` |
| Refresh page (force capture → OCR → translate) | Refresh Page | `Ctrl+Alt+R` |
| Select region | Select Region… | `Ctrl+Alt+S` |

`Show/Hide translation` flips the overlay to the Chinese original and back, so you
can check what was really said. Hotkeys are configurable in
**Settings → Hotkeys** (`ctrl+alt+m` style syntax; modifiers: ctrl, alt, shift).

---

## 11. Where things are stored

| What | Path |
|---|---|
| Logs | `%LOCALAPPDATA%\MangaTranslator\logs\app.log` (rotating, 3 × 2 MB) |
| Settings | `%LOCALAPPDATA%\MangaTranslator\config.json` |
| Glossary | `%LOCALAPPDATA%\MangaTranslator\glossary.json` (editable in the app) |
| Page cache | `%LOCALAPPDATA%\MangaTranslator\cache\pages.db` (SQLite) |
| Debug overlay dumps | `%LOCALAPPDATA%\MangaTranslator\debug\overlay_gen*.png` (debug mode only) |
| `.env` | next to `main.py` in the project folder |

Set `MT_DATA_DIR` to relocate everything (used by the self-test).

---

## 12. Settings overview

* **General** – Manga Mode, **interface language**, capture region, detection
  interval (`POLL_INTERVAL_MS`), stabilization duration (`STABLE_DURATION_MS`),
  change threshold (`CHANGE_THRESHOLD`), preview size, force-processing timeout,
  debug mode.
* **OCR** – engine (`rapid` / `paddle` / `stub`), minimum confidence
  (`OCR_MIN_CONFIDENCE`), OCR downscale limit, line-grouping gap, keep
  low-confidence text, simplified / traditional, vertical pass, `TRANSLATE_SFX`.
* **Translation** – provider (32 options), API key + status, model (with
  `Load models` straight from the API), target language (with `Load languages`
  for MT providers), endpoint, context pages (`CONTEXT_PAGES`), glossary file,
  timeout, retries, blocks per request, temperature, `Test connection`.
* **Overlay** – font size, card opacity, padding, max card width, display mode
  (translation / original / both), click-through, debug HUD, visibility.
* **Cache** – enabled, max size (`CACHE_MAX_MB`), pHash match tolerance, max age,
  `Clear cache now`.
* **Hotkeys** – enable/disable and rebind all four hotkeys.
* **Browser extension (config file / env)** – `bridge.enabled`,
  `bridge.autostart`, `bridge.port` (default 8770), `bridge.mode`
  (`cards` / `inplace`), `bridge.parallel_batches` (`BRIDGE_WORKERS`, default 3),
  `bridge.gap_pass` (`BRIDGE_GAP_PASS`, default on), and on the translation side
  `VISION_MODE` (`auto` / `always` / `off`), `vision_panels_per_request` (8),
  `vision_max_side` (1280), `vision_quality` (85), `vision_timeout_s` (180),
  `vision_max_gap_panels` (60), `vision_reasoning_effort` (`none`;
  `REASONING_EFFORT` sets the text-request one too).

### How fast it is, and what makes it fast

Measured on the tested chapter (`kuaikanmanhua.com/webs/comic-next/185055`,
113 panels, real key); 40 **cold** panels through the bridge
(`tools/bridge_speed.py --panels 40 --clear`):

| Build | 40 cold panels | Per panel | First answer |
|---|---|---|---|
| 1 reader, gap pass inline (before) | 22.2 s (108/min) | 0.55 s | 4.9 s |
| 3 readers, gap pass in the background (now) | **12.7 s (188/min)** | **0.32 s** | 6.8 s |

End to end with the extension, scrolling the whole chapter at 600 px/s, the progress
badge stayed at *done == total* the whole way (94/94 → 113/113,
`tools/extension_watch.py`): the translation keeps up with the page, so nothing is
ever waited for.

Four things do the work, in order of impact:

1. **Several readers at once** (`parallel_batches`, 3 by default). Reading is mostly
   *waiting* for the model; OCR itself stays serialised on one engine because it is
   fast (0.25 s per panel) and shared. A/B above: 1.75x.
2. **The vision gap pass runs on its own thread.** A model request for the panels OCR
   could not translate takes 3-40 s and used to block every other panel.
3. **A precise gap trigger.** A panel is re-read by the model only when the OCR
   *detector* saw text that our own filters then dropped for a debatable reason (a
   two-character minimum, low confidence, a non-CJK guess). Pure artwork and site
   watermarks no longer cost a request at all.
4. **`reasoning_effort: none`** for text requests as well as vision (measured on a
   real gateway: a 5-page vision read went 128 s → 38 s). The field is dropped
   automatically if a provider rejects it.

Cached panels (exact image hash) and repeated lines (translation memory) are free, so
scrolling back costs nothing. `tools/ocr_speed.py` shows the OCR engine scales only
1.4x with two callers, which is why it is left serialised.

Everything can also be set through `.env` / environment variables (see
`.env.example`); the environment wins over `config.json`.

---

## 13. How it is built

```text
main.py                        entry point: DPI awareness, args, error handling
manga_translator/
├── app.py                     tray + overlay + hotkeys + pipeline wiring (GUI thread)
├── core/
│   ├── config.py              settings (JSON + .env), API-key resolution, redaction
│   ├── i18n.py                interface translations (English source → Russian)
│   ├── dpi.py                 Per-Monitor-V2 awareness, monitor enumeration, DPI scale
│   ├── logging_setup.py       rotating log + secret masking
│   ├── models.py              Rect / ScreenRegion / TextSpan / Block / PageResult
│   └── paths.py               %LOCALAPPDATA% layout
├── capture/
│   ├── screen_capture.py      mss region grab, previews, capture hooks (overlay blanking)
│   ├── coordmap.py            physical ↔ logical pixel mapping per monitor
│   ├── region_selector.py     fullscreen drag selector (one window per screen)
│   ├── window_capture.py      window list / window-follow mode
│   └── synthetic.py           synthetic CJK pages + scripted frame sources (tests)
├── detection/
│   ├── image_similarity.py    change score (pixel diff + MAD + pHash), pHash identity
│   └── page_change.py         IDLE → CHANGING → stabilized page event state machine
├── ocr/
│   ├── base.py                engine interface
│   ├── rapid_engine.py        RapidOCR + vertical re-recognition pass
│   ├── paddle_engine.py       optional PaddleOCR plug-in
│   ├── stub_engine.py         deterministic engines for tests
│   ├── grouping.py            line → dialogue block clustering + reading order
│   └── filtering.py           confidence filter, CJK checks, SFX heuristics
├── translation/
│   ├── base.py                page-level translate interface (cancellable)
│   ├── providers.py           registry of 32 providers + live model/language discovery
│   ├── transport.py           shared HTTP: retries, backoff, error classification
│   ├── parsing.py             tolerant JSON answer parsing (shared by all LLMs)
│   ├── gemini.py              Gemini REST, responseSchema
│   ├── openai_compat.py       OpenAI-compatible chat APIs (OpenAI/DeepSeek/Groq/Ollama/…)
│   ├── anthropic.py           Anthropic Messages API
│   ├── mt_apis.py             Google Translate, DeepL, Yandex, Microsoft, LibreTranslate, MyMemory
│   ├── prompts.py             manga prompt + JSON schema
│   ├── context.py             rolling context window (1–2 pages)
│   └── fake.py                offline deterministic translator
├── browser/
│   ├── cdp.py                 minimal DevTools-protocol client (attach/launch)
│   ├── reader.py              panel discovery, image fetch, in-page card injection
│   ├── service.py             panel OCR+translation, background panel queue
│   ├── mode.py                tab controller: track scroll, draw cards, prefetch
│   ├── win.py                 the window that owns the debug port (for geometry)
│   └── dialog.py              "Browser mode…" dialog (connect / whole chapter)
├── pipeline/
│   ├── orchestrator.py        monitor thread + worker thread + generation guard
│   └── generation.py          generation counter / staleness checks
├── overlay/
│   ├── overlay_window.py      frameless, translucent, click-through, capture-quiet
│   ├── layout.py              wrapping, placement near bubbles, clamping (pure)
│   └── widgets.py             painting of cards, debug HUD, toasts (+ offscreen render)
├── cache/page_cache.py        SQLite page cache with pHash lookup, LRU cap, clear
├── glossary/glossary.py       user glossary: prompt injection + term enforcement
└── ui/
    ├── main_window.py         control panel: toolbar, menu, preview, panels
    ├── preview.py             page preview widget (frame + boxes + card rects)
    ├── panels.py              status panel, blocks table, log tail, hotkeys
    ├── theme.py               dark palette + stylesheet
    ├── tray.py                tray icon + menu (state-aware)
    ├── settings.py            settings dialog (provider/model/key), glossary, stats
    ├── model_loader.py        background model/language discovery + connection test
    ├── notifications.py       small readable notifications (toast + tray balloon)
    ├── hotkeys.py             pynput global hotkeys → Qt signals
    └── bridge.py              Qt signal publisher for worker threads
tools/
├── selftest.py                offline end-to-end verification (21 checks, no GUI/key)
└── live_check.py              live desktop verification (real screen, real overlay)
```

### Threading model

* **GUI thread** – Qt widgets only (overlay, tray, dialogs).
* **Monitor thread** – polls previews, runs change detection, bumps generations.
* **Worker thread** – full capture → cache → OCR → grouping → one API request →
  publish. Cancellation is checked between stages and inside retry sleeps.
* The worker publishes through Qt signals (`ui/bridge.py`); the overlay is only
  ever touched on the GUI thread. During a screenshot the worker asks the GUI
  thread (blocking queued call) to blank the overlay, so the previous page's
  translation can never be captured and re-OCR'd.

---

## 14. Tests and verification

```powershell
pip install pytest
python -m pytest tests -q        # detection, grouping, DPI, cache, every provider
                                 # protocol, pipeline scenarios A–F, overlay layout
                                 # + rendering, control panel GUI, localisation, app
                                 # wiring, config/glossary, bridge protocol + queue,
                                 # vision parser/snapping, in-place renderer
python main.py --selftest        # offline end-to-end self-test on this machine
python tools\live_check.py       # live desktop check (opens windows, ~25 s)
$env:MT_LIVE_PROVIDER="mymemory"; python tools\live_check.py   # …with a REAL translation API
python tools\live_provider_check.py   # real translation via a keyless provider
python tools\browser_live.py --hold 60     # browser mode on the real page
python tools\browser_align_check.py        # cards vs. bubbles, verified by OCR
python tools\bridge_client.py --from-browser --collect --limit 8   # bridge, no browser extension
python tools\bridge_speed.py --panels 40 --clear   # cold-cache speed measurement (A/B)
python tools\ocr_speed.py                      # is the OCR engine worth parallelising?
python tools\extension_live.py --scrolls 6     # the extension in a real Chrome
python tools\extension_modes.py                # cards stability + in-place, live
python tools\extension_watch.py --port <p> --minutes 11   # scroll a chapter, print every console message
python tools\extension_install_probe.py        # which way of installing the extension actually works
python tools\vision_vs_ocr.py                  # what each reader finds, side by side
python tools\vision_latency.py --counts 1,3,5   # gateway speed for vision batches
python tools\inplace_live.py                   # in-place renders from what the cache holds
python tools\cache_dump.py --limit 20 --previews   # what the app cached, per block
```

* `tests/` – offline and deterministic (the GUI tests build the real window with the
  offscreen Qt platform; the provider tests run every backend against local stub
  endpoints that record the exact request; the bridge tests drive a session with a
  fake backend and no socket).
* Localisation tests (`tests/test_i18n.py`) walk the package AST, so **every
  `tr()` string must have a Russian translation** and every template must keep the
  same `{placeholders}` in both languages — a missing translation fails the suite.
* `tools/extension_live.py` is the acceptance test for the extension: it starts
  Chrome, installs the unpacked extension over CDP, opens a chapter and counts the
  cards the extension actually drew (`26 cards` measured on
  `kuaikanmanhua.com/webs/comic-next/185055`, texts in Russian, positions in % of
  the image box). `tools/extension_modes.py` adds the anti-flicker measurement and
  switches to in-place mode through the extension's own popup, and
  `tools/extension_watch.py` scrolls a whole chapter while printing every DOM
  counter and console message — the measurement that shows the chapter filling in
  (badge `2/113 → 109/113`) without a single card being lost.
* `tools/vision_vs_ocr.py` is the "do we still need OCR?" experiment on saved
  panels: on a real chapter it read 5 blocks by OCR (1.8 s) versus 9 blocks by the
  vision model (127 s) — OCR misses whole bubbles (one panel: 0 of 3 found), the
  model reads them all, and the app therefore uses OCR for speed and the model for
  the gaps.
* `tools/selftest.py` – 26 checks covering dependencies, DPI, real PP-OCRv4 on
  synthetic simplified/traditional/vertical pages, the provider registry and
  backend factory, model discovery from a key, a live translation through a
  keyless provider (MyMemory), scenarios A–F, manual refresh, real screen capture,
  Gemini request format, overlay rendering with pixel-level verification, the
  control panel window, and stale-result cancellation. The only check that can be
  skipped is the live Gemini call (needs a real key).
* `tools/live_check.py` – 13 checks on the real desktop: the overlay cards are
  actually drawn (screen pixels change when the overlay is hidden), the pipeline's
  own screenshots do **not** contain the overlay, the overlay is click-through
  (Win32 hit test lands on the manga window), cards stay aligned with the bubbles,
  turning the page updates the overlay, and the control panel shows the live frame,
  lists the blocks, renders real content and its toolbar drives the overlay.
  Run it with `MT_LIVE_PROVIDER=mymemory` to do all of that through a real
  translation API (verified: `你好，今天天气真好。` → «Здравствуйте, это прекрасный
  день.», and after a page turn «Аура вышла из-под контроля» / «Почему?»).
* Localisation tests (`tests/test_i18n.py`) walk the package AST, so **every
  `tr()` string must have a Russian translation** and every template must keep the
  same `{placeholders}` in both languages — a missing translation fails the suite.
* `tools/selftest.py` – 26 checks covering dependencies, DPI, real PP-OCRv4 on
  synthetic simplified/traditional/vertical pages, the provider registry and
  backend factory, model discovery from a key, a live translation through a
  keyless provider (MyMemory), scenarios A–F, manual refresh, real screen capture,
  Gemini request format, overlay rendering with pixel-level verification, the
  control panel window, and stale-result cancellation. The only check that can be
  skipped is the live Gemini call (needs a real key).
* `tools/live_check.py` – 13 checks on the real desktop: the overlay cards are
  actually drawn (screen pixels change when the overlay is hidden), the pipeline's
  own screenshots do **not** contain the overlay, the overlay is click-through
  (Win32 hit test lands on the manga window), cards stay aligned with the bubbles,
  turning the page updates the overlay, and the control panel shows the live frame,
  lists the blocks, renders real content and its toolbar drives the overlay.
  Run it with `MT_LIVE_PROVIDER=mymemory` to do all of that through a real
  translation API (verified: `你好，今天天气真好。` → «Здравствуйте, это прекрасный
  день.», and after a page turn «Аура вышла из-под контроля» / «Почему?»).

---

## 15. Packaging (optional)

```powershell
pip install pyinstaller
pyinstaller packaging\manga_translator.spec
# -> dist\MangaTranslator\MangaTranslator.exe   (windowed, no console)
```

The spec collects the bundled PP-OCRv4 ONNX models and the PySide6/onnxruntime
hidden imports, and sets `console=False` so no console window appears during
normal use. Logs still go to `%LOCALAPPDATA%\MangaTranslator\logs\app.log`.
A one-file build (`--onefile`) works as well but starts 3–5 s slower because the
models must be unpacked on every launch.

Verified build on this machine: PyInstaller 6.22.3, Python 3.11.0 →
`dist\MangaTranslator\` (336 MB, `MangaTranslator.exe` 14 MB). The built
executable starts, registers its hotkeys, shows the tray icon and writes this to
its log:

```text
Manga Translator 0.9.0 starting (python 3.11.0)
display: {'awareness': 'per-monitor', 'scale': 1.0, 'monitors': [(0, 0, 2560, 1440), (2560, -473, 1080, 1920)]}
hotkeys active: <ctrl>+<alt>+M, <ctrl>+<alt>+H, <ctrl>+<alt>+R, <ctrl>+<alt>+S
ready; hotkeys: manga mode: ctrl+alt+m · overlay: ctrl+alt+h · refresh: ctrl+alt+r · region: ctrl+alt+s
```

Note: a frozen build has no console, so `--selftest` output is not visible there;
run the self-test from the Python checkout instead.

---

## 16. Known limitations

* **Translation needs a key and network** for cloud providers. Without any key you
  can still run the whole pipeline: `mymemory` (keyless, small daily quota),
  a local LLM server (Ollama / LM Studio / llama.cpp / vLLM) or the offline `fake`
  stub. Offline/rate-limit/quota errors are shown as readable notifications and the
  next page is retried; the app never crashes on them.
* **The default model name `gemini-3.8-flash` follows the task specification.**
  If your key does not expose it, press **Load models** in Settings and pick one
  from the live list (a `404` notification also says so).
* **Classic MT providers have no page context.** Google Translate, DeepL, Yandex,
  Microsoft, LibreTranslate and MyMemory translate the page as a batch of texts
  (still one request per page), so they cannot keep character voices or use the
  glossary prompt — the glossary is still enforced on the result. Use an LLM
  provider when tone and consistency matter.
* **Structured output** is requested with `response_format`/`responseSchema` where
  the provider supports it and automatically falls back to prompt-only JSON when a
  provider rejects it (the parser tolerates fences, bare arrays and id-keyed maps).
* **Vertical text** is recognized by re-reading tall columns rotated upright; on
  very stylised or heavily rotated lettering accuracy drops (that is the SFX case).
* **Traditional Chinese**: PP-OCRv4's dictionary contains traditional glyphs and
  they are usually read correctly, but a traditional character is occasionally
  returned as its simplified form (measured on the synthetic test page: 4/5 key
  glyphs correct).
* **SFX detection is heuristic** (glyph size, box area, extreme aspect ratio,
  rotation, isolated short low-confidence text). It is deliberately conservative
  because dropping a real line is worse than translating an SFX. Set
  `TRANSLATE_SFX=true` to translate everything.
* **Region selection is clamped to one screen.** Dragging a rectangle across two
  monitors with different DPI is not supported (each screen is scaled
  independently); window mode follows a window across monitors instead.
* **Window mode captures the window's screen rectangle**, not its content, so an
  occluded window shows whatever is on top of it. `mss`/GDI capture is also
  blocked by hardware-DRM content (some streaming players) — the app reports that
  instead of failing silently.
* **Cache identity is a perceptual hash** of the whole region. Two visually very
  similar pages can therefore share a cache entry — tune
  `Settings → Cache → Page match tolerance` (pHash bits, default 6) if that ever
  happens; a page that differs only by a tiny scroll offset is treated as the
  same page on purpose.
* **Page context is bounded** (`CONTEXT_PAGES`, default 2) and only used as
  context — old lines are never re-translated.
* **Debug HUD** re-paints on every poll and costs a little CPU; leave it off for
  normal reading.
* `paddleocr` is supported as an alternative OCR engine but not installed by
  default (it is a ~600 MB dependency and slower to set up than the ONNX port of
  the same models); selecting it without installing falls back to RapidOCR and
  tells you why.

---

## 17. Troubleshooting

| Symptom | Fix |
|---|---|
| "Select the manga region first" | Tray → Select Region… and drag over the page. |
| Nothing happens on page turns | Lower `Change detection interval`, or raise the sensitivity (`Change threshold`, smaller = more sensitive) in Settings → General. |
| Cards appear but text is gibberish | Wrong model for the provider: Settings → Translation → **Load models** → pick one → **Test connection**. |
| "rejected the API key" | Check the key for the *selected* provider (`Settings → Translation` shows which env var to set). |
| Model list is not loading | No key set, no network, or the provider has no listing endpoint — the built-in list is used and the reason is shown under the button. |
| "rate limit / quota reached" | The page is retried automatically with backoff; check your quota. |
| Local server not reachable | Start it first (e.g. `ollama serve`, LM Studio → Start server) and check the endpoint in Settings. |
| "The selected region is no longer on a connected monitor" | Re-select the region (monitors changed). |
| Overlay is not click-through | Enable `Click-through` in Settings → Overlay (it is on by default). |
| Console window appears when launched from a shortcut | Use the PyInstaller build (`console=False`) or start with `pythonw main.py`. |
| Need details | `%LOCALAPPDATA%\MangaTranslator\logs\app.log`, or run with `--debug`. |
