"""Minimal Chrome DevTools Protocol client (no Selenium/Playwright dependency).

Chrome 136+ only honours `--remote-debugging-port` together with a *non-default*
`--user-data-dir`, and it writes the actual port into `DevToolsActivePort` inside
that directory — that is what this module reads, so the app can also attach to a
browser it launched earlier.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ..core.logging_setup import get_logger

log = get_logger("browser.cdp")

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

DEFAULT_PORTS = (9222, 9223, 9224)


class CdpError(RuntimeError):
    pass


@dataclass
class Tab:
    id: str
    title: str
    url: str
    ws_url: str
    type: str = "page"

    def label(self) -> str:
        return f"{self.title[:60]} — {self.url[:60]}"


def find_browser_executable() -> Optional[str]:
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def port_from_profile(profile_dir: Path) -> Optional[int]:
    """Read the debug port Chrome wrote into a profile directory."""
    marker = Path(profile_dir) / "DevToolsActivePort"
    if not marker.exists():
        return None
    try:
        first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
        return int(first)
    except (OSError, ValueError, IndexError):
        return None


def discover_port(profile_dir: Optional[Path] = None,
                  ports: tuple = DEFAULT_PORTS) -> Optional[int]:
    """Find a live DevTools endpoint: our own profile first, then common ports."""
    if profile_dir:
        port = port_from_profile(profile_dir)
        if port and _is_alive(port):
            return port
    for port in ports:
        if _is_alive(port):
            return port
    return None


def _is_alive(port: int, timeout: float = 1.5) -> bool:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def list_tabs(port: int, only_pages: bool = True) -> List[Tab]:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5)
        payload = response.json()
    except Exception as exc:
        raise CdpError(f"cannot list browser tabs: {exc}") from exc
    tabs = []
    for item in payload:
        if only_pages and item.get("type") != "page":
            continue
        if not item.get("webSocketDebuggerUrl"):
            continue
        tabs.append(Tab(id=item.get("id", ""), title=item.get("title", ""),
                        url=item.get("url", ""), ws_url=item["webSocketDebuggerUrl"],
                        type=item.get("type", "page")))
    return tabs


def launch_browser(
    profile_dir: Path,
    url: Optional[str] = None,
    window_size: tuple = (1240, 1330),
    window_position: tuple = (60, 30),
    extra_args: tuple = (),
    wait_s: float = 20.0,
) -> Optional[int]:
    """Start a browser with remote debugging on a dedicated profile.

    Returns the debug port when the endpoint becomes reachable.
    """
    executable = find_browser_executable()
    if executable is None:
        raise CdpError("Chrome/Edge not found - install a Chromium-based browser")
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        executable,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,TranslateUI",
        f"--window-size={window_size[0]},{window_size[1]}",
        f"--window-position={window_position[0]},{window_position[1]}",
        *extra_args,
    ]
    if url:
        args.append(url)
    log.info("launching browser: %s", executable)
    subprocess.Popen(args, close_fds=True)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        port = port_from_profile(profile_dir)
        if port and _is_alive(port):
            return port
        time.sleep(0.4)
    return port_from_profile(profile_dir)


class CdpConnection:
    """One WebSocket connection to a tab (CDP commands are id/result pairs)."""

    def __init__(self, ws_url: str, timeout: float = 20.0):
        from websockets.sync.client import connect

        self.ws_url = ws_url
        self.timeout = timeout
        self._ws = connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=timeout)
        self._next_id = 1

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        message_id = self._next_id
        self._next_id += 1
        payload = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        self._ws.send(json.dumps(payload))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            raw = self._ws.recv(timeout=max(1.0, deadline - time.time()))
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("id") != message_id:
                continue  # event, not our reply
            if "error" in message:
                raise CdpError(f"{method}: {message['error'].get('message')}")
            return message.get("result") or {}
        raise CdpError(f"{method}: timed out")

    def evaluate(self, expression: str, await_promise: bool = False,
                 timeout: Optional[float] = None) -> Any:
        """Run JavaScript in the tab and return the JSON value."""
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            description = (result["exceptionDetails"].get("exception") or {}).get("description")
            raise CdpError(f"javascript error: {description or result['exceptionDetails']}")
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # pragma: no cover - already closed
            pass

    def __enter__(self) -> "CdpConnection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
