"""Desktop settings, keyboard, capture ordering and voice-draft integration."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QFont, QInputMethodEvent, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from vasl_ai_bridge.app import MainWindow
from vasl_ai_bridge.capture import CaptureError, WindowSource
from vasl_ai_bridge.chat_session import ChatMessage, new_id
from vasl_ai_bridge.chat_storage import ChatStore
from vasl_ai_bridge.hotkey import GlobalCaptureHotkey, HotkeyStatus, WM_KEYDOWN, WM_KEYUP
from vasl_ai_bridge.preferences import load_preferences
from vasl_ai_bridge.voice import VoiceError
from test_chat_app import FakeBridge, screenshot
from test_hotkey import FakeNativeApi


APP = QApplication.instance() or QApplication(["ASL_AI desktop integration tests"])


class FakeVoice(QObject):
    state_changed = Signal(str)
    transcript_ready = Signal(str)
    failed = Signal(str)

    def __init__(self, directory, parent=None):
        super().__init__(parent)
        self.directory = directory
        self.state = "idle"
        self.calls = []
        self.closed = False
        self.start_error = None

    @property
    def active(self):
        return self.state != "idle"

    def change_state(self, state):
        self.state = state
        self.state_changed.emit(state)

    def start(self, config):
        self.calls.append(("start", config))
        if self.start_error:
            raise self.start_error
        self.change_state("recording")

    def stop(self):
        self.calls.append(("stop",))
        self.change_state("transcribing")

    def deliver(self, transcript):
        self.change_state("idle")
        self.transcript_ready.emit(transcript)

    def fail(self, message):
        self.change_state("idle")
        self.failed.emit(message)

    def cancel(self):
        self.calls.append(("cancel",))
        self.change_state("idle")

    def close(self):
        self.calls.append(("close",))
        self.closed = True
        self.change_state("idle")


class FakeHotkey(QObject):
    activated = Signal()
    application_activated = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.shortcut = ""
        self.calls = []
        self.fail_shortcut = None

    def start(self, shortcut):
        self.shortcut = shortcut
        self.calls.append(("start", shortcut))
        return HotkeyStatus(True, "Ready", shortcut)

    @property
    def is_registered(self):
        return bool(self.shortcut)

    def replace(self, shortcut):
        self.calls.append(("replace", shortcut))
        if shortcut == self.fail_shortcut:
            return HotkeyStatus(False, "This binding could not be installed.", self.shortcut)
        self.shortcut = shortcut
        return HotkeyStatus(True, "Ready", shortcut)

    def unregister(self):
        self.calls.append(("unregister",))
        self.shortcut = ""
        return HotkeyStatus(True, "Disabled")


class DesktopIntegrationTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / "runtime" / "desktop-integration-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="desktop-test-", dir=test_root)
        self.root = Path(self.temporary.name)
        folder_dialog = patch("vasl_ai_bridge.app.NewGameDialog")
        self.addCleanup(folder_dialog.stop)
        mocked = folder_dialog.start().return_value
        mocked.exec.return_value = QDialog.DialogCode.Accepted
        mocked.selection.side_effect = lambda: (self.root, "Game-" + new_id())
        self.bridges = []
        self.windows = []
        self.window = self.new_window()

    def new_window(self):
        def bridge_factory(*args, **kwargs):
            bridge = FakeBridge(*args, **kwargs)
            self.bridges.append(bridge)
            return bridge
        with patch("vasl_ai_bridge.app.list_windows", return_value=[]):
            window = MainWindow(ChatStore(self.root), bridge_factory, hotkey_factory=FakeHotkey,
                                voice_factory=FakeVoice)
        self.windows.append(window)
        return window

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.002)
        APP.processEvents()
        self.assertTrue(predicate(), self.window.status.text())

    def tearDown(self):
        for window in self.windows:
            if window.bridge and window._active_turn:
                window.bridge.complete(status="interrupted")
            self.wait_for(lambda w=window: not w.busy and not w.monitor and not w.commands)
            window.close()
            window.deleteLater()
        self.windows.clear()
        APP.processEvents()
        self.temporary.cleanup()

    def pending_draft(self):
        image = screenshot("green")
        self.window.preview = image
        self.window.attach_preview()
        self.window.message_edit.setPlainText("Check this move")
        return image

    def assert_no_request(self):
        self.assertEqual(self.bridges, [])
        self.assertEqual(self.window.session.messages, [])

    def test_settings_survive_restart_and_update_existing_bubbles_and_draft(self):
        self.assertTrue(self.window._ensure_game_folder())
        self.window.session.messages.append(ChatMessage("existing", "assistant", "Keep this reply"))
        self.window.render_session()
        self.window.message_edit.setPlainText("Keep this draft")
        expected = replace(self.window.preferences, capture_shortcut="Mouse4", chat_font_size=18,
                           chat_font_weight="bold", ui_font_size=12, send_mode="ctrl_enter")
        self.assertTrue(self.window.apply_preferences(expected))
        self.assertEqual(self.window.message_edit.toPlainText(), "Keep this draft")
        self.assertEqual(self.window.message_edit.font().pointSize(), 18)
        bubble = self.window._message_widgets["existing"]
        self.assertEqual(bubble.text.font().pointSize(), 18)
        self.assertEqual(bubble.text.font().weight(), QFont.Weight.Bold)
        reader = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.assertEqual(load_preferences(reader), expected)
        reader = None
        self.window.close()
        restarted = self.new_window()
        self.assertEqual(restarted.preferences, expected)
        self.assertEqual(restarted.font().pointSize(), 12)
        self.assertEqual(restarted.message_edit.font().pointSize(), 18)
        self.assertEqual(restarted.message_edit.send_mode, "ctrl_enter")
        self.assertEqual(restarted.capture_hotkey.shortcut, "Mouse4")

    def test_failed_hotkey_registration_preserves_preferences_and_draft(self):
        before = self.window.preferences
        image = self.pending_draft()
        self.window.capture_hotkey.fail_shortcut = "Mouse5"
        self.assertFalse(self.window.apply_preferences(replace(before, capture_shortcut="Mouse5", chat_font_size=20)))
        self.assertEqual(self.window.preferences, before)
        self.assertEqual(self.window.capture_hotkey.shortcut, before.capture_shortcut)
        self.assertEqual(self.window.message_edit.toPlainText(), "Check this move")
        self.assertEqual(self.window.attachments, [image])
        self.assertIn("could not be installed", self.window.status.text())

    def test_failed_settings_save_rolls_back_the_registered_binding(self):
        before = self.window.preferences
        with patch("vasl_ai_bridge.app.save_preferences", side_effect=[OSError("Disk unavailable"), None]):
            self.assertFalse(self.window.apply_preferences(replace(before, capture_shortcut="Tab")))
        self.assertEqual(self.window.preferences, before)
        self.assertEqual(self.window.capture_hotkey.shortcut, before.capture_shortcut)
        self.assertEqual(self.window.capture_hotkey.calls[-2:], [("replace", "Tab"), ("replace", before.capture_shortcut)])
        self.assertIn("Disk unavailable", self.window.status.text())

    def test_failed_settings_save_and_failed_binding_rollback_disable_capture(self):
        self.assertTrue(self.window.set_capture_shortcut("Tab"))
        before = self.window.preferences
        self.window.capture_hotkey.fail_shortcut = before.capture_shortcut
        with patch("vasl_ai_bridge.app.save_preferences", side_effect=[OSError("Disk unavailable"), None]):
            self.assertFalse(self.window.apply_preferences(replace(before, capture_shortcut="Mouse4")))
        self.assertEqual(self.window.preferences, before)
        self.assertFalse(self.window.capture_hotkey.is_registered)
        self.assertIn("disabled", self.window.hotkey_label.text())
        self.assertIn("could not be restored", self.window.status.text())
        # Saving the same choice retries installation after it was disabled.
        self.window.capture_hotkey.fail_shortcut = None
        self.assertTrue(self.window.apply_preferences(before))
        self.assertEqual(self.window.capture_hotkey.shortcut, before.capture_shortcut)

    def test_default_is_disabled_and_other_settings_do_not_enable_capture(self):
        self.assertEqual(self.window.preferences.capture_shortcut, "")
        self.assertFalse(self.window.capture_hotkey.is_registered)
        self.assertEqual(self.window.hotkey_label.text(), "Capture shortcut: Disabled")
        calls = list(self.window.capture_hotkey.calls)
        self.assertTrue(self.window.apply_preferences(replace(self.window.preferences, chat_font_size=18)))
        self.assertEqual(self.window.capture_hotkey.calls, calls)
        self.assertFalse(self.window.capture_hotkey.is_registered)
        self.assertEqual(self.window.hotkey_label.text(), "Capture shortcut: Disabled")

    def test_disabled_choice_is_saved_and_can_be_enabled_again(self):
        self.assertTrue(self.window.set_capture_shortcut("Tab"))
        self.assertTrue(self.window.set_capture_shortcut(""))
        self.assertEqual(self.window.preferences.capture_shortcut, "")
        self.assertEqual(self.window.settings.value("capture_shortcut"), "")
        self.assertFalse(self.window.capture_hotkey.is_registered)
        self.assertEqual(self.window.hotkey_label.text(), "Capture shortcut: Disabled")
        self.assertTrue(self.window.set_capture_shortcut("Mouse4"))
        self.assertTrue(self.window.capture_hotkey.is_registered)
        self.assertEqual(self.window.hotkey_label.text(), "Capture shortcut: Mouse4")

    def test_failed_disabled_settings_save_restores_enabled_binding_and_label(self):
        self.assertTrue(self.window.set_capture_shortcut("Tab"))
        before = self.window.preferences
        with patch("vasl_ai_bridge.app.save_preferences", side_effect=[OSError("Disk unavailable"), None]):
            self.assertFalse(self.window.set_capture_shortcut(""))
        self.assertEqual(self.window.preferences, before)
        self.assertEqual(self.window.capture_hotkey.shortcut, "Tab")
        self.assertTrue(self.window.capture_hotkey.is_registered)
        self.assertEqual(self.window.hotkey_label.text(), "Capture shortcut: Tab")

    def test_failed_hook_release_restores_previous_enabled_binding_honestly(self):
        native = self.install_scoped_capture()
        self.window.preferences = replace(self.window.preferences, capture_shortcut="Tab")
        self.window._display_capture_shortcut("Tab")
        before = self.window.preferences
        native.failed_release_kind = "keyboard"
        try:
            self.assertFalse(self.window.set_capture_shortcut(""))
            self.assertEqual(self.window.preferences, before)
            self.assertEqual(self.window.capture_hotkey.shortcut, "Tab")
            self.assertTrue(self.window.capture_hotkey.is_registered)
            self.assertEqual(self.window.hotkey_label.text(), "Capture shortcut: Tab")
            self.assertIn("could not release", self.window.status.text())
        finally:
            native.failed_release_kind = None

    def test_all_function_keys_cannot_replace_selected_tab(self):
        self.assertTrue(self.window.set_capture_shortcut("Tab"))
        before = self.window.preferences
        calls = list(self.window.capture_hotkey.calls)
        for key in range(1, 25):
            self.assertFalse(self.window.set_capture_shortcut(f"Ctrl+Alt+F{key}"))
        self.assertEqual(self.window.preferences, before)
        self.assertEqual(self.window.capture_hotkey.shortcut, "Tab")
        self.assertEqual(self.window.capture_hotkey.calls, calls)

    def test_the_selected_send_shortcut_sends_once_and_key_repeat_does_not_send(self):
        self.window._set_auth_state("signed_in")
        self.window._models_received(FakeBridge(self.root).models())
        self.window.model_combo.setCurrentIndex(0)
        self.assertTrue(self.window.apply_preferences(replace(self.window.preferences, send_mode="ctrl_enter")))
        self.window.message_edit.setPlainText("Check LOS")
        self.window.message_edit.moveCursor(self.window.message_edit.textCursor().MoveOperation.End)
        QTest.keyClick(self.window.message_edit, Qt.Key.Key_Return)
        self.assertEqual(self.window.message_edit.toPlainText(), "Check LOS\n")
        self.assert_no_request()
        QTest.keyClick(self.window.message_edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.wait_for(lambda: bool(self.window._active_turn) and not self.window.commands)
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier, "\r", True)
        QApplication.sendEvent(self.window.message_edit, event)
        self.assertEqual(len(self.bridges), 1)
        self.assertEqual(len([call for call in self.bridges[0].calls if call[0] == "start_turn"]), 1)

    def test_send_shortcut_while_signed_out_keeps_the_draft_and_no_connection(self):
        self.window.message_edit.setPlainText("Not signed in yet")
        QTest.keyClick(self.window.message_edit, Qt.Key.Key_Return)
        self.assertEqual(self.window.message_edit.toPlainText(), "Not signed in yet")
        self.assert_no_request()

    def test_ime_candidate_confirmation_does_not_create_an_ai_request(self):
        self.window._set_auth_state("signed_in")
        QApplication.sendEvent(self.window.message_edit, QInputMethodEvent("zhong", []))
        QTest.keyClick(self.window.message_edit, Qt.Key.Key_Return)
        commit = QInputMethodEvent("", [])
        commit.setCommitString("中文")
        QApplication.sendEvent(self.window.message_edit, commit)
        QTest.keyClick(self.window.message_edit, Qt.Key.Key_Return)
        self.assert_no_request()
        self.assertIn("中文", self.window.message_edit.toPlainText())

    def test_voice_appends_to_current_draft_preserves_attachment_and_never_sends(self):
        image = self.pending_draft()
        self.window.toggle_voice()
        self.assertEqual(self.window.voice.state, "recording")
        self.assertEqual(self.window.voice_button.text(), "Stop recording")
        self.window.toggle_voice()
        self.assertEqual(self.window.voice.state, "transcribing")
        self.window.message_edit.setPlainText("Edited while transcribing")
        self.window.voice.deliver("Move the squad to K9.")
        self.assertEqual(self.window.message_edit.toPlainText(), "Edited while transcribing\nMove the squad to K9.")
        self.assertEqual(self.window.attachments, [image])
        self.assert_no_request()

    def test_voice_cancel_and_late_transcript_preserve_existing_draft(self):
        image = self.pending_draft()
        self.window.toggle_voice()
        self.window.cancel_voice()
        self.window.voice.deliver("A stale transcript")
        self.assertEqual(self.window.message_edit.toPlainText(), "Check this move")
        self.assertEqual(self.window.attachments, [image])
        self.assertFalse(self.window.voice.active)
        self.assert_no_request()

    def test_voice_failure_preserves_draft_and_attachment(self):
        image = self.pending_draft()
        self.window.toggle_voice()
        self.window.voice.fail("No speech recognized")
        self.assertEqual(self.window.message_edit.toPlainText(), "Check this move")
        self.assertEqual(self.window.attachments, [image])
        self.assertIn("No speech recognized", self.window.status.text())
        self.assert_no_request()

    def test_missing_voice_engine_is_reported_without_starting_a_request(self):
        self.window.voice.start_error = VoiceError("Choose a local model in Settings")
        self.window.toggle_voice()
        self.assertFalse(self.window.voice.active)
        self.assertEqual(self.window._voice_session_id, "")
        self.assertIn("Choose a local model", self.window.status.text())
        self.assert_no_request()

    def test_game_switch_and_send_are_blocked_while_voice_is_recording(self):
        session_id = self.window.session.session_id
        image = self.pending_draft()
        self.window._set_auth_state("signed_in")
        self.window.toggle_voice()
        self.assertFalse(self.window.new_button.isEnabled())
        self.assertFalse(self.window.open_button.isEnabled())
        self.assertFalse(self.window.send_button.isEnabled())
        self.window.new_session()
        with patch("vasl_ai_bridge.app.QFileDialog.getOpenFileName") as picker:
            self.window.open_session()
        picker.assert_not_called()
        self.assertFalse(self.window.load_checkpoint(self.root / "not-opened.aslgame"))
        self.window.send_message()
        self.assertEqual(self.window.session.session_id, session_id)
        self.assertEqual(self.window.message_edit.toPlainText(), "Check this move")
        self.assertEqual(self.window.attachments, [image])
        self.assert_no_request()

    def test_closing_window_cancels_and_closes_voice_and_rejects_late_transcript(self):
        self.window.message_edit.setPlainText("Keep me")
        self.window.toggle_voice()
        self.window.close()
        self.assertTrue(self.window.voice.closed)
        self.assertFalse(self.window.voice.active)
        self.assertIn(("cancel",), self.window.voice.calls)
        self.window.voice.deliver("Too late")
        self.assertEqual(self.window.message_edit.toPlainText(), "Keep me")
        self.assert_no_request()

    def test_hotkey_capture_freezes_before_foreground_and_selection(self):
        events = []
        original = screenshot("blue")
        self.window.source_combo.addItem("Board", WindowSource(123, "Board"))
        self.window.source_combo.setCurrentIndex(1)
        class Selection:
            selected_observation = original
            def __init__(self, observation, parent):
                events.append("dialog")
            def exec(self):
                return QDialog.DialogCode.Accepted
        self.window.selection_factory = Selection
        def capture(source, **kwargs):
            events.append("capture")
            self.assertEqual(source.handle, 123)
            return original
        with patch("vasl_ai_bridge.app.capture_visible_window", side_effect=capture), \
             patch.object(self.window, "_bring_capture_window_forward", side_effect=lambda: events.append("foreground")):
            self.window.capture_from_hotkey()
        self.assertEqual(events, ["capture", "foreground", "dialog"])
        self.assertEqual(self.window.preview, original)
        self.assertEqual(self.window.attachments, [original])
        self.assert_no_request()

    def test_visible_capture_failure_brings_the_status_to_the_user(self):
        self.window.source_combo.addItem("Board", WindowSource(123, "Board"))
        self.window.source_combo.setCurrentIndex(1)
        with patch("vasl_ai_bridge.app.capture_visible_window", side_effect=CaptureError("Board is not visible")), \
             patch.object(self.window, "_bring_capture_window_forward") as foreground:
            self.window.capture_from_hotkey()
        foreground.assert_called_once()
        self.assertIn("Board is not visible", self.window.status.text())
        self.assertEqual(self.window.attachments, [])
        self.assert_no_request()

    def install_scoped_capture(self):
        self.window.source_combo.addItem("Board", WindowSource(123, "Board"))
        self.window.source_combo.setCurrentIndex(1)
        native = FakeNativeApi()
        self.window.capture_hotkey.unregister()
        self.window._setup_hotkey(lambda parent: GlobalCaptureHotkey(
            parent, native_api=native, application=APP,
            target_window_getter=parent._capture_target_handle,
            application_window_getter=parent._capture_application_handle))
        self.assertTrue(self.window.capture_hotkey.replace("Tab").success)
        self.addCleanup(self.window.capture_hotkey.unregister)
        return native

    def test_main_window_native_shortcut_uses_the_exact_button_capture_path(self):
        original = screenshot("blue")
        self.window.message_edit.setPlainText("Keep this draft")
        class Selection:
            selected_observation = original
            def __init__(self, observation, parent):
                self.observation = observation
            def exec(self):
                return QDialog.DialogCode.Accepted
        self.window.selection_factory = Selection
        with patch.object(self.window, "capture_and_select", wraps=self.window.capture_and_select) as button:
            native = self.install_scoped_capture()
            native.foreground = int(self.window.winId())
            with patch("vasl_ai_bridge.app.capture_window", return_value=original) as selected_capture, \
                 patch("vasl_ai_bridge.app.capture_visible_window") as visible_capture:
                self.assertTrue(native.fire("keyboard", WM_KEYDOWN, 9))
                selected_capture.assert_not_called()
                self.assertTrue(native.fire("keyboard", WM_KEYUP, 9))
                APP.processEvents()
                button.assert_called_once_with()
                selected_capture.assert_called_once_with(WindowSource(123, "Board"), max_dimension=8192)
                visible_capture.assert_not_called()
        self.assertEqual(self.window.attachments, [original])
        self.assertEqual(self.window.message_edit.toPlainText(), "Keep this draft")
        self.assert_no_request()

    def test_board_native_shortcut_keeps_visible_pixels_before_foreground_and_dialog(self):
        original = screenshot("blue")
        events = []
        native = self.install_scoped_capture()
        native.foreground = 123
        class Selection:
            selected_observation = original
            def __init__(self, observation, parent):
                events.append("dialog")
            def exec(self):
                return QDialog.DialogCode.Accepted
        self.window.selection_factory = Selection
        def capture(source, **kwargs):
            events.append("capture")
            self.assertEqual(source.handle, 123)
            return original
        with patch("vasl_ai_bridge.app.capture_visible_window", side_effect=capture), \
             patch("vasl_ai_bridge.app.capture_window") as selected_capture, \
             patch.object(self.window, "_bring_capture_window_forward", side_effect=lambda: events.append("foreground")):
            self.assertTrue(native.fire("keyboard", WM_KEYDOWN, 9))
            self.assertTrue(native.fire("keyboard", WM_KEYUP, 9))
            APP.processEvents()
            selected_capture.assert_not_called()
        self.assertEqual(events, ["capture", "foreground", "dialog"])
        self.assertEqual(self.window.attachments, [original])
        self.assert_no_request()

    def test_unrelated_window_and_modal_settings_never_consume_capture_input(self):
        from vasl_ai_bridge.settings_dialog import SettingsDialog
        native = self.install_scoped_capture()
        dialog = SettingsDialog(self.window.preferences, self.window)
        self.addCleanup(dialog.close)
        with patch("vasl_ai_bridge.app.capture_window") as selected_capture, \
             patch("vasl_ai_bridge.app.capture_visible_window") as visible_capture:
            native.foreground = 456
            self.assertFalse(native.fire("keyboard", WM_KEYDOWN, 9))
            self.assertFalse(native.fire("keyboard", WM_KEYUP, 9))
            dialog.setModal(True)
            dialog.show()
            APP.processEvents()
            self.assertIs(QApplication.activeModalWidget(), dialog)
            for foreground in (int(dialog.winId()), int(self.window.winId()), 123):
                native.foreground = foreground
                self.assertFalse(native.fire("keyboard", WM_KEYDOWN, 9))
                self.assertFalse(native.fire("keyboard", WM_KEYUP, 9))
            APP.processEvents()
            selected_capture.assert_not_called()
            visible_capture.assert_not_called()
        self.assertEqual(self.window.capture_hotkey.diagnostics["native_matches"], 0)
        self.assert_no_request()
        dialog.close()
        APP.processEvents()

    def test_selection_or_closing_disables_both_native_capture_scopes(self):
        native = self.install_scoped_capture()
        with patch("vasl_ai_bridge.app.capture_window") as selected_capture, \
             patch("vasl_ai_bridge.app.capture_visible_window") as visible_capture:
            for flag in ("_selecting_screenshot", "_closing"):
                setattr(self.window, flag, True)
                try:
                    for foreground in (int(self.window.winId()), 123):
                        native.foreground = foreground
                        self.assertFalse(native.fire("keyboard", WM_KEYDOWN, 9))
                        self.assertFalse(native.fire("keyboard", WM_KEYUP, 9))
                    APP.processEvents()
                finally:
                    setattr(self.window, flag, False)
            selected_capture.assert_not_called()
            visible_capture.assert_not_called()
        self.assert_no_request()

    def test_main_shortcut_failure_keeps_existing_draft_attachment_and_preview(self):
        previous = self.pending_draft()
        native = self.install_scoped_capture()
        native.foreground = int(self.window.winId())
        with patch("vasl_ai_bridge.app.capture_window", side_effect=CaptureError("The selected window is minimized.")), \
             patch("vasl_ai_bridge.app.capture_visible_window") as visible_capture, \
             patch.object(self.window, "selection_factory") as selection:
            self.assertTrue(native.fire("keyboard", WM_KEYDOWN, 9))
            self.assertTrue(native.fire("keyboard", WM_KEYUP, 9))
            APP.processEvents()
            visible_capture.assert_not_called()
            selection.assert_not_called()
        self.assertEqual(self.window.attachments, [previous])
        self.assertIs(self.window.preview, previous)
        self.assertEqual(self.window.message_edit.toPlainText(), "Check this move")
        self.assertIn("minimized", self.window.status.text())
        self.assert_no_request()

    def test_cancelled_main_shortcut_selection_keeps_existing_draft_attachment_and_preview(self):
        previous = self.pending_draft()
        native = self.install_scoped_capture()
        native.foreground = int(self.window.winId())
        class CancelSelection:
            selected_observation = None
            def __init__(self, observation, parent):
                pass
            def exec(self):
                return QDialog.DialogCode.Rejected
        self.window.selection_factory = CancelSelection
        with patch("vasl_ai_bridge.app.capture_window", return_value=screenshot("blue")), \
             patch("vasl_ai_bridge.app.capture_visible_window") as visible_capture:
            self.assertTrue(native.fire("keyboard", WM_KEYDOWN, 9))
            self.assertTrue(native.fire("keyboard", WM_KEYUP, 9))
            APP.processEvents()
            visible_capture.assert_not_called()
        self.assertEqual(self.window.attachments, [previous])
        self.assertIs(self.window.preview, previous)
        self.assertEqual(self.window.message_edit.toPlainText(), "Check this move")
        self.assertIn("cancelled", self.window.status.text())
        self.assert_no_request()


if __name__ == "__main__":
    unittest.main()
