# Codex / Agent Task: Build a Real-Time Chinese Manga Translator for Windows

You are working as an implementation agent. Your job is to build a **working Windows application**, not just propose an architecture.

Do not stop after writing code. Create the project, install dependencies where possible, run it, fix runtime/import issues, and leave the project in a usable state.

Do not spend excessive time discussing options. Make reasonable engineering decisions yourself and proceed.

---

# Goal

Build a Windows desktop utility that automatically translates Chinese manga/comic pages on screen into Russian.

Desired user experience:

```text
Open manga in browser / reader
↓
Select target window or screen region once
↓
Enable Manga Mode
↓
Read normally
↓
Turn / scroll to next page
↓
App detects that the page changed
↓
App waits until the page stabilizes
↓
OCR detects Chinese text blocks
↓
All current-page dialogue is translated together
↓
Russian translations appear as overlay cards near/on top of the original bubbles
↓
User turns the next page
↓
Process repeats automatically
```

The user should NOT have to press a translate button for every page.

Primary target language: **Russian**.

Primary source language: **Chinese**, both Simplified and Traditional.

---

# Implementation Priorities

Priorities, in this exact order:

1. Reliable page-change detection.
2. Reliable screenshot capture.
3. Good Chinese OCR.
4. Correct mapping of OCR text to coordinates.
5. Fast page-level translation.
6. Correct overlay placement.
7. Cancellation of stale work.
8. Cache.
9. Settings / tray polish.
10. Advanced scanlation-style redraw only after MVP works.

Do not spend significant time on bubble inpainting before the basic overlay translator is working.

---

# Required MVP

The MVP is considered complete only when all of the following work:

- User can select a screen region.
- App continuously monitors that region.
- App detects a meaningful page change automatically.
- App waits for the new page to visually stabilize.
- App captures the final image.
- App performs Chinese OCR.
- OCR returns text + coordinates.
- App groups nearby OCR lines into usable dialogue blocks.
- All current-page blocks are translated in one LLM request where practical.
- Russian translations are associated back with the correct block IDs.
- An always-on-top click-through overlay displays translations over/near the page.
- When the user switches to another page, old translations disappear and new translations are produced.
- Stale translation results never appear on a newer page.
- Previously translated pages can be loaded from cache.
- User can manually refresh.
- User can show/hide translation overlay.

Only after this works should you improve UX or add advanced features.

---

# Platform

Target:

```text
Windows 10/11
```

Preferred language:

```text
Python
```

Use a different stack only if there is a strong technical reason.

Avoid Electron unless absolutely necessary.

---

# Suggested Project Structure

Use a modular project. Do not put everything into one file.

Example:

```text
manga_translator/
├── main.py
├── config.py
├── requirements.txt
├── .env.example
├── .gitignore
│
├── capture/
│   ├── screen_capture.py
│   ├── window_capture.py
│   └── region_selector.py
│
├── detection/
│   ├── page_change.py
│   └── image_similarity.py
│
├── ocr/
│   ├── base.py
│   ├── paddle.py
│   └── grouping.py
│
├── translation/
│   ├── base.py
│   ├── gemini.py
│   └── prompts.py
│
├── overlay/
│   ├── overlay_window.py
│   ├── layout.py
│   └── widgets.py
│
├── cache/
│   └── page_cache.py
│
├── glossary/
│   └── glossary.py
│
└── ui/
    ├── tray.py
    ├── settings.py
    └── notifications.py
```

This structure is only a guideline. Improve it if needed.

---

# 1. Region Selection

MVP must support:

```text
Select Region
```

User should be able to drag a rectangle over the manga area.

Store:

```text
x
y
width
height
monitor
DPI / scaling information
```

Region selection must work correctly on Windows scaling values such as:

- 100%
- 125%
- 150%

Support multi-monitor setups as well as reasonably possible.

Later, optionally add:

```text
Select Window
```

for Chrome, Edge, manga readers, Discord, Telegram, image viewers, etc.

Region mode is mandatory for MVP.

---

# 2. Screen Capture

Capture only the selected region.

Choose a fast Windows-compatible capture method.

Possible options include:

- `mss`
- Windows Graphics Capture
- DXGI/Desktop Duplication wrappers
- another fast capture implementation

Choose the simplest reliable option first.

