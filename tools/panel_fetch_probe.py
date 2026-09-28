"""Which way can we get the original panel bytes? (direct / canvas / CDP body)"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from manga_translator.browser.cdp import CdpConnection, discover_port, list_tabs
from manga_translator.browser.reader import read_state

PROFILE = Path(r"C:\Users\neuro\.dsh\translator\runtime\browser-profile")

conn = CdpConnection(list_tabs(discover_port(PROFILE))[0].ws_url)
state = read_state(conn)
panel = next(p for p in state.panels if p.nat_w > 800)
src = panel.src
print("panel:", panel.nat_w, "x", panel.nat_h, "| host:", src.split("/")[2])

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154 Safari/537.36")
for label, headers in (
    ("direct, no referer", {"User-Agent": UA}),
    ("direct, with referer", {"User-Agent": UA, "Referer": state.url}),
):
    try:
        response = httpx.get(src, headers=headers, timeout=30, follow_redirects=True)
        content_type = response.headers.get("content-type")
        allow = response.headers.get("access-control-allow-origin")
        print(f"{label}: HTTP {response.status_code} {content_type} "
              f"{len(response.content)} bytes ACAO={allow}")
    except Exception as exc:
        print(f"{label}: {type(exc).__name__} {exc}")

CANVAS_JS = """
(async () => {
  const url = %s;
  const img = new Image();
  img.crossOrigin = 'anonymous';
  const loaded = new Promise((resolve) => {
    img.onload = () => resolve('load');
    img.onerror = () => resolve('error');
  });
  img.src = url;
  if (await loaded !== 'load') return JSON.stringify({result: 'image error'});
  try {
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    canvas.getContext('2d').drawImage(img, 0, 0);
    const data = canvas.toDataURL('image/png');
    return JSON.stringify({result: 'ok', chars: data.length});
  } catch (error) {
    return JSON.stringify({result: 'tainted', error: String(error)});
  }
})()
""" % json.dumps(src)
try:
    print("canvas (crossOrigin=anonymous):", conn.evaluate(CANVAS_JS, await_promise=True, timeout=90))
except Exception as exc:
    print("canvas failed:", type(exc).__name__, exc)

# CDP network interception: reload with Network enabled and read response bodies
try:
    conn.call("Network.enable", {"maxResourceBufferSize": 64 * 1024 * 1024,
                                 "maxTotalBufferSize": 256 * 1024 * 1024})
    conn.call("Page.enable")
    seen: list = []
    import time

    conn.call("Page.reload", {"ignoreCache": False})
    deadline = time.time() + 25
    request_ids = {}
    while time.time() < deadline and len(seen) < 4:
        try:
            raw = conn._ws.recv(timeout=2)  # type: ignore[attr-defined]
        except Exception:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        method = message.get("method")
        params = message.get("params") or {}
        if method == "Network.responseReceived":
            response = params.get("response") or {}
            mime = (response.get("mimeType") or "")
            url = response.get("url") or ""
            if mime.startswith("image") and "sign=" in url:
                request_ids[params.get("requestId")] = (url, response.get("encodedDataLength"))
        if method == "Network.loadingFinished":
            key = params.get("requestId")
            if key in request_ids:
                try:
                    body = conn.call("Network.getResponseBody", {"requestId": key})
                    data = body.get("body") or ""
                    import base64 as _b64

                    size = len(_b64.b64decode(data)) if body.get("base64Encoded") else len(data)
                    seen.append((request_ids[key][0][-40:], size))
                except Exception as exc:
                    seen.append((request_ids[key][0][-40:], f"error {exc}"))
    print("CDP network bodies:", seen[:4] or "none captured")
except Exception as exc:
    print("CDP network failed:", type(exc).__name__, exc)

conn.close()
