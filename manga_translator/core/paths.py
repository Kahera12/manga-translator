"""Runtime paths (config, glossary, cache, logs, debug dumps).

Everything lives under %LOCALAPPDATA%\\MangaTranslator on Windows so the app
works when installed anywhere (including a PyInstaller onefile bundle, where
the executable's own folder is read-only / temporary).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import APP_NAME


def _base_dir() -> Path:
    override = os.environ.get("MT_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / APP_NAME
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / APP_NAME


@dataclass(frozen=True)
class Paths:
    root: Path
    config: Path
    glossary: Path
    cache_dir: Path
    cache_db: Path
    log_dir: Path
    log_file: Path
    debug_dir: Path
    env_file: Path

    def ensure(self) -> "Paths":
        for p in (self.root, self.cache_dir, self.log_dir, self.debug_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self


def get_paths() -> Paths:
    root = _base_dir()
    return Paths(
        root=root,
        config=root / "config.json",
        glossary=root / "glossary.json",
        cache_dir=root / "cache",
        cache_db=root / "cache" / "pages.db",
        log_dir=root / "logs",
        log_file=root / "logs" / "app.log",
        debug_dir=root / "debug",
        env_file=Path(sys.executable).resolve().parent / ".env"
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[2] / ".env",
    )
