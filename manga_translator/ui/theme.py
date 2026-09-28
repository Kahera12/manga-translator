"""Dark theme palette + stylesheet for the control panel."""
from __future__ import annotations

BG = "#0f1420"
BG_ALT = "#151c2b"
BG_CARD = "#1b2437"
BORDER = "#2b3855"
TEXT = "#e6edf7"
TEXT_DIM = "#93a4bf"
ACCENT = "#38bdf8"
OK = "#34d399"
WARN = "#fbbf24"
ERROR = "#f87171"
DETECT = "#22c55e"
SFX = "#f59e0b"
CARD = "#7dd3fc"

STYLESHEET = f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 12px;
}}
QMainWindow::separator {{ background: {BORDER}; width: 1px; height: 1px; }}
QToolBar {{
    background: {BG_ALT};
    border-bottom: 1px solid {BORDER};
    spacing: 6px;
    padding: 4px;
}}
QToolBar QToolButton {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 10px;
    color: {TEXT};
}}
QToolBar QToolButton:hover {{ border-color: {ACCENT}; }}
QToolBar QToolButton:checked {{
    background: #123449;
    border-color: {ACCENT};
    color: #d9f2ff;
}}
QToolBar QToolButton:disabled {{ color: {TEXT_DIM}; border-color: #222c42; }}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 14px;
    padding: 8px;
    background: {BG_ALT};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_DIM};
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 1px;
}}
QLabel#value {{ color: {TEXT}; }}
QLabel#dim {{ color: {TEXT_DIM}; }}
QLabel#status {{ color: {TEXT}; padding: 2px 6px; }}
QTableWidget {{
    background: {BG};
    alternate-background-color: {BG_ALT};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 6px;
    selection-background-color: #17415c;
}}
QHeaderView::section {{
    background: {BG_CARD};
    color: {TEXT_DIM};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 5px;
}}
QPlainTextEdit, QTextEdit {{
    background: #0b0f18;
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: #cbd5e1;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 11px;
}}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; }}
QTabBar::tab {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    padding: 5px 12px;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    color: {TEXT_DIM};
}}
QTabBar::tab:selected {{ background: {BG_CARD}; color: {TEXT}; }}
QPushButton {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 10px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:checked {{ background: #123449; border-color: {ACCENT}; }}
QStatusBar {{ background: {BG_ALT}; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QProgressBar {{
    background: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 5px;
    text-align: center;
    color: {TEXT};
    max-height: 14px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 12px; margin: -5px 0; border-radius: 6px;
}}
"""