The overlay itself must NOT contaminate OCR screenshots if possible.

If necessary:

- temporarily hide overlay during capture;
- or capture the source window beneath it;
- or structure the overlay so it is excluded from capture.

Verify this explicitly.

---

# 3. Page Change Detection

Do NOT run full OCR continuously.

Implement cheap change detection.

Recommended loop:

```text
Every ~100–300 ms:
    capture small/downscaled preview
    compute similarity to previous preview
    if change below threshold:
        do nothing
    if significant change detected:
        enter "changing" state
        wait until frames become stable
        when stable for ~250–500 ms:
            mark as new page
            trigger full processing
```

Use a robust image similarity metric.

Candidates:

- perceptual hash
- average hash / pHash
- normalized pixel difference
- SSIM
- combinations of downscaled grayscale difference + pHash

Do not treat tiny animation/cursor movement as a new page.

Make thresholds configurable.

---

# 4. Debounce / Stabilization

This is critical.

Scrolling may generate dozens of different intermediate frames.

Example:

```text
page starts changing
↓
several frames differ
↓
scrolling continues
↓
frames finally stabilize
↓
wait stable_duration
↓
process only the final frame
```

Do not OCR intermediate scroll frames.

Recommended configurable values:

```text
POLL_INTERVAL_MS=150
STABLE_DURATION_MS=350
CHANGE_THRESHOLD=...
```

Choose sensible defaults based on testing.

---

# 5. Processing Generation ID / Cancellation

Every detected page must receive a unique generation ID.

Example:

```python
page_generation += 1
current_generation = page_generation
```

All OCR/translation/overlay tasks must belong to that generation.

If user goes:

```text
Page A
→ Page B
→ Page C
```

while A/B are still processing:

- A and B results must be discarded;
- only C may update the overlay.

If requests can be cancelled safely, cancel them.

Otherwise invalidate their result using generation IDs.

This is mandatory.

---

# 6. OCR

Primary OCR requirement:

Good Chinese OCR.

Support:

- Simplified Chinese
- Traditional Chinese
- horizontal text
- vertical text
- small dialogue text
- speech bubbles
- captions
- text over artwork

Preferred initial candidate:

```text
PaddleOCR
```

But do not blindly use it if a better currently available local OCR is clearly superior for Chinese manga.

Avoid using Tesseract as the primary engine unless no better option is practical.

Prefer local OCR for speed and cost.

OCR result must include at least:

```json
{
  "text": "原文",
  "bbox": [x1, y1, x2, y2],
  "confidence": 0.95
}
```

Preserve original coordinates relative to the capture region.

---

# 7. OCR Grouping

OCR may return separate rows/characters.

Group related OCR detections into dialogue blocks.

Use heuristics based on:

- bounding-box distance
- overlap
- text direction
- vertical alignment
- horizontal alignment
- line spacing
- font size / box height similarity
- reading order

The MVP does NOT need perfect speech-bubble detection.

The goal is:

```text
multiple OCR lines belonging to one visible dialogue block
→ one translation block
```

Keep the original component boxes if useful for future layout.

---

# 8. Confidence Filtering

Use configurable confidence filtering.

Example:

```text
OCR_MIN_CONFIDENCE=0.55
```

Do not blindly discard every low-confidence result; manga OCR is noisy.

Provide a reasonable default.

Optionally retain low-confidence text for debugging.

---

# 9. Sound Effects

Add setting:

```text
TRANSLATE_SFX=false
```

Default:

```text
false
```

Try to avoid translating obvious giant stylized SFX.

Possible heuristics:

- unusually large text box
- extreme rotation
- isolated text outside typical dialogue regions
- very irregular aspect ratio
- low OCR confidence
- large glyphs over artwork

Do not let imperfect SFX detection block the MVP.

---

# 10. Translation Backend

Use Gemini API for the first implementation.

Default text model:

```text
gemini-3.8-flash
```

Make the model configurable:

```text
TEXT_MODEL=gemini-3.8-flash
```

Create an abstraction such as:

```python
translator.translate_page(blocks, context, glossary)
```

Do not hardwire the rest of the application to Gemini-specific code.

Future backends may include:

- DeepSeek
- another cloud model
- local LLM

---

# 11. API Key

Use an environment variable or secure local config:

```text
GEMINI_API_KEY=...
```

