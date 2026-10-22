"""Isolated settings, message readability and keyboard/IME behavior checks."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QFont, QInputMethodEvent, QKeyEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QScrollArea, QVBoxLayout, QWidget

from PIL import Image

from vasl_ai_bridge.capture import prepare_image
from vasl_ai_bridge.chat_session import ChatMessage
from vasl_ai_bridge.chat_widgets import ChatInput, MessageBubble, chat_font, preferred_font_family, ui_font
from vasl_ai_bridge.i18n import get_language, set_language, tr
from vasl_ai_bridge.preferences import AppPreferences, load_preferences, save_preferences
from vasl_ai_bridge.settings_dialog import SettingsDialog
from vasl_ai_bridge.voice import VoiceConfig, VoiceError


APP = QApplication.instance() or QApplication(["ASL_AI isolated desktop preference tests"])


class PreferenceTests(unittest.TestCase):
    def test_interface_language_roundtrip_preserves_other_preferences(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "settings.ini")
            for language in ("en", "zh_CN", "zh_TW"):
                with self.subTest(language=language):
                    writer = QSettings(path, QSettings.Format.IniFormat)
                    expected = AppPreferences(voice_program_path="C:/Local Voice/whisper-cli.exe",
                                              voice_model_path="D:/Models/ggml-small.bin", ui_language=language)
                    save_preferences(writer, expected)
                    reader = QSettings(path, QSettings.Format.IniFormat)
                    with patch("vasl_ai_bridge.preferences.system_language", side_effect=AssertionError("Saved choice must win")):
                        self.assertEqual(load_preferences(reader), expected)
                    self.assertEqual(reader.value("ui_language"), language)
                    writer, reader = None, None

    def test_first_launch_uses_system_language_without_writing_a_manual_choice(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.Format.IniFormat)
            for language in ("en", "zh_CN", "zh_TW"):
                with self.subTest(language=language), patch("vasl_ai_bridge.preferences.system_language", return_value=language):
                    self.assertEqual(load_preferences(settings).ui_language, language)
                    self.assertFalse(settings.contains("ui_language"))
            settings = None

    def test_invalid_saved_interface_language_falls_back_to_english(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("voice_model_path", "D:/Models/keep.bin")
            for value in ("", "invalid", "zh", False):
                with self.subTest(value=value):
                    settings.setValue("ui_language", value)
                    with patch("vasl_ai_bridge.preferences.system_language", side_effect=AssertionError("Invalid choice has an explicit fallback")):
                        loaded = load_preferences(settings)
                    self.assertEqual(loaded.ui_language, "en")
                    self.assertEqual(loaded.voice_model_path, "D:/Models/keep.bin")
                    self.assertEqual(loaded.voice_language, "auto")
            settings = None

    def test_preferences_survive_a_new_settings_reader_and_keep_other_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = str(Path(temporary) / "settings.ini")
            settings = QSettings(path, QSettings.Format.IniFormat)
            settings.setValue("unrelated", "keep")
            expected = AppPreferences("Mouse4", 18, "bold", 12, "ctrl_enter", "whisper_cpp",
                                      "C:/Local Voice/whisper-cli.exe", "D:/Models/ggml-small.bin", "auto")
            save_preferences(settings, expected)
            reader = QSettings(path, QSettings.Format.IniFormat)
            self.assertEqual(load_preferences(reader), expected)
            self.assertEqual(reader.value("unrelated"), "keep")
            reader, settings = None, None

    def test_bad_values_fall_back_individually_without_losing_valid_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.Format.IniFormat)
            for key, value in {"capture_shortcut": "Tab", "chat_font_size": "bad", "ui_font_size": 0,
                               "send_mode": "unknown", "chat_font_weight": "thin", "voice_backend": "cloud",
                               "voice_language": "unsupported", "voice_model_path": "D:/saved/model.pt"}.items():
                settings.setValue(key, value)
            actual = load_preferences(settings)
            self.assertEqual(actual.capture_shortcut, "Tab")
            self.assertEqual(actual.chat_font_size, 14)
            self.assertEqual(actual.ui_font_size, 11)
            self.assertEqual(actual.send_mode, "enter")
            self.assertEqual(actual.chat_font_weight, AppPreferences().chat_font_weight)
            self.assertEqual(actual.voice_backend, "auto")
            self.assertEqual(actual.voice_language, "auto")
            self.assertEqual(actual.voice_model_path, "D:/saved/model.pt")
            settings = None

    def test_legacy_medium_migrates_to_normal_without_losing_other_preferences(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.Format.IniFormat)
            values = {"chat_font_weight": "medium", "chat_font_size": 18, "capture_shortcut": "Mouse5",
                      "send_mode": "ctrl_enter", "voice_backend": "whisper_cpp",
                      "voice_program_path": "C:/voice/whisper-cli.exe", "voice_model_path": "D:/model.bin",
                      "unrelated": "keep"}
            for key, value in values.items():
                settings.setValue(key, value)
            loaded = load_preferences(settings)
            self.assertEqual(loaded.chat_font_weight, "normal")
            self.assertEqual(loaded.chat_font_size, 18)
            self.assertEqual(loaded.capture_shortcut, "Mouse5")
            self.assertEqual(loaded.send_mode, "ctrl_enter")
            self.assertEqual(loaded.voice_backend, "whisper_cpp")
            self.assertEqual(loaded.voice_program_path, "C:/voice/whisper-cli.exe")
            self.assertEqual(loaded.voice_model_path, "D:/model.bin")
            save_preferences(settings, loaded)
            self.assertEqual(settings.value("chat_font_weight"), "normal")
            self.assertEqual(settings.value("unrelated"), "keep")
            settings = None

    def test_function_bindings_migrate_to_disabled_and_tab_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.Format.IniFormat)
            self.assertEqual(load_preferences(settings).capture_shortcut, "")
            for number in range(1, 25):
                for prefix in ("", "Ctrl+Alt+"):
                    settings.setValue("capture_shortcut", prefix + f"F{number}")
                    self.assertEqual(load_preferences(settings).capture_shortcut, "")
            settings.setValue("capture_shortcut", "Tab")
            self.assertEqual(load_preferences(settings).capture_shortcut, "Tab")
            settings = None

    def test_old_language_hint_migrates_to_automatic_without_changing_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("voice_backend", "whisper_desktop")
            settings.setValue("voice_program_path", "D:/Desktop/renamed.exe")
            settings.setValue("voice_model_path", "D:/Desktop/large-v3.bin")
            for language in ("zh", "en"):
                settings.setValue("voice_language", language)
                loaded = load_preferences(settings)
                self.assertEqual(loaded.voice_language, "auto")
                self.assertEqual(loaded.voice_program_path, "D:/Desktop/renamed.exe")
                self.assertEqual(loaded.voice_model_path, "D:/Desktop/large-v3.bin")
            settings = None

    def test_preference_boundaries_reject_values_that_cannot_be_applied(self):
        for kwargs in ({"capture_shortcut": " "}, {"chat_font_size": 9}, {"chat_font_size": 29},
                       {"chat_font_size": True}, {"ui_font_size": 8}, {"chat_font_weight": "light"},
                       {"send_mode": "escape"}, {"voice_backend": "online"}, {"voice_language": "invalid"},
                        {"voice_model_path": None}, {"ui_language": "zh"}, {"ui_language": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                AppPreferences(**kwargs)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.input = ChatInput()
        self.sent = []
        self.input.sendRequested.connect(lambda: self.sent.append(self.input.toPlainText()))

    def tearDown(self):
        self.input.close()
        self.input.deleteLater()
        APP.processEvents()

    def press(self, key=Qt.Key.Key_Return, modifiers=Qt.KeyboardModifier.NoModifier):
        QTest.keyClick(self.input, key, modifiers)

    def test_default_enter_sends_and_shift_enter_inserts_a_line(self):
        self.input.setPlainText("Check LOS")
        self.press()
        self.assertEqual(self.sent, ["Check LOS"])
        self.assertEqual(self.input.toPlainText(), "Check LOS")
        self.input.moveCursor(self.input.textCursor().MoveOperation.End)
        self.press(modifiers=Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.input.toPlainText(), "Check LOS\n")
        self.assertEqual(len(self.sent), 1)

    def test_ctrl_enter_and_shift_enter_modes_keep_plain_enter_for_new_lines(self):
        for mode, modifier in (("ctrl_enter", Qt.KeyboardModifier.ControlModifier),
                               ("shift_enter", Qt.KeyboardModifier.ShiftModifier)):
            with self.subTest(mode=mode):
                self.sent.clear()
                self.input.set_send_mode(mode)
                self.input.setPlainText("Next move")
                self.input.moveCursor(self.input.textCursor().MoveOperation.End)
                self.press()
                self.assertEqual(self.input.toPlainText(), "Next move\n")
                self.assertEqual(self.sent, [])
                self.press(modifiers=modifier)
                self.assertEqual(self.sent, ["Next move\n"])

    def test_keypad_enter_uses_the_same_send_preference(self):
        self.input.set_send_mode("ctrl_enter")
        self.press(Qt.Key.Key_Enter, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier)
        self.assertEqual(len(self.sent), 1)

    def test_held_send_key_does_not_repeat_requests(self):
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r", True)
        QApplication.sendEvent(self.input, event)
        self.assertEqual(self.sent, [])

    def test_enter_during_ime_composition_and_commit_does_not_send(self):
        QApplication.sendEvent(self.input, QInputMethodEvent("zhong", []))
        self.press()
        self.assertEqual(self.sent, [])
        commit = QInputMethodEvent("", [])
        commit.setCommitString("中文")
        QApplication.sendEvent(self.input, commit)
        self.press()
        self.assertEqual(self.sent, [])
        APP.processEvents()
        self.press()
        self.assertEqual(len(self.sent), 1)

    def test_input_font_changes_keep_the_existing_draft(self):
        self.input.setPlainText("A draft worth keeping")
        self.input.apply_preferences(AppPreferences(chat_font_size=18, chat_font_weight="bold", send_mode="ctrl_enter"))
        self.assertEqual(self.input.font().pointSize(), 18)
        self.assertEqual(self.input.font().weight(), QFont.Weight.Bold)
        self.assertEqual(self.input.toPlainText(), "A draft worth keeping")
        self.assertEqual(self.input.send_mode, "ctrl_enter")


class BubbleTests(unittest.TestCase):
    def test_chat_input_body_header_and_interface_use_one_installed_cjk_family(self):
        with patch("vasl_ai_bridge.chat_widgets.QFontDatabase.families", return_value=["Segoe UI", "Microsoft YaHei UI", "Noto Sans SC"]):
            preferences = AppPreferences(chat_font_size=18, ui_font_size=12, chat_font_weight="bold")
            self.assertEqual(preferred_font_family(QFont("Segoe UI")), "Microsoft YaHei UI")
            input_widget = ChatInput()
            input_widget.apply_preferences(preferences)
            bubble = MessageBubble(ChatMessage("font", "assistant", "CJK font selection"), preferences=preferences)
            interface = ui_font(preferences, QFont("Segoe UI"))
            self.assertEqual(input_widget.font().family(), "Microsoft YaHei UI")
            self.assertEqual(bubble.text.font().family(), "Microsoft YaHei UI")
            self.assertEqual(bubble.heading.font().family(), "Microsoft YaHei UI")
            self.assertEqual(interface.family(), "Microsoft YaHei UI")
            self.assertEqual(interface.pointSize(), 12)
            self.assertEqual(interface.weight(), QFont.Weight.Normal)
            self.assertFalse(input_widget.font().styleStrategy() & QFont.StyleStrategy.NoFontMerging)
            self.assertFalse(bubble.text.font().styleStrategy() & QFont.StyleStrategy.NoFontMerging)
            input_widget.deleteLater()
            bubble.deleteLater()
            APP.processEvents()

    def test_font_selection_falls_back_to_available_cjk_then_the_existing_family(self):
        with patch("vasl_ai_bridge.chat_widgets.QFontDatabase.families", return_value=["Segoe UI", "Noto Sans SC"]):
            self.assertEqual(preferred_font_family(QFont("Segoe UI")), "Noto Sans SC")
        with patch("vasl_ai_bridge.chat_widgets.QFontDatabase.families", return_value=["Segoe UI"]):
            self.assertEqual(chat_font(AppPreferences(), QFont("Segoe UI")).family(), "Segoe UI")
    def settle_geometry(self):
        for _ in range(12):
            APP.processEvents()
            QTest.qWait(2)

    def assert_bubble_contents_fit(self, bubble):
        text = bubble.text
        self.assertGreaterEqual(text.height(), text.heightForWidth(text.width()))
        self.assertTrue(bubble.body.rect().contains(text.geometry()), (bubble.body.size(), text.geometry()))
        for button in bubble.attachment_buttons:
            self.assertGreaterEqual(button.height(), button.iconSize().height())
            self.assertGreaterEqual(button.height(), button.sizeHint().height())
            self.assertTrue(bubble.body.rect().contains(button.geometry()))
        children = [bubble.heading, text] + bubble.attachment_buttons
        for first, second in zip(children, children[1:]):
            self.assertLess(first.geometry().bottom(), second.geometry().top())

    def test_wrapping_and_screenshot_geometry_survive_narrow_and_wide_scroll_viewports(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        image = prepare_image(Image.new("RGB", (300, 180), "white"), "Synthetic board")
        user = MessageBubble(ChatMessage("user", "user", "Here is the board. 请检查棋盘截图和这次移动。 " * 4,
                                         attachments=[image]), content, inspect_callback=lambda *_: None)
        ai = MessageBubble(ChatMessage("ai", "assistant", ("A long tactical reply that must wrap completely. " * 12)
                                       + "\n\n" + ("第二段需要完整显示，不能被下方的消息遮住。" * 12)), content)
        final = MessageBubble(ChatMessage("final", "assistant", "A short final paragraph."), content)
        for bubble in (user, ai, final):
            layout.addWidget(bubble)
        scroll.setWidget(content)
        scroll.resize(500, 300)
        scroll.show()
        try:
            heights = []
            for width in (500, 800, 500):
                scroll.resize(width, 300)
                self.settle_geometry()
                with self.subTest(width=width):
                    self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                    for bubble in (user, ai, final):
                        self.assert_bubble_contents_fit(bubble)
                    self.assertGreater(user.body.width(), user.width() * .65)
                    self.assertGreater(ai.body.width(), ai.width() * .85)
                    self.assertGreater(final.body.width(), final.width() * .85)
                    self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
                    heights.append(ai.height())
            self.assertGreater(heights[0], heights[1])
            self.assertEqual(heights[0], heights[2])
            ai.apply_preferences(AppPreferences(chat_font_size=22, chat_font_weight="bold"))
            self.settle_geometry()
            self.assert_bubble_contents_fit(ai)
            self.assertGreater(ai.height(), heights[2])
        finally:
            scroll.close()
            scroll.deleteLater()
            APP.processEvents()

    def test_streaming_text_growth_recalculates_height_without_clipping(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        message = ChatMessage("stream", "assistant", "Thinking...", status="streaming")
        bubble = MessageBubble(message, content)
        layout.addWidget(bubble)
        scroll.setWidget(content)
        scroll.resize(500, 160)
        scroll.show()
        try:
            self.settle_geometry()
            original_height = bubble.height()
            message.text = "A streamed paragraph that needs room to wrap and remain readable. " * 20
            bubble.update_message(message)
            self.settle_geometry()
            self.assertGreater(bubble.height(), original_height)
            self.assert_bubble_contents_fit(bubble)
            self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        finally:
            scroll.close()
            scroll.deleteLater()
            APP.processEvents()

    def test_short_messages_and_save_alias_remain_compact_after_layout_settles(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        image = prepare_image(Image.new("RGB", (640, 420), "white"), "Synthetic board")
        user = MessageBubble(ChatMessage("user", "user", "Here is the starting board. Tell me your first move.",
                                         attachments=[image]), content, inspect_callback=lambda *_: None)
        ai = MessageBubble(ChatMessage("ai", "assistant", "Move Unit A to C2. This response only tests the chat interface."), content)
        save = MessageBubble(ChatMessage("save", "user", "Long internal save instructions. " * 30), content,
                             display_text=lambda _: "Prepare a game save report for me to review.")
        for bubble in (user, ai, save):
            layout.addWidget(bubble)
        scroll.setWidget(content)
        scroll.resize(800, 500)
        scroll.show()
        try:
            self.settle_geometry()
            for bubble in (user, ai, save):
                self.assert_bubble_contents_fit(bubble)
            self.assertLess(user.height(), 210)
            self.assertLess(ai.height(), 115)
            self.assertLess(save.height(), 115)
            self.assertLess(user.text.height(), user.text.fontMetrics().height() * 3)
            self.assertEqual(save.text.text(), "Prepare a game save report for me to review.")
        finally:
            scroll.close()
            scroll.deleteLater()
            APP.processEvents()

    def test_user_and_ai_have_distinct_alignment_and_colors(self):
        user = MessageBubble(ChatMessage("user-1", "user", "My move"))
        ai = MessageBubble(ChatMessage("ai-1", "assistant", "I will move next"))
        user.resize(600, 100)
        ai.resize(600, 100)
        self.assertEqual(user.row_layout.stretch(0), 1)
        self.assertEqual(user.row_layout.stretch(2), 0)
        self.assertEqual(ai.row_layout.stretch(0), 0)
        self.assertEqual(ai.row_layout.stretch(2), 1)
        self.assertNotEqual(user.body.styleSheet(), ai.body.styleSheet())
        self.assertLess(user.body.maximumWidth(), ai.body.maximumWidth())
        user.deleteLater()
        ai.deleteLater()

    def test_model_html_and_remote_images_remain_selectable_plain_text(self):
        content = '<img src="https://invalid.example/image.png"> **A rule** [link](https://invalid.example)'
        bubble = MessageBubble(ChatMessage("ai-1", "assistant", content))
        self.assertEqual(bubble.text.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(bubble.text.text(), content)
        self.assertTrue(bubble.text.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)
        bubble.deleteLater()

    def test_updates_preserve_display_alias_and_apply_readable_font(self):
        message = ChatMessage("report", "user", "Long internal save prompt")
        bubble = MessageBubble(message, display_text=lambda value: "Prepare my report" if value.role == "user" else value.text)
        self.assertEqual(bubble.text.text(), "Prepare my report")
        message.role, message.status, message.text = "assistant", "streaming", "Working..."
        bubble.update_message(message)
        self.assertIn("AI", bubble.title())
        self.assertIn("streaming", bubble.title())
        self.assertEqual(bubble.text.text(), "Working...")
        bubble.apply_preferences(AppPreferences(chat_font_size=20, chat_font_weight="bold"))
        self.assertEqual(bubble.text.font().pointSize(), 20)
        self.assertEqual(bubble.text.font().weight(), QFont.Weight.Bold)
        self.assertEqual(bubble.row_layout.stretch(2), 1)
        bubble.deleteLater()


class DialogTests(unittest.TestCase):
    def test_interface_language_selector_uses_autonyms_and_does_not_preview_changes(self):
        previous_language = get_language()
        set_language("zh_CN")
        try:
            original = AppPreferences(ui_language="zh_CN", voice_program_path="C:/voice.exe")
            with patch("vasl_ai_bridge.settings_dialog.resolve_voice_config", side_effect=VoiceError("Synthetic local engine status")):
                dialog = SettingsDialog(original)
            self.assertEqual([dialog.language_combo.itemText(index) for index in range(dialog.language_combo.count())],
                             ["English", "简体中文", "繁體中文"])
            self.assertEqual(dialog.language_combo.currentData(), "zh_CN")
            before = tr("Settings")
            self.assertEqual(dialog.windowTitle(), before)
            dialog.language_combo.setCurrentIndex(dialog.language_combo.findData("zh_TW"))
            self.assertEqual(dialog.preferences().ui_language, "zh_TW")
            self.assertEqual(get_language(), "zh_CN")
            self.assertEqual(tr("Settings"), before)
            self.assertEqual(dialog.windowTitle(), before)
            self.assertEqual(dialog.preferences().voice_language, "auto")
            self.assertEqual(dialog.preferences().voice_program_path, original.voice_program_path)
            dialog.reject()
            self.assertEqual(dialog.original, original)
            self.assertEqual(get_language(), "zh_CN")
            dialog.deleteLater()
        finally:
            set_language(previous_language)

    def test_accepting_language_choice_returns_it_without_applying_globally(self):
        previous_language = get_language()
        set_language("en")
        try:
            with patch("vasl_ai_bridge.settings_dialog.resolve_voice_config", side_effect=VoiceError("Synthetic local engine status")):
                dialog = SettingsDialog(AppPreferences())
            dialog.language_combo.setCurrentIndex(dialog.language_combo.findData("zh_TW"))
            dialog._accept_preferences()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dialog.preferences().ui_language, "zh_TW")
            self.assertEqual(get_language(), "en")
            dialog.deleteLater()
        finally:
            set_language(previous_language)

    def test_opening_and_cancelling_does_not_change_preferences(self):
        original = AppPreferences("Mouse5", 16, "bold", 12, "ctrl_enter", "whisper_cpp", "C:/voice.exe", "D:/voice.bin", "auto")
        dialog = SettingsDialog(original)
        self.assertEqual(dialog.preferences(), original)
        self.assertFalse(dialog.capture_editor.isEnabled())
        dialog.chat_size_spin.setValue(20)
        dialog.reject()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.original, original)
        dialog.deleteLater()

    def test_font_weight_selector_exposes_only_real_normal_and_bold_faces(self):
        dialog = SettingsDialog(AppPreferences())
        self.assertEqual([dialog.font_weight_combo.itemData(index) for index in range(dialog.font_weight_combo.count())],
                         ["normal", "bold"])
        self.assertEqual(dialog.preferences().chat_font_weight, "normal")
        dialog.deleteLater()

    def test_automatic_voice_disables_custom_controls_and_retains_paths(self):
        original = AppPreferences(voice_backend="auto", voice_program_path="C:/WhisperDesktop.exe",
                                  voice_model_path="D:/models/custom.bin")
        automatic = VoiceConfig("whisper_cpp", "C:/prepared/whisper-cli.exe", "D:/prepared/ggml-base.bin")
        with patch("vasl_ai_bridge.settings_dialog.resolve_voice_config", return_value=automatic):
            dialog = SettingsDialog(original)
        self.assertFalse(dialog.voice_program_edit.isEnabled())
        self.assertFalse(dialog.voice_model_edit.isEnabled())
        self.assertEqual(dialog.voice_program_edit.text(), automatic.program_path)
        self.assertEqual(dialog.voice_model_edit.text(), automatic.model_path)
        self.assertIn("retained", dialog.voice_hint.text())
        self.assertEqual(dialog.preferences().voice_program_path, original.voice_program_path)
        self.assertEqual(dialog.preferences().voice_model_path, original.voice_model_path)
        dialog.voice_backend_combo.setCurrentIndex(dialog.voice_backend_combo.findData("whisper_cpp"))
        self.assertTrue(dialog.voice_program_edit.isEnabled())
        self.assertTrue(dialog.voice_model_edit.isEnabled())
        self.assertEqual(dialog.voice_program_edit.text(), original.voice_program_path)
        self.assertEqual(dialog.voice_model_edit.text(), original.voice_model_path)
        self.assertIn("Choose whisper-cli.exe", dialog.voice_hint.text())
        dialog.voice_program_edit.setText("C:/manual/whisper-cli.exe")
        dialog.voice_model_edit.setText("D:/manual/ggml-small.bin")
        with patch("vasl_ai_bridge.settings_dialog.resolve_voice_config", return_value=automatic):
            dialog.voice_backend_combo.setCurrentIndex(0)
        self.assertEqual(dialog.preferences().voice_program_path, "C:/manual/whisper-cli.exe")
        self.assertEqual(dialog.preferences().voice_model_path, "D:/manual/ggml-small.bin")
        dialog.voice_backend_combo.setCurrentIndex(dialog.voice_backend_combo.findData("whisper_cpp"))
        self.assertEqual(dialog.voice_program_edit.text(), "C:/manual/whisper-cli.exe")
        dialog.deleteLater()

    def test_missing_automatic_engine_does_not_display_old_manual_paths(self):
        original = AppPreferences(voice_program_path="C:/WhisperDesktop.exe", voice_model_path="D:/old.bin")
        with patch("vasl_ai_bridge.settings_dialog.resolve_voice_config", side_effect=VoiceError("No verified local engine")):
            dialog = SettingsDialog(original)
        self.assertEqual(dialog.voice_program_edit.text(), "")
        self.assertEqual(dialog.voice_model_edit.text(), "")
        self.assertIn("No verified local engine", dialog.voice_hint.text())
        self.assertEqual(dialog.preferences(), original)
        dialog.deleteLater()

    def test_unverified_python_engines_are_not_selectable(self):
        dialog = SettingsDialog(AppPreferences())
        for backend in ("whisper", "faster_whisper"):
            self.assertEqual(dialog.voice_backend_combo.findData(backend), -1)
        dialog.deleteLater()

    def test_desktop_selection_saves_without_a_language_choice(self):
        original = AppPreferences(voice_program_path="C:/WhisperDesktop.exe", voice_model_path="D:/ggml-large-v2.bin")
        dialog = SettingsDialog(original)
        dialog.voice_backend_combo.setCurrentIndex(dialog.voice_backend_combo.findData("whisper_desktop"))
        self.assertEqual(dialog.voice_program_edit.text(), original.voice_program_path)
        self.assertEqual(dialog.voice_model_edit.text(), original.voice_model_path)
        self.assertIn("all subfolders", dialog.voice_hint.text())
        self.assertIn("Whisper.dll", dialog.voice_hint.text())
        self.assertIn("detected automatically", dialog.voice_hint.text())
        self.assertFalse(hasattr(dialog, "voice_language_combo"))
        dialog._accept_preferences()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.error_label.text(), "")
        self.assertEqual(dialog.preferences().voice_backend, "whisper_desktop")
        self.assertEqual(dialog.preferences().voice_language, "auto")
        dialog.deleteLater()

    def test_keyboard_special_keys_and_mouse_buttons_have_one_control(self):
        dialog = SettingsDialog(AppPreferences())
        self.assertEqual(dialog.capture_kind_combo.currentData(), "")
        self.assertFalse(dialog.capture_editor.isEnabled())
        self.assertEqual(dialog.preferences().capture_shortcut, "")
        dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData("keyboard"))
        dialog.special_key_combo.setCurrentIndex(dialog.special_key_combo.findData("CapsLock"))
        self.assertEqual(dialog.preferences().capture_shortcut, "CapsLock")
        dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData("Mouse4"))
        self.assertEqual(dialog.preferences().capture_shortcut, "Mouse4")
        self.assertFalse(dialog.special_key_combo.isEnabled())
        dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData("keyboard"))
        self.assertTrue(dialog.capture_editor.isEnabled())
        dialog.deleteLater()

    def test_empty_keyboard_binding_cannot_accept_and_valid_binding_can(self):
        dialog = SettingsDialog(AppPreferences())
        dialog.capture_kind_combo.setCurrentIndex(dialog.capture_kind_combo.findData("keyboard"))
        dialog.capture_editor.setKeySequence(QKeySequence())
        dialog._accept_preferences()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertIn("key combination", dialog.error_label.text())
        dialog.capture_editor.setKeySequence(QKeySequence("Shift+A"))
        dialog._accept_preferences()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.preferences().capture_shortcut, "Shift+A")
        dialog.deleteLater()

    def test_disabled_capture_saves_and_function_keys_are_rejected(self):
        dialog = SettingsDialog(AppPreferences())
        dialog._accept_preferences()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.preferences().capture_shortcut, "")
        dialog.deleteLater()
        for number in range(1, 25):
            dialog = SettingsDialog(AppPreferences(capture_shortcut="Tab"))
            self.assertEqual(dialog.special_key_combo.findData(f"F{number}"), -1)
            dialog.capture_editor.setKeySequence(QKeySequence(f"Ctrl+F{number}"))
            dialog._accept_preferences()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertIn("Function", dialog.error_label.text())
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
