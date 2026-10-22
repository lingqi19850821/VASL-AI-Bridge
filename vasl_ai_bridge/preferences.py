"""Small, local preferences shared by the desktop widgets."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QKeySequence

from .i18n import UI_LANGUAGES, system_language

SEND_MODES = ("enter", "ctrl_enter", "shift_enter")
FONT_WEIGHTS = ("normal", "bold")
VOICE_BACKENDS = ("auto", "whisper", "faster_whisper", "whisper_cpp", "whisper_desktop")
VOICE_LANGUAGES = ("auto", "zh", "en")


@dataclass(frozen=True)
class AppPreferences:
    capture_shortcut: str = ""
    chat_font_size: int = 14
    chat_font_weight: str = "normal"
    ui_font_size: int = 11
    send_mode: str = "enter"
    voice_backend: str = "auto"
    voice_program_path: str = ""
    voice_model_path: str = ""
    voice_language: str = "auto"
    ui_language: str = "en"

    def __post_init__(self) -> None:
        if not isinstance(self.capture_shortcut, str) or (self.capture_shortcut and not self.capture_shortcut.strip()):
            raise ValueError("Choose a screenshot shortcut.")
        for name, minimum, maximum in (("chat_font_size", 10, 28), ("ui_font_size", 9, 16)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name.replace('_', ' ').capitalize()} must be between {minimum} and {maximum}.")
        if self.chat_font_weight not in FONT_WEIGHTS:
            raise ValueError("Choose a supported chat font weight.")
        if self.send_mode not in SEND_MODES:
            raise ValueError("Choose a supported message send shortcut.")
        if self.voice_backend not in VOICE_BACKENDS:
            raise ValueError("Choose a supported local voice engine.")
        if self.voice_language not in VOICE_LANGUAGES:
            raise ValueError("Choose a supported voice language.")
        if not isinstance(self.voice_program_path, str) or not isinstance(self.voice_model_path, str):
            raise ValueError("Choose local voice program and model paths.")
        if self.ui_language not in UI_LANGUAGES:
            raise ValueError("Choose a supported interface language.")


def load_preferences(settings: QSettings) -> AppPreferences:
    """Use conservative defaults for individual malformed or older INI values."""
    defaults = AppPreferences()
    capture = str(settings.value("capture_shortcut", defaults.capture_shortcut))
    if not capture.strip():
        capture = defaults.capture_shortcut
    sequence = QKeySequence(capture)
    if sequence.count() == 1 and int(Qt.Key.Key_F1) <= int(sequence[0].key()) <= int(Qt.Key.Key_F24):
        capture = ""
    sizes = {}
    for field, minimum, maximum in (("chat_font_size", 10, 28), ("ui_font_size", 9, 16)):
        try:
            value = int(settings.value(field, getattr(defaults, field)))
        except (TypeError, ValueError, OverflowError):
            value = getattr(defaults, field)
        sizes[field] = value if minimum <= value <= maximum else getattr(defaults, field)
    # YaHei has real Regular and Bold faces. Older "medium" choices used a
    # synthesized or nearest face, so migrate that preference to Regular.
    weight = str(settings.value("chat_font_weight", defaults.chat_font_weight))
    if weight == "medium":
        weight = "normal"
    mode = str(settings.value("send_mode", defaults.send_mode))
    backend = str(settings.value("voice_backend", defaults.voice_backend))
    language = str(settings.value("ui_language")) if settings.contains("ui_language") else system_language()
    return AppPreferences(capture, sizes["chat_font_size"],
                          weight if weight in FONT_WEIGHTS else defaults.chat_font_weight,
                          sizes["ui_font_size"], mode if mode in SEND_MODES else defaults.send_mode,
                          backend if backend in VOICE_BACKENDS else defaults.voice_backend,
                          str(settings.value("voice_program_path", "")),
                          str(settings.value("voice_model_path", "")),
                          "auto", language if language in UI_LANGUAGES else "en")


def save_preferences(settings: QSettings, preferences: AppPreferences) -> None:
    """Write only this application's known keys and report a failed local save."""
    for name in ("capture_shortcut", "chat_font_size", "chat_font_weight", "ui_font_size", "send_mode",
                 "voice_backend", "voice_program_path", "voice_model_path", "voice_language", "ui_language"):
        settings.setValue(name, getattr(preferences, name))
    settings.sync()
    if settings.status() != QSettings.Status.NoError:
        raise OSError("The settings could not be saved. Check that the local data folder is writable.")
