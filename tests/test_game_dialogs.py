"""Offline player selections and confirmation boundaries for folder saves."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox

from vasl_ai_bridge.game_dialogs import (
    CLEAR_SCREENSHOTS_WARNING, DELETE_GAME_WARNING, EXPERIENCE_HINT, NewGameDialog,
    OpenGameDialog, SavePointDialog, StorageManagerDialog, format_size,
)


APP = QApplication.instance() or QApplication(["Game storage dialog tests"])


class FakeFolderStore:
    def __init__(self, folder):
        self.folder = folder
        self.records = [{"folder": str(folder), "name": "Vierville", "available": True}]
        self.points = [{"point_id": "point-1", "name": "Turn one"}, {"point_id": "point-2", "name": "Turn two"}]
        self.deleted_points = []
        self.cleared = []
        self.deleted_games = []
        self.fail_description = False
        self.fail_mutations = False

    def list_known_folders(self):
        return list(self.records)

    def describe(self, folder):
        if self.fail_description:
            raise ValueError("Game folder is no longer available")
        return {"folder": str(folder), "name": "Vierville", "bytes": 2097152, "screenshots": 4}

    def list_points(self, folder):
        return list(self.points)

    def delete_point(self, folder, point_id):
        if self.fail_mutations:
            raise OSError("The folder is read-only")
        self.deleted_points.append((folder, point_id))
        self.points = [point for point in self.points if point["point_id"] != point_id]

    def clear_screenshots(self, folder):
        if self.fail_mutations:
            raise OSError("The folder is read-only")
        self.cleared.append(folder)
        return 4

    def delete_folder(self, folder):
        if self.fail_mutations:
            raise OSError("The folder is read-only")
        self.deleted_games.append(folder)
        self.records = [record for record in self.records if Path(record["folder"]) != folder]


class GameDialogTests(unittest.TestCase):
    def setUp(self):
        self.test_root = Path(__file__).resolve().parents[1] / "runtime" / "game-dialog-tests"
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="asl-game-dialog-", dir=self.test_root)
        self.root = Path(self.temporary.name)
        self.store = FakeFolderStore(self.root / "game")
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        APP.processEvents()
        self.assertTrue(self.root.resolve().is_relative_to(self.test_root.resolve()))
        self.temporary.cleanup()

    def dialog(self, instance):
        self.dialogs.append(instance)
        return instance

    def test_new_game_accepts_location_name_without_creating_folder(self):
        dialog = self.dialog(NewGameDialog(default_parent=self.root))
        dialog.name_edit.setText("A new game")
        dialog.accept()
        self.assertEqual(QDialog.Accepted, dialog.result())
        self.assertEqual((self.root, "A new game"), dialog.selection())
        self.assertEqual([], list(self.root.iterdir()))

    def test_cancel_new_game_leaves_parent_and_existing_game_alone(self):
        existing = self.root / "Existing game"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("existing", encoding="utf-8")
        dialog = self.dialog(NewGameDialog(default_parent=self.root))
        dialog.name_edit.setText("New game")
        dialog.reject()
        self.assertEqual(QDialog.Rejected, dialog.result())
        self.assertEqual([existing], list(self.root.iterdir()))
        self.assertEqual("existing", marker.read_text(encoding="utf-8"))

    def test_new_game_rejects_reserved_names_traversal_and_existing_folder(self):
        (self.root / "Existing").mkdir()
        for name in ("", "..", "../escaped", "CON", "NUL.txt", "bad:name", "Existing"):
            with self.subTest(name=name):
                dialog = self.dialog(NewGameDialog(default_parent=self.root))
                dialog.name_edit.setText(name)
                dialog.accept()
                self.assertEqual(QDialog.Rejected, dialog.result())
                self.assertTrue(dialog.error_label.text())

    def test_new_game_rejects_missing_location(self):
        dialog = self.dialog(NewGameDialog(default_parent=self.root / "missing"))
        dialog.name_edit.setText("New game")
        dialog.accept()
        self.assertEqual(QDialog.Rejected, dialog.result())

    def test_save_point_selects_existing_or_custom_name_without_writes(self):
        dialog = self.dialog(SavePointDialog(self.store.points))
        self.assertEqual("", dialog.point_name())
        dialog.accept()
        self.assertEqual(QDialog.Rejected, dialog.result())
        dialog.name_combo.setCurrentIndex(1)
        self.assertEqual("Turn two", dialog.point_name())
        dialog.name_combo.setEditText("  Turn three  ")
        dialog.accept()
        self.assertEqual(QDialog.Accepted, dialog.result())
        self.assertEqual("Turn three", dialog.point_name())
        self.assertEqual([], self.store.deleted_points)

    def test_open_current_or_named_point_uses_canonical_storage_ids(self):
        dialog = self.dialog(OpenGameDialog(self.store))
        self.assertEqual((self.store.folder, None), dialog.selection())
        self.assertEqual("Current progress", dialog.point_combo.currentText())
        dialog.point_combo.setCurrentIndex(2)
        self.assertEqual((self.store.folder, "point-2"), dialog.selection())
        self.assertEqual(EXPERIENCE_HINT, dialog.point_combo.toolTip())
        dialog.accept()
        self.assertEqual(QDialog.Accepted, dialog.result())

    def test_open_missing_folder_stays_disabled_and_explains_failure(self):
        self.store.fail_description = True
        dialog = self.dialog(OpenGameDialog(self.store))
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.Open).isEnabled())
        self.assertIn("no longer available", dialog.error_label.text())
        with self.assertRaises(ValueError):
            dialog.selection()
        dialog.accept()
        self.assertEqual(QDialog.Rejected, dialog.result())

    def test_open_without_registered_games_can_browse_and_cancel_is_safe(self):
        self.store.records = []
        dialog = self.dialog(OpenGameDialog(self.store))
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.Open).isEnabled())
        with patch("vasl_ai_bridge.game_dialogs.QFileDialog.getExistingDirectory", return_value=""):
            dialog._browse()
        self.assertEqual(0, dialog.game_combo.count())
        with patch("vasl_ai_bridge.game_dialogs.QFileDialog.getExistingDirectory", return_value=str(self.store.folder)):
            dialog._browse()
        self.assertEqual((self.store.folder, None), dialog.selection())

    def test_corrupt_game_database_is_an_explained_open_failure(self):
        with patch.object(self.store, "describe", side_effect=sqlite3.DatabaseError("Invalid database")):
            dialog = self.dialog(OpenGameDialog(self.store))
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.Open).isEnabled())
        self.assertIn("Invalid database", dialog.error_label.text())

    def test_storage_usage_and_delete_point_require_confirmation(self):
        dialog = self.dialog(StorageManagerDialog(self.store, self.store.folder))
        self.assertEqual("Disk space used: 2.0 MB", dialog.usage_label.text())
        self.assertFalse(dialog.delete_point_button.isEnabled())
        dialog.point_list.setCurrentRow(0)
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question", return_value=QMessageBox.Cancel):
            dialog._delete_point()
        self.assertEqual([], self.store.deleted_points)
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question", return_value=QMessageBox.Yes):
            dialog._delete_point()
        self.assertEqual([(self.store.folder, "point-1")], self.store.deleted_points)
        self.assertEqual(1, dialog.point_list.count())

    def test_clear_all_images_warns_about_every_point_and_emits_after_success(self):
        dialog = self.dialog(StorageManagerDialog(self.store))
        cleared_signals = []
        dialog.screenshotsCleared.connect(cleared_signals.append)
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question", return_value=QMessageBox.Cancel):
            dialog._clear_screenshots()
        self.assertEqual([], self.store.cleared)
        self.assertEqual([], cleared_signals)
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question", return_value=QMessageBox.Yes) as question:
            dialog._clear_screenshots()
        self.assertEqual(CLEAR_SCREENSHOTS_WARNING, question.call_args.args[2])
        self.assertIn("All save points", CLEAR_SCREENSHOTS_WARNING)
        self.assertIn("reference files will remain", CLEAR_SCREENSHOTS_WARNING)
        self.assertEqual([self.store.folder], self.store.cleared)
        self.assertEqual([str(self.store.folder)], cleared_signals)

    def test_failed_delete_explains_failure_without_claiming_success(self):
        self.store.fail_mutations = True
        dialog = self.dialog(StorageManagerDialog(self.store))
        cleared_signals = []
        dialog.screenshotsCleared.connect(cleared_signals.append)
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question", return_value=QMessageBox.Yes), patch("vasl_ai_bridge.game_dialogs.QMessageBox.warning") as warning:
            dialog._clear_screenshots()
        self.assertIn("read-only", warning.call_args.args[2])
        self.assertEqual([], cleared_signals)
        self.assertEqual([], self.store.cleared)

    def test_storage_without_games_disables_destructive_actions(self):
        self.store.records = []
        dialog = self.dialog(StorageManagerDialog(self.store))
        self.assertFalse(dialog.delete_point_button.isEnabled())
        self.assertFalse(dialog.clear_screenshots_button.isEnabled())
        self.assertFalse(dialog.delete_game_button.isEnabled())

    def test_active_game_cannot_be_deleted_even_with_equivalent_path(self):
        dialog = self.dialog(StorageManagerDialog(self.store, self.root / "other" / ".." / "game"))
        self.assertFalse(dialog.delete_game_button.isEnabled())
        self.assertIn("Open another game", dialog.delete_game_button.toolTip())
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question") as question:
            dialog._delete_game()
        question.assert_not_called()
        self.assertEqual([], self.store.deleted_games)

    def test_whole_game_delete_can_be_cancelled_and_explains_preserved_data(self):
        dialog = self.dialog(StorageManagerDialog(self.store))
        self.assertTrue(dialog.delete_game_button.isEnabled())
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question", return_value=QMessageBox.Cancel) as question:
            dialog._delete_game()
        self.assertEqual(DELETE_GAME_WARNING, question.call_args.args[2])
        self.assertIn("all save points", DELETE_GAME_WARNING)
        self.assertIn("shared experience library", DELETE_GAME_WARNING)
        self.assertIn("unrelated files", DELETE_GAME_WARNING)
        self.assertEqual([], self.store.deleted_games)
        self.assertEqual(1, dialog.game_combo.count())

    def test_whole_game_delete_updates_list_and_emits_only_after_success(self):
        dialog = self.dialog(StorageManagerDialog(self.store))
        deleted_signals = []
        dialog.gameDeleted.connect(deleted_signals.append)
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question", return_value=QMessageBox.Yes):
            dialog._delete_game()
        self.assertEqual([self.store.folder], self.store.deleted_games)
        self.assertEqual([str(self.store.folder)], deleted_signals)
        self.assertEqual(0, dialog.game_combo.count())
        self.assertEqual(0, dialog.point_list.count())
        self.assertFalse(dialog.delete_game_button.isEnabled())
        self.assertFalse(dialog.clear_screenshots_button.isEnabled())
        self.assertIn("No game folder", dialog.usage_label.text())

    def test_failed_whole_game_delete_keeps_selection_and_does_not_emit(self):
        self.store.fail_mutations = True
        dialog = self.dialog(StorageManagerDialog(self.store))
        deleted_signals = []
        dialog.gameDeleted.connect(deleted_signals.append)
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question", return_value=QMessageBox.Yes), patch("vasl_ai_bridge.game_dialogs.QMessageBox.warning") as warning:
            dialog._delete_game()
        self.assertIn("read-only", warning.call_args.args[2])
        self.assertEqual([], deleted_signals)
        self.assertEqual([], self.store.deleted_games)
        self.assertEqual(1, dialog.game_combo.count())
        self.assertTrue(dialog.delete_game_button.isEnabled())

    def test_whole_game_delete_selects_another_remaining_game(self):
        second = self.root / "Other game"
        self.store.records.append({"folder": str(second), "name": "Other game"})
        dialog = self.dialog(StorageManagerDialog(self.store, active_folder=second))
        self.assertFalse(dialog.delete_game_button.isEnabled())
        dialog.game_combo.setCurrentIndex(0)
        with patch("vasl_ai_bridge.game_dialogs.QMessageBox.question", return_value=QMessageBox.Yes):
            dialog._delete_game()
        self.assertEqual(1, dialog.game_combo.count())
        self.assertEqual(second, dialog._folder)
        self.assertFalse(dialog.delete_game_button.isEnabled())

    def test_readable_sizes(self):
        self.assertEqual("0 bytes", format_size(-1))
        self.assertEqual("9 bytes", format_size(9))
        self.assertEqual("1.0 KB", format_size(1024))
        self.assertEqual("2.0 GB", format_size(2 * 1024 ** 3))


if __name__ == "__main__":
    unittest.main()
