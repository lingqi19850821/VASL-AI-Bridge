# -*- mode: python ; coding: utf-8 -*-
"""Two executables share one allowlisted onedir payload."""

import json
from pathlib import Path
import re
import sys

ROOT = Path(SPECPATH).resolve().parent
ICON_FILE = ROOT / "vasl_ai_bridge" / "assets" / "app.ico"
METADATA = ROOT / "build" / "windows" / "metadata"
VERSION_RESOURCE = str(METADATA / "windows-version-info.txt")
EXCLUDES = [
    "numpy", "torch", "torchaudio", "torchvision", "whisper", "faster_whisper",
    "ctranslate2", "transformers", "matplotlib", "cv2", "scipy", "tkinter",
    "PyQt5", "PyQt6", "PySide2", "IPython", "pytest",
]
ALLOWED_THIRD_PARTY = {"vasl_ai_bridge", "PIL", "PySide6", "shiboken6", "packaging", "_pyi_rth_utils"}

data_files = []
for name in json.loads((METADATA / "bundle-data.json").read_text(encoding="utf-8")):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Build metadata includes an unsafe data path.")
    data_files.append((str(METADATA / relative), str(relative.parent)))
data_files.append((str(ROOT / "vasl_ai_bridge" / "voice_worker.py"), "vasl_ai_bridge"))
data_files.append((str(ICON_FILE), "vasl_ai_bridge/assets"))

desktop = Analysis(
    [str(ROOT / "packaging" / "desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=data_files,
    hiddenimports=["vasl_ai_bridge.app", "PySide6.QtPdf", "PySide6.QtMultimedia"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
speech = Analysis(
    [str(ROOT / "packaging" / "speech_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["vasl_ai_bridge.whisper_desktop_worker", "vasl_ai_bridge.whisper_cpp_native_worker"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# A build environment may contain many unrelated packages. Fail instead of
# silently redistributing any Python package outside this runtime allowlist.
for analysis in (desktop, speech):
    # Qt 6.11 on Windows imports the OS ICU API with unversioned symbols.
    # A Conda-based Python can cause dependency discovery to pick its unrelated
    # ICU DLL (versioned symbols) instead, shadowing the Windows implementation.
    # The application does not use Conda ICU; let Windows provide its OS DLLs.
    analysis.binaries = [entry for entry in analysis.binaries
                         if not re.fullmatch(r"(?:icu|icuuc|icuin|icudt\d+)\.dll", Path(entry[0]).name.lower())]
    unexpected = sorted({name for name, _source, _kind in analysis.pure
                         if name.split(".")[0] not in sys.stdlib_module_names | ALLOWED_THIRD_PARTY})
    if unexpected:
        raise RuntimeError("Unexpected packages in payload: " + ", ".join(unexpected))

desktop_pyz = PYZ(desktop.pure)
speech_pyz = PYZ(speech.pure)
desktop_exe = EXE(
    desktop_pyz, desktop.scripts, [],
    exclude_binaries=True,
    name="VASL AI Bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    icon=str(ICON_FILE),
    version=VERSION_RESOURCE,
    contents_directory="_internal",
)
speech_exe = EXE(
    speech_pyz, speech.scripts, [("u", None, "OPTION"), ("X utf8", None, "OPTION")],
    exclude_binaries=True,
    name="ASL_AI-Speech",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    version=VERSION_RESOURCE,
    contents_directory="_internal",
)
payload = COLLECT(
    desktop_exe, speech_exe,
    desktop.binaries, desktop.datas,
    speech.binaries, speech.datas,
    strip=False,
    upx=False,
    name="VASL AI Bridge",
)
