"""Application wiring: tray + overlay + hotkeys + pipeline on the Qt GUI thread.

Threading contract:
  * widgets are touched only on the GUI thread;
  * the pipeline publishes through QtPublisher signals (queued to the GUI thread);
  * the capture hooks (called from the worker thread) blank the overlay through a
    blocking queued call, so screenshots never contain our own cards.
"""
from __future__ import annotations

import time
from typing import List, Optional

from PySide6.QtCore import QMetaObject, QObject, QThread, QTimer, Qt, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_DISPLAY_NAME, __version__
from .browser.cdp import Tab, discover_port, launch_browser as launch_cdp_browser
from .browser.mode import BrowserMode
from .browser.win import window_for_port
from .bridge.server import BridgeServer
from .bridge.service import BridgeSession
from .cache.page_cache import PageCache
from .capture.coordmap import CoordMap
from .capture.region_selector import RegionSelector
from .capture.screen_capture import CaptureHooks, ScreenCapture
from .capture.window_capture import WindowInfo, is_window_alive, list_windows, window_rect, window_region
from .core import dpi
from .core.config import Config
from .core.logging_setup import get_logger, setup_logging
from .core.models import PageResult, PipelineError, ScreenRegion
from .core.paths import get_paths
from .glossary.glossary import Glossary
from .overlay.overlay_window import OverlayWindow, card_rects_region_px, layout_settings
from .overlay.widgets import DebugInfo, render_overlay_image
from .pipeline.orchestrator import PageJob, PagePipeline
from .translation import create_backend
from .ui.bridge import QtPublisher
from .core.i18n import set_language, tr
from .ui.hotkeys import HotkeyManager
from .ui.main_window import MainWindow
from .ui.notifications import Notifier
from .ui.settings import GlossaryDialog, SettingsDialog, StatsDialog
from .ui.tray import Tray

log = get_logger("app")