Requirements:

- never hardcode the real key;
- never print the key to logs;
- add `.env` to `.gitignore`;
- provide `.env.example`.

Example:

```text
GEMINI_API_KEY=
TEXT_MODEL=gemini-3.8-flash
TARGET_LANGUAGE=Russian
```

---

# 12. Page-Level Translation

Do NOT send one API request per bubble.

Gather all dialogue blocks from the current page.

Example input:

```json
[
  {"id": 1, "text": "……"},
  {"id": 2, "text": "……"},
  {"id": 3, "text": "……"}
]
```

Send them together so the model has page context.

Require structured output:

```json
[
  {"id": 1, "translation": "…"},
  {"id": 2, "translation": "…"},
  {"id": 3, "translation": "…"}
]
```

Validate the result.

Handle malformed model output.

If structured-output / JSON-schema support is available and reliable, use it.

Do not trust free-form text parsing if structured responses are supported.

---

# 13. Translation Prompt

Use a dedicated prompt for manga translation.

Intent:

```text
Translate Chinese manga/dialogue into natural Russian.
```

Requirements:

- preserve meaning;
- preserve tone;
- preserve character personality;
- preserve slang;
- keep dialogue natural;
- avoid overly formal Russian;
- do not translate literally when that sounds unnatural;
- maintain consistent names;
- maintain consistent terminology;
- keep results reasonably concise;
- do not merge unrelated bubbles;
- do not add translator commentary;
- do not explain the translation;
- return only structured data requested by the schema.

Suggested conceptual prompt:

```text
You are translating Chinese manga into natural Russian.

Translate only the current page dialogue.

Rules:
- Preserve meaning and tone.
- Keep character voices distinct.
- Preserve slang, humor and casual phrasing.
- Prefer natural Russian over literal word-for-word translation.
- Keep character names and terminology consistent with the provided glossary/context.
- Keep translations concise enough to fit near the original dialogue.
- Do not add explanations.
- Do not merge unrelated dialogue blocks.
- Return only the requested structured JSON.
```

Adapt this as needed.

---

# 14. Context Across Pages

Keep limited recent dialogue context.

Suggested:

```text
previous 1–2 pages
or
last 10–30 translated lines
```

Use it only as context.

Do NOT ask the model to translate old lines again.

Do not let context grow forever.

Keep a configurable rolling window.

Example:

```text
CONTEXT_PAGES=2
```

---

# 15. Glossary

Implement a user-editable glossary.

Example:

```json
{
  "林雪": "Линь Сюэ",
  "灵气": "духовная ци",
  "师兄": "старший брат по школе"
}
```

The translator must use glossary mappings consistently.

Use a simple JSON/YAML format for MVP.

Expose the glossary path in settings/config.

---

# 16. Overlay

Implement a transparent always-on-top overlay over the selected region.

Requirements:

- borderless;
- transparent background;
- click-through by default;
- always-on-top;
- aligned precisely with capture coordinates;
- does not block scrolling/clicking in the manga reader;
- follows selected region coordinates;
- hides when Manga Mode is disabled.

Each translated block should display near or over its original OCR bbox.

---

# 17. Overlay Card Style

For MVP use readable overlay cards.

Example:

```text
┌─────────────────┐
│ Русский перевод │
└─────────────────┘
```

Requirements:

- semi-transparent background;
- readable font;
- automatic wrapping;
- reasonable padding;
- avoid going off-screen;
- adapt width to the OCR block;
- prefer covering as little artwork as practical.

Do not spend excessive time on visual polish before functionality works.

---

# 18. Original / Translation Toggle

Implement:

```text
Show translation
Hide translation
```

Provide:

- tray menu action;
- and/or configurable hotkey.

This lets the user quickly inspect the Chinese original.

---

# 19. Manual Refresh

Implement:

```text
Refresh Page
```

This should force:

```text
capture
→ OCR
→ translation
→ overlay update
```

even if automatic change detection did not trigger.

---

# 20. Cache

Implement page cache.

Store at minimum:

```text
page identity/hash
OCR blocks
translations
coordinates
timestamp
optional context metadata
```

Use a perceptual or similarity-friendly page identity rather than only exact binary hash.

If the user navigates:

```text
A → B → A
```

