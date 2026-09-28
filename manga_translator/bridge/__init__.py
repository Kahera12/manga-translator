"""WebSocket bridge between the browser extension and the translator.

The extension runs in the user's normal Chrome (no debug profile, no CDP): it
sends the page's panel images and draws what comes back in page coordinates.
:mod:`~manga_translator.bridge.protocol` is the wire format,
:mod:`~manga_translator.bridge.server` the endpoint and
:mod:`~manga_translator.bridge.service` the batched translation behind it.
"""
from .protocol import (
    DEFAULT_PORT,
    PROTOCOL_VERSION,
    BlockPayload,
    PanelPayload,
    PanelSpec,
    decode,
    encode,
)
from .server import BridgeServer
from .service import BridgeSession, SessionStatus

__all__ = [
    "BlockPayload",
    "BridgeServer",
    "BridgeSession",
    "DEFAULT_PORT",
    "PROTOCOL_VERSION",
    "PanelPayload",
    "PanelSpec",
    "SessionStatus",
    "decode",
    "encode",
]
