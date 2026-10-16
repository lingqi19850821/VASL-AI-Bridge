"""Offline checks for the ordinary-chat workflow and asynchronous request control."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication, QDialog, QListWidget, QMessageBox, QPushButton

from vasl_ai_bridge.app import MainWindow, REVIEW_REQUEST, SAVE_REQUEST, RUNTIME_TOOLS_VERSION
from vasl_ai_bridge.capture import CaptureError, WindowSource, prepare_image
from vasl_ai_bridge.chat_session import ChatSession, new_id
from vasl_ai_bridge.chat_storage import ChatStore
from vasl_ai_bridge.codex_bridge import BridgeError
from vasl_ai_bridge.storage import StorageError


APP = QApplication.instance() or QApplication(["ASL_AI offline chat tests"])


def screenshot(color="white"):
    return prepare_image(Image.new("RGB", (40, 30), color), "Synthetic board")


class FakeBridge:
    _login_sequence = 0

    def __init__(self, runtime_dir, tool_handler=None):
        self.runtime_dir, self.tool_handler = runtime_dir, tool_handler
        self.events = queue.Queue()
        self.calls = []
        self.thread_id, self.turn_id = "", ""
        self.running = False
        self.thread_count = self.turn_count = 0
        self.fail_resume = self.fail_steer = False
        self.fail_logout = False
        self.fail_cancel_login = False

    def start(self):
        self.running = True
        self.calls.append(("start",))
        return {}

    def login(self, mode="chatgpt", key=""):
        self.calls.append(("login", mode, key))
        if mode == "chatgpt":
            FakeBridge._login_sequence += 1
            return {"authUrl": "https://auth.openai.com/test", "loginId": f"login-{FakeBridge._login_sequence}"}
        return {}

    def cancel_login(self, login_id):
        self.calls.append(("cancel_login", login_id))
        if self.fail_cancel_login:
            raise RuntimeError("The login cancellation request failed")
        return {}

    def logout(self):
        self.calls.append(("logout",))
        if self.fail_logout:
            raise RuntimeError("Connection failed")
        self.events.put({"method": "account/updated", "params": {"authMode": None}})
        return {}

    def models(self):
        self.calls.append(("models",))
        return [{"id": "gpt-6.1-sol", "displayName": "GPT-6.1 Sol"}, {"id": "test-model", "displayName": "Test model"}]

    def start_thread(self, model="", instructions=""):
        self.thread_count += 1
        self.thread_id = f"thread-{self.thread_count}"
        self.calls.append(("start_thread", model, instructions))
        return self.thread_id

    def resume(self, thread_id, model="", instructions=""):
        self.calls.append(("resume", thread_id, model))
        if self.fail_resume:
            raise RuntimeError("Local history is unavailable")
        self.thread_id = thread_id
        return thread_id

    def start_turn(self, text, images=(), *, effort=""):
        self.turn_count += 1
        self.turn_id = f"turn-{self.turn_count}"
        self.calls.append(("start_turn", text, list(images)))
        self.calls.append(("reasoning_effort", self.turn_id, effort))
        return self.turn_id

    def steer(self, text, images=()):
        if self.fail_steer:
            raise RuntimeError("This model does not accept steering")
        self.calls.append(("steer", text, list(images)))
        return {}

    def delta(self, text):
        self.events.put({"method": "item/agentMessage/delta", "params": {
            "threadId": self.thread_id, "turnId": self.turn_id, "itemId": "reply-1", "delta": text}})

    def complete(self, text="AI reply", status="completed"):
        turn_id = self.turn_id
        if status == "completed":
            self.events.put({"method": "item/completed", "params": {"threadId": self.thread_id,
                "turnId": turn_id, "item": {"id": "reply-1", "type": "agentMessage", "text": text}}})
        self.events.put({"method": "turn/completed", "params": {"threadId": self.thread_id,
                        "turn": {"id": turn_id, "status": status}}})
        self.turn_id = ""

    def interrupt(self):
        self.calls.append(("interrupt",))
        self.complete(status="interrupted")
        return {}

    def poll_event(self, timeout=0):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self.running = False
        self.events.put({"method": "bridge/closed", "params": {}})


class RecordingBridge(FakeBridge):
    """Offline runtime stand-in that exercises the real app tool callbacks."""

    def start_turn(self, text, images=(), *, effort=""):
        result = super().start_turn(text, images, effort=effort)
        history, offset = [], 0
        while offset is not None:
            page = self.tool_handler("read_game_history", {"offset": offset, "limit": 30})
            history.extend(page["messages"])
            offset = page["next_offset"]
        sources = [item for item in history if item["role"] == "user"
                   and item["text"] not in {SAVE_REQUEST, REVIEW_REQUEST}]
        source = sources[-1]["message_id"]
        matches = self.tool_handler("search_experience", {"query": "road"})
        self.calls.append(("experience_read", matches))
        lessons = []
        if sources[-1]["text"].startswith("RESULT:"):
            lessons = [{"title": "Road exposure", "lesson": "An exposed road move can draw defensive fire.",
                        "conditions": "ASL infantry crossing an observed road",
                        "observed_result": sources[-1]["text"], "evidence_kind": "reported_outcome",
                        "source_message_ids": [source]}]
        recorded = self.tool_handler("record_turn_memory", {
            "intents": [{"action": "Use cover", "goal": "Avoid the exposed road", "source_message_ids": [source]}],
            "lessons": lessons, "processed_message_ids": [item["message_id"] for item in sources],
        })
        self.calls.append(("memory_saved", recorded))
        if text.endswith(REVIEW_REQUEST):
            review = self.tool_handler("record_review", {"title": "Review so far", "text": "Review of recorded play; outcome unknown.",
                                      "source_message_ids": [sources[0]["message_id"], source]})
            self.calls.append(("review_saved", review))
        return result


class ChatAppTests(unittest.TestCase):
    def setUp(self):
        FakeBridge._login_sequence = 0
        test_root = Path(__file__).resolve().parents[1] / "runtime" / "chat-ui-tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="chat-ui-test-", dir=test_root)
        self.root = Path(self.temp.name)
        folder_patcher = patch("vasl_ai_bridge.app.NewGameDialog")
        self.new_folder_dialog = folder_patcher.start()
        self.addCleanup(folder_patcher.stop)
        self.new_folder_dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
        self.new_folder_dialog.return_value.selection.side_effect = lambda: (self.root, "Game-" + new_id())
        self.bridges = []
        def factory(*args, **kwargs):
            bridge = FakeBridge(*args, **kwargs)
            self.bridges.append(bridge)
            return bridge
        with patch("vasl_ai_bridge.app.list_windows", return_value=[]):
            self.window = MainWindow(ChatStore(self.root), factory)

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.002)
        APP.processEvents()
        self.assertTrue(predicate(), self.window.status.text())

    def tearDown(self):
        if self.window.bridge and self.window._active_turn:
            self.window.bridge.complete(status="interrupted")
        elif self.window.monitor and self.window.monitor.login_id:
            self.window.cancel_sign_in()
        self.wait_for(lambda: not self.window.busy and not self.window.monitor and not self.window.commands)
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()
        self.temp.cleanup()

    def signed_in(self):
        self.window._set_auth_state("signed_in")
        if not self.window._model_catalog:
            self.window._models_received(FakeBridge(self.root).models())
        if self.window.model_combo.currentIndex() < 0:
            self.window.model_combo.setCurrentIndex(0)

    def fake_login(self):
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True), patch.object(self.window, "_return_after_login"):
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor))
            self.bridges[-1].events.put({"method": "account/login/completed", "params": {
                "loginId": self.window.monitor.login_id, "success": True, "error": None}})
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)

    def send(self, text="Hello"):
        self.signed_in()
        self.window.message_edit.setPlainText(text)
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn) and not self.window.commands)
        return self.bridges[-1]

    def finish(self, text="AI reply"):
        self.bridges[-1].complete(text)
        self.wait_for(lambda: not self.window.busy and not self.window.monitor and not self.window.commands)

    def test_startup_geometry_fits_frame_inside_available_area_without_changing_font(self):
        original_font = self.window.font().toString()
        for available in (QRect(100, 50, 1600, 1100), QRect(-1200, 100, 1200, 760), QRect(0, 0, 800, 500)):
            with self.subTest(available=available):
                self.window.setMinimumSize(850, 620)
                self.window.resize(1375, 970)
                self.window.move(5000, 5000)
                screen = SimpleNamespace(availableGeometry=lambda: available)
                def frame():
                    return QRect(self.window.x(), self.window.y(), self.window.width() + 16, self.window.height() + 39)
                with patch.object(self.window, "screen", return_value=screen), patch.object(self.window, "frameGeometry", side_effect=frame):
                    self.window._fit_initial_geometry()
                    self.assertTrue(available.contains(frame()))
                self.assertEqual(self.window.size().width(), min(1375, available.width() - 16))
                self.assertEqual(self.window.size().height(), min(970, available.height() - 39))
                self.assertLessEqual(self.window.minimumWidth(), available.width() - 16)
                self.assertLessEqual(self.window.minimumHeight(), available.height() - 39)
                self.assertEqual(self.window.font().toString(), original_font)

    def test_hide_and_show_after_startup_preserves_player_window_size(self):
        with patch.object(self.window, "_fit_initial_geometry") as fit:
            self.window.show()
            APP.processEvents()
            self.window.resize(1150, 700)
            self.window.hide()
            self.window.show()
            APP.processEvents()
            fit.assert_called_once_with()
            self.assertEqual((self.window.width(), self.window.height()), (1150, 700))

    def test_startup_capture_and_reference_import_do_not_create_model_connection(self):
        self.assertEqual(self.bridges, [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.window.source_combo.addItem("Board", WindowSource(123, "Board"))
        self.window.source_combo.setCurrentIndex(1)
        with patch("vasl_ai_bridge.app.capture_window", return_value=screenshot()):
            self.window.capture_current()
        source = self.root / "rules.txt"
        source.write_text("A1.1 Test rule", encoding="utf-8")
        self.assertTrue(self.window.import_reference(source))
        self.assertEqual(self.bridges, [])
        self.assertIn("Test rule", self.window.session.references[0].text())

    def test_text_only_send_needs_no_selected_window_or_capture(self):
        with patch("vasl_ai_bridge.app.capture_window", side_effect=AssertionError("Unexpected capture")):
            bridge = self.send("Which phase are we in?")
        calls = [call for call in bridge.calls if call[0] == "start_turn"]
        self.assertEqual(calls[0][2], [])
        self.finish("Rally phase")
        self.assertEqual([message.role for message in self.window.session.messages], ["user", "assistant"])

    def test_attachment_is_frozen_when_preview_changes(self):
        original, changed = screenshot("blue"), screenshot("red")
        self.window.preview = original
        self.window.attach_preview()
        self.window.preview = changed
        self.window.preview_panel.set_observation(changed)
        bridge = self.send("Look here")
        call = next(call for call in bridge.calls if call[0] == "start_turn")
        self.assertEqual(call[2], [original.data_url])
        self.assertEqual(self.window.session.messages[0].attachments, [original])
        self.assertEqual(self.window.attachments, [])
        self.finish()

    def test_screenshot_only_message_is_supported(self):
        self.signed_in()
        image = screenshot()
        self.window.preview = image
        self.window.attach_preview()
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.finish()
        self.assertEqual(self.window.session.messages[0].text, "")
        self.assertEqual(self.window.session.messages[0].attachments, [image])

    def test_failed_capture_does_not_reuse_preview_or_change_attached_image(self):
        original = screenshot()
        self.window.preview = original
        self.window.attach_preview()
        with patch("vasl_ai_bridge.app.capture_window", side_effect=CaptureError("Window closed")):
            self.assertIsNone(self.window.capture_current(source=WindowSource(123, "Board")))
        self.assertIsNone(self.window.preview)
        self.assertEqual(self.window.attachments, [original])

    def test_stop_retains_partial_answer_history_and_draft_and_allows_next_turn(self):
        bridge = self.send()
        bridge.delta("Move the squad")
        self.wait_for(lambda: self.window.session.messages[-1].text == "Move the squad")
        self.window.message_edit.setPlainText("Actually it is in K9")
        self.assertTrue(self.window.message_edit.isEnabled())
        self.window.stop_request()
        self.wait_for(lambda: not self.window.busy and not self.window.monitor and not self.window.commands)
        self.assertEqual(self.window.session.messages[-1].status, "interrupted")
        self.assertEqual(self.window.session.messages[-1].text, "Move the squad")
        self.assertEqual(self.window.message_edit.toPlainText(), "Actually it is in K9")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.finish("Corrected")
        self.assertEqual(bridge.thread_count, 1)
        self.assertEqual(bridge.turn_count, 2)

    def test_steering_success_is_archived_and_failure_retains_draft(self):
        bridge = self.send()
        self.window.message_edit.setPlainText("The squad is in K9")
        self.window.send_message()
        self.wait_for(lambda: not self.window.commands)
        self.assertEqual(self.window.session.messages[-1].text, "The squad is in K9")
        bridge.fail_steer = True
        self.window.message_edit.setPlainText("Wait, check LOS first")
        self.window.send_message()
        self.wait_for(lambda: not self.window.commands)
        self.assertEqual(self.window.message_edit.toPlainText(), "Wait, check LOS first")
        self.assertIn("Click Stop", self.window.status.text())
        self.finish()

    def test_new_draft_typed_during_steer_is_not_discarded(self):
        self.send()
        self.window.message_edit.setPlainText("Correction one")
        self.window.send_message()
        self.window.message_edit.setPlainText("Correction two")
        self.wait_for(lambda: not self.window.commands)
        self.assertEqual(self.window.message_edit.toPlainText(), "Correction two")
        self.finish()

    def test_save_requests_reviewable_report_and_save_as_preserves_corrections(self):
        self.send("Start scenario S01")
        self.finish("Ready")
        self.window.request_save_report()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.finish("Turn 2. Squad in J9.")
        self.assertTrue(self.window._report_ready)
        self.assertFalse(self.window.save_report_button.isHidden())
        self.window.message_edit.setPlainText("The squad is actually in K9")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.finish("Turn 2. Squad in K9.")
        self.assertEqual(self.window.session.save_report, "Turn 2. Squad in K9.")
        chosen = self.window.game_folder
        with patch("vasl_ai_bridge.app.SavePointDialog") as dialog, patch("vasl_ai_bridge.app.QFileDialog.getSaveFileName") as path_dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog.return_value.point_name.return_value = "Turn two"
            self.assertEqual(self.window.save_session(), chosen.resolve())
        path_dialog.assert_not_called()
        point = self.window.folders.list_points(chosen)[0]
        restored = self.window.folders.load_point(chosen, point["point_id"])
        self.assertEqual(restored.save_report, "Turn 2. Squad in K9.")
        self.assertIn("The squad is actually in K9", [message.text for message in restored.messages])
        self.assertNotIn(b"sk-test-only-key", self.window.folders.database_path(chosen).read_bytes())

    def test_cancel_save_dialog_leaves_conversation_and_report_available(self):
        self.window.session.add_message("assistant", "Current report")
        self.window.session.save_report = "Current report"
        self.window._report_ready = True
        with patch("vasl_ai_bridge.app.SavePointDialog") as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.assertIsNone(self.window.save_session())
        self.assertEqual(self.window.session.save_report, "Current report")
        self.assertEqual(self.bridges, [])

    def test_preparing_save_report_preserves_unsent_draft_and_attachment(self):
        self.send("Start game")
        self.finish("Ready")
        image = screenshot()
        self.window.message_edit.setPlainText("My unsent draft")
        self.window.preview = image
        self.window.attach_preview()
        self.window.request_save_report()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.assertEqual(self.window.message_edit.toPlainText(), "My unsent draft")
        self.assertEqual(self.window.attachments, [image])
        call = [call for call in self.bridges[-1].calls if call[0] == "start_turn"][-1]
        self.assertEqual(call[2], [])
        self.finish("Current game report")

    def test_load_is_local_and_first_send_starts_snapshot_thread_without_old_image_upload(self):
        image = screenshot()
        saved = ChatSession(backend_mode="apiKey", backend_thread_id="saved-thread", save_report="Unit at K9")
        saved.add_message("user", "Earlier board", attachments=[image])
        saved.add_message("assistant", "Unit at K9")
        path = self.window.store.save(saved, self.root / "saved.aslgame")
        self.assertTrue(self.window.load_checkpoint(path))
        self.assertEqual(self.bridges, [])
        self.assertEqual(self.window.attachments, [])
        bridge = self.send("Continue")
        self.assertTrue(any(call[0] == "start_thread" for call in bridge.calls))
        self.assertFalse(any(call[0] == "resume" for call in bridge.calls))
        call = next(call for call in bridge.calls if call[0] == "start_turn")
        self.assertEqual(call[2], [])
        self.assertIn("Recheck", call[1])
        self.finish()

    def test_lost_local_thread_uses_saved_report_and_complete_archive_stays_searchable(self):
        bridge = self.send("Earlier turn")
        self.finish("Unit at K9")
        self.window.session.save_report = "Turn 2: unit at K9"
        saved = self.window.store.save(self.window.session, self.root / "portable.aslgame")
        self.window.load_checkpoint(saved)
        bridge.fail_resume = True
        self.window.message_edit.setPlainText("Continue")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        call = [call for call in bridge.calls if call[0] == "start_turn"][-1]
        self.assertIn("Turn 2: unit at K9", call[1])
        self.assertIn("Earlier turn", str(self.window._handle_tool("search_game_history", {"query": "Earlier turn"})))
        self.finish()

    def test_compaction_notice_never_deletes_archived_messages(self):
        for index in range(40):
            self.window.session.add_message("user", f"Turn history {index}")
        bridge = self.send("Continue")
        before = len(self.window.session.messages)
        self.window._handle_event({"method": "item/started", "params": {"threadId": bridge.thread_id,
            "item": {"type": "contextCompaction"}}})
        self.assertIn("compacting", self.window.status.text())
        self.window._handle_event({"method": "item/completed", "params": {"threadId": bridge.thread_id,
            "item": {"type": "contextCompaction"}}})
        self.assertEqual(len(self.window.session.messages), before)
        self.assertIn("Turn history 0", str(self.window._handle_tool("search_game_history", {"query": "Turn history 0"})))
        call = next(call for call in bridge.calls if call[0] == "start_turn")
        self.assertIn("Turn history 0", call[1])
        self.finish()

    def test_model_refresh_is_explicit_and_changing_model_resumes_same_game(self):
        self.signed_in()
        self.window.refresh_models()
        self.wait_for(lambda: not self.window.commands)
        self.assertEqual(self.window.model_combo.count(), 2)
        bridge = self.send()
        self.finish()
        self.window.model_combo.setCurrentIndex(1)
        self.window.message_edit.setPlainText("Try selected model")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.assertIn(("resume", "thread-1", "test-model"), bridge.calls)
        self.finish()

    def test_reasoning_levels_default_and_explicit_changes_reach_same_game(self):
        self.signed_in()
        self.window._models_received([{
            "model": "gpt-6.1-sol", "displayName": "GPT-6.1 Sol", "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "high", "max", "ultra")],
        }])
        self.assertEqual(self.window.reasoning_combo.currentData(), "")
        self.window.reasoning_combo.setCurrentIndex(self.window.reasoning_combo.findData("ultra"))
        bridge = self.send("Use my selected reasoning level")
        self.assertEqual([call[-1] for call in bridge.calls if call[0] == "reasoning_effort"], ["ultra"])
        self.assertFalse(self.window.reasoning_combo.isEnabled())
        self.finish()
        self.window.reasoning_combo.setCurrentIndex(0)
        self.send("Return to the model default")
        self.assertEqual([call[-1] for call in bridge.calls if call[0] == "reasoning_effort"], ["ultra", "medium"])
        self.assertEqual(bridge.thread_count, 1)
        recorded = self.window.session.legacy_metadata["asl_ai_model_requests"]
        self.assertEqual([row["requested_effort"] for row in recorded], ["ultra", "medium"])
        self.assertEqual(recorded[-1]["source"], "application_request")
        loaded = self.window.folders.load_point(self.window.game_folder)
        self.assertEqual(loaded.legacy_metadata["asl_ai_model_requests"], recorded)
        self.finish()

    def test_model_and_reasoning_are_blank_until_login_and_not_restored(self):
        self.assertEqual(self.window.model_combo.count(), 0)
        self.assertEqual(self.window.reasoning_combo.count(), 0)
        self.assertFalse(self.window.model_combo.isEnabled())
        self.assertFalse(self.window.reasoning_combo.isEnabled())
        self.assertFalse(self.window.models_button.isEnabled())
        self.signed_in()
        self.window._models_received([
            {"model": "gpt-6.1-sol", "defaultReasoningEffort": "medium", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]},
            {"model": "gpt-6-astra", "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "max"}, {"reasoningEffort": "ultra"}]},
        ])
        self.window.reasoning_combo.setCurrentIndex(self.window.reasoning_combo.findData("high"))
        self.window.model_combo.setCurrentIndex(self.window.model_combo.findData("gpt-6-astra"))
        self.assertEqual(self.window.reasoning_combo.currentData(), "")
        self.window.reasoning_combo.setCurrentIndex(self.window.reasoning_combo.findData("ultra"))
        self.assertFalse(self.window.settings.contains("preferred_model"))
        self.assertFalse(self.window.settings.contains("reasoning_efforts_by_model"))
        self.window._set_auth_state("signed_out")
        self.assertEqual(self.window.model_combo.count(), 0)
        self.assertEqual(self.window.reasoning_combo.count(), 0)
        self.assertFalse(self.window.model_combo.isEnabled())
        self.assertFalse(self.window.reasoning_combo.isEnabled())
        # Previously written unpublished settings cannot seed a signed-out picker.
        self.window.settings.setValue("preferred_model", "gpt-6-astra")
        self.window.settings.setValue("reasoning_efforts_by_model", '{"gpt-6-astra":"ultra"}')
        with patch("vasl_ai_bridge.app.list_windows", return_value=[]):
            restarted = MainWindow(ChatStore(self.root), FakeBridge)
        try:
            self.assertEqual(restarted.model_combo.count(), 0)
            self.assertEqual(restarted.reasoning_combo.count(), 0)
            self.assertFalse(restarted.model_combo.isEnabled())
            self.assertFalse(restarted.reasoning_combo.isEnabled())
            self.assertEqual(restarted.bridge, None)
        finally:
            restarted.close()
            restarted.deleteLater()
            APP.processEvents()

    def test_login_refreshes_catalog_without_selecting_an_old_or_default_model(self):
        self.fake_login()
        self.assertTrue(any(call[0] == "models" for call in self.bridges[-1].calls))
        self.assertEqual(self.window.model_combo.count(), 2)
        self.assertIsNone(self.window.model_combo.currentData())
        self.assertEqual(self.window.reasoning_combo.count(), 0)
        self.assertTrue(self.window.model_combo.isEnabled())
        self.assertFalse(self.window.reasoning_combo.isEnabled())
        self.window.message_edit.setPlainText("Ready to choose")
        self.assertFalse(self.window.send_button.isEnabled())
        self.window.model_combo.setCurrentIndex(1)
        self.assertTrue(self.window.reasoning_combo.isEnabled())
        self.assertTrue(self.window.send_button.isEnabled())

    def test_refresh_drops_missing_selection_and_ignores_signed_out_catalog_results(self):
        self.signed_in()
        self.window.model_combo.setCurrentIndex(1)
        self.window._models_received([{"model": "luna-fixture", "displayName": "Luna fixture"}])
        self.assertEqual(self.window.model_combo.count(), 1)
        self.assertIsNone(self.window.model_combo.currentData())
        self.assertEqual(self.window.model_combo.findData("test-model"), -1)
        self.window._set_auth_state("signed_out")
        self.window._models_received([{"model": "old-account-model"}])
        self.assertEqual(self.window.model_combo.count(), 0)
        self.fake_login()
        self.assertIsNone(self.window.model_combo.currentData())
        self.assertEqual(self.window.model_combo.findData("luna-fixture"), -1)

    def test_dead_runtime_during_model_refresh_clears_stale_account_choices(self):
        self.fake_login()
        self.window.model_combo.setCurrentIndex(0)
        self.bridges[-1].running = False
        self.window.refresh_models()
        self.wait_for(lambda: not self.window.commands)
        self.assertEqual(self.window._auth_state, "disconnected")
        self.assertEqual(self.window.model_combo.count(), 0)
        self.assertEqual(self.window.reasoning_combo.count(), 0)
        self.assertFalse(self.window.model_combo.isEnabled())
        self.assertFalse(self.window.reasoning_combo.isEnabled())
        self.assertEqual(self.window.login_button.text(), "Sign in with ChatGPT")

    def test_model_refresh_auth_error_requires_signin_and_keeps_game(self):
        bridge = self.send("Retain this game")
        self.finish()
        messages = list(self.window.session.messages)
        with patch.object(bridge, "models", side_effect=BridgeError("opaque", error_info={"codexErrorInfo": "unauthorized"})):
            self.window.refresh_models()
            self.wait_for(lambda: not self.window.commands)
        self.assertEqual(self.window._auth_state, "disconnected")
        self.assertEqual(self.window.model_combo.count(), 0)
        self.assertIn("Sign in with ChatGPT again", self.window.status.text())
        self.assertEqual(self.window.session.messages, messages)

    def test_current_effort_removed_from_refreshed_catalog_does_not_silently_lower_request(self):
        self.signed_in()
        self.window._models_received([{"model": "gpt-6.1-sol", "defaultReasoningEffort": "low",
                                      "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "max"}]}])
        self.window.reasoning_combo.setCurrentIndex(self.window.reasoning_combo.findData("max"))
        self.window._models_received([{"model": "gpt-6.1-sol", "defaultReasoningEffort": "low",
                                      "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}])
        self.assertEqual(self.window.reasoning_combo.currentData(), "max")
        self.signed_in()
        self.window.message_edit.setPlainText("Do not silently change my effort")
        self.window.send_message()
        self.wait_for(lambda: not self.window.commands and not self.window.busy)
        self.assertFalse(any(call[0] == "start_turn" for bridge in self.bridges for call in bridge.calls))
        self.assertIn("not advertised", self.window.status.text())

    def test_explicit_effort_is_checked_against_runtime_catalog_before_first_request(self):
        self.signed_in()
        self.window._models_received([{"model": "gpt-6.1-sol", "defaultReasoningEffort": "low",
                                      "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "max"}]}])
        self.window.reasoning_combo.setCurrentIndex(self.window.reasoning_combo.findData("max"))
        self.window.message_edit.setPlainText("Use my Max selection")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn) and not self.window.commands)
        bridge = self.bridges[-1]
        self.assertEqual([call[-1] for call in bridge.calls if call[0] == "reasoning_effort"], ["max"])
        self.assertIn("gpt-6.1-sol", self.window._model_catalog)
        self.finish()

    def test_advertised_ultra_is_enabled_and_sent_without_substitution(self):
        self.signed_in()
        self.window._models_received([{"model": "gpt-6.1-sol", "defaultReasoningEffort": "low",
                                      "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "max"}, {"reasoningEffort": "ultra"}]}])
        index = self.window.reasoning_combo.findData("ultra")
        self.assertTrue(self.window.reasoning_combo.model().item(index).isEnabled())
        self.window.reasoning_combo.setCurrentIndex(index)
        self.assertEqual(self.window.reasoning_combo.currentData(), "ultra")
        self.signed_in()
        self.window.message_edit.setPlainText("Do not normalize my Ultra choice")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn) and not self.window.commands)
        self.assertEqual([call[-1] for call in self.bridges[-1].calls if call[0] == "reasoning_effort"], ["ultra"])
        self.assertEqual(self.window.session.legacy_metadata["asl_ai_model_requests"][-1]["requested_effort"], "ultra")
        self.finish()

    def test_service_reroute_is_recorded_without_claiming_requested_model_was_served(self):
        bridge = self.send("Record the requested model")
        self.window._handle_event({"method": "model/rerouted", "params": {
            "threadId": bridge.thread_id, "turnId": bridge.turn_id,
            "fromModel": "gpt-6.1-sol", "toModel": "fixture-reroute",
        }})
        record = self.window.session.legacy_metadata["asl_ai_model_requests"][-1]
        self.assertEqual(record["model"], "gpt-6.1-sol")
        self.assertEqual(record["rerouted_to_model"], "fixture-reroute")
        self.assertIn("service changed", self.window.status.text())
        self.finish()

    def test_rpc_failure_preserves_structured_usage_error_through_worker(self):
        self.signed_in()
        bridge = self.window._ensure_bridge()
        error = BridgeError("Opaque runtime rejection", error_info={
            "code": -32000, "data": {"codexErrorInfo": "usageLimitExceeded"}})
        with patch.object(bridge, "start_turn", side_effect=error):
            self.window.message_edit.setPlainText("Keep this decision")
            self.window.send_message()
            self.wait_for(lambda: not self.window.commands and not self.window.busy)
        self.assertIn("usage limit", self.window.status.text().lower())
        self.assertNotIn("Opaque runtime rejection", self.window.status.text())
        self.assertEqual(bridge.thread_count, 1)
        self.assertEqual(self.window.model_combo.currentData(), "gpt-6.1-sol")
        stored = self.window.folders.load_point(self.window.game_folder)
        self.assertEqual(stored.messages[0].text, "Keep this decision")
        self.assertEqual(stored.messages[-1].status, "failed")

    def test_final_stream_errors_have_distinct_english_messages_without_switching_models(self):
        cases = (
            ({"message": "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account."}, "cannot use the selected model"),
            ({"message": "opaque", "codexErrorInfo": "usageLimitExceeded"}, "usage limit"),
            ({"message": "opaque", "codexErrorInfo": "rateLimitExceeded"}, "Too many requests"),
            ({"message": "opaque", "codexErrorInfo": "unauthorized"}, "Sign in with ChatGPT again"),
            ({"message": "opaque", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": None}}}, "internet connection"),
            ({"message": "opaque", "codexErrorInfo": "serverOverloaded"}, "temporarily unavailable"),
            ({"message": "Regional policy restriction", "code": 403}, "Regional policy restriction"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                bridge = self.send("Play this action")
                chosen = self.window.model_combo.currentData()
                request_count = len([call for call in bridge.calls if call[0] == "start_turn"])
                bridge.events.put({"method": "error", "params": {
                    "threadId": bridge.thread_id, "turnId": bridge.turn_id,
                    "error": error, "willRetry": False}})
                bridge.complete(status="failed")
                self.wait_for(lambda: not self.window.busy and not self.window.monitor and not self.window.commands)
                self.assertIn(expected, self.window.status.text())
                self.assertIn(expected, self.window.session.messages[-1].text)
                self.assertEqual(self.window.session.messages[-1].status, "failed")
                self.assertEqual(len([call for call in bridge.calls if call[0] == "start_turn"]), request_count)
                if error.get("codexErrorInfo") == "unauthorized":
                    self.assertEqual(self.window.model_combo.count(), 0)
                    self.assertFalse(self.window.model_combo.isEnabled())
                else:
                    self.assertEqual(self.window.model_combo.currentData(), chosen)

    def test_retriable_error_waits_for_completion_and_does_not_replace_successful_reply(self):
        bridge = self.send("Wait for this reply")
        assistant = self.window.session.messages[-1]
        bridge.events.put({"method": "error", "params": {
            "threadId": bridge.thread_id, "turnId": bridge.turn_id,
            "error": {"message": "temporary", "codexErrorInfo": "serverOverloaded"}, "willRetry": True}})
        self.wait_for(lambda: "retrying" in self.window.status.text())
        self.assertTrue(self.window.busy)
        self.assertEqual(assistant.status, "pending")
        bridge.complete("Recovered reply")
        self.wait_for(lambda: not self.window.busy and not self.window.monitor)
        self.assertEqual(assistant.text, "Recovered reply")
        self.assertEqual(assistant.status, "completed")
        self.assertEqual(self.window.status.text(), "Ready.")

    def test_quota_failure_current_progress_reopens_locally_without_new_ai_report(self):
        self.signed_in()
        image = screenshot("green")
        self.window.attachments = [image]
        bridge = self.send("The board at the stopping point")
        turn_id = bridge.turn_id
        bridge.events.put({"method": "turn/completed", "params": {
            "threadId": bridge.thread_id,
            "turn": {"id": turn_id, "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}}})
        bridge.turn_id = ""
        self.wait_for(lambda: not self.window.busy and not self.window.monitor)
        self.assertFalse(self.window._report_ready)
        self.assertFalse(self.window.session.save_report)
        self.assertNotIn("Click Save", self.window.status.text())
        self.assertNotIn("save report", self.window.status.text().lower())
        folder = self.window.game_folder
        calls = list(bridge.calls)
        self.window._set_auth_state("signed_out")
        self.assertTrue(self.window.load_checkpoint(folder))
        self.assertEqual(bridge.calls, calls)
        self.assertEqual(self.window.session.messages[0].text, "The board at the stopping point")
        self.assertEqual(self.window.session.messages[0].attachments[0].png, image.png)
        self.assertEqual(self.window.session.messages[-1].status, "failed")

    def test_usage_error_does_not_hide_a_local_save_failure(self):
        bridge = self.send("Check disk failure too")
        with patch.object(self.window.folders, "save_current", side_effect=StorageError("Disk full")):
            self.window._turn_failed(BridgeError("quota", error_info={"codexErrorInfo": "usageLimitExceeded"}))
        bridge.turn_id = ""
        self.window.monitor.stopped.set()
        self.wait_for(lambda: not self.window.monitor)
        self.assertIn("usage limit", self.window.status.text().lower())
        self.assertIn("Disk full", self.window.status.text())
        self.assertNotIn("stored locally", self.window.status.text())

    def test_old_backend_tool_catalog_migrates_with_complete_chat_and_historical_images(self):
        early = screenshot("blue")
        self.window.session.add_message("user", "Original scenario Note", attachments=[early])
        self.window.session.add_message("assistant", "Original rule clarification")
        self.window.session.backend_mode = "chatgpt"
        self.window.session.backend_thread_id = "old-reader-thread"
        bridge = self.send("Continue using the upgraded Reference image reader")
        self.assertFalse(any(call[0] == "resume" for call in bridge.calls))
        seed = next(call for call in bridge.calls if call[0] == "start_turn")
        self.assertIn("Original scenario Note", seed[1])
        self.assertIn("Original rule clarification", seed[1])
        self.assertIn(early.observation_id, seed[1])
        self.assertEqual(seed[2], [])
        self.assertEqual(self.window.session.legacy_metadata["asl_ai_runtime_tools_version"], RUNTIME_TOOLS_VERSION)
        self.assertEqual(self.window.session.messages[0].attachments[0].png, early.png)
        self.finish()

    def test_reference_catalog_tells_ai_how_to_read_selected_png(self):
        from vasl_ai_bridge.chat_session import Reference
        image = screenshot()
        self.window.session.references.append(Reference("oob", "order-of-battle.png", "image/png", image.png))
        context = self.window._reference_context()
        self.assertIn('"image_available": true', context)
        self.assertIn('"reader": "read_reference_image"', context)
        self.assertIn("read_reference_image", context)
        self.assertNotIn("format has no text reader", context)

    def test_reference_picker_filters_formats_and_import_rejects_unsupported_file(self):
        from vasl_ai_bridge.reference_formats import REFERENCE_FILE_FILTER
        with patch("vasl_ai_bridge.app.QFileDialog.getOpenFileNames", return_value=([], "")) as picker:
            self.window.add_files()
        self.assertEqual(picker.call_args.args[-1], REFERENCE_FILE_FILTER)
        self.assertNotIn("All files", REFERENCE_FILE_FILTER)
        unsupported = self.root / "unsupported.exe"
        unsupported.write_bytes(b"MZ synthetic fixture")
        self.assertFalse(self.window.import_reference(unsupported))
        self.assertEqual(self.window.session.references, [])
        self.assertFalse(self.window.library.path.exists())

    def _reference_removal_sources(self):
        self.window.new_session()
        paths = []
        for name in ("rules.txt", "charts.txt", "scenario.txt"):
            path = self.root / name
            path.write_text("Synthetic reference content: " + name, encoding="utf-8")
            self.assertTrue(self.window.import_reference(path))
            paths.append(path)
        return paths

    def _choose_reference_removal(self, names, *, accept=True, confirm=True):
        def interact(dialog):
            listing = dialog.findChild(QListWidget, "reference_library_files")
            button = dialog.findChild(QPushButton, "remove_library_references")
            self.assertFalse(button.isEnabled())
            before = listing.count()
            for index in range(listing.count()):
                item = listing.item(index)
                item.setSelected(item.text() in names)
            self.assertTrue(button.isEnabled())
            button.click()
            self.assertEqual(listing.count(), before - (len(names) if confirm else 0))
            return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected
        answer = QMessageBox.StandardButton.Yes if confirm else QMessageBox.StandardButton.No
        with patch("vasl_ai_bridge.app.QDialog.exec", new=interact), patch("vasl_ai_bridge.app.QMessageBox.question", return_value=answer):
            self.window.choose_references()

    def test_library_multi_remove_preserves_sources_and_updates_game_and_tool_access(self):
        paths = self._reference_removal_sources()
        originals = {path: path.read_bytes() for path in paths}
        references = {item.name: item for item in self.window.session.references}
        self.window._apply_reference_selection([references["rules.txt"].reference_id, references["scenario.txt"].reference_id])
        self._choose_reference_removal({"rules.txt", "charts.txt"})
        self.assertEqual([item.name for item in self.window.library.list_references()], ["scenario.txt"])
        self.assertEqual([item.name for item in self.window.library.selected_references()], ["scenario.txt"])
        self.assertEqual([item.name for item in self.window.session.references], ["scenario.txt"])
        self.assertEqual([item.name for item in self.window._tool_snapshot.references], ["scenario.txt"])
        current = self.window.folders.load_point(self.window.game_folder)
        self.assertEqual([item.name for item in current.references], ["scenario.txt"])
        self.assertEqual(self.window._handle_tool("search_reference_files", {"query": "rules.txt"})["matches"], [])
        self.assertEqual({path: path.read_bytes() for path in paths}, originals)
        self.assertIn("Removed 2", self.window.status.text())
        self.assertTrue(self.window.import_reference(paths[0]))
        self.assertEqual({item.name for item in self.window.session.references}, {"scenario.txt", "rules.txt"})

    def test_rejected_library_removal_confirmation_keeps_files_and_selection(self):
        self._reference_removal_sources()
        self._choose_reference_removal({"rules.txt", "charts.txt"}, confirm=False)
        self.assertEqual(len(self.window.library.list_references()), 3)
        self.assertEqual(len(self.window.library.selected_references()), 3)
        self.assertEqual(len(self.window.session.references), 3)

    def test_cancel_reference_dialog_discards_confirmed_staged_removal(self):
        self._reference_removal_sources()
        self._choose_reference_removal({"rules.txt", "charts.txt"}, accept=False)
        self.assertEqual(len(self.window.library.list_references()), 3)
        self.assertEqual(len(self.window.library.selected_references()), 3)
        self.assertEqual(len(self.window.session.references), 3)

    def test_failed_library_removal_keeps_active_references_and_reports_error(self):
        self._reference_removal_sources()
        with patch.object(self.window.library, "remove_references", side_effect=StorageError("Synthetic removal failure")):
            self._choose_reference_removal({"rules.txt"})
        self.assertEqual(len(self.window.library.list_references()), 3)
        self.assertEqual(len(self.window.session.references), 3)
        self.assertIn("Synthetic removal failure", self.window.status.text())

    def test_library_compaction_failure_reports_completed_removal(self):
        self._reference_removal_sources()
        with patch.object(self.window.library, "compact", side_effect=StorageError("Synthetic space recovery failure")):
            self._choose_reference_removal({"rules.txt"})
        self.assertNotIn("rules.txt", [item.name for item in self.window.library.list_references()])
        self.assertNotIn("rules.txt", [item.name for item in self.window.session.references])
        self.assertIn("Files were removed", self.window.status.text())
        self.assertIn("could not be reclaimed", self.window.status.text())

    def test_library_removal_reports_unsaved_game_change_without_restoring_removed_tools(self):
        self._reference_removal_sources()
        with patch.object(self.window.folders, "save_current", side_effect=StorageError("Synthetic game save failure")), \
                patch.object(self.window.library, "compact", side_effect=StorageError("Synthetic space recovery failure")):
            self._choose_reference_removal({"rules.txt"})
        self.assertNotIn("rules.txt", [item.name for item in self.window.library.list_references()])
        self.assertNotIn("rules.txt", [item.name for item in self.window._tool_snapshot.references])
        self.assertIn("reference change could not be saved", self.window.status.text())
        self.assertIn("Storage space could not be reclaimed", self.window.status.text())

    def test_supported_reimport_repairs_selected_legacy_image_without_changing_saved_original(self):
        from vasl_ai_bridge.chat_session import Reference
        image = screenshot()
        legacy = Reference("old-image", "legacy.bin", "application/octet-stream", image.png)
        self.window.session.references = [legacy]
        self.window.library.adopt_references([legacy])
        self.window.library.set_selected([legacy.reference_id])
        archive = self.window.store.save(self.window.session, self.root / "original-legacy.aslgame")
        original_archive = archive.read_bytes()
        source = self.root / "order-of-battle.png"
        source.write_bytes(image.png)
        self.assertTrue(self.window.import_reference(source))
        repaired = self.window.session.references[0]
        self.assertIsNot(repaired, legacy)
        self.assertEqual(repaired.reference_id, legacy.reference_id)
        self.assertEqual(repaired.content, legacy.content)
        self.assertEqual(repaired.name, source.name)
        self.assertEqual(legacy.name, "legacy.bin")
        self.assertEqual(archive.read_bytes(), original_archive)
        result = self.window._handle_tool("read_reference_image", {"reference_id": legacy.reference_id})
        self.assertTrue(result["success"])
        self.assertEqual(self.window.store.load(archive).references[0].name, "legacy.bin")

    def test_subscription_login_uses_browser_and_does_not_start_game_turn(self):
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True) as browser:
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor))
            self.bridges[-1].events.put({"method": "account/login/completed", "params": {
                "loginId": "login-1", "success": True, "error": None}})
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
        browser.assert_called_once()
        self.assertEqual(self.window._authenticated_mode, "chatgpt")
        self.assertFalse(any(call[0] == "start_turn" for call in self.bridges[-1].calls))

    def test_references_and_attachments_round_trip_in_manual_save(self):
        reference = self.root / "rules.txt"
        reference.write_text("A4.1 Movement rule", encoding="utf-8")
        self.window.import_reference(reference)
        image = screenshot()
        self.window.preview = image
        self.window.attach_preview()
        self.send("Check movement")
        self.finish("Current report")
        self.window.session.save_report = "Current report"
        self.window._report_ready = True
        chosen = self.root / "portable.aslgame"
        self.window.save_session(path=chosen)
        loaded = self.window.store.load(chosen)
        self.assertEqual(loaded.messages[0].attachments[0].png, image.png)
        self.assertIn("Movement rule", loaded.references[0].text())

    def test_save_errors_remain_visible_after_capture_notice(self):
        self.assertTrue(self.window._ensure_game_folder())
        self.window.session.add_message("user", "Hi")
        with patch.object(self.window.folders, "save_current", side_effect=StorageError("Disk full")):
            self.window._autosave()
        self.window.notice("Screenshot captured")
        self.assertIn("Disk full", self.window.status.text())

    def test_new_and_open_preserve_current_session_when_local_save_fails(self):
        original = self.window.session
        original.add_message("user", "Unsaved conversation")
        other = ChatSession()
        other.add_message("user", "Other game")
        saved = self.window.store.save(other, self.root / "other.aslgame")
        with patch.object(self.window.store, "save", side_effect=StorageError("Disk full")):
            self.window.new_session()
            self.assertIs(self.window.session, original)
            self.assertFalse(self.window.load_checkpoint(saved))
            self.assertIs(self.window.session, original)

    def test_opening_older_snapshot_isolates_recovery_and_avoids_future_context(self):
        self.send("Turn 1")
        self.finish("Turn 1 result")
        self.window.session.save_report = "Turn 1 report"
        old_file = self.window.store.save(self.window.session, self.root / "turn-1.aslgame")
        old_count = len(self.window.session.messages)
        self.window.message_edit.setPlainText("Turn 4 later event")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.finish("Turn 4 result")
        original_folder = self.window.game_folder
        later_count = len(self.window.folders.load_point(original_folder).messages)
        self.assertTrue(self.window.load_checkpoint(old_file))
        self.assertEqual(len(self.window.session.messages), old_count)
        self.window.message_edit.setPlainText("Resume the earlier snapshot")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.finish("Rechecked Turn 1")
        self.assertEqual(self.bridges[-1].thread_count, 2)
        self.assertEqual(len(self.window.folders.load_point(original_folder).messages), later_count)
        self.assertEqual(len(self.window.store.load(old_file).messages), old_count)
        recovered = self.window.folders.load_point(self.window.game_folder)
        self.assertFalse(any("Turn 4" in message.text for message in recovered.messages))
        reference = self.root / "snapshot-rules.txt"
        reference.write_text("A1.1 Added after opening old snapshot", encoding="utf-8")
        self.assertTrue(self.window.import_reference(reference))
        untouched = self.window.folders.load_point(original_folder)
        self.assertEqual(len(untouched.messages), later_count)
        self.assertEqual(untouched.references, [])

    def test_dead_bridge_requires_subscription_login_without_erasing_archive(self):
        bridge = self.send("First message")
        self.finish("First answer")
        bridge.running = False
        self.window._check_connection()
        self.window.message_edit.setPlainText("Reconnect")
        self.window.send_message()
        self.assertEqual(len(self.window.session.messages), 2)
        self.assertFalse(self.window.send_button.isEnabled())
        self.assertIn("interrupted", self.window.connection_label.text())
        self.fake_login()
        self.window.model_combo.setCurrentIndex(0)
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.assertEqual(len(self.bridges), 2)
        self.assertTrue(any(call[0] == "login" and call[1] == "chatgpt" for call in self.bridges[-1].calls))
        self.finish("Second answer")
        self.assertEqual(len(self.window.session.messages), 4)

    def test_runtime_loss_seeds_all_messages_after_last_saved_report(self):
        bridge = self.send("Turn 2")
        self.finish("Turn 2 report: squad at K9")
        self.window.session.save_report = "Turn 2 report: squad at K9"
        self.window.message_edit.setPlainText("Turn 4: the squad moved to L9")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.finish("Turn 4: squad at L9")
        bridge.running = False
        original_factory = self.window.bridge_factory
        def unavailable_thread(*args, **kwargs):
            replacement = original_factory(*args, **kwargs)
            replacement.fail_resume = True
            return replacement
        self.window.bridge_factory = unavailable_thread
        self.fake_login()
        self.window.model_combo.setCurrentIndex(0)
        self.window.message_edit.setPlainText("Continue at the current stopping point")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        restarted = self.bridges[-1]
        seed = next(call[1] for call in restarted.calls if call[0] == "start_turn")
        self.assertIn("Turn 2 report: squad at K9", seed)
        self.assertIn("Turn 4: the squad moved to L9", seed)
        self.assertIn("Turn 4: squad at L9", seed)
        self.assertNotIn('"text": "Turn 2"', seed)
        self.assertIsNot(restarted, bridge)
        self.finish()

    def test_recovery_preserves_full_archive_when_report_marker_is_missing(self):
        self.window.session.add_message("user", "Turn 1 original move")
        self.window.session.add_message("assistant", "Turn 1 original result")
        self.window.session.save_report = "Imported report with no matching message"
        bridge = self.send("Continue")
        seed = next(call[1] for call in bridge.calls if call[0] == "start_turn")
        self.assertIn("Imported report with no matching message", seed)
        self.assertIn("Turn 1 original move", seed)
        self.assertIn("Turn 1 original result", seed)
        self.finish()

    def test_close_during_first_bridge_start_prevents_authentication_and_inference(self):
        starting, proceed = threading.Event(), threading.Event()
        bridge = FakeBridge(self.root / "fake-runtime")
        def blocked_start():
            starting.set()
            proceed.wait(2)
            bridge.running = True
            return {}
        bridge.start = blocked_start
        self.window.bridge_factory = lambda *args, **kwargs: bridge
        self.signed_in()
        self.window.message_edit.setPlainText("Start game")
        self.window.send_message()
        self.wait_for(starting.is_set)
        self.window.close()
        self.assertTrue(self.window._closing)
        proceed.set()
        self.wait_for(lambda: not self.window.busy and not self.window.commands)
        self.assertFalse(bridge.running)
        self.assertFalse(any(call[0] in {"login", "start_turn"} for call in bridge.calls))

    def test_failed_stop_can_be_retried_and_does_not_mark_normal_reply_interrupted(self):
        bridge = self.send("Start reply")
        with patch.object(bridge, "interrupt", side_effect=RuntimeError("Temporary connection problem")):
            self.window.stop_request()
            self.wait_for(lambda: not self.window.commands)
        self.assertFalse(self.window._stop_requested)
        self.assertTrue(self.window.cancel_button.isEnabled())
        self.finish("Completed normally after the failed stop")
        self.assertEqual(self.window.session.messages[-1].status, "completed")
        self.window.message_edit.setPlainText("Another response")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn))
        self.window.stop_request()
        self.wait_for(lambda: not self.window.busy and not self.window.monitor and not self.window.commands)
        self.assertEqual(self.window.session.messages[-1].status, "interrupted")

    def test_model_output_and_window_metadata_are_plain_text(self):
        self.window.session.add_message("assistant", "<img src='https://example.test/x'>")
        self.window.render_session()
        bubble = next(iter(self.window._message_widgets.values()))
        self.assertEqual(bubble.text.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.window.preview_panel.details.textFormat(), Qt.TextFormat.PlainText)

    def test_new_game_and_reopen_keep_library_selection_and_unchecked_files(self):
        first, second = self.root / "rules.txt", self.root / "scenario.txt"
        first.write_text("RULE_ONE shared reference", encoding="utf-8")
        second.write_text("SCENARIO_TWO private setup", encoding="utf-8")
        self.assertTrue(self.window.import_reference(first))
        self.assertTrue(self.window.import_reference(second))
        selected_id = self.window.session.references[0].reference_id
        self.window._apply_reference_selection([selected_id])
        self.window.new_session()
        self.assertEqual([item.name for item in self.window.session.references], ["rules.txt"])
        self.assertEqual(len(self.window.library.list_references()), 2)
        self.assertEqual(self.window._handle_tool("search_reference_files", {"query": "SCENARIO_TWO"})["matches"], [])
        from vasl_ai_bridge.reference_library import ReferenceLibrary
        self.assertEqual([item.name for item in ReferenceLibrary(self.root).selected_references()], ["rules.txt"])
        bridge = self.send("Read my rules")
        sent = next(call[1] for call in bridge.calls if call[0] == "start_turn")
        self.assertIn("rules.txt", sent)
        self.assertNotIn("scenario.txt", sent)
        self.finish()

    def test_portable_save_opens_when_optional_library_import_fails(self):
        from vasl_ai_bridge.chat_session import Reference, new_id
        saved = ChatSession(save_report="Saved report", references=[Reference(new_id(), "rules.txt", "text/plain", b"A1.1 saved rule")])
        path = self.window.store.save(saved, self.root / "saved.aslgame")
        with patch.object(self.window.library, "adopt_references", side_effect=StorageError("Library is unavailable")):
            self.assertTrue(self.window.load_checkpoint(path))
        result = self.window._handle_tool("read_reference_file", {"reference_id": saved.references[0].reference_id})
        self.assertIn("saved rule", result["text"])
        self.assertIn("remain usable", self.window.status.text())

    def test_hotkey_freezes_pixels_before_selection_and_never_sends(self):
        from PySide6.QtWidgets import QDialog
        from vasl_ai_bridge.screenshot_selection import crop_observation
        events = []
        original = screenshot("blue")
        cropped = crop_observation(original, (3, 4, 20, 22))
        window = self.window
        class Selection:
            def __init__(self, observation, parent):
                events.append("selection")
                self.selected_observation = cropped
                self.assert_frozen = observation is original
            def exec(self):
                return QDialog.DialogCode.Accepted
        window.selection_factory = Selection
        window.source_combo.addItem("Board", WindowSource(123, "Board"))
        window.source_combo.setCurrentIndex(1)
        def capture(*args, **kwargs):
            events.append("capture")
            self.assertEqual(kwargs["max_dimension"], 8192)
            return original
        with patch("vasl_ai_bridge.app.capture_visible_window", side_effect=capture), patch.object(window, "activateWindow", side_effect=lambda: events.append("activate")) as activation:
            window.capture_from_hotkey()
        self.assertEqual(events, ["capture", "activate", "selection"])
        activation.assert_called_once_with()
        self.assertEqual(window.attachments, [cropped])
        self.assertEqual(cropped.captured_at, original.captured_at)
        self.assertEqual(self.bridges, [])
        bridge = self.send("Check LOS in this frozen image")
        call = next(call for call in bridge.calls if call[0] == "start_turn")
        self.assertEqual(call[2], [cropped.data_url])
        self.finish()

    def test_cancelled_selection_preserves_existing_attachment(self):
        from PySide6.QtWidgets import QDialog
        original = screenshot()
        self.window.preview = original
        self.window.attach_preview()
        class CancelSelection:
            selected_observation = None
            def __init__(self, observation, parent):
                pass
            def exec(self):
                return QDialog.DialogCode.Rejected
        self.window.selection_factory = CancelSelection
        self.window.select_preview()
        self.assertEqual(self.window.preview, original)
        self.assertEqual(self.window.attachments, [original])
        self.assertEqual(self.bridges, [])

    def test_login_completion_returns_window_and_account_loss_disables_send(self):
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True), patch.object(self.window, "_return_after_login") as returned:
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor))
            self.assertIn("Waiting", self.window.connection_label.text())
            self.bridges[-1].events.put({"method": "account/login/completed", "params": {
                "loginId": "login-1", "success": True}})
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
        returned.assert_called_once()
        self.window.message_edit.setPlainText("My retained draft")
        self.assertFalse(self.window.send_button.isEnabled())
        self.window.model_combo.setCurrentIndex(0)
        self.assertTrue(self.window.send_button.isEnabled())
        self.bridges[-1].events.put({"method": "account/updated", "params": {"authMode": None}})
        self.window._check_connection()
        self.assertFalse(self.window.send_button.isEnabled())
        self.assertIn("Not signed in", self.window.connection_label.text())
        self.assertEqual(self.window.message_edit.toPlainText(), "My retained draft")

    def test_account_button_changes_to_logout_and_preserves_game_when_used(self):
        self.assertEqual(self.window.login_button.text(), "Sign in with ChatGPT")
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True) as browser, patch.object(self.window, "_return_after_login"):
            self.window.login_button.click()
            self.wait_for(lambda: bool(self.window.monitor) and not self.window.commands)
            self.assertEqual(self.window.login_button.text(), "Cancel sign-in")
            self.assertTrue(self.window.login_button.isEnabled())
            bridge = self.bridges[-1]
            bridge.events.put({"method": "account/login/completed", "params": {
                "loginId": "login-1", "success": True}})
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
            self.assertEqual(self.window.login_button.text(), "Sign out")
            self.assertTrue(self.window.login_button.isEnabled())
            self.window.sign_in()  # A signed-in account must not start another browser flow.
            self.assertEqual(sum(call[0] == "login" for call in bridge.calls), 1)
            browser.assert_called_once()
        message = self.window.session.add_message("user", "Retain this conversation")
        attachment = screenshot()
        self.window.attachments = [attachment]
        self.window.message_edit.setPlainText("My unsent draft")
        self.window.login_button.click()
        self.assertFalse(self.window.send_button.isEnabled())
        self.wait_for(lambda: not self.window.commands)
        self.assertEqual(bridge.calls[-1], ("logout",))
        self.assertEqual(self.window.login_button.text(), "Sign in with ChatGPT")
        self.assertTrue(self.window.login_button.isEnabled())
        self.assertFalse(self.window.send_button.isEnabled())
        self.assertEqual(self.window.session.messages, [message])
        self.assertEqual(self.window.attachments, [attachment])
        self.assertEqual(self.window.message_edit.toPlainText(), "My unsent draft")
        self.fake_login()
        self.assertEqual(self.window.login_button.text(), "Sign out")

    def test_failed_logout_retains_signed_in_action_and_reports_failure(self):
        self.fake_login()
        bridge = self.bridges[-1]
        bridge.fail_logout = True
        self.window.login_button.click()
        self.wait_for(lambda: not self.window.commands)
        self.assertEqual(self.window._auth_state, "signed_in")
        self.assertEqual(self.window.login_button.text(), "Sign out")
        self.assertTrue(self.window.login_button.isEnabled())
        self.assertIn("Could not sign out", self.window.status.text())

    def test_failed_or_unrelated_login_never_returns_window(self):
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True), patch.object(self.window, "_return_after_login") as returned:
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor))
            self.window._handle_event({"method": "account/login/completed", "params": {
                "loginId": "another-login", "success": True}})
            self.assertEqual(self.window._auth_state, "waiting")
            returned.assert_not_called()
            self.bridges[-1].events.put({"method": "account/login/completed", "params": {
                "loginId": "login-1", "success": False, "error": "Sign-in cancelled"}})
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
            returned.assert_not_called()
        self.assertEqual(self.window._auth_state, "signed_out")
        self.assertEqual(self.window.login_button.text(), "Sign in with ChatGPT")
        self.assertTrue(self.window.login_button.isEnabled())

    def test_abandoned_browser_login_can_be_cancelled_and_retried_without_losing_game(self):
        message = self.window.session.add_message("user", "Retain this game while I sign in")
        attachment = screenshot()
        self.window.attachments = [attachment]
        self.window.message_edit.setPlainText("My unsent draft")
        session_id = self.window.session.session_id
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True) as browser, patch.object(self.window, "_return_after_login") as returned:
            self.window.login_button.click()
            self.wait_for(lambda: bool(self.window.monitor) and not self.window.commands)
            old_bridge, old_login = self.bridges[-1], self.window.monitor.login_id
            # No completion event is delivered: closing the browser leaves the flow pending.
            self.assertEqual(self.window.login_button.text(), "Cancel sign-in")
            self.assertTrue(self.window.login_button.isEnabled())
            self.window.login_button.click()
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
            self.assertIn(("cancel_login", old_login), old_bridge.calls)
            self.assertFalse(old_bridge.running)
            self.assertEqual(self.window._auth_state, "signed_out")
            self.assertEqual(self.window.login_button.text(), "Sign in with ChatGPT")
            self.assertTrue(self.window.login_button.isEnabled())
            returned.assert_not_called()
            self.window.login_button.click()
            self.wait_for(lambda: bool(self.window.monitor) and not self.window.commands)
            new_bridge, new_login = self.bridges[-1], self.window.monitor.login_id
            self.assertIsNot(new_bridge, old_bridge)
            self.assertNotEqual(new_login, old_login)
            new_bridge.events.put({"method": "account/login/completed", "params": {
                "loginId": new_login, "success": True}})
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
            self.assertEqual(browser.call_count, 2)
            returned.assert_called_once()
        self.assertEqual(self.window._auth_state, "signed_in")
        self.assertEqual(self.window.login_button.text(), "Sign out")
        self.assertEqual(self.window.session.session_id, session_id)
        self.assertEqual(self.window.session.messages, [message])
        self.assertEqual(self.window.attachments, [attachment])
        self.assertEqual(self.window.message_edit.toPlainText(), "My unsent draft")

    def test_browser_open_failure_cancels_runtime_login_and_allows_retry(self):
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=False):
            self.window.sign_in()
            self.wait_for(lambda: bool(self.bridges) and not self.window.commands and not self.window.monitor)
        bridge = self.bridges[-1]
        self.assertIn(("cancel_login", "login-1"), bridge.calls)
        self.assertFalse(bridge.running)
        self.assertEqual(self.window._auth_state, "signed_out")
        self.assertTrue(self.window.login_button.isEnabled())
        self.assertIn("browser", self.window.status.text().lower())
        self.fake_login()
        self.assertEqual(self.window._auth_state, "signed_in")

    def test_login_timeout_cancels_runtime_login_and_recovers_without_restart(self):
        with patch("vasl_ai_bridge.app.LOGIN_TIMEOUT_SECONDS", .01), patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True), patch.object(self.window, "_return_after_login") as returned:
            self.window.sign_in()
            self.wait_for(lambda: bool(self.bridges) and not self.window.commands and not self.window.monitor)
            returned.assert_not_called()
        bridge = self.bridges[-1]
        self.assertIn(("cancel_login", "login-1"), bridge.calls)
        self.assertFalse(bridge.running)
        self.assertEqual(self.window._auth_state, "signed_out")
        self.assertTrue(self.window.login_button.isEnabled())
        self.assertIn("timed out", self.window.status.text().lower())
        self.fake_login()
        self.assertEqual(self.window._auth_state, "signed_in")

    def test_failed_login_cancel_retires_connection_and_next_login_still_works(self):
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True):
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor) and not self.window.commands)
            bridge, login_id = self.bridges[-1], self.window.monitor.login_id
            bridge.fail_cancel_login = True
            self.window.login_button.click()
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
        self.assertIn(("cancel_login", login_id), bridge.calls)
        self.assertFalse(bridge.running)
        self.assertEqual(self.window._auth_state, "signed_out")
        self.assertTrue(self.window.login_button.isEnabled())
        self.fake_login()
        self.assertIsNot(self.bridges[-1], bridge)
        self.assertEqual(self.window._auth_state, "signed_in")

    def test_login_retry_waits_for_pending_cancel_cleanup(self):
        started, release = threading.Event(), threading.Event()
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True):
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor) and not self.window.commands)
            bridge = self.bridges[-1]
            cancel = bridge.cancel_login
            def delayed_cancel(login_id):
                started.set()
                if not release.wait(3):
                    raise RuntimeError("Test cancellation gate timed out")
                return cancel(login_id)
            try:
                with patch.object(bridge, "cancel_login", side_effect=delayed_cancel):
                    self.window.login_button.click()
                    self.wait_for(started.is_set)
                    self.assertFalse(self.window.login_button.isEnabled())
                    self.window.sign_in()
                    self.assertEqual(len(self.bridges), 1)
                    self.assertEqual(sum(call[0] == "login" for call in bridge.calls), 1)
                    # Processing UI events above succeeds while the RPC is pending.
                    release.set()
                    self.wait_for(lambda: not self.window.monitor and not self.window.commands)
            finally:
                release.set()
        self.assertFalse(bridge.running)
        self.assertTrue(self.window.login_button.isEnabled())
        self.fake_login()
        self.assertEqual(self.window._auth_state, "signed_in")

    def test_cancelled_login_late_events_cannot_sign_in_or_disrupt_new_attempt(self):
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True), patch.object(self.window, "_return_after_login") as returned:
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor) and not self.window.commands)
            old_worker, old_bridge = self.window.monitor, self.bridges[-1]
            old_login = old_worker.login_id
            self.window.cancel_sign_in()
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
            old_completion = {"method": "account/login/completed", "params": {"loginId": old_login, "success": True}}
            old_account = {"method": "account/updated", "params": {"authMode": "chatgpt"}}
            self.window._monitor_event(old_worker, old_completion)
            self.window._monitor_event(old_worker, old_account)
            self.assertEqual(self.window._auth_state, "signed_out")
            returned.assert_not_called()
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor) and not self.window.commands)
            new_worker, new_bridge = self.window.monitor, self.bridges[-1]
            self.assertIsNot(new_bridge, old_bridge)
            self.assertNotEqual(new_worker.login_id, old_login)
            # The old runtime's queue and already-queued GUI callbacks must stay isolated.
            old_bridge.events.put(old_completion)
            old_bridge.events.put(old_account)
            self.window._monitor_event(old_worker, old_completion)
            self.window._monitor_event(old_worker, old_account)
            self.window._monitor_event(old_worker, {"method": "bridge/closed", "params": {}})
            self.assertIs(self.window.monitor, new_worker)
            self.assertEqual(self.window._auth_state, "waiting")
            returned.assert_not_called()
            new_bridge.events.put({"method": "account/login/completed", "params": {
                "loginId": new_worker.login_id, "success": True}})
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
            returned.assert_called_once()
            self.assertEqual(self.window._auth_state, "signed_in")

    def test_old_monitor_queued_failure_does_not_reset_new_login(self):
        with patch("vasl_ai_bridge.app.QDesktopServices.openUrl", return_value=True), patch.object(self.window, "_return_after_login"):
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor) and not self.window.commands)
            old_worker = self.window.monitor
            self.window.cancel_sign_in()
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
            self.window.sign_in()
            self.wait_for(lambda: bool(self.window.monitor) and not self.window.commands)
            new_worker = self.window.monitor
            status = self.window.status.text()
            self.window._monitor_error(old_worker, "An old login timed out")
            self.assertIs(self.window.monitor, new_worker)
            self.assertEqual(self.window._auth_state, "waiting")
            self.assertEqual(self.window.status.text(), status)
            self.bridges[-1].events.put({"method": "account/login/completed", "params": {
                "loginId": new_worker.login_id, "success": True}})
            self.wait_for(lambda: not self.window.monitor and not self.window.commands)
        self.assertEqual(self.window._auth_state, "signed_in")

    def test_login_return_targets_own_window_and_stops_on_account_loss(self):
        self.signed_in()
        with patch("vasl_ai_bridge.app.QApplication.platformName", return_value="windows"), patch("vasl_ai_bridge.app.bring_window_to_front", return_value=True) as activate:
            self.window._return_after_login()
            activate.assert_called_once_with(int(self.window.winId()))
            self.assertTrue(self.window.isVisible())
            self.assertFalse(self.window.login_return_timer.isActive())
        with patch("vasl_ai_bridge.app.QApplication.platformName", return_value="windows"), patch("vasl_ai_bridge.app.bring_window_to_front", return_value=False):
            self.window._return_after_login()
            self.assertTrue(self.window.login_return_timer.isActive())
            self.window._set_auth_state("signed_out")
            self.assertFalse(self.window.login_return_timer.isActive())

    def test_denied_window_return_is_bounded_and_keeps_confirmed_login(self):
        self.signed_in()
        with patch("vasl_ai_bridge.app.QApplication.platformName", return_value="windows"), patch("vasl_ai_bridge.app.bring_window_to_front", return_value=False) as activate, patch("vasl_ai_bridge.app.QApplication.alert") as alert:
            self.window._return_after_login()
            self.wait_for(lambda: self.window._login_return_attempts == 3)
            self.assertEqual(activate.call_count, 3)
            alert.assert_called_once_with(self.window)
            self.assertFalse(self.window.login_return_timer.isActive())
        self.assertEqual(self.window._auth_state, "signed_in")
        self.assertEqual(self.window.login_button.text(), "Sign out")
        self.assertIn("VASL AI Bridge taskbar", self.window.status.text())

    def test_shortcut_is_persisted_only_after_successful_registration(self):
        from vasl_ai_bridge.hotkey import HotkeyStatus
        class Hotkey:
            def replace(self, sequence):
                return HotkeyStatus(sequence != "Ctrl+Alt+K", "Already in use", sequence)
            def unregister(self):
                pass
        self.window.capture_hotkey = Hotkey()
        self.assertTrue(self.window.set_capture_shortcut("Ctrl+Alt+S"))
        self.assertFalse(self.window.set_capture_shortcut("Ctrl+Alt+K"))
        self.assertEqual(self.window.settings.value("capture_shortcut"), "Ctrl+Alt+S")

    def test_normal_play_records_memory_before_turn_started_and_new_model_reads_prior_game(self):
        def factory(*args, **kwargs):
            bridge = RecordingBridge(*args, **kwargs)
            self.bridges.append(bridge)
            return bridge
        self.window.bridge_factory = factory
        bridge = self.send("RESULT: A squad was pinned crossing the road.")
        saved = next(call[1] for call in bridge.calls if call[0] == "memory_saved")
        self.assertTrue(saved["saved"])
        self.assertEqual(len(saved["lesson_ids"]), 1)
        self.finish("Take the covered route.")
        original_game = self.window.session.session_id
        original_folder = self.window.game_folder
        self.window.new_session()
        self.assertNotEqual(self.window.session.session_id, original_game)
        self.window.model_combo.addItem("Another model", "test-model")
        self.window.model_combo.setCurrentIndex(self.window.model_combo.findData("test-model"))
        self.window.message_edit.setPlainText("What did earlier games teach about the road?")
        self.window.send_message()
        self.wait_for(lambda: bool(self.window._active_turn) and not self.window.commands)
        matches = [call[1] for call in bridge.calls if call[0] == "experience_read"][-1]
        self.assertIn("Road exposure", str(matches))
        self.finish("Prior experience is a candidate, not a rule.")
        self.assertTrue((original_folder / ".vasl-ai-bridge" / "memory" / (original_game + ".json")).is_file())
        self.assertFalse((self.root / "sessions").exists())
        self.assertEqual(self.window.memory.pending_message_ids(self.window.session, {"game_folder": self.window.game_folder}), [])

    def test_review_midgame_preserves_draft_and_can_continue_after_durable_review(self):
        def factory(*args, **kwargs):
            bridge = RecordingBridge(*args, **kwargs)
            self.bridges.append(bridge)
            return bridge
        self.window.bridge_factory = factory
        self.send("Deployment on turn one")
        self.finish("Initial plan")
        game_id = self.window.session.session_id
        self.window.message_edit.setPlainText("Unsent next move")
        self.window.attachments = [screenshot("red")]
        self.window.review_game()
        self.wait_for(lambda: bool(self.window._active_turn) and not self.window.commands)
        self.assertEqual(self.window.message_edit.toPlainText(), "Unsent next move")
        self.assertEqual(len(self.window.attachments), 1)
        review = next(call[1] for call in self.bridges[-1].calls if call[0] == "review_saved")
        self.assertFalse(review["game_ended"])
        self.finish("So far, the outcome is unknown.")
        self.assertEqual(self.window.session.save_report, "")
        self.send("Continue the same game")
        self.finish("Next action")
        self.assertEqual(self.window.session.session_id, game_id)

    def test_new_game_cancel_keeps_current_folder_messages_and_draft(self):
        self.send("Original game")
        self.finish()
        original, folder = self.window.session, self.window.game_folder
        self.window.message_edit.setPlainText("Keep this draft")
        self.new_folder_dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
        self.window.new_session()
        self.assertIs(self.window.session, original)
        self.assertEqual(self.window.game_folder, folder)
        self.assertEqual(self.window.message_edit.toPlainText(), "Keep this draft")

    def test_named_point_overwrite_requires_confirmation_without_path_dialog(self):
        self.send("Turn one")
        self.finish("First report")
        self.window.session.save_report = "First report"
        self.window._report_ready = True
        folder = self.window.game_folder
        with patch("vasl_ai_bridge.app.SavePointDialog") as dialog:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog.return_value.point_name.return_value = "Turn one"
            self.window.save_session()
            original = self.window.folders.list_points(folder)[0]
            self.window.session.save_report = "Changed report"
            self.window._report_ready = True
            with patch("vasl_ai_bridge.app.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                self.assertIsNone(self.window.save_session())
            self.assertEqual(self.window.folders.load_point(folder, original["point_id"]).save_report, "First report")
            with patch("vasl_ai_bridge.app.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                self.assertEqual(self.window.save_session(), folder)
        self.assertEqual(self.window.folders.load_point(folder, original["point_id"]).save_report, "Changed report")

    def test_clear_screenshots_reloads_live_state_and_autosave_cannot_restore_images(self):
        image = screenshot()
        self.window.attachments = [image]
        self.send("First board")
        self.finish("Plan")
        folder = self.window.game_folder
        self.window.folders.save_point(folder, self.window.session, "Before cleanup")
        self.assertEqual(self.window.folders.clear_screenshots(folder), 1)
        self.window._screenshots_cleared(str(folder))
        self.assertEqual(self.window.session.messages[0].attachments, [])
        self.assertTrue(self.window._autosave())
        result = self.window._handle_tool("read_game_image", {"observation_id": image.observation_id})
        self.assertIn("deleted", str(result).lower())
        self.assertEqual(self.window.folders.describe(folder)["screenshots"], 0)
        self.assertTrue(self.window._needs_resume)

    def test_early_folder_checkpoint_retains_later_lessons_but_not_later_chat(self):
        def factory(*args, **kwargs):
            bridge = RecordingBridge(*args, **kwargs)
            self.bridges.append(bridge)
            return bridge
        self.window.bridge_factory = factory
        self.send("Turn one deployment")
        self.finish("Use cover")
        folder = self.window.game_folder
        point = self.window.folders.save_point(folder, self.window.session, "Early")
        self.send("RESULT: Turn five road crossing pinned the squad.")
        self.finish("Later evidence")
        self.assertTrue(self.window.load_checkpoint(folder, point_id=point["point_id"]))
        self.assertNotIn("Turn five", str(self.window._handle_tool("read_game_history", {})))
        lessons = self.window._handle_tool("search_experience", {"query": "road"})
        self.assertIn("Road exposure", str(lessons))
        self.assertEqual(self.window.session.backend_thread_id, "")

    def test_missing_memory_write_is_visible_and_close_does_not_start_inference(self):
        bridge = self.send("A reported event to remember")
        self.finish("Ordinary fake reply without memory callback")
        self.assertIn("waiting", self.window.memory_label.text())
        before = len([call for call in bridge.calls if call[0] == "start_turn"])
        self.window.close()
        self.wait_for(lambda: not self.window.commands)
        self.assertEqual(len([call for call in bridge.calls if call[0] == "start_turn"]), before)

    def test_failed_local_save_does_not_start_model_turn(self):
        self.signed_in()
        self.window.message_edit.setPlainText("Preserve this request")
        with patch.object(self.window.folders, "save_current", side_effect=StorageError("Disk full")):
            self.window.send_message()
        self.assertEqual(self.bridges, [])
        self.assertIn("Disk full", self.window.status.text())


if __name__ == "__main__":
    unittest.main()