class MangaTranslatorApp(QObject):
    def __init__(self, argv: Optional[List[str]] = None):
        super().__init__()
        self.paths = get_paths().ensure()
        setup_logging(self.paths.log_file)
        self.config = Config.load(self.paths)
        set_language(self.config.general.language)
        setup_logging(self.paths.log_file, debug=self.config.general.debug_mode)

        self.qt = QApplication.instance() or QApplication(argv or [])
        self.qt.setApplicationName(APP_DISPLAY_NAME)
        self.qt.setApplicationVersion(__version__)
        self.qt.setQuitOnLastWindowClosed(False)

        log.info("=" * 72)
        log.info("%s %s starting (python %s)", APP_DISPLAY_NAME, __version__,
                 ".".join(str(p) for p in __import__("sys").version_info[:3]))
        log.info("data dir: %s", self.paths.root)
        log.info("display: %s", dpi.current_desktop_mode())
        log.info("config: %s", self.paths.config)

        self._quiet_ctx = None
        self._masks_cache: list = []
        self._overlay_gone = False
        self._screen_timer = QTimer(self)
        self._screen_timer.setSingleShot(True)
        self._screen_timer.setInterval(400)
        self._screen_timer.timeout.connect(self._on_screens_changed)

        self.coordmap = self._build_coordmap()
        self.overlay = OverlayWindow(self.config.snapshot().overlay, self.coordmap)
        self.tray = Tray(window_lister=self._list_windows)
        self.notifier = Notifier()
        self.notifier.attach(self.tray.icon, self.overlay)

        self.capture = ScreenCapture(
            CaptureHooks(before=self._quiet_overlay, after=self._restore_overlay, settle_ms=20)
        )
        self.cache = PageCache(
            self.paths,
            max_mb=self.config.cache.max_mb,
            hamming_threshold=self.config.cache.hamming_threshold,
            max_age_days=self.config.cache.max_age_days,
            enabled=self.config.cache.enabled,
        )
        self.glossary = Glossary(self.config.glossary_file()).load(create_sample=True)
        self.backend, backend_warning = create_backend(self.config.translation, self.config.api_key())

        self.bridge = QtPublisher(self)
        self.pipeline = PagePipeline(
            self.config,
            self.capture,
            self.backend,
            self.cache,
            self.glossary,
            publisher=self.bridge.publisher,
            mask_provider=lambda: self._masks_cache,
        )
        self.bridge.pageReady.connect(self._on_page_ready)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.statusChanged.connect(self._on_status)
        self.bridge.detectorChanged.connect(self._on_detector)
        self.bridge.pageStarted.connect(self._on_page_started)
        self.bridge.busyChanged.connect(self._on_busy)
        self.bridge.previewReady.connect(self._on_preview)

        self.hotkeys = HotkeyManager(self.config.hotkeys, self)
        self.hotkeys.toggleMangaMode.connect(self._toggle_manga_mode)
        self.hotkeys.toggleOverlay.connect(self._toggle_overlay)
        self.hotkeys.refreshPage.connect(self._refresh_page)
        self.hotkeys.selectRegion.connect(self._select_region)

        self.selector = RegionSelector(self.coordmap)
        self.selector.selected.connect(self._on_region_selected)
        self.selector.cancelled.connect(self._on_region_cancelled)

        self._browser_port: Optional[int] = None
        self._browser_window = None
        self._browser_window_ts = 0.0
        self.browser_mode = BrowserMode(self.config, self.cache, self.glossary,
                                       self._browser_client_rect)
        self.browser_mode.pageReady.connect(self._on_browser_page)
        self.browser_mode.statusChanged.connect(self._on_browser_status)
        self.browser_mode.failed.connect(self._on_browser_failed)

        # The extension bridge: a local WebSocket the browser extension talks to.
        self.ws_bridge = BridgeServer(self._make_bridge_session,
                                      port=self.config.bridge.port,
                                      on_change=self._on_bridge_change)
        self._bridge_tick = QTimer(self)
        self._bridge_tick.setInterval(1500)
        self._bridge_tick.timeout.connect(self._refresh_bridge_ui)

        self.window = MainWindow(self)
        self._wire_window()

        self._wire_tray()
        self._connect_screen_signals()

        self._manga_mode = False
        self._window_handle: Optional[int] = None
        self._follow_timer = QTimer(self)
        self._follow_timer.setInterval(1000)
        self._follow_timer.timeout.connect(self._follow_window)
        self._last_result = None
        self._debug_dumps = 0
        self.window.set_hotkeys(self.hotkey_mapping())
        self.window.set_mode_state(False, self.config.overlay.visible,
                                  self.config.overlay.show_debug_hud)

        if backend_warning:
            self.notifier.warn(backend_warning, toast=False)
        if self.pipeline.engine_warning:
            self.notifier.warn(self.pipeline.engine_warning, toast=False)

    # -- construction helpers ---------------------------------------------
    def _build_coordmap(self) -> CoordMap:
        try:
            return CoordMap.build(QGuiApplication.screens(), dpi.list_monitors())
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("coordinate map build failed (%s); assuming 100%%", exc)
            return CoordMap.uniform(1.0)

    def _wire_tray(self) -> None:
        tray = self.tray
        tray.mangaModeToggled.connect(self._set_manga_mode)
        tray.overlayToggled.connect(self._set_overlay_visible)
        tray.selectRegionRequested.connect(self._select_region)
        tray.selectWindowRequested.connect(self._select_window)
        tray.refreshRequested.connect(self._refresh_page)
        tray.settingsRequested.connect(self._open_settings)
        tray.glossaryRequested.connect(self._open_glossary)
        tray.statsRequested.connect(self._open_stats)
        tray.clearCacheRequested.connect(self._clear_cache)
        tray.openLogsRequested.connect(self._open_logs)
        tray.showWindowRequested.connect(self.show_window)
        tray.browserModeRequested.connect(self.open_browser_mode)
        tray.quitRequested.connect(self.shutdown)
        tray.set_manga_mode(False)
        tray.set_overlay_visible(True)

    def _wire_window(self) -> None:
        """The control panel issues the same commands as the tray menu."""
        window = self.window
        window.mangaModeToggled.connect(self._set_manga_mode)
        window.overlayToggled.connect(self._set_overlay_visible)
        window.selectRegionRequested.connect(self._select_region)
        window.selectWindowRequested.connect(self._select_window)
        window.refreshRequested.connect(self._refresh_page)
        window.settingsRequested.connect(self._open_settings)
        window.glossaryRequested.connect(self._open_glossary)
        window.statisticsRequested.connect(self._open_stats)
        window.clearCacheRequested.connect(self._clear_cache)
        window.openLogsRequested.connect(self._open_logs)
        window.openDataRequested.connect(self._open_data_folder)
        window.quitRequested.connect(self.shutdown)
        window.previewRequested.connect(self.request_preview)
        window.debugHudToggled.connect(self._set_debug_hud)
        window.hideToTrayRequested.connect(self._on_window_hidden)
        window.browserModeRequested.connect(self.open_browser_mode)
        window.bridgeToggled.connect(self._on_bridge_toggled)

    def _on_bridge_toggled(self, enabled: bool) -> None:
        """The panel's Start/Stop button (and the matching setting)."""
        if enabled:
            self.start_bridge()
        else:
            self.stop_bridge()

    def _connect_screen_signals(self) -> None:
        app = QGuiApplication.instance()
        app.screenAdded.connect(lambda *_: self._screen_timer.start())
        app.screenRemoved.connect(lambda *_: self._screen_timer.start())
        app.primaryScreenChanged.connect(lambda *_: self._screen_timer.start())
        for screen in QGuiApplication.screens():
            self._watch_screen(screen)

    def _watch_screen(self, screen) -> None:
        try:
            screen.geometryChanged.connect(lambda *_: self._screen_timer.start())
            screen.logicalDotsPerInchChanged.connect(lambda *_: self._screen_timer.start())
        except Exception:  # pragma: no cover - older Qt
            pass

    # -- capture hooks (called from the worker thread) ---------------------
    def _invoke_overlay(self, slot: str) -> None:
        """Blank/restore the overlay on the GUI thread.

        The pipeline grabs from its own worker thread, so a blocking queued call
        is used. If the grab ever happens *on* the GUI thread (tests, manual
        refresh from a slot) we call it directly - a blocking queued call to
        ourselves would deadlock. Late calls during shutdown are dropped.
        """
        if self._overlay_gone:
            return
        try:
            if QThread.currentThread() is self.overlay.thread():
                getattr(self.overlay, slot)()
            else:
                QMetaObject.invokeMethod(self.overlay, slot, Qt.BlockingQueuedConnection)
        except RuntimeError:  # pragma: no cover - overlay already destroyed
            self._overlay_gone = True
            log.debug("overlay %s dropped: window is gone", slot)

    def _quiet_overlay(self) -> None:
        self._invoke_overlay("request_quiet")

    def _restore_overlay(self) -> None:
        self._invoke_overlay("request_unquiet")

    # -- extension bridge --------------------------------------------------
    def _make_bridge_session(self, send) -> BridgeSession:
        """One session per connected tab (runs on the bridge's own thread)."""
        snapshot = self.config.snapshot()
        return BridgeSession(
            snapshot, self.cache, self.glossary, self.backend, send,
            engine_getter=lambda: self.pipeline.engine,
        )

    def _bridge_alive(self) -> bool:
        return getattr(self, "ws_bridge", None) is not None and self.ws_bridge.running

    def start_bridge(self) -> bool:
        if not self._bridge_alive():
            self.ws_bridge.port = int(self.config.bridge.port)
            if not self.ws_bridge.start():
                self.notifier.warn(self.ws_bridge.last_error or
                                   tr("The extension bridge could not start."), toast=True)
                return False
        self._bridge_tick.start()
        self.notifier.info(
            tr("Extension bridge is listening on {url}. Load the extension folder in Chrome "
               "(chrome://extensions → Developer mode → Load unpacked).").format(
                url=self.ws_bridge.status()["url"]),
            toast=False,
        )
        self._refresh_bridge_ui()
        return True

    def stop_bridge(self) -> None:
        self._bridge_tick.stop()
        self.ws_bridge.stop()
        self._refresh_bridge_ui()

    def bridge_status(self) -> dict:
        return self.ws_bridge.status() if getattr(self, "ws_bridge", None) else {"running": False}

    def _sync_bridge(self) -> None:
        """Follow the settings: start/stop/rebind the bridge after a config change."""
        settings = self.config.bridge
        if not settings.enabled:
            if self._bridge_alive():
                self.stop_bridge()
            return
        if self._bridge_alive() and self.ws_bridge.port != int(settings.port):
            self.stop_bridge()
        if not self._bridge_alive():
            self.start_bridge()

    def _on_bridge_change(self) -> None:
        """Called from the bridge thread - bounce UI work to the GUI thread."""
        try:
            QMetaObject.invokeMethod(self, "_refresh_bridge_ui", Qt.QueuedConnection)
        except RuntimeError:  # pragma: no cover - shutting down
            pass

    @Slot()
    def _refresh_bridge_ui(self) -> None:
        try:
            self.window.set_bridge_state(self.bridge_status())
        except RuntimeError:  # pragma: no cover - window already gone
            pass

    # -- UI slots ---------------------------------------------------------
    @Slot(object, object)
    def _on_page_ready(self, result, job: PageJob) -> None:
        self._last_result = result
        self.overlay.clear()
        self._masks_cache = []
        if result.blocks:
            debug = self._debug_info(result, job)
            self.overlay.set_page(result.blocks, job.region, debug)
            self._masks_cache = card_rects_region_px(self.overlay.cards, job.region, self.coordmap)
        self.pipeline.set_masks(self._masks_cache)
        shown = len([b for b in result.blocks if b.translation])
        origin = "cache" if result.from_cache else f"{result.total_ms:.0f} ms"
        self.tray.set_status(
            tr("{blocks} blocks · gen {generation} · {origin}").format(
                blocks=shown, generation=result.generation, origin=origin
            )
        )
        self.window.set_page(result, job, self._masks_cache)
        if result.error:
            self.notifier.warn(result.error)
        if self.config.general.debug_mode:
            self._dump_debug_frame(result)

    @Slot(object)
    def _on_page_started(self, job: PageJob) -> None:
        # The previous page's cards stay visible while the new one is read:
        # clearing here would leave the reader looking at nothing for seconds on
        # every scroll (webtoons scroll constantly). "Refresh" clears explicitly.
        if job.kind == "manual":
            self.overlay.clear()
        self.window.set_status_message(
            tr("page {generation} — {kind}…").format(generation=job.generation, kind=job.kind)
        )
        self.tray.set_status(tr("page {generation} ({kind})").format(
            generation=job.generation, kind=job.kind
        ))

    @Slot(str, str)
    def _on_failed(self, message: str, kind: str) -> None:
        self.notifier.error(message, title=f"{APP_DISPLAY_NAME} — {kind}")
        self.tray.set_status(tr("error: {kind}").format(kind=kind))
        self.window.set_status_message(message, "error")

    @Slot(str, str)
    def _on_status(self, message: str, level: str) -> None:
        self.tray.set_status(message)
        self.window.set_status_message(message, level)

    @Slot(bool)
    def _on_busy(self, busy: bool) -> None:
        self.window.set_busy(bool(busy))

    @Slot(object, object)
    def _on_preview(self, frame, region) -> None:
        self.window.set_preview_frame(frame, region)

    @Slot(str, float, int)
    def _on_detector(self, state: str, score: float, generation: int) -> None:
        self.window.set_detector_state(state, score, generation)
        if self.config.general.debug_mode and self._last_result is not None:
            self.overlay.set_debug(self._debug_info(self._last_result, None, score=score, state=state))

    # -- actions ----------------------------------------------------------
    def _toggle_manga_mode(self) -> None:
        self._set_manga_mode(not self._manga_mode)

    @Slot(bool)
    def _set_manga_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled and self.config.region() is None:
            self.tray.set_manga_mode(False)
            self.notifier.warn(tr("Select the manga region first — drag over the page now."), toast=False)
            self._select_region()
            return
        self._manga_mode = enabled
        self.config.general.manga_mode_enabled = enabled
        self.config.save()
        self.tray.set_manga_mode(enabled)
        self.window.set_mode_state(enabled, self.config.overlay.visible,
                                   self.config.overlay.show_debug_hud)
        if enabled:
            region = self.config.region()
            self.overlay.follow_region(region)
            self.overlay.settings = self.config.snapshot().overlay
            self.overlay.set_click_through(self.config.overlay.click_through)
            self.overlay.clear()
            self.overlay.show_translation(self.config.overlay.visible)
            self.pipeline.set_region(region)
            self.pipeline.start()
            self.pipeline.resume()
            self.pipeline.refresh(force=False)
            self.request_preview()
            self.hotkeys.start()
            self.notifier.info(tr("Manga Mode on — translations appear automatically."), toast=False)
            self.window.set_status_message(tr("Manga Mode on — monitoring the region"))
            log.info("Manga Mode enabled")
        else:
            self.pipeline.pause()
            self.overlay.clear()
            self.overlay.show_translation(False)
            self.window.set_status_message(tr("Manga Mode off"))
            self.notifier.info(tr("Manga Mode off."), toast=False)
            log.info("Manga Mode disabled")

    def _toggle_overlay(self) -> None:
        self._set_overlay_visible(not self.config.overlay.visible)

    @Slot(bool)
    def _set_overlay_visible(self, visible: bool) -> None:
        self.config.overlay.visible = bool(visible)
        self.config.save()
        self.tray.set_overlay_visible(bool(visible))
        self.window.set_mode_state(self._manga_mode, bool(visible),
                                   self.config.overlay.show_debug_hud)
        if visible and self._manga_mode:
            self.overlay.show_translation(True)
        elif not visible:
            self.overlay.show_translation(False)
        log.info("translation overlay %s", "shown" if visible else "hidden")

    @Slot(bool)
    def _set_debug_hud(self, enabled: bool) -> None:
        self.config.overlay.show_debug_hud = bool(enabled)
        self.config.save()
        self.overlay.settings = self.config.snapshot().overlay
        self.overlay.update()
        if enabled and self._last_result is not None:
            self.overlay.set_debug(self._debug_info(self._last_result, None))
        log.info("debug HUD %s", "on" if enabled else "off")

    @Slot()
    def _on_window_hidden(self) -> None:
        self.hide_window()

    def _refresh_page(self) -> None:
        if not self._manga_mode:
            self.notifier.warn(tr("Enable Manga Mode first."), toast=False)
            return
        if self.pipeline.refresh(force=True) is None:
            self.notifier.warn(tr("Select a region first."), toast=False)

    def _select_region(self) -> None:
        if self.selector.active:
            return
        self.overlay.hide()
        self.selector.start()

    @Slot(object)
    def _on_region_selected(self, region: ScreenRegion) -> None:
        self.config.set_region(region)
        self.config.save()
        self._window_handle = None
        self._follow_timer.stop()
        self.capture.refresh_desktop()
        self.pipeline.set_region(region)
        if self.overlay.follow_region(region):
            self.overlay.clear()
            self.overlay.show_translation(self.config.overlay.visible and self._manga_mode)
        self.notifier.info(
            tr("Region set: {w}×{h} at ({x},{y}) · {scale}% scaling").format(
                w=region.width, h=region.height, x=region.x, y=region.y,
                scale=region.dpi_scale * 100,
            ),
            toast=False,
        )
        if not self._manga_mode:
            self._set_manga_mode(True)

    @Slot()
    def _on_region_cancelled(self) -> None:
        if self._manga_mode:
            self.overlay.show_translation(self.config.overlay.visible)
        self.notifier.info(tr("Region selection cancelled."), toast=False)

    def _list_windows(self) -> List[WindowInfo]:
        import os

        try:
            return list_windows(exclude_pid=os.getpid())
        except Exception as exc:  # pragma: no cover
            log.warning("window listing failed: %s", exc)
            return []

    @Slot(int)
    def _select_window(self, handle: int) -> None:
        info = next((w for w in self._list_windows() if w.handle == handle), None)
        if info is None:
            self.notifier.warn(tr("That window is gone."), toast=False)
            return
        region = window_region(info, self.coordmap)
        self._window_handle = handle
        self._follow_timer.start()
        self._on_region_selected(region)
        self.notifier.info(tr("Following window: {title}").format(title=info.title[:40]), toast=False)

    def _follow_window(self) -> None:
        handle = self._window_handle
        if not handle:
            self._follow_timer.stop()
            return
        if not is_window_alive(handle):
            self._follow_timer.stop()
            self._window_handle = None
            self.notifier.warn(tr("The followed window was closed."), toast=False)
            return
        rect = window_rect(handle)
        if rect is None:
            return
        region = self.config.region()
        if region is None:
            return
        if (int(rect.x), int(rect.y), int(rect.w), int(rect.h)) != (
            region.x, region.y, region.width, region.height
        ):
            self.coordmap = self._build_coordmap()
            new_region = ScreenRegion(
                x=int(rect.x), y=int(rect.y), width=int(rect.w), height=int(rect.h),
                monitor=self.coordmap.monitor_for_rect(rect).name,
                dpi_scale=self.coordmap.monitor_for_rect(rect).scale,
            )
            log.info("followed window moved/resized -> %s", new_region.to_dict())
            self.config.set_region(new_region)
            self.config.save()
            self.pipeline.set_region(new_region)
            self.overlay.follow_region(new_region)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self.pipeline.describe(), self.window)
        dialog.selectRegionRequested.connect(lambda: (dialog.accept(), self._select_region()))
        dialog.clearCacheRequested.connect(self._clear_cache)
        dialog.applied.connect(self._apply_config)
        dialog.exec()

    def _open_glossary(self) -> None:
        dialog = GlossaryDialog(self.glossary, self.window)
        dialog.saved.connect(lambda g: self.notifier.info(tr("Glossary saved ({n} terms).").format(n=len(g)), toast=False))
        dialog.exec()

    def _open_stats(self) -> None:
        StatsDialog(self.pipeline.describe(), self.window).exec()

    def _open_logs(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.log_dir)))

    def _open_data_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.paths.root)))

    def _clear_cache(self) -> None:
        result = self.cache.clear()
        self.notifier.info(
            tr("Cache cleared ({n} pages).").format(n=result.get("removed", 0)), toast=False
        )
        self.window.refresh_state()

    @Slot(object)
    def _apply_config(self, config: Config) -> None:
        previous_language = self.config.general.language
        self.config = config
        self.config.save()
        new_language = set_language(self.config.general.language)
        self.config.general.language = new_language
        self.glossary.path = self.config.glossary_file()
        self.overlay.settings = self.config.snapshot().overlay
        self.overlay.set_click_through(self.config.overlay.click_through)
        self.cache.configure(
            self.config.cache.enabled,
            self.config.cache.max_mb,
            self.config.cache.hamming_threshold,
        )
        self.backend, warning = create_backend(self.config.translation, self.config.api_key())
        self.pipeline.backend = self.backend
        self.pipeline.update_config(self.config)
        self.hotkeys.restart(self.config.hotkeys)
        self._sync_bridge()
        self.window.set_hotkeys(self.hotkey_mapping())
        self.window.set_mode_state(self._manga_mode, self.config.overlay.visible,
                                   self.config.overlay.show_debug_hud)
        if new_language != previous_language:
            self.window.retranslate()
            self.tray.retranslate()
            self.notifier.info(tr("Language changed — interface updated."), toast=False)
        if warning:
            self.notifier.warn(warning, toast=False)
        if self._manga_mode:
            self.pipeline.refresh(force=False)
        self.notifier.info(tr("Settings applied."), toast=False)
        self.window.refresh_state()
        log.info("settings applied")

    # -- debug helpers -----------------------------------------------------
    def _debug_info(self, result, job: Optional[PageJob], score: float = 0.0, state: str = "") -> DebugInfo:
        region = self.config.region()
        return DebugInfo(
            generation=result.generation,
            page_hash=result.page_hash,
            change_score=score or (job.change_score if job else 0.0),
            state=state or self.pipeline.detector.status.state,
            stage="cache" if result.from_cache else "ocr+mt",
            from_cache=result.from_cache,
            ocr_ms=result.ocr_ms,
            translate_ms=result.translate_ms,
            block_count=len(result.blocks),
            extra=[f"region {region.width}x{region.height}@{region.dpi_scale:.2f}"] if region else [],
        )

    def _dump_debug_frame(self, result) -> None:
        """Save an overlay render for OCR/layout tuning (debug mode only)."""
        try:
            region = self.config.region()
            if region is None:
                return
            size = (self.overlay.width() or region.width, self.overlay.height() or region.height)
            image = render_overlay_image(
                size,
                self.overlay.cards,
                layout_settings(self.config.overlay),
                debug=self._debug_info(result, None),
            )
            path = self.paths.debug_dir / f"overlay_gen{result.generation}.png"
            image.save(str(path))
            self._debug_dumps += 1
            log.info("debug overlay written to %s", path)
        except Exception as exc:  # pragma: no cover - debug only
            log.debug("debug dump failed: %s", exc)

    # -- screens / DPI -----------------------------------------------------
    def _on_screens_changed(self) -> None:
        log.info("display configuration changed; rebuilding coordinate map")
        self.coordmap = self._build_coordmap()
        self.overlay.coordmap = self.coordmap
        self.selector.coordmap = self.coordmap
        self.capture.refresh_desktop()
        region = self.config.region()
        if region is None:
            return
        try:
            self.capture.clamp(region)
        except Exception as exc:
            log.warning("stored region is no longer valid: %s", exc)
            self.notifier.warn(
                tr("The selected region is no longer on a connected monitor. Select it again."),
                toast=False,
            )
            self.pipeline.pause()
            self.overlay.hide()
            return
        self.overlay.follow_region(region)
        self.pipeline.set_region(region)
        if self._manga_mode:
            self.pipeline.refresh(force=False)
        self.request_preview()

    # -- browser mode ------------------------------------------------------
    def browser_profile_dir(self):
        return self.paths.root / "browser-profile"

    def browser_port(self):
        """Port of a browser with remote debugging (ours first, then defaults)."""
        if self._browser_port is not None:
            return self._browser_port
        self._browser_port = discover_port(self.browser_profile_dir())
        return self._browser_port

    def launch_browser(self):
        """Start Chrome/Edge with remote debugging on a dedicated profile."""
        try:
            self._browser_port = launch_cdp_browser(
                self.browser_profile_dir(),
                url="about:blank",
            )
        except Exception as exc:
            log.warning("could not launch a browser: %s", exc)
            self.notifier.error(f"Не удалось запустить браузер: {exc}")
            return None
        self._browser_window = None
        self._browser_window_ts = 0.0
        return self._browser_port

    def connect_browser_tab(self, tab: Tab) -> bool:
        if not self.browser_mode.attach(tab):
            return False
        # screen capture and browser mode would fight over the overlay
        self.pipeline.pause()
        self.overlay.clear()
        self.browser_mode.start()
        self.window.set_status_message(f"Браузерный режим: {tab.title[:50]}")
        self.notifier.info(f"Браузерный режим подключён: {tab.title[:50]}", toast=False)
        return True

    def disconnect_browser(self) -> None:
        self.browser_mode.detach()
        self.overlay.clear()
        self.pipeline.set_masks([])
        self.window.set_status_message("Браузерный режим отключён")

    def open_browser_mode(self) -> None:
        from .browser.dialog import BrowserDialog

        dialog = BrowserDialog(self, self.window)
        dialog.exec()
        self.window.refresh_state()

    def _browser_client_rect(self):
        """Client area of the browser window (refreshed every couple of seconds)."""
        port = self._browser_port
        if port is None:
            return (0, 0, 0, 0)
        now = time.monotonic()
        if self._browser_window is None or now - self._browser_window_ts > 2.0:
            hint = ""
            if self.browser_mode.tab is not None:
                hint = self.browser_mode.tab.title
            self._browser_window = window_for_port(port, hint)
            self._browser_window_ts = now
        if self._browser_window is None:
            return (0, 0, 0, 0)
        if self._browser_window.hwnd and not is_window_alive(self._browser_window.hwnd):
            self._browser_window = None
            return (0, 0, 0, 0)
        return self._browser_window.client

    @Slot(object, object, object)
    def _on_browser_page(self, blocks, preview, region) -> None:
        # Browser mode paints its cards inside the page itself (exact alignment);
        # the control panel shows the page image and the block table instead.
        if not self.browser_mode.active:
            return
        result = PageResult(
            generation=self.pipeline.generations.current,
            page_hash="browser",
            blocks=list(blocks),
            from_cache=False,
            total_ms=0.0,
        )
        self._last_result = result
        if preview is not None:
            self.window.preview.set_frame(preview)
        self.window.set_page(result, None, [])
        self.overlay.show_translation(False)

    @Slot(str)
    def _on_browser_status(self, message: str) -> None:
        self.window.set_status_message(message)
        self.tray.set_status(message)

    @Slot(str)
    def _on_browser_failed(self, message: str) -> None:
        self.notifier.error(message)

    # -- public API (used by the control panel, tray and CLI) -------------
    def describe(self) -> dict:
        """Everything the control panel displays, in one dictionary."""
        payload = self.pipeline.describe()
        payload["manga_mode"] = self._manga_mode
        payload["detector"] = self.pipeline.detector_status()
        payload["displays"] = self.coordmap.describe()
        last = self.pipeline.last_result()
        payload["last_page"] = (
            {
                "generation": last.generation,
                "page_hash": last.page_hash,
                "blocks": len(last.blocks),
                "translated": len([b for b in last.blocks if b.translation]),
                "ocr_ms": last.ocr_ms,
                "translate_ms": last.translate_ms,
                "total_ms": last.total_ms,
                "from_cache": last.from_cache,
            }
            if last is not None
            else None
        )
        return payload

    def window_list(self) -> List[WindowInfo]:
        return self._list_windows()

    def hotkey_mapping(self) -> dict:
        s = self.config.hotkeys
        if not s.enabled:
            return {"Global hotkeys": "disabled"}
        return {
            "Toggle Manga Mode": s.toggle_manga_mode,
            "Show/Hide translation": s.toggle_overlay,
            "Refresh page": s.refresh_page,
            "Select region": s.select_region,
        }

    def request_preview(self) -> None:
        """One fresh frame for the control panel (never blocks the GUI thread)."""
        if self.pipeline.is_running:
            self.pipeline.request_preview()
            return
        region = self.config.region()
        if region is None:
            return
        try:
            frame = self.capture.grab(region)
        except PipelineError as exc:
            log.debug("preview grab failed: %s", exc.message)
            return
        self.window.set_preview_frame(frame, region)

    def refresh_page(self) -> None:
        self._refresh_page()

    def set_overlay_visible(self, visible: bool) -> None:
        self._set_overlay_visible(bool(visible))

    def open_settings(self) -> None:
        self._open_settings()

    def open_glossary(self) -> None:
        self._open_glossary()

    def clear_cache(self) -> None:
        self._clear_cache()

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        self.window.refresh_state()
        self.request_preview()

    def hide_window(self) -> None:
        self.window.hide()

    # -- lifecycle ---------------------------------------------------------
    def select_region(self) -> None:
        """Public entry point used by the CLI."""
        self._select_region()

    def set_manga_mode(self, enabled: bool) -> None:
        """Public entry point used by the CLI / tray."""
        self._set_manga_mode(bool(enabled))

    def run(self, show_window: bool = True) -> int:
        if self.tray.icon.isSystemTrayAvailable():
            self.tray.show()
        else:
            log.warning("no system tray available; use hotkeys (%s)", self.hotkeys.describe())
            self.notifier.attach(None, self.overlay)
        self.hotkeys.start()

        if self.config.bridge.enabled and self.config.bridge.autostart:
            self.start_bridge()

        region = self.config.region()
        if region is not None:
            self.overlay.follow_region(region)
            self.pipeline.set_region(region)
        if self.config.general.manga_mode_enabled and region is not None:
            self._set_manga_mode(True)
        else:
            self.notifier.info(
                tr("Manga Translator is running in the tray. Select a region and enable Manga Mode "
                   "({hotkey}).").format(hotkey=self.config.hotkeys.toggle_manga_mode),
                toast=False,
            )
        if show_window:
            self.show_window()
        log.info("ready; hotkeys: %s", self.hotkeys.describe())
        return self.qt.exec()

    def shutdown(self) -> None:
        log.info("shutting down")
        try:
            self.hotkeys.stop()
            self.pipeline.stop(timeout=2.0)
            if getattr(self, "ws_bridge", None) is not None:
                self.ws_bridge.stop()
        except Exception:  # pragma: no cover
            log.exception("error while stopping the pipeline")
        try:
            self.bridge.silenced()   # drop any late worker events
            self.cache.close()
            self.config.save()
            self.overlay.hide()
            self.tray.hide()
            self.window.hide()
        finally:
            self.qt.quit()


def show_fatal_error(message: str) -> None:
    """Last-resort readable error (never a traceback)."""
    log.error("fatal: %s", message)
    try:
        QApplication.instance() or QApplication([])
        QMessageBox.critical(None, f"{APP_DISPLAY_NAME} — error", message)
    except Exception:  # pragma: no cover - GUI unavailable
        print(f"{APP_DISPLAY_NAME}: {message}")
