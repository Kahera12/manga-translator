"""Load the unpacked extension into an already running Chrome over CDP.

    python tools/extension_load.py --port 2775 [--extension extension]

Why this exists: Chrome 137+ ignores ``--load-extension`` on the command line
(silently - the browser starts, the extension is simply never loaded), so the only
scriptable way to install an unpacked extension is the CDP ``Extensions`` domain,
which is browser-scoped and therefore needs a connection to the *browser* endpoint
rather than to a tab.

Prints the loaded extension ids, or the reason it could not be loaded.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402


def browser_ws(port: int) -> str:
    response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=5.0)
    response.raise_for_status()
    return response.json().get("webSocketDebuggerUrl", "")


def targets(port: int) -> list:
    response = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5.0)
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--extension", default="extension")
    args = parser.parse_args()

    path = (ROOT / args.extension).resolve()
    if not (path / "manifest.json").exists():
        print(f"no manifest.json in {path}")
        return 1

    ws_url = browser_ws(args.port)
    print(f"browser endpoint: {ws_url}")
    from websockets.sync.client import connect

    with connect(ws_url, open_timeout=10.0, max_size=16 * 1024 * 1024) as socket:
        def call(message_id: int, method: str, params: dict = None) -> dict:
            payload = {"id": message_id, "method": method}
            if params:
                payload["params"] = params
            socket.send(json.dumps(payload))
            while True:
                raw = socket.recv(timeout=30.0)
                message = json.loads(raw)
                if message.get("id") == message_id:
                    return message

        reply = call(1, "Extensions.loadUnpacked", {"path": str(path)})
        if "error" in reply:
            print(f"Extensions.loadUnpacked failed: {reply['error'].get('message')}")
            print("fallback: load it by hand via chrome://extensions → Load unpacked")
            return 2
        print(f"loadUnpacked -> {reply.get('result')}")

    loaded = [t for t in targets(args.port)
              if t.get("type") in ("service_worker", "background_page")]
    print(f"\ntargets: {len(targets(args.port))}, extension workers: {len(loaded)}")
    for target in loaded:
        print(f"  {target.get('type')}: {target.get('url')}")
    return 0 if loaded else 3


if __name__ == "__main__":
    sys.exit(main())
