# PyInstaller spec for the Real-Time Chinese Manga Translator.
#
#   pip install pyinstaller
#   pyinstaller packaging/manga_translator.spec
#   -> dist/MangaTranslator/MangaTranslator.exe   (windowed, no console)
#
# Notes
#  * rapidocr_onnxruntime ships its PP-OCRv4 .onnx models + config.yaml as package
#    data, so the whole package is collected (models are ~16 MB).
#  * PySide6 needs its Qt plugins; the hooks below collect the essentials for a
#    tray + translucent overlay application.
#  * `console=False` removes the console window; log output goes to
#    %LOCALAPPDATA%\MangaTranslator\logs\app.log (see README).

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(SPEC)), ".."))

datas = []
datas += collect_data_files("rapidocr_onnxruntime", includes=["*.yaml", "models/*.onnx"])
datas += [(os.path.join(ROOT, ".env.example"), ".")]

hiddenimports = []
hiddenimports += collect_submodules("rapidocr_onnxruntime")
hiddenimports += [
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "cv2",
    "mss",
    "mss.windows",
    "PIL.ImageGrab",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas", "pytest", "PySide6.QtWebEngineCore"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MangaTranslator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,              # no console window during normal use
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MangaTranslator",
)
