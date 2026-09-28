"""Probe a Chrome tab via CDP: can we get the reader's images from the DOM?"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
BASE = f"http://127.0.0.1:{PORT}"

try:
    version = httpx.get(f"{BASE}/json/version", timeout=5).json()
    print("browser:", version.get("Browser"), "|", version.get("webSocketDebuggerUrl", "")[:60])
except Exception as exc:
    print("no debug endpoint on", PORT, "->", type(exc).__name__, exc)
    sys.exit(2)

tabs = httpx.get(f"{BASE}/json/list", timeout=5).json()
pages = [t for t in tabs if t.get("type") == "page"]
print(f"tabs: {len(pages)}")
for tab in pages:
    print(f"  - {tab.get('title', '')[:60]!r} {tab.get('url', '')[:70]}")

target = next((t for t in pages if "kuaikan" in (t.get("url") or "")), None)
if target is None:
    print("no kuaikan tab")
    sys.exit(3)

from websockets.sync.client import connect  # noqa: E402

JS_IMAGES = """
(() => {
  const out = [];
  for (const img of document.images) {
    const r = img.getBoundingClientRect();
    if (img.naturalWidth < 200 || img.naturalHeight < 200) continue;
    const style = getComputedStyle(img);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    out.push({
      src: (img.currentSrc || img.src || '').slice(0, 160),
      x: Math.round(r.x + window.scrollX), y: Math.round(r.y + window.scrollY),
      w: Math.round(r.width), h: Math.round(r.height),
      nw: img.naturalWidth, nh: img.naturalHeight,
      complete: img.complete,
    });
  }
  return JSON.stringify({
    images: out,
    dpr: window.devicePixelRatio,
    scroll: {x: window.scrollX, y: window.scrollY},
    viewport: {w: innerWidth, h: innerHeight},
    docHeight: document.documentElement.scrollHeight,
  });
})()
"""

with connect(target["webSocketDebuggerUrl"], max_size=40 * 1024 * 1024, open_timeout=10) as ws:
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                        "params": {"expression": JS_IMAGES, "returnByValue": True}}))
    while True:
        message = json.loads(ws.recv(timeout=20))
        if message.get("id") == 1:
            break
    result = message["result"]["result"].get("value")
    data = json.loads(result) if result else {}

print(f"devicePixelRatio: {data.get('dpr')}  scroll: {data.get('scroll')}  "
      f"viewport: {data.get('viewport')}  document height: {data.get('docHeight')}")
images = data.get("images") or []
print(f"images >= 200px: {len(images)}")
for image in images[:12]:
    print(f"  {image['w']}x{image['h']} (natural {image['nw']}x{image['nh']}) "
          f"at ({image['x']},{image['y']}) complete={image['complete']} {image['src'][-60:]}")
