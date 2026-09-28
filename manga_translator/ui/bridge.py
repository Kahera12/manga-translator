"""Qt signal bridge: worker threads -> GUI thread.

The pipeline never touches widgets. It publishes through this object, whose
signals are delivered as queued calls on the GUI thread, which is what keeps the
overlay responsive while OCR/translation run in the background.

Emitting is guarded: during shutdown a background thread may publish one last
event after the Qt object is already gone ("Signal source has been deleted"),
which must never crash the app.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..core.logging_setup import get_logger
from ..pipeline.orchestrator import PageJob, Publisher

log = get_logger("ui.bridge")


class QtPublisher(QObject):
    pageStarted = Signal(object)          # PageJob
    pageReady = Signal(object, object)    # PageResult, PageJob
    failed = Signal(str, str)             # message, kind
    statusChanged = Signal(str, str)      # message, level
    detectorChanged = Signal(str, float, int)  # state, score, generation
    busyChanged = Signal(bool)
    previewReady = Signal(object, object)  # frame, region (control-panel preview)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bridge = _Bridge(self)

    @property
    def publisher(self) -> Publisher:
        return self._bridge

    def silenced(self) -> None:
        """Called on shutdown so late worker events are dropped quietly."""
        self._bridge.silence()


class _Bridge(Publisher):
    """Plain Publisher implementation that only emits Qt signals."""

    def __init__(self, owner: QtPublisher):
        self._owner = owner
        self._silent = False

    def silence(self) -> None:
        self._silent = True

    def _emit(self, name: str, *args) -> None:
        if self._silent:
            return
        try:
            getattr(self._owner, name).emit(*args)
        except RuntimeError:  # pragma: no cover - Qt object already destroyed
            self._silent = True
            log.debug("dropped %s after shutdown", name)

    def page_started(self, job: PageJob) -> None:
        self._emit("pageStarted", job)

    def page_ready(self, result, job: PageJob) -> None:
        self._emit("pageReady", result, job)

    def error(self, message: str, kind: str = "error") -> None:
        self._emit("failed", message, kind)

    def status(self, message: str, level: str = "info") -> None:
        self._emit("statusChanged", message, level)

    def detector(self, state: str, score: float, generation: int) -> None:
        self._emit("detectorChanged", state, float(score), int(generation))

    def busy(self, processing: bool) -> None:
        self._emit("busyChanged", bool(processing))

    def preview(self, frame, region) -> None:
        self._emit("previewReady", frame, region)
