"""Windowed entry point with privacy-filtered, local startup diagnostics."""

from __future__ import annotations

import ctypes
import importlib
import os
from pathlib import Path
import sys

from . import APP_NAME

_PACKAGE_ROOT = Path(__file__).resolve().parent
# Only fixed application filenames may appear in a shareable diagnostic. An
# unknown file, even inside the package directory, gets the external marker.
_DIAGNOSTIC_FILES = frozenset({
    "__init__.py", "__main__.py", "app.py", "capture.py", "chat_session.py",
    "chat_storage.py", "chat_widgets.py", "codex_bridge.py",
    "desktop_installation.py", "frozen_runtime.py", "game_tools.py", "hotkey.py", "launcher.py",
    "packaged_self_test.py",
    "preferences.py", "provider.py", "reasoning.py", "reference_formats.py",
    "reference_library.py", "reference_text.py", "screenshot_selection.py",
    "session.py", "settings_dialog.py", "storage.py", "voice.py",
    "voice_worker.py", "whisper_cpp_native_worker.py", "whisper_desktop_worker.py",
    "window_activation.py",
})
_ERROR_CATEGORIES = (
    (ImportError, "ImportError"), (ModuleNotFoundError, "ModuleNotFoundError"),
    (RuntimeError, "RuntimeError"), (ValueError, "ValueError"),
    (TypeError, "TypeError"), (AttributeError, "AttributeError"),
    (KeyError, "KeyError"), (IndexError, "IndexError"),
    (OSError, "OSError"), (FileNotFoundError, "FileNotFoundError"),
    (PermissionError, "PermissionError"), (NotADirectoryError, "NotADirectoryError"),
    (IsADirectoryError, "IsADirectoryError"), (FileExistsError, "FileExistsError"),
    (TimeoutError, "TimeoutError"), (ConnectionError, "ConnectionError"),
    (ConnectionRefusedError, "ConnectionRefusedError"),
    (ConnectionResetError, "ConnectionResetError"), (BrokenPipeError, "BrokenPipeError"),
    (SyntaxError, "SyntaxError"), (IndentationError, "IndentationError"),
    (UnicodeError, "UnicodeError"), (UnicodeDecodeError, "UnicodeDecodeError"),
    (UnicodeEncodeError, "UnicodeEncodeError"), (MemoryError, "MemoryError"),
    (RecursionError, "RecursionError"), (AssertionError, "AssertionError"),
    (NotImplementedError, "NotImplementedError"), (Exception, "Exception"),
)
_MAX_ERRORS = 8
_MAX_FRAMES = 128


def _error_category(error: BaseException) -> str:
    # Custom exception class names can contain user data too. Never serialize
    # their names, messages, args, notes, source text, or local variables.
    for error_type, label in _ERROR_CATEGORIES:
        if type(error) is error_type:
            return label
    return "Exception"


def _frame_location(filename: str, line: int) -> str:
    # Accept only known package-relative filenames from frozen tracebacks.
    if getattr(sys, "frozen", False):
        normalized = filename.replace("\\", "/")
        if normalized.startswith("vasl_ai_bridge/") and normalized.removeprefix("vasl_ai_bridge/") in _DIAGNOSTIC_FILES:
            return f"{normalized}:{line}"
    try:
        # Reject unknown paths without touching their filesystem locations (a
        # traceback filename can even name a network share). Resolve only a
        # recognized application source candidate, to check symlink containment.
        candidate = Path(os.path.abspath(filename))
        relative = candidate.relative_to(_PACKAGE_ROOT).as_posix()
        if relative not in _DIAGNOSTIC_FILES:
            return "<external>"
        if candidate.resolve().relative_to(_PACKAGE_ROOT).as_posix() != relative:
            return "<external>"
    except (OSError, RuntimeError, ValueError):
        return "<external>"
    return f"vasl_ai_bridge/{relative}:{line}"


def _startup_diagnostic(error: BaseException) -> str:
    """Build diagnostics from allowed fields, without formatting raw traceback."""
    lines = [f"{APP_NAME} startup diagnostic (privacy-filtered)"]
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < _MAX_ERRORS:
        seen.add(id(current))
        lines.append("Error category: " + _error_category(current))
        lines.append("Stack (outermost first):")
        frame = current.__traceback__
        count = 0
        while frame is not None and count < _MAX_FRAMES:
            lines.append("  " + _frame_location(frame.tb_frame.f_code.co_filename, frame.tb_lineno))
            frame = frame.tb_next
            count += 1
        if count == 0:
            lines.append("  <unavailable>")
        if frame is not None:
            lines.append("  <additional frames omitted>")
        if current.__cause__ is not None:
            current = current.__cause__
            relation = "Caused by:"
        elif not current.__suppress_context__ and current.__context__ is not None:
            current = current.__context__
            relation = "During handling of:"
        else:
            current = None
        if current is not None:
            lines.append(relation)
    if current is not None:
        lines.append("<additional errors omitted>")
    return "\n".join(lines) + "\n"


def _show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, f"{APP_NAME} startup error", 0x10)
    elif sys.stderr is not None:
        sys.stderr.write(message + "\n")


def _report_startup_error(error: Exception) -> None:
    local = os.environ.get("LOCALAPPDATA")
    display_path = r"%LOCALAPPDATA%\ASL_AI\startup-error.log" if local else "~/.asl_ai/startup-error.log"
    try:
        root = Path(local) / "ASL_AI" if local else Path.home() / ".asl_ai"
        log_path = root / "startup-error.log"
        root.mkdir(parents=True, exist_ok=True)
        log_path.write_text(_startup_diagnostic(error), encoding="utf-8")
        details = "A privacy-filtered diagnostic was saved locally to:\n" + display_path
        details += "\n\nYou can share the diagnostic manually when reporting the problem."
    except (OSError, RuntimeError, ValueError):
        details = "The startup diagnostic could not be saved."
    _show_error(
        f"{APP_NAME} could not start.\n\n"
        + "Error category: " + _error_category(error)
        + "\n\n"
        + details
    )


def main() -> int:
    try:
        application = importlib.import_module(".app", package=__package__)
        result = application.main()
        if result:
            raise RuntimeError(f"{APP_NAME} exited with code {result}.")
        return 0
    except Exception as error:
        _report_startup_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
