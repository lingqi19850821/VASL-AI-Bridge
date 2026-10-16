"""One small settings dialog; the owner applies and persists the chosen values."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QKeySequenceEdit, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from . import APP_NAME
from .preferences import AppPreferences
from .hotkey import native_shortcut
from .voice import VoiceConfig, VoiceError, resolve_voice_config


class SettingsDialog(QDialog):
    def __init__(self, preferences: AppPreferences, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(440)
        self.original = preferences
        layout = QVBoxLayout(self)

        capture = QGroupBox("Screenshot")
        capture_form = QFormLayout(capture)
        self.capture_kind_combo = QComboBox()
        for text, value in (("Disabled", ""), ("Keyboard shortcut", "keyboard"), ("Mouse side button 1", "Mouse4"),
                            ("Mouse side button 2", "Mouse5")):
            self.capture_kind_combo.addItem(text, value)
        capture_form.addRow("Capture with", self.capture_kind_combo)
        self.capture_editor = QKeySequenceEdit()
        self.capture_editor.setMaximumSequenceLength(1)
        capture_form.addRow("Keyboard shortcut", self.capture_editor)
        self.special_key_combo = QComboBox()
        self.special_key_combo.addItem("Custom keyboard shortcut", "")
        for key in ("Tab", "CapsLock", "Shift"):
            self.special_key_combo.addItem(key, key)
        capture_form.addRow("Use a single key", self.special_key_combo)
        self.capture_hint = QLabel(f"With {APP_NAME} in front, the shortcut works like Capture and select. With the selected board in front, it freezes the visible image before selection opens.")
        self.capture_hint.setWordWrap(True)
        capture_form.addRow(self.capture_hint)
        layout.addWidget(capture)

        reading = QGroupBox("Text and messages")
        form = QFormLayout(reading)
        self.chat_size_spin = QSpinBox()
        self.chat_size_spin.setRange(10, 28)
        self.chat_size_spin.setSuffix(" pt")
        self.chat_size_spin.setValue(preferences.chat_font_size)
        form.addRow("Chat text size", self.chat_size_spin)
        self.font_weight_combo = QComboBox()
        for text, value in (("Normal", "normal"), ("Bold", "bold")):
            self.font_weight_combo.addItem(text, value)
        self.font_weight_combo.setCurrentIndex(self.font_weight_combo.findData(preferences.chat_font_weight))
        form.addRow("Chat text weight", self.font_weight_combo)
        self.ui_size_spin = QSpinBox()
        self.ui_size_spin.setRange(9, 16)
        self.ui_size_spin.setSuffix(" pt")
        self.ui_size_spin.setValue(preferences.ui_font_size)
        form.addRow("Interface text size", self.ui_size_spin)
        self.send_mode_combo = QComboBox()
        for text, value in (("Enter", "enter"), ("Ctrl+Enter", "ctrl_enter"), ("Shift+Enter", "shift_enter")):
            self.send_mode_combo.addItem(text, value)
        self.send_mode_combo.setCurrentIndex(self.send_mode_combo.findData(preferences.send_mode))
        form.addRow("Send a message", self.send_mode_combo)
        self.send_hint = QLabel()
        self.send_hint.setWordWrap(True)
        form.addRow(self.send_hint)
        layout.addWidget(reading)

        voice = QGroupBox("Local voice input")
        voice_form = QFormLayout(voice)
        self._custom_voice_program = preferences.voice_program_path
        self._custom_voice_model = preferences.voice_model_path
        self._updating_voice_paths = False
        self.voice_backend_combo = QComboBox()
        for text, value in (("Automatic local engine (recommended)", "auto"), ("whisper.cpp (CPU)", "whisper_cpp"),
                            ("Whisper Desktop (existing installation)", "whisper_desktop")):
            self.voice_backend_combo.addItem(text, value)
        backend_index = self.voice_backend_combo.findData(preferences.voice_backend)
        self.voice_backend_combo.setCurrentIndex(max(0, backend_index))
        voice_form.addRow("Engine", self.voice_backend_combo)
        self.voice_program_edit = QLineEdit(preferences.voice_program_path)
        self.voice_program_edit.setPlaceholderText("Choose whisper-cli.exe")
        program_row = QHBoxLayout()
        program_row.addWidget(self.voice_program_edit, 1)
        self.voice_program_browse = QPushButton("Browse...")
        self.voice_program_browse.clicked.connect(self._browse_voice_program)
        program_row.addWidget(self.voice_program_browse)
        voice_form.addRow("Program", program_row)
        self.voice_model_edit = QLineEdit(preferences.voice_model_path)
        self.voice_model_edit.setPlaceholderText("Choose an existing GGML model file")
        model_row = QHBoxLayout()
        model_row.addWidget(self.voice_model_edit, 1)
        self.voice_model_file_browse = QPushButton("File...")
        self.voice_model_file_browse.clicked.connect(self._browse_voice_model_file)
        model_row.addWidget(self.voice_model_file_browse)
        voice_form.addRow("Model", model_row)
        self.voice_hint = QLabel()
        self.voice_hint.setWordWrap(True)
        self.voice_hint.setTextFormat(Qt.TextFormat.PlainText)
        voice_form.addRow(self.voice_hint)
        layout.addWidget(voice)
        self.error_label = QLabel()
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setStyleSheet("color: #9b302b;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._accept_preferences)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.credit_label = QLabel('Created by <a href="https://github.com/lingqi19850821">lingqi19850821</a>')
        self.credit_label.setTextFormat(Qt.TextFormat.RichText)
        self.credit_label.setOpenExternalLinks(True)
        layout.addWidget(self.credit_label)
        if preferences.capture_shortcut in ("Mouse4", "Mouse5"):
            self.capture_kind_combo.setCurrentIndex(self.capture_kind_combo.findData(preferences.capture_shortcut))
        elif preferences.capture_shortcut:
            self.capture_kind_combo.setCurrentIndex(self.capture_kind_combo.findData("keyboard"))
            self.capture_editor.setKeySequence(QKeySequence(preferences.capture_shortcut))
        self.capture_kind_combo.currentIndexChanged.connect(self._capture_kind_changed)
        self.special_key_combo.currentIndexChanged.connect(self._special_key_changed)
        self.send_mode_combo.currentIndexChanged.connect(self._send_mode_changed)
        self.voice_backend_combo.currentIndexChanged.connect(self._voice_engine_changed)
        self.voice_program_edit.textChanged.connect(self._voice_program_changed)
        self.voice_model_edit.textChanged.connect(self._voice_model_changed)
        self._capture_kind_changed()
        self._send_mode_changed()
        self._voice_engine_changed()

    def _capture_kind_changed(self, *args) -> None:
        keyboard = self.capture_kind_combo.currentData() == "keyboard"
        self.capture_editor.setEnabled(keyboard)
        self.special_key_combo.setEnabled(keyboard)

    def _special_key_changed(self, *args) -> None:
        key = self.special_key_combo.currentData()
        if key:
            self.capture_editor.setKeySequence(QKeySequence(key))

    def _send_mode_changed(self, *args) -> None:
        self.send_hint.setText("Shift+Enter adds a new line." if self.send_mode_combo.currentData() == "enter"
                               else "Enter adds a new line.")

    def _voice_engine_changed(self, *args) -> None:
        automatic = self.voice_backend_combo.currentData() == "auto"
        for control in (self.voice_program_edit, self.voice_program_browse, self.voice_model_edit,
                        self.voice_model_file_browse):
            control.setEnabled(not automatic)
        if automatic:
            try:
                resolved = resolve_voice_config(VoiceConfig())
                program, model = resolved.program_path, resolved.model_path
                text = "The paths shown are the actual automatically selected engine and model. Manual paths are retained separately. Spoken language is detected automatically. Speech becomes a draft for review before Send."
            except VoiceError as error:
                program = model = ""
                text = str(error)
        else:
            program, model = self._custom_voice_program, self._custom_voice_model
            if self.voice_backend_combo.currentData() == "whisper_desktop":
                text = "Choose your installed Desktop executable or enter its installation folder, and select the existing GGML model. The folder and all subfolders are searched for Whisper.dll, whose native interface is checked before recording. Spoken language is detected automatically. Speech becomes a draft for review before Send."
            else:
                text = "Choose whisper-cli.exe and its GGML model file. This CPU engine has passed a local real-model transcription check. Spoken language is detected automatically. Speech becomes a draft for review before Send."
        self._updating_voice_paths = True
        try:
            self.voice_program_edit.setText(program)
            self.voice_model_edit.setText(model)
        finally:
            self._updating_voice_paths = False
        self.voice_hint.setText(text)
        self.voice_program_edit.setPlaceholderText("Desktop executable or installation folder" if self.voice_backend_combo.currentData() == "whisper_desktop" else "Choose whisper-cli.exe")

    def _voice_program_changed(self, text: str) -> None:
        if not self._updating_voice_paths and self.voice_backend_combo.currentData() != "auto":
            self._custom_voice_program = text

    def _voice_model_changed(self, text: str) -> None:
        if not self._updating_voice_paths and self.voice_backend_combo.currentData() != "auto":
            self._custom_voice_model = text

    def preferences(self) -> AppPreferences:
        kind = self.capture_kind_combo.currentData()
        shortcut = (self.capture_editor.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                    if kind == "keyboard" else kind)
        return replace(self.original, capture_shortcut=shortcut, chat_font_size=self.chat_size_spin.value(),
                       chat_font_weight=self.font_weight_combo.currentData(), ui_font_size=self.ui_size_spin.value(),
                       send_mode=self.send_mode_combo.currentData(), voice_backend=self.voice_backend_combo.currentData(),
                       voice_program_path=self._custom_voice_program.strip(), voice_model_path=self._custom_voice_model.strip(),
                       voice_language="auto")

    def _browse_voice_program(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose local voice executable", self.voice_program_edit.text(),
                                            "Executables (*.exe);;All files (*)")
        if path:
            self.voice_program_edit.setText(path)

    def _browse_voice_model_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose local voice model", self.voice_model_edit.text(),
                                            "GGML models (*.bin);;All files (*)")
        if path:
            self.voice_model_edit.setText(path)

    def _accept_preferences(self) -> None:
        try:
            preferences = self.preferences()
            if self.capture_kind_combo.currentData() == "keyboard":
                native_shortcut(preferences.capture_shortcut)
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.accept()