page A should ideally reappear from cache almost instantly, without repeating OCR or translation.

---

# 21. Cache Key

Use a robust image identity approach.

Possible design:

```text
normalized screenshot
↓
resize
↓
grayscale
↓
perceptual hash
```

Small differences from:

- anti-aliasing;
- tiny scroll offset;
- cursor;
- scaling;

should not always create a new page entry.

Do not aggressively merge genuinely different pages.

---

# 22. Cache Storage

Use a simple persistent local store.

Options:

- SQLite
- JSON metadata + image/hash files
- another lightweight embedded solution

Prefer SQLite if it simplifies lookup and maintenance.

Add configurable maximum cache size.

Example:

```text
CACHE_ENABLED=true
CACHE_MAX_MB=1000
```

Implement `Clear Cache`.

---

# 23. Performance

Target experience:

```text
page turned
↓
~0.2–0.5 s detect/stabilize
↓
OCR
↓
translation
↓
overlay
```

Aim for translations to appear within a couple of seconds where possible.

Optimization principles:

- downscale previews for change detection;
- avoid full OCR until stable;
- avoid duplicate API calls;
- cache pages;
- translate all blocks in one request;
- run OCR/API off the UI thread;
- do not block the overlay event loop.

---

# 24. Threading / Async

UI must remain responsive.

Use:

- async tasks;
- worker threads;
- process pool where appropriate;
- or a clean producer/consumer design.

Do not run expensive OCR synchronously on the GUI thread.

---

# 25. Error Handling

Handle these cases gracefully:

- no region selected;
- OCR finds nothing;
- OCR crashes;
- Gemini API key missing;
- invalid API key;
- network unavailable;
- request timeout;
- quota exceeded;
- rate limited;
- malformed model response;
- cache corruption;
- selected monitor disconnected;
- DPI changes;
- capture failure;
- overlay failure.

Do not show raw Python tracebacks to the user.

Log technical details to a file.

Show small readable notifications.

---

# 26. Retry Strategy

For temporary API/network errors:

- use limited retries;
- use backoff;
- do not spam Gemini repeatedly;
- cancel retry if page generation has changed.

Never keep retrying an old page after user has moved on.

---

# 27. DPI / Multi-Monitor

This is important.

Test coordinate mapping with:

- 100% scaling;
- 125% scaling;
- 150% scaling;
- secondary monitor;
- monitors with different scaling where possible.

Overlay rectangles must align with the OCR source.

If Windows APIs return logical vs physical pixels differently, handle the conversion correctly.

---

# 28. Tray

Implement a tray icon.

Minimum menu:

```text
Enable Manga Mode
Disable Manga Mode
Select Region
Refresh Page
Show/Hide Translation
Settings
Clear Cache
Exit
```

Do not require the user to keep a large main window open.

---

# 29. Global Hotkeys

Use a small number of hotkeys.

Suggested:

```text
Toggle Manga Mode
Show/Hide Translation
Refresh Page
```

Make them configurable.

Do not hardcode user-specific keys.

Choose reasonable defaults yourself.

---

# 30. Settings

Provide simple local settings.

At minimum:

```text
General
- Manga Mode enabled
- Selected region
- Change detection interval
- Stabilization duration
- Change threshold

OCR
- OCR engine
- Confidence threshold
- Simplified Chinese enabled
- Traditional Chinese enabled

Translation
- Gemini API key or env-based key status
- Text model
- Target language
- Context page count
- Glossary path
- Translate SFX

Overlay
- Font size
- Background opacity
- Padding
- Show original/translation
- Click-through

Cache
- Enabled
- Max size
- Clear cache
```

A simple settings dialog is enough.

---

# 31. Logging

Create application logs.

Log:

- page-change detection;
- page generation IDs;
- OCR duration;
- OCR block count;
- translation duration;
- cache hit/miss;
- API errors;
- discarded stale generations.

Do NOT log:

- API key;
- other secrets.

Avoid dumping entire manga pages/text into logs unless debug mode is explicitly enabled.

---

# 32. Debug Mode

Add optional debug mode.

Useful debug overlays:

- OCR bounding boxes;
- confidence scores;
- block IDs;
- current page hash;
- change score;
- generation ID.

Default debug mode off.

This will make OCR/layout tuning much easier.

---

# 33. Testing Scenarios

