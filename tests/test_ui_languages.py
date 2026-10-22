"""Offline application checks for live language changes and original records."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit

from vasl_ai_bridge.app import MainWindow, SAVE_REQUEST, REVIEW_REQUEST, inspect_observation
from vasl_ai_bridge.capture import WindowSource, prepare_image
from vasl_ai_bridge.chat_storage import ChatStore
from vasl_ai_bridge.i18n import get_language, localize_error, set_language, tr
from vasl_ai_bridge.preferences import AppPreferences, load_preferences, save_preferences
from vasl_ai_bridge.screenshot_selection import ScreenshotSelectionDialog
from vasl_ai_bridge.settings_dialog import SettingsDialog

APP = QApplication.instance() or QApplication(["ASL_AI offline language integration tests"])


class ApplicationLanguageTests(unittest.TestCase):
    def setUp(self):
        self.previous_language = get_language()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        save_preferences(QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat), AppPreferences())
        self.factory = Mock(side_effect=AssertionError("Language changes must not start the AI runtime"))
        with patch("vasl_ai_bridge.app.list_windows", return_value=[]):
            self.window = MainWindow(ChatStore(self.root), self.factory)
        self.window.game_folder = self.window.folders.create_folder(self.root, "Synthetic game", self.window.session)

    def tearDown(self):
        self.window._active_turn = ""
        self.window._preparing = False
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()
        self.temp.cleanup()
        set_language(self.previous_language)

    def test_live_switch_preserves_active_game_draft_images_and_protocol_choices(self):
        window = self.window
        window._set_auth_state("signed_in")
        window._models_received([{"id": "test-model", "displayName": "New game",
                                 "supportedReasoningEfforts": ["low", "max"], "defaultReasoningEffort": "max"}])
        window.model_combo.setCurrentIndex(0)
        window.reasoning_combo.setCurrentIndex(window.reasoning_combo.findData("max"))
        source = WindowSource(123, "New game")
        window.source_combo.addItem(source.title, source)
        window.source_combo.setCurrentIndex(1)
        image = prepare_image(Image.new("RGB", (40, 30), "white"), "New game")
        window.preview = image
        window.preview_panel.set_observation(image)
        window.attachments = [image]
        window.message_edit.setPlainText("Screenshot {private}\n未发送的草稿")
        ordinary = window.session.add_message("user", "Screenshot", attachments=[image])
        pending = window.session.add_message("assistant", status="pending")
        window.render_session()
        session = window.session
        folder = window.game_folder
        window._active_turn = "synthetic-active-turn"
        for language, send, settings, placeholder in (
            ("zh_CN", "发送", "设置...", "思考中..."),
            ("zh_TW", "傳送", "設定...", "思考中..."),
            ("en", "Send", "Settings...", "Thinking..."),
        ):
            with self.subTest(language=language):
                self.assertTrue(window.apply_preferences(replace(window.preferences, ui_language=language)))
                self.assertIs(window.session, session)
                self.assertEqual(window.game_folder, folder)
                self.assertEqual(window._active_turn, "synthetic-active-turn")
                self.assertEqual(window.model_combo.currentData(), "test-model")
                self.assertEqual(window.model_combo.currentText(), "New game")
                self.assertEqual(window.reasoning_combo.currentData(), "max")
                self.assertEqual(window.source_combo.currentData(), source)
                self.assertEqual(window.source_combo.currentText(), "New game")
                self.assertEqual(window.message_edit.toPlainText(), "Screenshot {private}\n未发送的草稿")
                self.assertEqual(window.attachments, [image])
                self.assertEqual(window.preview_panel.observation.png, image.png)
                self.assertEqual(window._message_widgets[ordinary.message_id].text.text(), "Screenshot")
                self.assertEqual(window._message_widgets[pending.message_id].text.text(), placeholder)
                self.assertEqual(window.send_button.text(), send)
                self.assertEqual(window.settings_button.text(), settings)
                self.assertEqual(window._message_widgets[ordinary.message_id].attachment_buttons[0].text(), tr("Screenshot"))
                self.assertEqual(window.preview_panel.image.toolTip(), tr("Double-click to inspect the screenshot at its original size."))
                loaded = load_preferences(QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat))
                self.assertEqual(loaded.ui_language, language)
        self.factory.assert_not_called()

    def test_failed_setting_save_keeps_language_and_current_widgets(self):
        previous = self.window.preferences
        with patch("vasl_ai_bridge.app.save_preferences", side_effect=[OSError("Synthetic save failure"), None]):
            self.assertFalse(self.window.apply_preferences(replace(previous, ui_language="zh_TW")))
        self.assertEqual(self.window.preferences, previous)
        self.assertEqual(get_language(), "en")
        self.assertEqual(self.window.send_button.text(), "Send")
        self.factory.assert_not_called()

    def test_whole_game_review_entry_cannot_start_a_model_request(self):
        window = self.window
        window.session.add_message("user", "Recorded play remains available")
        window.render_session()
        window.update_controls()
        self.assertTrue(window.review_button.isHidden())
        self.assertFalse(window.review_button.isEnabled())
        before = len(window.session.messages)
        window.review_game()
        window._start_turn(REVIEW_REQUEST, [], review=True)
        self.assertEqual(len(window.session.messages), before)
        self.assertFalse(window.busy)
        self.assertIn("temporarily unavailable", window.status.text())
        self.factory.assert_not_called()

    def test_saved_language_is_loaded_by_new_application_window(self):
        self.assertTrue(self.window.apply_preferences(replace(self.window.preferences, ui_language="zh_TW")))
        with patch("vasl_ai_bridge.app.list_windows", return_value=[]):
            reopened = MainWindow(ChatStore(self.root), self.factory)
        try:
            self.assertEqual(reopened.preferences.ui_language, "zh_TW")
            self.assertEqual(reopened.send_button.text(), "傳送")
            self.assertEqual(reopened.memory_label.text(), "戰術紀錄與共用經驗儲存在本機。")
        finally:
            reopened.close()
            reopened.deleteLater()
            APP.processEvents()
        self.factory.assert_not_called()

    def test_application_requests_and_failure_suffixes_do_not_rewrite_model_text(self):
        window = self.window
        save = window.session.add_message("user", SAVE_REQUEST)
        review = window.session.add_message("user", REVIEW_REQUEST)
        genuine = window.session.add_message("assistant", "Your usage limit has been reached. Check your account's usage limits before trying again.")
        failure = window.session.add_message("assistant", genuine.text, status="failed")
        window.session.legacy_metadata["asl_ai_request_failures"] = {failure.message_id: failure.text}
        window.render_session()
        self.assertTrue(window.apply_preferences(replace(window.preferences, ui_language="zh_CN")))
        self.assertEqual(save.text, SAVE_REQUEST)
        self.assertEqual(review.text, REVIEW_REQUEST)
        self.assertEqual(window._message_widgets[save.message_id].text.text(), tr("Prepare a game save report for me to review."))
        self.assertEqual(window._message_widgets[genuine.message_id].text.text(), genuine.text)
        self.assertEqual(window._message_widgets[failure.message_id].text.text(), localize_error(failure.text))
        self.assertNotEqual(window._message_widgets[failure.message_id].text.text(), failure.text)

    def test_localized_dialog_chrome_and_screen_area_preserve_pixels(self):
        image = prepare_image(Image.new("RGB", (40, 30), "white"), "Synthetic board")
        for language, settings_title, selection_title, save in (
            ("zh_CN", "设置", "选择截图区域", "保存"),
            ("zh_TW", "設定", "選擇截圖區域", "儲存"),
            ("en", "Settings", "Select screenshot area", "Save"),
        ):
            set_language(language)
            settings = SettingsDialog(replace(AppPreferences(), ui_language=language))
            selection = ScreenshotSelectionDialog(image)
            self.assertEqual(settings.windowTitle(), settings_title)
            self.assertEqual(settings.buttons.button(QDialogButtonBox.StandardButton.Save).text().replace("&", ""), save)
            self.assertEqual(settings.capture_editor.findChild(QLineEdit).placeholderText(),
                             "按下快捷键" if language == "zh_CN" else "按下快速鍵" if language == "zh_TW" else "Press shortcut")
            self.assertEqual(selection.windowTitle(), selection_title)
            selection.keep_full_image()
            self.assertEqual(selection.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(selection.selected_observation.png, image.png)
            settings.deleteLater()
            selection.deleteLater()

    def test_open_chat_screenshot_inspector_follows_language_switch(self):
        image = prepare_image(Image.new("RGB", (40, 30), "white"), "Screenshot - original size")
        message = self.window.session.add_message("user", "Screenshot", attachments=[image])
        self.window.render_session()
        dialog = inspect_observation(self.window._message_widgets[message.message_id], image)
        try:
            self.assertEqual(dialog.windowTitle(), "Screenshot - original size")
            self.assertTrue(self.window.apply_preferences(replace(self.window.preferences, ui_language="zh_TW")))
            self.assertEqual(dialog.windowTitle(), "截圖 — 原始尺寸")
            self.assertTrue(self.window.apply_preferences(replace(self.window.preferences, ui_language="en")))
            self.assertEqual(dialog.windowTitle(), "Screenshot - original size")
            self.assertEqual(self.window.session.get_message(message.message_id).attachments[0].png, image.png)
        finally:
            dialog.close()

    def test_notices_translate_dynamic_counts_names_and_nested_diagnostics(self):
        self.assertTrue(self.window.apply_preferences(replace(self.window.preferences, ui_language="zh_TW")))
        name = "Open {私人}"
        self.window.notice(f"Added {name} to your local library and selected it for this game. Send a message to ask AI to read it.")
        self.assertIn(name, self.window.status.text())
        self.assertTrue(self.window.status.text().startswith("已將 "))
        self.window.notice("Could not load model choices. The connection was interrupted. Check your internet connection and try again.", True)
        self.assertTrue(self.window.status.text().startswith("無法載入模型清單。"))
        self.assertNotIn("The connection was interrupted", self.window.status.text())
        self.window.save_error = "[WinError 5] C:/private/Open.aslgame"
        self.window.notice("Ready.")
        self.assertTrue(self.window.status.text().startswith("就緒。\n本機儲存警告："))
        self.assertIn(self.window.save_error, self.window.status.text())


if __name__ == "__main__":
    unittest.main()
