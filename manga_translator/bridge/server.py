"""WebSocket server the browser extension connects to (127.0.0.1 only).

Deliberately tiny: it owns the socket, one :class:`~manga_translator.bridge.service.BridgeSession`
per connection and nothing else. All HTTP/WS work happens on this server's own
thread; the translation worker of each session runs separately, so a slow model
never blocks the socket (the extension keeps sending scroll updates meanwhile).

The server binds the loopback interface only - it is a local bridge, not a service.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional, Set

from ..core.logging_setup import get_logger
from .protocol import DEFAULT_PORT, PanelSpec, decode, encode
from .service import BridgeSession

log = get_logger("bridge.server")

PORT_RANGE = 10          # if 8770 is taken, try the next few before giving up


class BridgeServer:
    """Owns the WebSocket endpoint and the sessions of the connected tabs."""

    def __init__(self, session_factory: Callable[[Callable[[str], None]], BridgeSession],
                 host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 on_change: Optional[Callable[[], None]] = None):
        self._factory = session_factory
        self.host = host
        self.port = int(port)
        self._on_change = on_change
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._sessions: Set[BridgeSession] = set()
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.started_at = 0.0
        self.last_error = ""

    # -- lifecycle --------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> bool:
        if self._server is not None:
            return True
        try:
            from websockets.sync.server import serve
        except ImportError as exc:                       # pragma: no cover
            self.last_error = f"websockets is not installed: {exc}"
            log.error("%s", self.last_error)
            return False
        last: Optional[Exception] = None
        for offset in range(PORT_RANGE):
            candidate = self.port + offset
            try:
                self._server = serve(self._handler, self.host, candidate,
                                     max_size=64 * 1024 * 1024, ping_interval=20,
                                     ping_timeout=20)
            except OSError as exc:
                last = exc
                continue
            if offset:
                log.info("port %d was busy; the bridge listens on %d instead",
                         self.port, candidate)
            self.port = candidate
            break
        if self._server is None:
            self.last_error = f"could not bind {self.host}:{self.port} ({last})"
            log.error("%s", self.last_error)
            return False
        self._thread = threading.Thread(target=self._serve, name="mt-bridge", daemon=True)
        self._thread.start()
        self.started_at = time.time()
        self.last_error = ""
        log.info("bridge listening on ws://%s:%d/ws", self.host, self.port)
        self._changed()
        return True

    def _serve(self) -> None:
        try:
            self._server.serve_forever()
        except Exception as exc:                          # pragma: no cover - shutdown noise
            if self._server is not None:
                log.warning("bridge server stopped: %s", exc)
        finally:
            self._server = None

    def stop(self) -> None:
        server, self._server = self._server, None
        with self._lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()
        if server is not None:
            try:
                server.shutdown()
            except Exception as exc:                      # pragma: no cover
                log.debug("bridge shutdown: %s", exc)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        log.info("bridge stopped")

    # -- connections ------------------------------------------------------
    def _handler(self, conn) -> None:
        path = ""
        try:
            path = getattr(getattr(conn, "request", None), "path", "") or ""
        except Exception:                                 # pragma: no cover
            path = ""
        if path and path not in ("/", "/ws"):
            log.warning("rejecting a connection to %r (expected /ws)", path)
            try:
                conn.close(code=1008, reason="use /ws")
            except Exception:
                pass
            return
        session = self._factory(lambda text: self._send(conn, text))
        session.start()
        with self._lock:
            self._sessions.add(session)
        peer = self._peer(conn)
        log.info("extension connected from %s (%d session(s))", peer, len(self._sessions))
        self._changed()
        try:
            for raw in conn:
                message = decode(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                if not self._dispatch(session, message):
                    log.debug("ignoring message %r", message.get("type"))
        except Exception as exc:
            log.debug("connection from %s ended: %s", peer, exc)
        finally:
            session.close()
            with self._lock:
                self._sessions.discard(session)
            log.info("extension disconnected (%d session(s) left)", len(self._sessions))
            self._changed()

    @staticmethod
    def _peer(conn) -> str:
        try:
            address = conn.remote_address
            return f"{address[0]}:{address[1]}"
        except Exception:                                 # pragma: no cover
            return "?"

    def _send(self, conn, text: str) -> None:
        """Thread-safe send: sessions emit from their own worker thread."""
        with self._send_lock:
            conn.send(text)

    def _dispatch(self, session: BridgeSession, message: dict) -> bool:
        kind = message.get("type")
        if kind == "hello":
            session.hello(str(message.get("url") or ""), str(message.get("title") or ""),
                          message.get("tab"))
            return True
        if kind == "register":
            panels = [PanelSpec.from_dict(item) for item in message.get("panels") or []
                      if isinstance(item, dict)]
            session.register(panels, str(message.get("page") or ""))
            return True
        if kind == "request":
            keys = [str(k) for k in message.get("keys") or []]
            session.request(keys, str(message.get("mode") or ""),
                            bool(message.get("priority", False)))
            return True
        if kind == "cancel":
            session.cancel([str(k) for k in message.get("keys") or []])
            return True
        if kind == "status":
            session.send_status()
            return True
        return False

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception as exc:                      # pragma: no cover
                log.debug("bridge on_change failed: %s", exc)

    # -- introspection ----------------------------------------------------
    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._sessions)

    def sessions(self) -> List[BridgeSession]:
        with self._lock:
            return list(self._sessions)

    def status(self) -> dict:
        sessions = self.sessions()
        return {
            "running": self.running,
            "host": self.host,
            "port": self.port,
            "clients": len(sessions),
            "url": f"ws://{self.host}:{self.port}/ws",
            "error": self.last_error,
            "sessions": [s.status.as_dict() for s in sessions],
        }

    def broadcast(self, message_type: str, **payload: str) -> None:
        for session in self.sessions():
            session.send(message_type, **payload)


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, int(port))) != 0