Actually test these flows.

## Scenario A — Normal page turn

```text
Page A
wait for translation
Page B
```

Expected:

- A overlay disappears;
- B detected automatically;
- B OCR runs;
- B translation appears.

## Scenario B — Fast page switching

```text
A → B → C
```

Expected:

- late A/B results never overlay C;
- C wins because it has the latest generation ID.

## Scenario C — Cache

```text
A → B → A
```

Expected:

- second visit to A should use cache if similarity matches;
- avoid duplicate Gemini request.

## Scenario D — Continuous scrolling

Scroll gradually through content.

Expected:

- application does not trigger OCR on every intermediate frame;
- translation starts only when view stabilizes.

## Scenario E — No text

Switch to an image/page with no Chinese dialogue.

Expected:

- no crash;
- overlay clears;
- no unnecessary translation request.

## Scenario F — API offline

Disable internet.

Expected:

- app remains running;
- readable error notification;
- next page can be retried after connection returns.

## Scenario G — Resize / DPI

Move or resize the monitored region/window if relevant.

Expected:

- coordinate mapping remains correct.

---

# 34. Packaging

After the Python version works, prepare packaging instructions.

Preferred:

```text
PyInstaller
```

Goal:

- runnable Windows executable;
- no visible console window during normal use;
- tray starts normally.

Do not let packaging work block the functional MVP.

---

# 35. Dependencies

Use a `requirements.txt` or equivalent.

Pin versions when version sensitivity matters.

Before finishing:

- install dependencies;
- verify imports;
- start the application;
- fix broken package names / APIs.

Do not leave pseudocode imports.

---

# 36. README

Create a concise README containing:

1. what the application does;
2. system requirements;
3. Python version;
4. dependency install command;
5. where to set `GEMINI_API_KEY`;
6. how to run;
7. how to select a region;
8. how to enable Manga Mode;
9. how to refresh;
10. how to show/hide overlay;
11. where cache is stored;
12. current known limitations.

---

# 37. Do Not Overbuild

Before the MVP works, do NOT spend major effort on:

- perfect speech-bubble segmentation;
- font matching;
- inpainting;
- full image reconstruction;
- automatic typesetting;
- chapter downloading;
- browser extension integration;
- cloud backend;
- user accounts;
- telemetry;
- unnecessary framework migrations.

Get the core live translator working first.

---

# 38. Phase 2 — Only After MVP

After MVP is stable, optionally implement:

## Better Bubble Detection

Detect speech bubbles explicitly and place translations inside them.

## Background Cleanup

Cover/remove original Chinese text.

## Inpainting

Restore bubble/background underneath Chinese glyphs.

## Typesetting

Fit Russian translation into bubble shape.

## Export

Save a translated page image.

## Chapter Mode

Process entire folders/chapters offline.

## Better OCR

Swap in a manga-specific OCR model if it significantly improves Chinese text detection.

---

# 39. Final Deliverable

Do not return only an explanation.

You must leave behind a working project.

Before finishing:

1. Create all required files.
2. Install or declare dependencies.
3. Run the program.
4. Fix import errors.
5. Fix obvious runtime errors.
6. Verify region selection.
7. Verify screenshot capture.
8. Verify change detection.
9. Verify OCR on at least one Chinese test image/page if available.
10. Verify Gemini request formatting.
11. Verify structured translation parsing.
12. Verify overlay rendering.
13. Verify stale-generation cancellation.
14. Verify cache logic.
15. Verify manual refresh.
16. Verify show/hide overlay.

Then provide a short implementation report.

Report:

```text
Implemented:
- ...

Files created/changed:
- ...

Dependencies:
- ...

Run:
- ...

Configuration:
- ...

Hotkeys:
- ...

Known limitations:
- ...
```

If something cannot be tested because the environment lacks a GUI/API key/test manga, clearly state that specific limitation, but still test every component that can be tested.

---

# Acceptance Criterion

The project succeeds when this user experience works:

```text
I open Chinese manga.
I select its screen region once.
I enable Manga Mode.
I turn the page.
I wait briefly.
All detected dialogue on the new page appears translated into Russian as an overlay.
I turn another page.
The overlay updates automatically.
I go back to a previously translated page.
The cached translation appears quickly.
```

That is the primary goal. Optimize for this workflow.
