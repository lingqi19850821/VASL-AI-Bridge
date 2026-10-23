"""Windowed entry point with privacy-filtered, local startup diagnostics."""

from __future__ import annotations

import ctypes
from configparser import ConfigParser, Error as ConfigError
import importlib
import os
from pathlib import Path
import sys

from . import APP_NAME
from .translations_startup import TRANSLATIONS as _STARTUP_TRANSLATIONS

_PACKAGE_ROOT = Path(__file__).resolve().parent
# Only fixed application filenames may appear in a shareable diagnostic. An
# unknown file, even inside the package directory, gets the external marker.
_DIAGNOSTIC_FILES = frozenset({
    "__init__.py", "__main__.py", "app.py", "capture.py", "chat_session.py",
    "chat_storage.py", "chat_widgets.py", "codex_bridge.py",
    "desktop_installation.py", "frozen_runtime.py", "game_tools.py", "hotkey.py", "launcher.py",
    "preferences.py", "provider.py", "reasoning.py", "reference_formats.py",
    "reference_library.py", "reference_text.py", "screenshot_selection.py",
    "session.py", "settings_dialog.py", "storage.py", "voice.py",
    "voice_worker.py", "whisper_cpp_native_worker.py", "whisper_desktop_worker.py",
    "window_activation.py",
    "i18n.py", "translations_app.py", "translations_settings.py", "translations_widgets.py",
    "translations_errors.py", "translations_startup.py",
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


def _startup_data_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "ASL_AI" if local else Path.home() / ".asl_ai"


def _system_startup_language() -> str:
    """Read the Windows user's UI language without importing Qt or app modules."""
    if os.name != "nt":
        return "en"
    try:
        get_language = ctypes.windll.kernel32.GetUserDefaultUILanguage
        get_language.restype = ctypes.c_ushort
        language_id = get_language()
    except (AttributeError, OSError, TypeError, ValueError):
        return "en"
    return {
        0x0804: "zh_CN", 0x1004: "zh_CN",  # China, Singapore
        0x0404: "zh_TW", 0x0C04: "zh_TW", 0x1404: "zh_TW",  # Taiwan, Hong Kong, Macao
    }.get(language_id, "en")


def _startup_language() -> str:
    """Honor a saved UI choice even when Qt or application imports have failed."""
    try:
        settings = ConfigParser(interpolation=None)
        with (_startup_data_root() / "settings.ini").open(encoding="utf-8-sig") as stream:
            settings.read_file(stream)
        if settings.has_option("General", "ui_language"):
            language = settings.get("General", "ui_language")
            return language if language in {"en", "zh_CN", "zh_TW"} else "en"
    except FileNotFoundError:
        pass
    except (ConfigError, OSError, RuntimeError, UnicodeError, ValueError):
        return "en"
    return _system_startup_language()


def _startup_text(source: str, language: str, **values: str) -> str:
    pair = _STARTUP_TRANSLATIONS.get(source)
    translated = pair[0 if language == "zh_CN" else 1] if pair and language in {"zh_CN", "zh_TW"} else source
    return translated.format(**values)


def _show_error(message: str, *, language: str | None = None) -> None:
    if os.name == "nt":
        title = _startup_text("{app_name} startup error", language or _startup_language(), app_name=APP_NAME)
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    elif sys.stderr is not None:
        sys.stderr.write(message + "\n")


def _report_startup_error(error: Exception) -> None:
    language = _startup_language()
    local = os.environ.get("LOCALAPPDATA")
    display_path = r"%LOCALAPPDATA%\ASL_AI\startup-error.log" if local else "~/.asl_ai/startup-error.log"
    try:
        root = _startup_data_root()
        log_path = root / "startup-error.log"
        root.mkdir(parents=True, exist_ok=True)
        log_path.write_text(_startup_diagnostic(error), encoding="utf-8")
        details = _startup_text("A privacy-filtered diagnostic was saved locally to:\n{path}", language, path=display_path)
        details += "\n\n" + _startup_text("You can share the diagnostic manually when reporting the problem.", language)
    except (OSError, RuntimeError, ValueError):
        details = _startup_text("The startup diagnostic could not be saved.", language)
    _show_error(
        _startup_text("{app_name} could not start.", language, app_name=APP_NAME) + "\n\n"
        + _startup_text("Error category: {category}", language, category=_error_category(error))
        + "\n\n"
        + details,
        language=language,
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
