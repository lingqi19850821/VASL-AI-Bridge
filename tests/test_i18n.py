"""Offline localization checks for locale choice, data boundaries and Qt UI."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from string import Formatter
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QLocale, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QDialogButtonBox, QFileDialog,
    QLabel, QLineEdit, QListWidget, QPlainTextEdit, QPushButton, QTextEdit,
    QVBoxLayout, QWidget,
)

from vasl_ai_bridge.i18n import (
    LANGUAGE_NAMES, UI_LANGUAGES, catalog, get_language, localize_error,
    localize_widget_tree, normalize_language, set_language, system_language, tr,
)

APP = QApplication.instance() or QApplication(["ASL_AI offline localization tests"])


class LocalizationTests(unittest.TestCase):
    def setUp(self):
        self.previous_language = get_language()
        set_language("en")

    def tearDown(self):
        set_language(self.previous_language)

    def test_system_locale_uses_script_and_chinese_regions(self):
        expected = {
            "en_US": "en", "fr_CA": "en", "ja_JP": "en", "zh_CN": "zh_CN",
            "zh_SG": "zh_CN", "zh_TW": "zh_TW", "zh_HK": "zh_TW",
            "zh_MO": "zh_TW", "zh_Hans_HK": "zh_CN", "zh_Hant_CN": "zh_TW",
        }
        for locale, result in expected.items():
            with self.subTest(locale=locale):
                self.assertEqual(system_language(QLocale(locale)), result)

    def test_supported_choices_have_autonyms_and_defensive_fallback(self):
        self.assertEqual(UI_LANGUAGES, ("en", "zh_CN", "zh_TW"))
        self.assertEqual(tuple(LANGUAGE_NAMES.values()), ("English", "简体中文", "繁體中文"))
        self.assertEqual(normalize_language("zh-Hant"), "zh_TW")
        self.assertEqual(normalize_language(None), "en")
        self.assertEqual(set_language("invalid"), "en")

    def test_first_launch_uses_display_language_instead_of_regional_format(self):
        for display, expected in (("zh-Hant-TW", "zh_TW"), ("zh-Hans-CN", "zh_CN"), ("en-CA", "en")):
            with self.subTest(display=display):
                regional = SimpleNamespace(uiLanguages=lambda: [display])
                with patch("vasl_ai_bridge.i18n.QLocale.system", return_value=regional):
                    self.assertEqual(system_language(), expected)

    def test_catalog_preserves_format_fields_in_both_scripts(self):
        self.assertTrue(catalog())
        formatter = Formatter()
        def fields(text):
            return sorted((name, spec, conversion) for _, name, spec, conversion in formatter.parse(text) if name)
        for source, pair in catalog().items():
            with self.subTest(source=source):
                self.assertEqual(len(pair), 2)
                for translated in pair:
                    self.assertTrue(translated)
                    self.assertEqual(fields(source), fields(translated))

    def test_english_and_unknown_fallback_and_untouched_inserted_values(self):
        source = "Could not open this game: {detail}"
        self.assertEqual(tr(source, detail="Open {private}"), "Could not open this game: Open {private}")
        set_language("zh_CN")
        self.assertEqual(tr("User-chosen text {braces}"), "User-chosen text {braces}")
        self.assertEqual(tr(source, detail="Open {private}"), "无法打开此对局：Open {private}")
        set_language("zh_TW")
        self.assertEqual(tr(source, detail="Open {private}"), "無法開啟此對局：Open {private}")
        self.assertEqual(tr("Local preview\n{source} | {captured_at}", source="Open {private}", captured_at="12:00"),
                         "本機預覽\nOpen {private} | 12:00")

    def test_all_classified_request_failures_translate_in_both_scripts(self):
        from vasl_ai_bridge.request_errors import _MESSAGES
        for language in ("zh_CN", "zh_TW"):
            set_language(language)
            for kind, message in _MESSAGES.items():
                with self.subTest(language=language, kind=kind):
                    self.assertNotEqual(localize_error(message), message)
                    self.assertEqual(localize_error("Could not send during this response: " + message +
                                     " Your draft is retained. Click Stop, then Send to continue."),
                                     tr("Could not send during this response: {error} Your draft is retained. Click Stop, then Send to continue.",
                                        error=tr(message)))

    def test_known_errors_translate_and_preserve_external_diagnostics(self):
        set_language("zh_CN")
        diagnostic = r"[WinError 5] C:\Games\Open\私人.aslgame {error}"
        self.assertEqual(localize_error("Could not open this game: " + diagnostic), "无法打开此对局：" + diagnostic)
        self.assertEqual(localize_error(diagnostic), diagnostic)
        self.assertEqual(localize_error("Could not open this game: The requested save point no longer exists."),
                         "无法打开此对局：请求的存档点已不存在。")
        self.assertEqual(localize_error("Windows could not enable the keyboard capture binding. Windows error 5."),
                         "Windows 无法启用 keyboard 截图按键。Windows 错误 5。")
        self.assertEqual(localize_error("X" * 32769), "X" * 32769)

    def test_microphone_diagnostic_keeps_device_and_code(self):
        set_language("zh_TW")
        diagnostic = "IOError (Open; device=USB 麦克风; 16000 Hz mono Int16)"
        message = "Microphone stopped: " + diagnostic + ". Reading the microphone failed. Check whether the device was disconnected or changed."
        translated = localize_error(message)
        self.assertIn(diagnostic, translated)
        self.assertTrue(translated.startswith("麥克風已停止："))
        self.assertTrue(translated.endswith("讀取麥克風失敗。請檢查裝置是否中斷連線或發生變更。"))

    def test_standard_buttons_follow_application_choice_and_return_to_english(self):
        for language, save, cancel in (("zh_CN", "保存", "取消"), ("zh_TW", "儲存", "取消"), ("en", "Save", "Cancel")):
            set_language(language)
            box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            self.assertEqual(box.button(QDialogButtonBox.StandardButton.Save).text().replace("&", ""), save)
            self.assertEqual(box.button(QDialogButtonBox.StandardButton.Cancel).text().replace("&", ""), cancel)
            self.assertEqual(QCoreApplication.translate("QFileDialog", "File name:"),
                             "文件名：" if language == "zh_CN" else "檔案名稱：" if language == "zh_TW" else "File name:")
            box.deleteLater()

    def test_actual_standard_edit_menus_translate_and_preserve_original_text(self):
        expected = {
            "en": ("Undo", "Redo", "Cut", "Copy", "Paste", "Delete", "Select All"),
            "zh_CN": ("撤销", "重做", "剪切", "复制", "粘贴", "删除", "全选"),
            "zh_TW": ("復原", "取消復原", "剪下", "複製", "貼上", "刪除", "全選"),
        }
        original = r"User original {source} 字符 C:\Games\Save.aslgame"
        for language, labels in expected.items():
            set_language(language)
            for cls in (QLineEdit, QTextEdit, QPlainTextEdit):
                with self.subTest(language=language, editor=cls.__name__):
                    edit = cls(original)
                    edit.selectAll()
                    menu = edit.createStandardContextMenu()
                    actions = [action for action in menu.actions() if not action.isSeparator()]
                    self.assertEqual(tuple(action.text().split("\t")[0].replace("&", "") for action in actions), labels)
                    # Unhandled Qt shortcut contexts must retain Ctrl, rather
                    # than becoming an empty translation or changing editing.
                    self.assertIn("Ctrl+", actions[2].text())
                    self.assertEqual(edit.text() if isinstance(edit, QLineEdit) else edit.toPlainText(), original)
                    menu.deleteLater()
                    edit.deleteLater()
            self.assertEqual(QCoreApplication.translate("UnknownQtContext", "Untouched Qt text"), "Untouched Qt text")
            self.assertEqual(QCoreApplication.translate("QFileDialog", "Unknown picker label"), "Unknown picker label")

    def test_actual_qt_picker_static_labels_and_actions_follow_language(self):
        expected = {
            "en": ("Look in:", "File name:", "Files of type:", "Show Size", "Show Type", "Show Date Modified", "Name"),
            "zh_CN": ("查找位置：", "文件名：", "文件类型：", "显示大小", "显示类型", "显示修改日期", "名称"),
            "zh_TW": ("尋找位置：", "檔案名稱：", "檔案類型：", "顯示大小", "顯示類型", "顯示修改日期", "名稱"),
        }
        from pathlib import Path
        for language, labels in expected.items():
            set_language(language)
            dialog = QFileDialog(None, "Picker", str(Path(__file__).parent))
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            # Child widgets/actions are built synchronously. Rendering and
            # filesystem population do not need to complete to inspect chrome.
            found_labels = {child.text().replace("&", "") for child in dialog.findChildren(QLabel)}
            found_actions = {child.text().replace("&", "") for child in dialog.findChildren(QAction)}
            self.assertTrue(set(labels[:3]).issubset(found_labels), (language, found_labels))
            self.assertTrue(set(labels[3:6]).issubset(found_actions), (language, found_actions))
            self.assertEqual(QCoreApplication.translate("QFileSystemModel", "Name"), labels[6])
            self.assertTrue(APP.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs))
            buttons = {child.text().replace("&", "") for child in dialog.findChildren(QAbstractButton)}
            self.assertIn("Open" if language == "en" else "打开" if language == "zh_CN" else "開啟", buttons)
            dialog.deleteLater()

    def test_static_binding_switches_without_changing_user_content(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        button = QPushButton("Could not open this game: {detail}")
        # Use a known plain error as a static label; arbitrary data may contain
        # the exact same English words and must remain unchanged elsewhere.
        source = "The requested save point no longer exists."
        label = QLabel(source)
        entry = QLineEdit(source)
        entry.setPlaceholderText(source)
        editor = QTextEdit(source)
        listing = QListWidget()
        listing.addItem(source)
        names = QComboBox()
        names.setProperty("i18n_skip_items", True)
        names.addItem(source)
        editable = QComboBox()
        editable.setEditable(True)
        editable.addItem(source)
        user_label = QLabel(source)
        user_label.setProperty("i18n_skip", True)
        choices = QComboBox()
        choices.addItem(source)
        for widget in (button, label, entry, editor, listing, names, editable, user_label, choices):
            layout.addWidget(widget)
        localize_widget_tree(root)
        set_language("zh_CN")
        localize_widget_tree(root)
        self.assertEqual(label.text(), "请求的存档点已不存在。")
        self.assertEqual(entry.placeholderText(), label.text())
        self.assertEqual(choices.itemText(0), label.text())
        self.assertEqual(entry.text(), source)
        self.assertEqual(editor.toPlainText(), source)
        self.assertEqual(listing.item(0).text(), source)
        self.assertEqual(names.itemText(0), source)
        self.assertEqual(editable.currentText(), source)
        self.assertEqual(user_label.text(), source)
        label.setText("A newer dynamic state")
        set_language("zh_TW")
        localize_widget_tree(root)
        self.assertEqual(label.text(), "A newer dynamic state")
        self.assertEqual(choices.itemText(0), "要求的存檔點已不存在。")
        set_language("en")
        localize_widget_tree(root)
        self.assertEqual(choices.itemText(0), source)
        root.deleteLater()


if __name__ == "__main__":
    unittest.main()
