"""Find a way to get the unpacked extension into a real Chrome, and prove it works.

    python tools/extension_install_probe.py [--url <chapter>] [--extension extension]

Chrome 137+ refuses `--load-extension` on the command line, and
`Extensions.loadUnpacked` over a *port* based debugging session returns an id
without the extension ever appearing in `chrome://extensions`. This probe tries the
combinations and reports, for each, whether the extension is really installed
(visible in chrome://extensions) and whether its content script actually runs
(an isolated execution context appears on the page after a reload).

Read-only with respect to the project: it only starts browsers in runtime/ profiles.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

EXT_LIST_JS = """
(() => {
  const m = document.querySelector('extensions-manager');
  if (!m || !m.shadowRoot) return 'no-manager';
  const l = m.shadowRoot.querySelector('extensions-item-list');
  if (!l || !l.shadowRoot) return 'no-list';
  return JSON.stringify([...l.shadowRoot.querySelectorAll('extensions-item')]
    .map(i => ({id: i.id, name: ((i.shadowRoot.querySelector('#name') || {}).textContent || '').trim()})));
})()
"""

PAGE_STATE_JS = ("JSON.stringify({cards: document.querySelectorAll('.mt-card').length,"
                 " holders: document.querySelectorAll('.mt-holder').length,"
                 " badge: (document.querySelector('[data-mt-badge]') || {}).textContent || ''})")


class Session:
    """A browser process we drive over CDP."""

    def __init__(self, name: str, profile: Path, url: str, extra: list, executable: str):
        self.name = name
        self.profile = profile
        self.url = url
        self.extra = extra
        self.executable = executable
        self.port = 0
        self.process = None

    def start(self) -> bool:
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [self.executable, "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", *self.extra, self.url]
        print(f"\n=== {self.name}\n    {' '.join(args[1:4])} ...")
        self.process = subprocess.Popen(args, close_fds=True)
        for _ in range(40):
            port = self._port()
            if port:
                self.port = port
                return True
            time.sleep(0.5)
        return False

    def _port(self) -> int:
        marker = self.profile / "DevToolsActivePort"
        if not marker.exists():
            return 0
        try:
            port = int(marker.read_text().splitlines()[0])
            httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=1.0)
            return port
        except Exception:
            return 0

    def close(self) -> None:
        if self.process is not None:
            try:
                self.process.kill()
            except Exception:
                pass

    # -- cdp helpers ------------------------------------------------------
    def ws(self, kind: str = "browser") -> str:
        if kind == "browser":
            return httpx.get(f"http://127.0.0.1:{self.port}/json/version",
                             timeout=5.0).json()["webSocketDebuggerUrl"]
        tabs = self.targets()
        wanted = [t for t in tabs if kind in (t.get("url") or "")]
        if not wanted:
            raise RuntimeError(f"no target matching {kind!r}")
        return wanted[0]["webSocketDebuggerUrl"]

    def targets(self) -> list:
        return httpx.get(f"http://127.0.0.1:{self.port}/json/list", timeout=5.0).json()

    def call(self, ws_url: str, steps: list) -> dict:
        from websockets.sync.client import connect

        out: dict = {}
        with connect(ws_url, open_timeout=10.0, max_size=32 * 1024 * 1024) as socket:
            for index, (method, params) in enumerate(steps, start=1):
                socket.send(json.dumps({"id": index, "method": method, "params": params or {}}))
            wanted = {index: method for index, (method, _p) in enumerate(steps, start=1)}
            deadline = time.time() + 30
            while wanted and time.time() < deadline:
                try:
                    message = json.loads(socket.recv(timeout=5.0))
                except TimeoutError:
                    break
                if message.get("id") in wanted:
                    out[wanted.pop(message["id"])] = message
        return out

    def load_unpacked(self, extension: Path) -> str:
        reply = self.call(self.ws("browser"), [("Extensions.loadUnpacked",
                                                {"path": str(extension)})])
        message = reply.get("Extensions.loadUnpacked") or {}
        if "error" in message:
            return f"error: {message['error'].get('message')}"
        return str((message.get("result") or {}).get("id") or "no id")

    def installed(self) -> str:
        self.call(self.ws("browser"), [("Target.createTarget", {"url": "chrome://extensions/"})])
        time.sleep(3.0)
        try:
            page = self.ws("chrome://extensions")
        except RuntimeError:
            return "no extensions page"
        reply = self.call(page, [("Runtime.evaluate", {"expression": EXT_LIST_JS,
                                                       "returnByValue": True})])
        value = (((reply.get("Runtime.evaluate") or {}).get("result") or {})
                 .get("result") or {}).get("value")
        return str(value)

    def content_script_worlds(self, seconds: float = 14.0) -> int:
        """Reload the chapter and count isolated worlds (content script contexts)."""
        from websockets.sync.client import connect

        page = self.ws("kuaikan")
        worlds = 0
        with connect(page, open_timeout=10.0, max_size=32 * 1024 * 1024) as socket:
            socket.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            socket.send(json.dumps({"id": 2, "method": "Page.reload", "params": {}}))
            deadline = time.time() + seconds
            while time.time() < deadline:
                try:
                    message = json.loads(socket.recv(timeout=2.0))
                except TimeoutError:
                    break
                if message.get("method") == "Runtime.executionContextCreated":
                    context = message["params"]["context"]
                    origin = context.get("origin") or ""
                    kind = (context.get("auxData") or {}).get("type")
                    if kind == "isolated" or "chrome-extension" in origin:
                        worlds += 1
            socket.send(json.dumps({"id": 3, "method": "Runtime.evaluate",
                                    "params": {"expression": PAGE_STATE_JS,
                                               "returnByValue": True}}))
            deadline = time.time() + 8
            while time.time() < deadline:
                try:
                    message = json.loads(socket.recv(timeout=2.0))
                except TimeoutError:
                    break
                if message.get("id") == 3:
                    print("    page:",
                          ((message.get("result") or {}).get("result") or {}).get("value"))
                    break
        return worlds


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://www.kuaikanmanhua.com/webs/comic-next/185055")
    parser.add_argument("--extension", default="extension")
    parser.add_argument("--executable",
                        default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    args = parser.parse_args()

    extension = (ROOT / args.extension).resolve()
    variants = [
        ("A: --load-extension + feature off",
         ["--load-extension=" + str(extension),
          "--disable-features=Translate,TranslateUI,DisableLoadExtensionCommandLineSwitch"],
         False),
        ("B: --load-extension + enable-unsafe-extension-debugging",
         ["--load-extension=" + str(extension), "--enable-unsafe-extension-debugging"],
         False),
        ("C: no flag, Extensions.loadUnpacked",
         ["--enable-unsafe-extension-debugging"], True),
        ("D: --load-extension only (baseline)",
         ["--load-extension=" + str(extension)], False),
    ]
    for name, extra, load_via_cdp in variants:
        session = Session(name, ROOT / f"runtime/probe-{name[0].lower()}", args.url, extra,
                          args.executable)
        try:
            if not session.start():
                print("    could not start")
                continue
            print(f"    port {session.port}")
            if load_via_cdp:
                print(f"    loadUnpacked -> {session.load_unpacked(extension)}")
            print(f"    installed: {session.installed()}")
            worlds = session.content_script_worlds()
            print(f"    content script worlds: {worlds}")
            if worlds:
                print(f"    ==> WORKS: {name}")
                return 0
        except Exception as exc:
            print(f"    failed: {type(exc).__name__}: {exc}")
        finally:
            session.close()
            time.sleep(1.0)
    print("\nno variant produced a content script; the extension must be loaded by hand "
          "via chrome://extensions")
    return 1


if __name__ == "__main__":
    sys.exit(main())
