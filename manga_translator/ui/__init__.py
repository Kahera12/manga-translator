"""Tray, settings, notifications, hotkeys and the Qt signal bridge."""

from .bridge import QtPublisher
from .hotkeys import HotkeyManager
from .notifications import Notifier
from .settings import GlossaryDialog, SettingsDialog, StatsDialog
from .tray import Tray, make_icon

__all__ = [
    "QtPublisher",
    "HotkeyManager",
    "Notifier",
    "Tray",
    "make_icon",
    "SettingsDialog",
    "GlossaryDialog",
    "StatsDialog",
]
