"""What does the reader's DOM look like around a panel image?

    python tools/panel_dom_probe.py --port 8066

For the first few large images on the page it prints the computed position/float,
the parent chain (tag, class, children, width) and the nearest positioned ancestor,
so the overlay-anchoring rules can be checked against a real layout instead of
assumptions.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

JS = r"""
(() => {
  const describe = (el) => {
    if (!el) return null;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      cls: (el.className && String(el.className).slice(0, 60)) || '',
      id: el.id || '',
      children: el.children ? el.children.length : 0,
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      position: style.position,
      cssFloat: style.cssFloat,
      display: style.display,
      overflow: style.overflow,
    };
  };
  const out = [];
  let index = 0;
  for (const img of document.images) {
    if (img.naturalWidth < 500 || img.naturalHeight < 500) continue;
    if (index >= 4) break;
    index += 1;
    let positioned = null;
    let node = img.parentElement;
    for (let depth = 0; node && depth < 6; node = node.parentElement, depth++) {
      if (getComputedStyle(node).position !== 'static') { positioned = node; break; }
    }
    out.push({
      src: (img.currentSrc || img.src || '').slice(-40),
      natural: [img.naturalWidth, img.naturalHeight],
      clientWidth: img.clientWidth,
      own: describe(img),
      parent: describe(img.parentElement),
      grandparent: describe(img.parentElement && img.parentElement.parentElement),
      nearestPositioned: describe(positioned),
      parentEqualsBody: img.parentElement === document.body,
    });
  }
  return JSON.stringify(out);
})()
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    tabs = httpx.get(f"http://127.0.0.1:{args.port}/json/list", timeout=5.0).json()
    pages = [t for t in tabs if t.get("type") == "page" and "chrome://" not in (t.get("url") or "")]
    if not pages:
        print("no page target")
        return 1
    from websockets.sync.client import connect

    with connect(pages[0]["webSocketDebuggerUrl"], open_timeout=10.0,
                 max_size=16 * 1024 * 1024) as socket:
        socket.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": JS, "returnByValue": True}}))
        while True:
            message = json.loads(socket.recv(timeout=20.0))
            if message.get("id") == 1:
                value = ((message.get("result") or {}).get("result") or {}).get("value")
                break
    panels = json.loads(value or "[]")
    print(f"{len(panels)} large image(s)\n")
    for panel in panels:
        print(f"--- {panel['src']}  natural={panel['natural']} clientWidth={panel['clientWidth']}")
        for label in ("own", "parent", "grandparent", "nearestPositioned"):
            info = panel.get(label)
            if info is None:
                print(f"    {label:18} —")
                continue
            print(f"    {label:18} <{info['tag']} class={info['cls']!r} id={info['id']!r}> "
                  f"children={info['children']} {info['width']}x{info['height']} "
                  f"position={info['position']} float={info['cssFloat']} display={info['display']}")
        print(f"    parent is body: {panel['parentEqualsBody']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
