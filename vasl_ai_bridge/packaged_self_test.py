"""Offline checks run by the shipped executable against synthetic local data."""

from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from . import APP_NAME

def _pdf_fixture() -> bytes:
    stream = b"BT /F1 14 Tf 40 740 Td (Synthetic packaged reference) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    return data + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n"


def _require(condition: bool) -> None:
    if not condition:
        raise AssertionError("A synthetic packaged check failed.")


def _run(checks: dict[str, bool], directory: Path) -> None:
    # All files and preferences are synthetic. Neither external AI software nor
    # a user's desktop, microphone, credentials, or game archive is exercised.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PIL import Image
    checks["pillow_import"] = True
    from PySide6.QtCore import Qt
    checks["qt_core_import"] = True
    from PySide6.QtGui import QImage
    checks["qt_gui_import"] = True
    from PySide6.QtMultimedia import QAudioFormat
    checks["qt_multimedia_import"] = True
    from PySide6.QtWidgets import QApplication
    checks["qt_widgets_import"] = True
    from . import app as application
    from .chat_session import ChatSession
    from .chat_storage import ChatStore
    from .codex_bridge import find_codex_executable
    from .frozen_runtime import speech_helper_path
    from .launcher import _startup_diagnostic
    from .reference_library import ReferenceLibrary
    from .reference_text import extract_document, render_pdf_page

    qt_app = QApplication.instance() or QApplication([f"{APP_NAME} packaged verification"])
    _require(qt_app.platformName() in {"windows", "offscreen"})
    checks["qt_platform"] = True
    audio = QAudioFormat()
    audio.setSampleRate(16000)
    _require(audio.sampleRate() == 16000)
    checks["qt_multimedia_import"] = True

    store = ChatStore(directory / "data")
    session = ChatSession(title="Synthetic packaged check")
    session.add_message("user", "Synthetic UTF-8 中文")
    saved = store.save(session)
    loaded = store.load(saved)
    _require(loaded.messages[0].text == "Synthetic UTF-8 中文")
    checks["sqlite_archive_roundtrip"] = True

    image = BytesIO()
    Image.new("RGB", (32, 24), "blue").save(image, "PNG")
    png = directory / "synthetic.png"
    png.write_bytes(image.getvalue())
    reference = ReferenceLibrary(store.root).import_file(png)
    _require(reference.content == image.getvalue())
    _require(not QImage.fromData(reference.content, "PNG").isNull())
    checks["pillow_png_reference"] = True
    pdf = _pdf_fixture()
    _require("Synthetic packaged reference" in extract_document("synthetic.pdf", pdf)[0].text)
    _require(render_pdf_page(pdf, 1).startswith("data:image/png;base64,"))
    checks["qt_pdf_text_and_render"] = True

    old_list_windows = application.list_windows
    application.list_windows = lambda: []
    window = None
    try:
        window = application.MainWindow(store=store)
        window.connection_timer.stop()
        window.login_return_timer.stop()
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        qt_app.processEvents()
        _require(not window.grab().isNull())
        window.settings.setValue("packaged-verification", True)
        window.settings.sync()
        _require(window.settings.fileName() == str(store.root / "settings.ini").replace("\\", "/"))
        _require(window.bridge is None)
        checks["main_window_render_and_settings"] = True
    finally:
        if window is not None:
            window.close()
            qt_app.processEvents()
        application.list_windows = old_list_windows

    old_codex = os.environ.get("ASL_AI_CODEX_PATH")
    try:
        os.environ["ASL_AI_CODEX_PATH"] = str(directory / "missing-codex.exe")
        _require(find_codex_executable() is None)
        checks["missing_external_codex"] = True
    finally:
        if old_codex is None:
            os.environ.pop("ASL_AI_CODEX_PATH", None)
        else:
            os.environ["ASL_AI_CODEX_PATH"] = old_codex

    try:
        raise RuntimeError("PRIVATE_SYNTHETIC_USER C:\\Users\\private\\secret.txt")
    except RuntimeError as error:
        diagnostic = _startup_diagnostic(error)
    _require("PRIVATE_SYNTHETIC_USER" not in diagnostic and "C:\\Users" not in diagnostic)
    _require("vasl_ai_bridge/packaged_self_test.py:" in diagnostic)
    checks["anonymous_relative_startup_stack"] = True

    if getattr(sys, "frozen", False):
        helper = str(speech_helper_path())
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for backend in ("whisper_cpp_native", "whisper_desktop"):
            result = subprocess.run([helper, "--backend", backend], input=b"{}", capture_output=True,
                                    timeout=20, creationflags=flags)
            response = json.loads(result.stdout)
            _require(result.returncode == 1 and isinstance(response.get("error"), str))
            checks[backend + "_helper_ipc"] = True


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", required=True)
    parser.add_argument("--self-test-report", type=Path, required=True)
    args = parser.parse_args(arguments)
    from . import __version__
    from .launcher import _error_category, _startup_diagnostic
    checks: dict[str, bool] = {}
    report = {"schema": 1, "version": __version__, "frozen": bool(getattr(sys, "frozen", False)),
              "passed": False, "checks": checks}
    try:
        args.self_test_report.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="synthetic-self-test-", dir=args.self_test_report.parent) as temporary:
            _run(checks, Path(temporary))
        report["passed"] = bool(checks) and all(checks.values())
    except Exception as error:
        # Public build reports never serialize exception text or local paths.
        report["error_category"] = _error_category(error)
        report["diagnostic"] = _startup_diagnostic(error)
    try:
        args.self_test_report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        return 1
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
