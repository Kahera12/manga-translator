"""Entry point: Real-Time Chinese Manga Translator for Windows.

    python main.py                   # control panel + tray + overlay
    python main.py --tray-only       # no window, tray only
    python main.py --select-region   # start by picking the capture region
    python main.py --manga-mode      # enable Manga Mode on startup
    python main.py --debug           # verbose logging + debug HUD
    python main.py --selftest        # offline self-test (no GUI, no key)
    python main.py --version
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback

# DPI awareness must be declared before Qt (and before any window) is created.
from manga_translator import APP_DISPLAY_NAME, __version__
from manga_translator.core import dpi as _dpi


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="manga-translator",
        description="Real-time Chinese manga translator (region -> OCR -> Gemini -> overlay).",
    )
    parser.add_argument("--debug", action="store_true", help="verbose logging and debug HUD")
    parser.add_argument("--select-region", action="store_true", help="start by selecting the region")
    parser.add_argument("--manga-mode", action="store_true", help="enable Manga Mode on startup")
    parser.add_argument("--tray-only", action="store_true",
                        help="do not open the control panel, stay in the tray")
    parser.add_argument("--set-env", nargs=2, metavar=("NAME", "VALUE"),
                        help="override a setting through the environment, e.g. TEXT_MODEL")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--selftest", action="store_true",
                        help="run the offline self-test instead of the GUI")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.version:
        print(f"{APP_DISPLAY_NAME} {__version__}")
        return 0
    if args.set_env:
        os.environ[args.set_env[0]] = args.set_env[1]
    if args.debug:
        os.environ["DEBUG_MODE"] = "true"

    if args.selftest:
        from tools.selftest import run_selftest

        return 0 if run_selftest(verbose=True) else 1

    _dpi.enable_per_monitor_dpi_awareness()

    from manga_translator.app import MangaTranslatorApp, show_fatal_error

    try:
        app = MangaTranslatorApp(sys.argv)
    except Exception as exc:  # pragma: no cover - startup failures
        detail = traceback.format_exc()
        try:
            from manga_translator.core.paths import get_paths

            paths = get_paths().ensure()
            (paths.log_dir / "startup-error.log").write_text(detail, encoding="utf-8")
        except Exception:
            print(detail, file=sys.stderr)
        show_fatal_error(
            f"Could not start the application: {exc.__class__.__name__}: {exc}\n"
            "See the log file for details."
        )
        return 2

    from PySide6.QtCore import QTimer

    if args.select_region:
        QTimer.singleShot(900, app.select_region)
    if args.manga_mode:
        QTimer.singleShot(1100, lambda: app.set_manga_mode(True))
    if args.tray_only:
        QTimer.singleShot(0, app.hide_window)

    return app.run(show_window=not args.tray_only)


if __name__ == "__main__":
    sys.exit(main())
