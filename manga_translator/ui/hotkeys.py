"""Global hotkeys via pynput (configurable, never hardcoded).

Callbacks arrive on pynput's listener thread; they are re-emitted as Qt signals
so the GUI work always happens on the GUI thread.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

from PySide6.QtCore import QObject, Signal

from ..core.config import HotkeySettings
from ..core.logging_setup import get_logger

log = get_logger("hotkeys")


class HotkeyManager(QObject):
    toggleMangaMode = Signal()
    toggleOverlay = Signal()
    refreshPage = Signal()
    selectRegion = Signal()

    def __init__(self, settings: HotkeySettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._listener = None
        self._available: Optional[bool] = None

    def available(self) -> bool:
        try:
            import pynput  # noqa: F401
        except Exception:
            return False
        return True

    def start(self) -> bool:
        self.stop()
        if not self.settings.enabled:
            log.info("hotkeys disabled in settings")
            return False
        if not self.available():
            log.warning("pynput is not installed; global hotkeys unavailable")
            return False
        try:
            from pynput import keyboard

            mapping: Dict[str, Callable[[], None]] = {
                self.settings.as_pynput()["toggle_manga_mode"]: self.toggleMangaMode.emit,
                self.settings.as_pynput()["toggle_overlay"]: self.toggleOverlay.emit,
                self.settings.as_pynput()["refresh_page"]: self.refreshPage.emit,
                self.settings.as_pynput()["select_region"]: self.selectRegion.emit,
            }
            mapping = {spec: cb for spec, cb in mapping.items() if spec}
            self._listener = keyboard.GlobalHotKeys(mapping)
            self._listener.daemon = True
            self._listener.start()
            log.info("hotkeys active: %s", ", ".join(f"{k}" for k in mapping))
            return True
        except Exception as exc:  # pragma: no cover - depends on the desktop
            log.warning("could not register global hotkeys: %s", exc)
            self._listener = None
            return False

    def restart(self, settings: HotkeySettings) -> bool:
        self.settings = settings
        return self.start()

    def stop(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # pragma: no cover
                pass

    def describe(self) -> str:
        s = self.settings
        return (
            f"manga mode: {s.toggle_manga_mode} · overlay: {s.toggle_overlay} · "
            f"refresh: {s.refresh_page} · region: {s.select_region}"
        )
