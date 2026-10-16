"""Offline model-catalog and nonpersistent selection contracts."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from vasl_ai_bridge.preferences import AppPreferences, load_preferences, save_preferences
from vasl_ai_bridge.reasoning import (
    default_reasoning_effort, model_identifier, reasoning_effort_description,
    reasoning_effort_label, supported_reasoning_efforts,
)
from vasl_ai_bridge.settings_dialog import SettingsDialog
from vasl_ai_bridge.voice import VoiceError


APP = QApplication.instance() or QApplication(["ASL_AI offline reasoning tests"])


class ReasoningCatalogTests(unittest.TestCase):
    def test_runtime_catalog_exposes_ultra_only_when_advertised_and_keeps_default(self):
        model = {"model": "gpt-6-astra", "defaultReasoningEffort": "low",
                 "supportedReasoningEfforts": [
                     {"reasoningEffort": "ultra", "description": "More reasoning work"},
                     {"reasoningEffort": "low"}, {"reasoningEffort": "max"},
                     {"reasoningEffort": "low"},
                 ]}
        self.assertEqual(model_identifier(model), "gpt-6-astra")
        self.assertEqual(supported_reasoning_efforts(model), ("low", "max", "ultra"))
        self.assertEqual(default_reasoning_effort(model), "low")
        self.assertEqual(reasoning_effort_description(model, "ultra"), "More reasoning work")
        other = {"id": "model-with-max", "defaultReasoningEffort": "medium",
                 "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "max"}]}
        self.assertEqual(supported_reasoning_efforts(other), ("medium", "max"))
        self.assertNotIn("ultra", supported_reasoning_efforts(other))
        self.assertEqual(default_reasoning_effort(other), "medium")

    def test_cached_and_compatibility_catalog_fields_use_the_same_capabilities(self):
        cached = {"slug": "cached-model", "default_reasoning_level": "high",
                  "supported_reasoning_levels": [{"effort": "high"}, {"effort": "ultra"}]}
        compatible = {"id": "cached-model", "default_reasoning_effort": "high",
                      "supported_reasoning_efforts": [{"reasoning_effort": "high"}, {"reasoning_effort": "ultra"}]}
        for model in (cached, compatible):
            self.assertEqual(model_identifier(model), "cached-model")
            self.assertEqual(supported_reasoning_efforts(model), ("high", "ultra"))
            self.assertEqual(default_reasoning_effort(model), "high")

    def test_malformed_and_missing_catalogs_never_invent_support_or_default(self):
        for model in ({}, {"supportedReasoningEfforts": "ultra", "defaultReasoningEffort": "ultra"},
                      {"supportedReasoningEfforts": [None, {}, {"reasoningEffort": 1}, "unknown"]}):
            self.assertEqual(supported_reasoning_efforts(model), ())
            self.assertEqual(default_reasoning_effort(model), "")
        model = {"supportedReasoningEfforts": ["low"], "defaultReasoningEffort": "ultra"}
        self.assertEqual(supported_reasoning_efforts(model), ("low",))
        self.assertEqual(default_reasoning_effort(model), "")
        self.assertEqual(model_identifier({"model": "bad\nmodel", "id": "valid-model"}), "valid-model")
        self.assertEqual(model_identifier({"model": "", "id": 3}), "")

    def test_labels_distinguish_model_default_from_explicit_no_reasoning(self):
        self.assertEqual(reasoning_effort_label(""), "Model default")
        self.assertEqual(reasoning_effort_label("none"), "None")
        self.assertEqual(reasoning_effort_label("ultra"), "Ultra")
        self.assertEqual(reasoning_effort_label("max"), "Max")


class ModelSelectionPersistenceTests(unittest.TestCase):
    def settings_directory(self):
        root = Path(__file__).resolve().parents[1] / "runtime" / "reasoning-tests"
        root.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=root)

    def test_settings_roundtrip_preserves_fonts_and_voice_without_storing_model_choices(self):
        with self.settings_directory() as temporary:
            path = str(Path(temporary) / "settings.ini")
            writer = QSettings(path, QSettings.Format.IniFormat)
            writer.setValue("unrelated", "keep")
            expected = AppPreferences(chat_font_size=18, ui_font_size=12, send_mode="ctrl_enter",
                                      voice_backend="whisper_desktop", voice_program_path="C:/voice/desktop.exe",
                                      voice_model_path="D:/models/large-v3.bin")
            save_preferences(writer, expected)
            reader = QSettings(path, QSettings.Format.IniFormat)
            self.assertEqual(load_preferences(reader), expected)
            self.assertEqual(reader.value("unrelated"), "keep")
            self.assertFalse(reader.contains("preferred_model"))
            self.assertFalse(reader.contains("reasoning_efforts_by_model"))
            reader = writer = None

    def test_unknown_model_selection_keys_do_not_affect_loaded_preferences(self):
        with self.settings_directory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.Format.IniFormat)
            expected = AppPreferences(chat_font_size=18, voice_model_path="D:/models/local.bin")
            save_preferences(settings, expected)
            for model, efforts in (("gpt-6-astra", '{"gpt-6-astra":"ultra"}'),
                                   ("bad\nmodel", "{broken")):
                with self.subTest(model=model, efforts=efforts):
                    settings.setValue("preferred_model", model)
                    settings.setValue("reasoning_efforts_by_model", efforts)
                    loaded = load_preferences(settings)
                    self.assertEqual(loaded, expected)
                    self.assertFalse(hasattr(loaded, "preferred_model"))
                    self.assertFalse(hasattr(loaded, "reasoning_efforts_by_model"))
            settings = None

    def test_settings_dialog_edits_fonts_and_preserves_voice_without_model_fields(self):
        original = AppPreferences(voice_program_path="C:/voice/desktop.exe", voice_model_path="D:/models/local.bin")
        with patch("vasl_ai_bridge.settings_dialog.resolve_voice_config", side_effect=VoiceError("No local engine")):
            dialog = SettingsDialog(original)
        try:
            dialog.chat_size_spin.setValue(18)
            result = dialog.preferences()
            self.assertEqual(result.chat_font_size, 18)
            self.assertEqual(result.voice_program_path, original.voice_program_path)
            self.assertEqual(result.voice_model_path, original.voice_model_path)
            self.assertFalse(hasattr(result, "preferred_model"))
            self.assertFalse(hasattr(result, "reasoning_efforts_by_model"))
        finally:
            dialog.close()
            dialog.deleteLater()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
