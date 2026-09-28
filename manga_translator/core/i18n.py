"""Tiny gettext-style localisation for the user interface.

English source strings are the keys, so any string that is not translated falls
back to English instead of showing a placeholder. Templates use ``str.format``
placeholders (``{name}``), which makes word order natural in every language.

    from .ui.i18n import tr, set_language
    set_language("ru")
    label.setText(tr("Refresh Page"))
    status.setText(tr("{n} blocks · {origin}").format(n=3, origin="cache"))
"""
from __future__ import annotations

import threading
from typing import Dict, List, Tuple

DEFAULT_LANGUAGE = "ru"

LANGUAGES: List[Tuple[str, str]] = [
    ("ru", "Русский"),
    ("en", "English"),
]

_lock = threading.RLock()
_language = DEFAULT_LANGUAGE

# --- English -> Russian ----------------------------------------------------
RU: Dict[str, str] = {
    # ---- main window ------------------------------------------------------
    "Manga Translator — control panel": "Manga Translator — панель управления",
    "Manga Translator": "Manga Translator",
    "Manga Mode": "Режим манги",
    "Manga Mode: ON": "Режим манги: ВКЛ",
    "Manga Mode ON": "Режим манги ВКЛ",
    "Translation": "Перевод",
    "Select Region": "Выбрать область",
    "Select Window": "Выбрать окно",
    "Refresh Page": "Обновить страницу",
    "Settings": "Настройки",
    "Glossary": "Глоссарий",
    "Hide to tray": "Свернуть в трей",
    "Live page preview": "Предпросмотр страницы",
    "About": "О программе",
    "Controls": "Управление",
    "Log": "Журнал",
    "Clear cache": "Очистить кэш",
    "Open log file": "Открыть файл журнала",
    "Open data folder": "Открыть папку данных",
    "Statistics…": "Статистика…",
    "Hotkeys": "Горячие клавиши",
    "Exit": "Выход",
    "File": "Файл",
    "Run": "Запуск",
    "View": "Вид",
    "Tools": "Инструменты",
    "Help": "Справка",
    "Debug HUD on overlay": "Отладочный HUD на оверлее",
    "Draw OCR boxes in preview": "Рисовать рамки OCR в предпросмотре",
    "Refresh preview": "Обновить предпросмотр",
    "Enable/disable automatic page translation": "Включить/выключить автоматический перевод страниц",
    "Show/hide the on-screen translation overlay": "Показать/скрыть оверлей с переводом",
    "Select a region to start (Run → Select Region)":
        "Выберите область, чтобы начать (Запуск → Выбрать область)",
    "Monitoring the selected region…": "Наблюдение за выбранной областью…",
    "No suitable windows found. Use Select Region instead.":
        "Подходящих окон не найдено. Используйте «Выбрать область».",
    "Capture and follow:": "Захватить и следить за окном:",
    "Save page text": "Сохранить текст страницы",
    "Text files (*.txt)": "Текстовые файлы (*.txt)",
    "Could not save the file:\n": "Не удалось сохранить файл:\n",
    "saved ": "сохранено: ",
    "ready": "готово",
    "Global hotkeys": "Глобальные горячие клавиши",
    "hotkeys disabled": "горячие клавиши отключены",
    "Closing hides the window; the tray keeps translating.":
        "Закрытие скрывает окно; перевод продолжает работать в трее.",
    "Hotkeys are global and work while another window (your reader) has focus.<br>"
    "The overlay itself is click-through: scrolling and clicking keep working.":
        "Горячие клавиши глобальные — работают, когда фокус в другом окне (вашей читалке).<br>"
        "Сам оверлей «сквозной» для мыши: прокрутка и клики продолжают работать.",
    "Region → page change detection → PP-OCRv4 → one Gemini request per page → "
    "click-through overlay.<br><br>":
        "Область → определение смены страницы → PP-OCRv4 → один запрос к провайдеру "
        "на страницу → оверлей, сквозной для мыши.<br><br>",
    "<b>{name}</b> {version}<br>": "<b>{name}</b> {version}<br>",
    "OCR: {engine}<br>": "OCR: {engine}<br>",
    "Translator: {backend} ({model})<br>": "Переводчик: {backend} ({model})<br>",
    "Data folder: ": "Папка данных: ",
    "Region → change detection → OCR → one request per page → overlay.":
        "Область → определение смены страницы → OCR → один запрос на страницу → оверлей.",
    # ---- panels -----------------------------------------------------------
    "Status": "Состояние",
    "Dialogue blocks": "Блоки диалога",
    "Manga Mode label": "Режим манги",
    "Region": "Область",
    "Displays": "Мониторы",
    "OCR engine": "Движок OCR",
    "Translator": "Переводчик",
    "Generation": "Поколение",
    "Detector": "Детектор",
    "Change score": "Оценка изменения",
    "Page hash": "Хеш страницы",
    "Last page": "Последняя страница",
    "Cache": "Кэш",
    "Counters": "Счётчики",
    "not selected": "не выбрана",
    "off": "выкл",
    "ON": "ВКЛ",
    "—": "—",
    "no page processed yet": "страница ещё не обработана",
    "Original (zh)": "Оригинал (zh)",
    "Translation (ru)": "Перевод (ru)",
    "Conf": "Увер.",
    "Kind": "Тип",
    "Box": "Рамка",
    "dialogue": "диалог",
    "SFX": "SFX",
    "skipped": "пропущено",
    "Copy translations": "Копировать перевод",
    "Save page text…": "Сохранить текст страницы…",
    "{blocks} blocks · {translated} translated": "{blocks} блоков · переведено {translated}",
    "{blocks} blocks · {translated} translated · from cache":
        "{blocks} блоков · переведено {translated} · из кэша",
    "no dialogue on this page": "на этой странице нет диалогов",
    "copied {n} line(s) to the clipboard": "скопировано строк: {n}",
    "Auto-refresh": "Автообновление",
    "Refresh now": "Обновить сейчас",
    "no log file": "файл журнала не найден",
    "{pages} pages · {mb} MB · {hits} hits": "{pages} страниц · {mb} МБ · попаданий: {hits}",
    "{pages} pages · {mb} MB · {hits} hits · disabled":
        "{pages} страниц · {mb} МБ · попаданий: {hits} · выключен",
    "{w}×{h} at ({x},{y}) · {monitor}": "{w}×{h} в ({x},{y}) · {monitor}",
    "{displays} · region at {scale}%": "{displays} · область при {scale}%",
    "{state} · stable {ms} ms": "{state} · стабильно {ms} мс",
    "{score} (pixels {pixels}, phash {bits} bits)":
        "{score} (пиксели {pixels}, phash {bits} бит)",
    "{total} ms total · ocr {ocr} ms · translate {mt} ms · {blocks} blocks":
        "{total} мс всего · OCR {ocr} мс · перевод {mt} мс · блоков: {blocks}",
    "{total} ms total · ocr {ocr} ms · translate {mt} ms · {blocks} blocks · from cache":
        "{total} мс всего · OCR {ocr} мс · перевод {mt} мс · блоков: {blocks} · из кэша",
    "pages {pages} · cache hits {hits} · stale dropped {stale} · errors {errors}":
        "страниц {pages} · попаданий в кэш {hits} · устаревших отброшено {stale} · ошибок {errors}",
    "idle": "ожидание",
    "changing": "смена страницы",
    # ---- tray -------------------------------------------------------------
    "Manga Translator — idle": "Manga Translator — ожидание",
    "Open control panel": "Открыть панель управления",
    "Enable Manga Mode": "Включить режим манги",
    "Disable Manga Mode": "Выключить режим манги",
    "Select Region…": "Выбрать область…",
    "Select Window…": "Выбрать окно…",
    "Show translation": "Показать перевод",
    "Hide translation": "Скрыть перевод",
    "Settings…": "Настройки…",
    "Glossary…": "Глоссарий…",
    "Clear Cache": "Очистить кэш",
    "Open Log Folder": "Открыть папку журналов",
    "(not available)": "(недоступно)",
    "(no windows found)": "(окна не найдены)",
    "Manga Translator — {status}": "Manga Translator — {status}",
    # ---- preview ----------------------------------------------------------
    "Captured region — green = translated, amber = SFX/untranslated,\n"
    "dashed blue = where the overlay card is drawn (double-click to refresh)":
        "Захваченная область — зелёный = переведено, янтарный = SFX/без перевода,\n"
        "пунктир синий = где будет карточка оверлея (двойной клик — обновить)",
    "No region selected yet": "Область ещё не выбрана",
    # ---- settings dialog --------------------------------------------------
    "Manga Translator — Settings": "Manga Translator — Настройки",
    "Manga Translator — Statistics": "Manga Translator — Статистика",
    "General": "Общие",
    "OCR": "OCR",
    "Overlay": "Оверлей",
    "Language": "Язык интерфейса",
    "Interface language": "Язык интерфейса",
    "Manga Mode enabled": "Режим манги включён",
    "Capture region": "Область захвата",
    "Change detection interval": "Интервал проверки изменений",
    "Stabilization duration": "Время стабилизации",
    "Change threshold": "Порог изменения",
    "Preview size": "Размер предпросмотра",
    "Force processing after": "Принудительно обрабатывать через",
    "Debug mode (OCR boxes, hashes, timings)":
        "Режим отладки (рамки OCR, хеши, тайминги)",
    "Engine": "Движок",
    "Minimum confidence": "Минимальная уверенность",
    "OCR downscale limit": "Предел уменьшения для OCR",
    "Line grouping gap ratio": "Коэффициент склейки строк",
    "Keep low-confidence text for debug": "Сохранять текст с низкой уверенностью (отладка)",
    "Simplified Chinese": "Упрощённый китайский",
    "Traditional Chinese": "Традиционный китайский",
    "Re-read vertical CJK columns": "Перечитывать вертикальные колонки CJK",
    "Translate sound effects (TRANSLATE_SFX)": "Переводить звуковые эффекты (TRANSLATE_SFX)",
    "Provider": "Провайдер",
    "API key status": "Статус ключа",
    "API key": "API-ключ",
    "Model": "Модель",
    "Target language": "Целевой язык",
    "Endpoint": "Endpoint",
    "Context pages": "Страниц контекста",
    "Glossary file": "Файл глоссария",
    "Request timeout": "Таймаут запроса",
    "Structured output": "Структурированный вывод",
    "Ignore site UI text (titles, next-chapter bars)":
        "Игнорировать текст интерфейса сайта (заголовки, полосы «следующая глава»)",
    "Browser mode: translate big stylised text too":
        "Браузерный режим: переводить и крупный стилизованный текст",
    "Drop site menus that never move":
        "Убирать меню сайта, которые не двигаются (язык, кнопки, значки)",
    "API retries": "Повторы запросов",
    "Blocks per request": "Блоков на запрос",
    "Temperature (LLMs)": "Температура (для LLM)",
    "Default": "По умолчанию",
    "Load models": "Загрузить модели",
    "Load languages": "Загрузить языки",
    "Test connection": "Проверить соединение",
    "Ask the provider which models this API key can use":
        "Запросить у провайдера список моделей, доступных этому ключу",
    "Reset to the provider's default endpoint": "Вернуть стандартный endpoint провайдера",
    "key for the selected provider (empty = use .env / environment)":
        "ключ для выбранного провайдера (пусто = взять из .env или переменных среды)",
    "translating a test line…": "перевожу тестовую строку…",
    "loading from {provider}…": "загружаю список из {provider}…",
    "{n} built-in option(s); press '{button}' to fetch the live list with your key":
        "встроенных вариантов: {n}; нажмите «{button}», чтобы получить список по вашему ключу",
    "press 'Load models' to fetch the list with your key":
        "нажмите «Загрузить модели», чтобы получить список по вашему ключу",
    "{n} {unit}s from {label}{suffix}": "{n} {unit} от {label}{suffix}",
    "could not fetch the list ({error}) — using built-in options":
        "не удалось получить список ({error}) — используются встроенные варианты",
    "model": "модель",
    "models": "моделей",
    "language": "язык",
    "languages": "языков",
    "Font size": "Размер шрифта",
    "Card opacity": "Прозрачность карточки",
    "Card padding": "Отступы в карточке",
    "Max card width": "Максимальная ширина карточки",
    "Display mode": "Режим отображения",
    "Click-through (never blocks scrolling/clicking)":
        "Сквозной для мыши (не мешает прокрутке и кликам)",
    "Show debug HUD": "Показывать отладочный HUD",
    "Overlay visible": "Оверлей видим",
    "Cache enabled": "Кэш включён",
    "Maximum size": "Максимальный размер",
    "Page match tolerance (pHash bits)": "Допуск совпадения страниц (биты pHash)",
    "Maximum age": "Максимальный возраст",
    "Stored": "Занято",
    "Clear cache now": "Очистить кэш сейчас",
    "Global hotkeys enabled": "Глобальные горячие клавиши включены",
    "Show/Hide translation": "Показать/скрыть перевод",
    "Refresh page": "Обновить страницу",
    "Select region": "Выбрать область",
    "Toggle Manga Mode": "Переключить режим манги",
    "Format: ctrl+alt+m · modifiers: ctrl, alt, shift":
        "Формат: ctrl+alt+m · модификаторы: ctrl, alt, shift",
    "Glossary — term consistency": "Глоссарий — единообразие терминов",
    "Chinese source": "Китайский оригинал",
    "Russian target": "Русский перевод",
    "Add term": "Добавить термин",
    "Remove selected": "Удалить выбранное",
    "Cancel": "Отмена",
    "Apply": "Применить",
    "OK": "OK",
    "Close": "Закрыть",
    "Save": "Сохранить",
    "Statistics": "Статистика",
    "OCR engine:": "Движок OCR:",
    "Translator:": "Переводчик:",
    "Generation:": "Поколение:",
    "Pages processed:": "Обработано страниц:",
    "Cache hits:": "Попаданий в кэш:",
    "Stale discarded:": "Устаревших отброшено:",
    "Avg OCR:": "Средний OCR:",
    "Avg translation:": "Средний перевод:",
    "Blocks total:": "Всего блоков:",
    "Errors:": "Ошибок:",
    "Cache entries:": "Записей в кэше:",
    "Cache size:": "Размер кэша:",
    "Region:": "Область:",
    # ---- pipeline status / errors ----------------------------------------
    "Reading the page…": "Читаю страницу…",
    "Loading the OCR model…": "Загружаю модель OCR…",
    "No Chinese dialogue found on this page.": "Китайский текст на этой странице не найден.",
    "Page unchanged.": "Страница не изменилась.",
    "Screen capture failed. Select the region again.":
        "Не удалось захватить экран. Выберите область заново.",
    "Select a region first.": "Сначала выберите область.",
    "Translating {n} blocks…": "Перевожу блоков: {n}…",
    "Showing {known} known lines, translating {n} new…":
        "Показываю {known} уже переведённых строк, перевожу {n} новых…",
    "All lines came from the translation memory.":
        "Все строки взяты из памяти переводов.",
    "Translation retry in {s:.0f}s ({kind})…": "Повтор перевода через {s:.0f} с ({kind})…",
    "Page from cache ({n} blocks).": "Страница из кэша ({n} блоков).",
    "No translation was produced for this page.": "Для этой страницы перевод не получен.",
    "Unexpected internal error ({kind}). See the log file.":
        "Непредвиденная внутренняя ошибка ({kind}). Подробности в файле журнала.",
    "{n} blocks came back untranslated.": "Без перевода вернулось блоков: {n}.",
    # ---- capture ----------------------------------------------------------
    "Screen capture library (mss) is not installed. Run: pip install -r requirements.txt":
        "Библиотека захвата экрана (mss) не установлена. Выполните: pip install -r requirements.txt",
    "Cannot start screen capture ({kind}).": "Не удалось запустить захват экрана ({kind}).",
    "The selected region is no longer on a connected monitor. Select a new region.":
        "Выбранная область больше не находится на подключённом мониторе. Выберите область заново.",
    "Screen capture failed - the region may be protected (DRM) or the monitor changed.":
        "Не удалось захватить экран: область может быть защищена (DRM) или монитор изменился.",
    "The captured region is completely black - the app you selected may block "
    "screen capture (hardware DRM).":
        "Захваченная область полностью чёрная: выбранное приложение может блокировать "
        "захват экрана (аппаратный DRM).",
    # ---- application ------------------------------------------------------
    "Manga Mode on — translations appear automatically.":
        "Режим манги включён — перевод появляется автоматически.",
    "Manga Mode off.": "Режим манги выключен.",
    "Enable Manga Mode first.": "Сначала включите режим манги.",
    "Select the manga region first — drag over the page now.":
        "Сначала выберите область манги — обведите страницу сейчас.",
    "Region set: {w}×{h} at ({x},{y}) · {scale}% scaling":
        "Область задана: {w}×{h} в ({x},{y}) · масштаб {scale}%",
    "Region selection cancelled.": "Выбор области отменён.",
    "Settings applied.": "Настройки применены.",
    "That window is gone.": "Это окно уже закрыто.",
    "The followed window was closed.": "Отслеживаемое окно закрыто.",
    "Following window: {title}": "Слежу за окном: {title}",
    "Cache cleared ({n} pages).": "Кэш очищен (страниц: {n}).",
    "Glossary saved ({n} terms).": "Глоссарий сохранён (терминов: {n}).",
    "The selected region is no longer on a connected monitor. Select it again.":
        "Выбранная область больше не на подключённом мониторе. Выберите её заново.",
    "Manga Translator is running in the tray. Select a region and enable Manga Mode "
    "({hotkey}).":
        "Manga Translator работает в трее. Выберите область и включите режим манги ({hotkey}).",
    "Language changed — interface updated.": "Язык изменён — интерфейс обновлён.",
    "Manga Mode on — monitoring the region": "Режим манги включён — слежу за областью",
    "Manga Mode off": "Режим манги выключен",
    "page {generation} — {kind}…": "страница {generation} — {kind}…",
    "{blocks} blocks · gen {generation} · {origin}":
        "блоков: {blocks} · поколение {generation} · {origin}",
    "page {generation} ({kind})": "страница {generation} ({kind})",
    "error: {kind}": "ошибка: {kind}",
    " (key: {names})": " (ключ: {names})",
    "cache": "кэш",
    "ocr+mt": "ocr+перевод",
    # ---- browser mode ----------------------------------------------------
    "Browser mode": "Браузерный режим",
    "Browser mode…": "Браузерный режим…",
    "Translate a browser tab directly (no screen capture)":
        "Переводить вкладку браузера напрямую (без захвата экрана)",
    "Browser mode reads the page images straight from the tab: no screen capture, "
    "full resolution and no reader UI. Chrome must be started with remote debugging "
    "(the button below does that with its own profile).":
        "Браузерный режим берёт картинки страницы прямо из вкладки: без захвата экрана, "
        "в полном разрешении и без интерфейса читалки. Chrome нужно запустить с "
        "отладочным портом — кнопка ниже делает это с отдельным профилем.",
    "Not connected": "Не подключено",
    "Find tabs": "Найти вкладки",
    "Open Chrome with debugging": "Открыть Chrome с отладкой",
    "Connect": "Подключить",
    "Translate the whole chapter": "Перевести всю главу",
    "Translate visible pages": "Перевести видимые страницы",
    "Disconnect": "Отключить",
    "Scanning the chapter for pages…": "Сканирую главу: ищу страницы…",
    "No browser with remote debugging found.":
        "Браузер с включённой отладкой не найден.",
    "Chrome did not start with a debugging port.":
        "Chrome не запустился с отладочным портом.",
    "Debug port {port}: {n} tab(s)": "Отладочный порт {port}: вкладок {n}",
    "Debug port {port}": "Отладочный порт {port}",
    "Chapter queued: {n} pages": "Глава поставлена в очередь: страниц {n}",
    "Chapter scan failed": "Не удалось просканировать главу",
    "Cannot list tabs": "Не удалось получить список вкладок",
    # ---- provider / backend messages -------------------------------------
    "Google Gemini": "Google Gemini",
    "{label} rejected the API key (HTTP {status}){hint}. Check Settings → Translation.":
        "{label} отклонил API-ключ (HTTP {status}){hint}. Проверьте Настройки → Перевод.",
    "{label} rejected the API key{hint}: {detail}":
        "{label} отклонил API-ключ{hint}: {detail}",
    "{label}: model or endpoint not found (HTTP 404).{hint} {detail}":
        "{label}: модель или endpoint не найдены (HTTP 404).{hint} {detail}",
    " Check the model name ('{model}') or the base URL in Settings → Translation; "
    "use 'Load models' to list what the key can use.":
        " Проверьте имя модели («{model}») или endpoint в Настройках → Перевод; "
        "кнопка «Загрузить модели» покажет, что доступно ключу.",
    "{label} rejected the request: {detail}": "{label} отклонил запрос: {detail}",
    "{label}: the page had too much text for one request.":
        "{label}: текста на странице слишком много для одного запроса.",
    "{label}: rate limit / quota reached. The next page will be retried automatically.":
        "{label}: достигнут лимит запросов/квоты. Следующая страница будет повторена автоматически.",
    "{label} is temporarily unavailable (HTTP {status}). Retrying shortly.":
        "{label} временно недоступен (HTTP {status}). Скоро повторю.",
    "{label} request failed (HTTP {status}): {detail}":
        "Запрос к {label} не удался (HTTP {status}): {detail}",
    "{label}: no answer in time (request timed out).":
        "{label}: нет ответа вовремя (таймаут запроса).",
    "{label}: no connection. Check your internet connection.":
        "{label}: нет соединения. Проверьте подключение к интернету.",
    "{label}: request failed.": "{label}: запрос не удался.",
    "{label} returned a response that is not JSON.":
        "{label} вернул ответ, который не является JSON.",
    "{label} returned no answer.": "{label} не вернул ответ.",
    "{label} returned an empty answer.": "{label} вернул пустой ответ.",
    "{label} blocked this page (content filter).": "{label} заблокировал страницу (фильтр контента).",
    "The page had too much text for one answer; it will be split.":
        "Текста слишком много для одного ответа; он будет разбит на части.",
    "{label} needs a key: set {env} in .env / the environment or paste it in "
    "Settings → Translation.":
        "{label} требует ключ: задайте {env} в .env или переменных среды, либо вставьте его "
        "в Настройки → Перевод.",
    "{label}: no model selected.": "{label}: модель не выбрана.",
    "Gemini API key is not set. Put GEMINI_API_KEY=... into .env "
    "(next to main.py), set the environment variable, or paste it in "
    "Settings → Translation.":
        "API-ключ Gemini не задан. Добавьте GEMINI_API_KEY=... в .env рядом с main.py, "
        "задайте переменную среды или вставьте ключ в Настройки → Перевод.",
    "TEXT_MODEL is empty - set it to a Gemini text model name.":
        "TEXT_MODEL пуст — укажите имя текстовой модели Gemini.",
    "Gemini blocked this page (safety filter). Nothing was translated.":
        "Gemini заблокировал страницу (фильтр безопасности). Перевод не выполнен.",
    "Gemini refused to translate this page (safety filter).":
        "Gemini отказался переводить страницу (фильтр безопасности).",
    "The page had too much text for one answer. It will be split into smaller parts.":
        "Текста слишком много для одного ответа. Он будет разбит на части.",
    "Gemini returned an unreadable response.": "Gemini вернул нечитаемый ответ.",
    "Gemini did not answer in time (request timed out).": "Gemini не ответил вовремя (таймаут).",
    "No connection to the Gemini API. Check your internet connection.":
        "Нет соединения с Gemini API. Проверьте подключение к интернету.",
    "Claude returned an empty answer.": "Claude вернул пустой ответ.",
    "Claude refused to translate this page.": "Claude отказался переводить страницу.",
    "Claude returned no usable translation for this page.":
        "Claude не вернул пригодный перевод для этой страницы.",
    "The provider returned an empty answer.": "Провайдер вернул пустой ответ.",
    "The provider returned no answer.": "Провайдер не вернул ответ.",
    "The provider blocked this page (content filter).":
        "Провайдер заблокировал страницу (фильтр контента).",
    "{label} returned no translation for this page.":
        "{label} не вернул перевод для этой страницы.",
    "Offline stub translation failure.": "Сбой офлайн-заглушки перевода.",
    "No image to read.": "Нет изображения для чтения.",
    # -- browser extension bridge -----------------------------------------
    "Browser extension": "Расширение для браузера",
    "Load the `extension` folder in Chrome: chrome://extensions → "
    "Developer mode → Load unpacked.":
        "Загрузите папку `extension` в Chrome: chrome://extensions → "
        "Режим разработчика → Загрузить распакованное расширение.",
    "Extension bridge is listening on {url}. Load the extension folder in Chrome "
    "(chrome://extensions → Developer mode → Load unpacked).":
        "Мост для расширения слушает {url}. Загрузите папку расширения в Chrome "
        "(chrome://extensions → Режим разработчика → Загрузить распакованное расширение).",
    "The extension bridge could not start.": "Мост для расширения не удалось запустить.",
    "listening · {n} tab(s) connected": "работает · подключено вкладок: {n}",
    "listening · waiting for the extension": "работает · ждёт расширение",
    "{done}/{total} panels · {blocks} blocks · {failed} failed":
        "{done}/{total} страниц · {blocks} блоков · ошибок: {failed}",
    "{engine} · {model} · {mode}": "Движок: {engine} · модель: {model} · режим: {mode}",
    "Address": "Адрес",
    "Progress": "Прогресс",
    "Reading": "Чтение",
    "Vision": "Мультимодальная модель",
    "stopped": "остановлено",
    "in-place": "в картинку",
    "cards": "карточки",
    "Start": "Запустить",
    "Stop": "Остановить",
    "{label} returned no usable translation for this page. "
    "Try another model or check the log.":
        "{label} не вернул пригодный перевод для этой страницы. Попробуйте другую модель "
        "или посмотрите журнал.",
    "{label} returned an answer that is not valid JSON for this page. "
    "Try again or switch the model/provider.":
        "{label} вернул ответ, который не является корректным JSON для этой страницы. "
        "Повторите попытку или смените модель/провайдера.",
    # ---- provider notes (registry) ---------------------------------------
    "page-level JSON translation with responseSchema":
        "постраничный перевод в JSON по схеме responseSchema",
    "classic MT, one request per page (batch of texts)":
        "классический MT: один запрос на страницу (пакет текстов)",
    "cheap, strong on Chinese → Russian": "дешёвый, силён на китайском → русском",
    "hundreds of models behind one key": "сотни моделей по одному ключу",
    "very low latency": "очень низкая задержка",
    "endpoint + deployment name go into the base URL; a deployment name is the 'model' value":
        "endpoint и имя развёртывания указываются в базовом URL; имя развёртывания — это «модель»",
    "requires ?api-version=… (add it to the base URL if your gateway needs it)":
        "требуется ?api-version=… (добавьте в базовый URL, если ваш шлюз этого требует)",
    "offline, free, no API key": "офлайн, бесплатно, без API-ключа",
    "start LM Studio's local server first": "сначала запустите локальный сервер LM Studio",
    "excellent Russian, one request per page": "отличный русский, один запрос на страницу",
    "good for Russian": "хорош для русского",
    "docker run -p 5000:5000 libretranslate/libretranslate":
        "docker run -p 5000:5000 libretranslate/libretranslate",
    "works without a key (anonymous daily quota), good for a smoke test":
        "работает без ключа (анонимная дневная квота), годится для быстрой проверки",
    "no network; structure/overlay testing only":
        "без сети; только для проверки структуры и оверлея",
    "runs on this machine": "работает на этом компьютере",
    "no page context — but one request per page, quick and cheap":
        "без контекста страницы — но один запрос на страницу, быстро и дешево",
}

TRANSLATIONS: Dict[str, Dict[str, str]] = {"ru": RU, "en": {}}


def available_languages() -> List[Tuple[str, str]]:
    return list(LANGUAGES)


def language_name(code: str) -> str:
    for language_code, label in LANGUAGES:
        if language_code == code:
            return label
    return code


def set_language(code: str) -> str:
    global _language
    code = (code or DEFAULT_LANGUAGE).strip().lower()
    if code not in TRANSLATIONS:
        code = DEFAULT_LANGUAGE
    with _lock:
        _language = code
    return code


def get_language() -> str:
    with _lock:
        return _language


def tr(text: str) -> str:
    """Translate an English source string (returns it unchanged if unknown)."""
    if not text:
        return text
    with _lock:
        table = TRANSLATIONS.get(_language) or {}
    return table.get(text, text)


def tr_or(text: str, fallback: str) -> str:
    """Translate, but use an explicit fallback instead of the English source."""
    translated = tr(text)
    return fallback if translated == text else translated
