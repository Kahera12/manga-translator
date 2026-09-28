"""Control-panel panels: status, dialogue blocks table, log tail, hotkey list."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Block
from . import theme
from ..core.i18n import tr

DIM = f"color: {theme.TEXT_DIM};"


def _value_label(text: str = "—") -> QLabel:
    label = QLabel(text)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setStyleSheet(f"color: {theme.TEXT};")
    return label


class StatusPanel(QGroupBox):
    """Live state of the pipeline: mode, region, detector, timings, cache."""

    ROWS = (
        ("Manga Mode", "lbl_mode"),
        ("Region", "lbl_region"),
        ("Displays", "lbl_display"),
        ("OCR engine", "lbl_engine"),
        ("Translator", "lbl_backend"),
        ("Generation", "lbl_generation"),
        ("Detector", "lbl_detector"),
        ("Change score", "lbl_change"),
        ("Page hash", "lbl_hash"),
        ("Last page", "lbl_timing"),
        ("Cache", "lbl_cache"),
        ("Counters", "lbl_stats"),
    )

    def __init__(self, parent=None):
        super().__init__(tr("Status"), parent)
        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(4)
        self._keys: List[QLabel] = []
        for title, attribute in self.ROWS:
            setattr(self, attribute, _value_label("—"))
            key = QLabel(tr(title))
            key.setStyleSheet(DIM)
            form.addRow(key, getattr(self, attribute))
            self._keys.append(key)

    def retranslate(self) -> None:
        self.setTitle(tr("Status"))
        for (title, _attribute), label in zip(self.ROWS, self._keys):
            label.setText(tr(title))

    # -- update -----------------------------------------------------------
    def update_state(self, payload: Dict[str, object]) -> None:
        stats = payload.get("stats") or {}
        cache = payload.get("cache") or {}
        region = payload.get("region")
        detector = payload.get("detector") or {}
        last = payload.get("last_page") or {}

        self.lbl_mode.setText(tr("ON") if payload.get("manga_mode") else tr("off"))
        self.lbl_mode.setStyleSheet(
            f"color: {theme.OK}; font-weight: bold;" if payload.get("manga_mode") else DIM
        )
        if region:
            self.lbl_region.setText(
                tr("{w}×{h} at ({x},{y}) · {monitor}").format(
                    w=region.get("width"), h=region.get("height"),
                    x=region.get("x"), y=region.get("y"),
                    monitor=region.get("monitor", ""),
                )
            )
        else:
            self.lbl_region.setText(tr("not selected"))
        scale = region.get("dpi_scale") if region else None
        displays = str(payload.get("displays", "—"))
        self.lbl_display.setText(
            tr("{displays} · region at {scale}%").format(displays=displays, scale=float(scale) * 100)
            if scale else displays
        )
        self.lbl_engine.setText(str(payload.get("engine", "—")))
        self.lbl_backend.setText(f"{payload.get('backend', '—')} · {payload.get('model', '')}")
        self.lbl_generation.setText(str(payload.get("generation", 0)))
        self.lbl_detector.setText(
            tr("{state} · stable {ms} ms").format(
                state=tr(str(detector.get("state", "idle"))), ms=detector.get("stable_for_ms", 0)
            )
        )
        self.lbl_change.setText(
            tr("{score} (pixels {pixels}, phash {bits} bits)").format(
                score=f"{float(detector.get('score', 0.0)):.3f}",
                pixels=f"{float(detector.get('changed_fraction', 0.0)):.3f}",
                bits=detector.get("hamming", 0),
            )
        )
        self.lbl_hash.setText(str(detector.get("page_hash") or "—"))
        if last:
            template = (
                "{total} ms total · ocr {ocr} ms · translate {mt} ms · {blocks} blocks · from cache"
                if last.get("from_cache")
                else "{total} ms total · ocr {ocr} ms · translate {mt} ms · {blocks} blocks"
            )
            self.lbl_timing.setText(
                tr(template).format(
                    total=f"{last.get('total_ms', 0):.0f}",
                    ocr=f"{last.get('ocr_ms', 0):.0f}",
                    mt=f"{last.get('translate_ms', 0):.0f}",
                    blocks=last.get("blocks", 0),
                )
            )
        else:
            self.lbl_timing.setText("—")
        cache_template = (
            "{pages} pages · {mb} MB · {hits} hits"
            if cache.get("enabled", 1)
            else "{pages} pages · {mb} MB · {hits} hits · disabled"
        )
        self.lbl_cache.setText(
            tr(cache_template).format(
                pages=int(cache.get("entries", 0)),
                mb=f"{float(cache.get('bytes', 0)) / 1e6:.1f}",
                hits=int(cache.get("hits", 0)),
            )
        )
        self.lbl_stats.setText(
            tr("pages {pages} · cache hits {hits} · stale dropped {stale} · errors {errors}").format(
                pages=stats.get("pages_processed", 0),
                hits=stats.get("cache_hits", 0),
                stale=stats.get("stale_discarded", 0),
                errors=stats.get("errors", 0),
            )
        )


class BlocksPanel(QGroupBox):
    """Table of the detected dialogue blocks and their Russian translations."""

    saved = Signal()

    HEADERS = ("#", "Original (zh)", "Translation (ru)", "Conf", "Kind", "Box")

    def __init__(self, parent=None):
        super().__init__(tr("Dialogue blocks"), parent)
        self.table = QTableWidget(0, len(self.HEADERS), self)
        self.table.setHorizontalHeaderLabels([tr(h) for h in self.HEADERS])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        self.btn_copy = QPushButton(tr("Copy translations"))
        self.btn_copy.clicked.connect(self.copy_translations)
        self.btn_save = QPushButton(tr("Save page text…"))
        self.lbl_summary = QLabel(tr("no page processed yet"))
        self.lbl_summary.setStyleSheet(DIM)

        row = QHBoxLayout()
        row.addWidget(self.lbl_summary)
        row.addStretch(1)
        row.addWidget(self.btn_copy)
        row.addWidget(self.btn_save)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(row)

        self._blocks: List[Block] = []
        self._summary = ("", {})
        self.btn_save.clicked.connect(self._save_requested)

    def retranslate(self) -> None:
        self.setTitle(tr("Dialogue blocks"))
        self.table.setHorizontalHeaderLabels([tr(h) for h in self.HEADERS])
        self.btn_copy.setText(tr("Copy translations"))
        self.btn_save.setText(tr("Save page text…"))
        if self._blocks:
            self.set_blocks(self._blocks, self._summary[0] == "cache")
        else:
            self.lbl_summary.setText(tr("no page processed yet"))

    # -- content ----------------------------------------------------------
    def set_blocks(self, blocks: Sequence[Block], from_cache: bool = False) -> None:
        self._blocks = list(blocks)
        self.table.setRowCount(len(self._blocks))
        translated = 0
        for row, block in enumerate(self._blocks):
            kind = "SFX" if block.is_sfx else ("dialogue" if block.translation else "skipped")
            values = [
                str(block.id),
                block.text,
                block.translation or "—",
                f"{block.confidence:.2f}",
                tr(kind),
                f"{block.bbox.w:.0f}×{block.bbox.h:.0f} @ {block.bbox.x:.0f},{block.bbox.y:.0f}",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 4:
                    item.setForeground(
                        QColor(theme.SFX) if block.is_sfx
                        else QColor(theme.OK) if block.translation else QColor(theme.TEXT_DIM)
                    )
                if column == 2 and not block.translation:
                    item.setForeground(QColor(theme.TEXT_DIM))
                self.table.setItem(row, column, item)
            translated += 1 if block.translation else 0
        self._summary = ("cache" if from_cache else "fresh", {
            "blocks": len(self._blocks), "translated": translated,
        })
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        mode, values = self._summary
        if not self._blocks:
            self.lbl_summary.setText(tr("no dialogue on this page"))
            return
        template = (
            "{blocks} blocks · {translated} translated · from cache" if mode == "cache"
            else "{blocks} blocks · {translated} translated"
        )
        self.lbl_summary.setText(tr(template).format(**values))

    def copy_translations(self) -> str:
        text = "\n".join(
            f"[{b.id}] {b.translation}" for b in self._blocks if b.translation
        )
        QGuiApplication.clipboard().setText(text)
        self.lbl_summary.setText(
            tr("copied {n} line(s) to the clipboard").format(n=len(text.splitlines()))
        )
        return text

    def page_text(self) -> str:
        lines = []
        for block in self._blocks:
            kind = tr("SFX" if block.is_sfx else "dialogue")
            lines.append(f"[{block.id}] ({kind}) {block.text}")
            if block.translation:
                lines.append(f"      -> {block.translation}")
        return "\n".join(lines)

    def _save_requested(self) -> None:
        self.saved.emit()


class LogPanel(QWidget):
    """Tail of the application log (technical details stay out of the user's way)."""

    def __init__(self, log_file: Optional[Path] = None, parent=None, max_lines: int = 400):
        super().__init__(parent)
        self.log_file = Path(log_file) if log_file else None
        self.view = QPlainTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(max_lines * 2)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.auto = QPushButton(tr("Auto-refresh"))
        self.auto.setCheckable(True)
        self.auto.setChecked(True)
        self.refresh_button = QPushButton(tr("Refresh now"))
        self.refresh_button.clicked.connect(self.reload)
        self.btn_open = QPushButton(tr("Open log file"))
        self.lbl_path = QLabel(str(self.log_file) if self.log_file else tr("no log file"))
        self.lbl_path.setStyleSheet(DIM)
        self.lbl_path.setTextInteractionFlags(Qt.TextSelectableByMouse)

        row = QHBoxLayout()
        row.addWidget(self.auto)
        row.addWidget(self.refresh_button)
        row.addWidget(self.btn_open)
        row.addStretch(1)
        row.addWidget(self.lbl_path)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        layout.addLayout(row)

        self._offset = 0
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.reload()

    def retranslate(self) -> None:
        self.auto.setText(tr("Auto-refresh"))
        self.refresh_button.setText(tr("Refresh now"))
        self.btn_open.setText(tr("Open log file"))
        if self.log_file is None:
            self.lbl_path.setText(tr("no log file"))

    def set_log_file(self, path: Path) -> None:
        self.log_file = Path(path)
        self.lbl_path.setText(str(self.log_file))
        self._offset = 0
        self.view.clear()
        self.reload()

    def _tick(self) -> None:
        if self.auto.isChecked():
            self.reload()

    def reload(self) -> None:
        if self.log_file is None or not self.log_file.exists():
            return
        try:
            size = self.log_file.stat().st_size
            if size < self._offset:      # rotated
                self._offset = 0
                self.view.clear()
            with self.log_file.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return
        if not chunk:
            return
        self.view.moveCursor(QTextCursor.End)
        self.view.insertPlainText(chunk)
        self.view.moveCursor(QTextCursor.End)


class BridgePanel(QGroupBox):
    """State of the browser-extension bridge (the WebSocket the extension uses)."""

    toggleRequested = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(tr("Browser extension"), parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        row = QHBoxLayout()
        self.lbl_state = _value_label(tr("stopped"))
        self.lbl_state.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold;")
        self.btn_toggle = QPushButton(tr("Start"))
        self.btn_toggle.setCheckable(True)
        self.btn_toggle.toggled.connect(self._on_toggle)
        row.addWidget(self.lbl_state, 1)
        row.addWidget(self.btn_toggle)
        layout.addLayout(row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(4)
        self.lbl_url = _value_label("—")
        self.lbl_page = _value_label("—")
        self.lbl_progress = _value_label("—")
        self.lbl_engine = _value_label("—")
        self._keys: List[QLabel] = []
        for title, widget in (("Address", self.lbl_url), ("Page", self.lbl_page),
                              ("Progress", self.lbl_progress), ("Reading", self.lbl_engine)):
            key = QLabel(tr(title))
            key.setStyleSheet(DIM)
            form.addRow(key, widget)
            self._keys.append(key)
        layout.addLayout(form)

        self.hint = QLabel(tr("Load the `extension` folder in Chrome: chrome://extensions → "
                              "Developer mode → Load unpacked."))
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(DIM)
        layout.addWidget(self.hint)

    def _on_toggle(self, checked: bool) -> None:
        self.toggleRequested.emit(checked)

    def retranslate(self) -> None:
        self.setTitle(tr("Browser extension"))
        for (title, _attr), label in zip(
            (("Address", None), ("Page", None), ("Progress", None), ("Reading", None)), self._keys
        ):
            label.setText(tr(title))
        self.btn_toggle.setText(tr("Stop") if self.btn_toggle.isChecked() else tr("Start"))
        self.hint.setText(tr("Load the `extension` folder in Chrome: chrome://extensions → "
                             "Developer mode → Load unpacked."))

    def update_state(self, status: Dict[str, object]) -> None:
        running = bool(status.get("running"))
        clients = int(status.get("clients") or 0)
        if self.btn_toggle.isChecked() != running:
            self.btn_toggle.blockSignals(True)
            self.btn_toggle.setChecked(running)
            self.btn_toggle.blockSignals(False)
            self.btn_toggle.setText(tr("Stop") if running else tr("Start"))
        if running:
            self.lbl_state.setText(
                tr("listening · {n} tab(s) connected").format(n=clients) if clients
                else tr("listening · waiting for the extension")
            )
        else:
            self.lbl_state.setText(tr("stopped"))
        self.lbl_state.setStyleSheet(
            f"color: {theme.OK}; font-weight: bold;" if running else f"color: {theme.TEXT_DIM};"
        )
        self.lbl_url.setText(str(status.get("url") or "—"))
        error = str(status.get("error") or "")
        if error and not running:
            self.lbl_url.setText(error)
        sessions = status.get("sessions") or []
        if not sessions:
            self.lbl_page.setText("—")
            self.lbl_progress.setText("—")
            self.lbl_engine.setText("—")
            return
        session = sessions[0]
        title = str(session.get("page_title") or session.get("page_url") or "—")
        self.lbl_page.setText(title if len(title) <= 60 else title[:57] + "…")
        done, total = int(session.get("processed") or 0), int(session.get("panels") or 0)
        blocks = int(session.get("blocks") or 0)
        failed = int(session.get("failed") or 0)
        self.lbl_progress.setText(
            tr("{done}/{total} panels · {blocks} blocks · {failed} failed").format(
                done=done, total=total, blocks=blocks, failed=failed)
        )
        self.lbl_engine.setText(
            tr("{engine} · {model} · {mode}").format(
                engine=tr("Vision") if session.get("vision") else tr("OCR"),
                model=str(session.get("model") or "—"),
                mode=tr("in-place") if session.get("mode") == "inplace" else tr("cards"),
            )
        )


class HotkeyPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__(tr("Controls"), parent)
        self.form = QFormLayout(self)
        self._rows: Dict[str, QLabel] = {}
        self._mapping: Dict[str, str] = {}

    def retranslate(self) -> None:
        self.setTitle(tr("Controls"))
        self.set_hotkeys(self._mapping)

    def set_hotkeys(self, mapping: Dict[str, str]) -> None:
        self._mapping = dict(mapping)
        while self.form.rowCount():
            self.form.removeRow(0)
        self._rows.clear()
        for action, keys in mapping.items():
            key = QLabel(keys)
            key.setStyleSheet(f"color: {theme.ACCENT}; font-family: Consolas;")
            name = QLabel(tr(action))
            name.setStyleSheet(DIM)
            self.form.addRow(name, key)
            self._rows[action] = key
