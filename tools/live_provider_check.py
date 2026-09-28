"""Live check of translation providers: a real keyless API + the GUI workers."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"C:\Users\neuro\.dsh\translator")

from manga_translator.core.config import Config
from manga_translator.core.paths import Paths
from manga_translator.translation import create_backend, list_models
from manga_translator.translation.base import TranslateItem, TranslateRequest

root = Path(tempfile.mkdtemp()) / "data"
paths = Paths(
    root=root, config=root / "config.json", glossary=root / "glossary.json",
    cache_dir=root / "cache", cache_db=root / "cache" / "pages.db",
    log_dir=root / "logs", log_file=root / "logs" / "app.log",
    debug_dir=root / "debug", env_file=root / ".env",
).ensure()

cfg = Config.load(paths)
cfg.translation.provider = "mymemory"
cfg.translation.backend = "mymemory"
cfg.translation.target_language = "Russian"
backend, warning = create_backend(cfg.translation, "")
print("backend:", type(backend).__name__, "| warning:", warning)
try:
    result = backend.translate_page(
        TranslateRequest(
            items=[
                TranslateItem(id=1, text="你好，今天天气真好。"),
                TranslateItem(id=2, text="师兄，你来了！"),
            ],
            target_language="Russian",
        )
    )
    print("translations:", result.translations)
    print("elapsed ms:", round(result.elapsed_ms))
except Exception as exc:
    print("FAILED:", type(exc).__name__, exc)

# model/language discovery for providers that need no key
for provider in ("mymemory", "fake", "deepl"):
    items, unit, error = list_models(provider, "", "")
    print(f"{provider}: {len(items)} {unit}s, error={error!r}, sample={items[:3]}")

# --- the same through the Settings dialog (background worker threads) -------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["MT_DATA_DIR"] = str(root)

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from manga_translator.ui.settings import SettingsDialog  # noqa: E402


def wait(ms: int) -> None:
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(ms)
    loop.exec()


application = QApplication.instance() or QApplication([])
dialog = SettingsDialog(cfg, {})
dialog.cmb_backend.setCurrentIndex(dialog.cmb_backend.findData("mymemory"))
print("provider in the dialog:", dialog._current_provider())
print("model options:", [dialog.cmb_model.itemText(i) for i in range(dialog.cmb_model.count())])

dialog.test_connection()
for _ in range(120):
    application.processEvents()
    if dialog.lbl_test.text() and "translating" not in dialog.lbl_test.text():
        break
    wait(250)
print("Test connection  ->", dialog.lbl_test.text())

dialog.load_models()
for _ in range(60):
    application.processEvents()
    if dialog.lbl_models.text() and "loading" not in dialog.lbl_models.text():
        break
    wait(250)
print("Load models      ->", dialog.lbl_models.text())
