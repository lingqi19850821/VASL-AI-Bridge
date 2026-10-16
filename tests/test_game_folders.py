"""Game-folder snapshots share evidence without leaking later events on rewind."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from vasl_ai_bridge.chat_session import ChatSession, Reference
from vasl_ai_bridge.chat_storage import ChatStore
from vasl_ai_bridge.game_folders import DELETED_IMAGES_KEY, MANAGED_DIRECTORY, GameFolderStore
from vasl_ai_bridge.session import Observation
from vasl_ai_bridge.storage import StorageError


def image(identifier="board-1", pixel=b"\x10\x20\x30"):
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00" + pixel)) + chunk(b"IEND", b""))
    return Observation(identifier, "2026-10-14T15:00:00+00:00", "Board", png, 1, 1)


class GameFolderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="asl-ai-game-folder-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = GameFolderStore(ChatStore(self.root / "application-data"))
        self.session = ChatSession(title="Vierville")
        self.session.add_message("user", "Initial deployment", attachments=[image()])
        self.session.references.append(Reference("rules", "rules.md", "text/markdown", b"Movement rules"))
        self.folder = self.store.create_folder(self.root, "Vierville", self.session)

    def connect(self):
        return closing(sqlite3.connect(self.store.database_path(self.folder)))

    def test_points_and_current_share_only_one_database_and_original_blobs(self):
        first = self.store.save_point(self.folder, self.session, "Turn 1")
        self.session.add_message("user", "Same original board", attachments=[image("board-2")])
        second = self.store.save_point(self.folder, self.session, "Turn 2")
        self.assertNotEqual(first["point_id"], second["point_id"])
        self.assertEqual(len(list(self.folder.rglob("*.aslgame"))), 1)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM blobs").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM images").fetchone()[0], 2)
        self.assertEqual(len(self.store.list_points(self.folder)), 2)

    def test_checkpoint_has_exact_messages_text_reference_selection_and_no_future(self):
        first = self.store.save_point(self.folder, self.session, "Deployment")
        self.session.messages[0].text = "Corrected deployment"
        self.session.references = []
        self.session.add_message("assistant", "The bridge attack succeeded")
        self.store.save_current(self.folder, self.session)
        restored = self.store.load_point(self.folder, first["point_id"])
        self.assertEqual([item.text for item in restored.messages], ["Initial deployment"])
        self.assertEqual([item.reference_id for item in restored.references], ["rules"])
        current = self.store.load_point(self.folder)
        self.assertEqual(len(current.messages), 2)
        self.assertEqual(current.references, [])

    def test_overwrite_and_delete_keep_full_review_originals_and_other_point(self):
        first = self.store.save_point(self.folder, self.session, "Turn 1")
        self.session.add_message("assistant", "Original continuation before rewind")
        second = self.store.save_point(self.folder, self.session, "Turn 2")
        earlier = self.store.load_point(self.folder, first["point_id"])
        earlier.add_message("assistant", "Alternative continuation")
        self.store.save_point(self.folder, earlier, "Turn 2", overwrite=True)
        self.store.delete_point(self.folder, second["point_id"])
        self.assertEqual([point["name"] for point in self.store.list_points(self.folder)], ["Turn 1"])
        with self.assertRaises(StorageError):
            self.store.load_point(self.folder, second["point_id"])
        original = self.store.load_point(self.folder, first["point_id"])
        self.assertEqual(len(original.messages), 1)
        texts = [item.text for item in self.store.load_history(self.folder).messages]
        self.assertIn("Original continuation before rewind", texts)
        self.assertIn("Alternative continuation", texts)

    def test_unsaved_later_continuation_survives_rewind_for_review(self):
        first = self.store.save_point(self.folder, self.session, "Turn 1")
        self.session.add_message("assistant", "Later action without named checkpoint")
        self.store.save_current(self.folder, self.session)
        earlier = self.store.load_point(self.folder, first["point_id"])
        earlier.add_message("assistant", "New branch")
        self.store.save_current(self.folder, earlier)
        self.assertNotIn("Later action without named checkpoint", [item.text for item in self.store.load_point(self.folder).messages])
        self.assertIn("Later action without named checkpoint", [item.text for item in self.store.load_history(self.folder).messages])

    def test_name_collision_requires_explicit_overwrite_and_does_not_save_current(self):
        self.store.save_point(self.folder, self.session, "Turn 1")
        self.session.add_message("assistant", "Not yet saved")
        with self.assertRaises(StorageError):
            self.store.save_point(self.folder, self.session, "TURN 1")
        self.assertEqual(len(self.store.load_point(self.folder).messages), 1)
        self.assertEqual(len(self.store.list_points(self.folder)), 1)

    def test_clear_screenshots_affects_all_points_keeps_refs_and_deleted_source_markers(self):
        self.session.references.append(Reference("notes-image", "Notes.png", "image/png", image().png))
        first = self.store.save_point(self.folder, self.session, "Turn 1")
        self.session.add_message("user", "A new board", attachments=[image("board-2", b"\x40\x50\x60")])
        self.store.save_current(self.folder, self.session)
        self.assertEqual(self.store.clear_screenshots(self.folder), 2)
        restored = self.store.load_point(self.folder, first["point_id"])
        self.assertEqual(restored.messages[0].attachments, [])
        marker = restored.legacy_metadata[DELETED_IMAGES_KEY]["board-1"]
        self.assertEqual(marker["captured_at"], image().captured_at)
        self.assertIn(restored.messages[0].message_id, marker["message_ids"])
        self.assertEqual(restored.references[-1].content, image().png)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM images").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM blobs").fetchone()[0], 2)
        self.assertEqual(self.store.clear_screenshots(self.folder), 0)
        current = self.store.load_point(self.folder)
        current.add_message("user", "New capture after cleanup", attachments=[image("board-3")])
        self.store.save_current(self.folder, current)
        self.assertEqual(self.store.describe(self.folder)["screenshots"], 1)
        self.assertIn("board-1", self.store.load_point(self.folder).legacy_metadata[DELETED_IMAGES_KEY])

    def test_moving_folder_preserves_identity_points_and_relative_evidence(self):
        point = self.store.save_point(self.folder, self.session, "Saved")
        moved = self.root / "Player chosen new name"
        self.assertEqual(self.folder.parent.resolve(), self.root)
        self.assertEqual(moved.parent.resolve(), self.root)
        self.folder.rename(moved)
        with self.assertRaises(StorageError):
            self.store.save_current(self.folder, self.session)
        self.assertFalse(self.folder.exists())
        restored = self.store.load_point(moved, point["point_id"])
        self.assertEqual(restored.session_id, self.session.session_id)
        self.assertEqual(restored.messages[0].attachments[0].png, image().png)
        known = self.store.list_known_folders()
        self.assertEqual(len(known), 1)
        self.assertEqual(known[0]["folder"], str(moved))

    def test_failed_point_save_rolls_back_shared_content_and_checkpoint(self):
        first = self.store.save_point(self.folder, self.session, "Before")
        self.session.add_message("assistant", "Must be rolled back")
        original = self.store._writer._write

        def fail_after_write(*args):
            original(*args)
            raise StorageError("Synthetic write failure")

        with patch.object(self.store._writer, "_write", side_effect=fail_after_write):
            with self.assertRaises(StorageError):
                self.store.save_point(self.folder, self.session, "After")
        self.assertEqual([point["point_id"] for point in self.store.list_points(self.folder)], [first["point_id"]])
        self.assertEqual(len(self.store.load_point(self.folder).messages), 1)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)

    def test_creation_rejects_nonempty_or_escaping_names_and_keeps_existing_files(self):
        existing = self.root / "Existing"
        existing.mkdir()
        sentinel = existing / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        for name in ("Existing", "../outside", "CON", "Bad/Name", ".."):
            with self.subTest(name=name), self.assertRaises(StorageError):
                self.store.create_folder(self.root, name, ChatSession())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_wrong_game_save_and_credentials_leave_folder_intact(self):
        with self.assertRaises(StorageError):
            self.store.save_current(self.folder, ChatSession())
        self.session.legacy_metadata["access_token"] = "not allowed"
        with self.assertRaises(StorageError):
            self.store.save_current(self.folder, self.session)
        self.assertEqual(len(self.store.load_point(self.folder).messages), 1)

    def test_actual_disk_usage_and_registry_are_reported_without_hidden_sessions(self):
        extra = self.folder / "player-notes.txt"
        extra.write_text("Extra file in the selected folder", encoding="utf-8")
        info = self.store.describe(self.folder)
        self.assertEqual(info["bytes"], sum(item.stat().st_size for item in self.folder.rglob("*") if item.is_file()))
        self.assertEqual(info["game_id"], self.session.session_id)
        self.assertFalse((self.store.root / "sessions").exists())
        self.assertTrue(self.store.list_known_folders()[0]["available"])

    def test_legacy_portable_archive_can_be_imported_without_modifying_original(self):
        portable = self.root / "original.aslgame"
        ChatStore(self.root / "legacy-app").save(self.session, portable)
        before = portable.read_bytes()
        imported = self.store.chat_store.load(portable)
        folder = self.store.create_folder(self.root, "Imported", imported)
        self.assertEqual(portable.read_bytes(), before)
        self.assertEqual(self.store.load_point(folder).messages[0].attachments[0].png, image().png)

    def test_stale_open_session_cannot_restore_deleted_screenshots(self):
        self.store.save_point(self.folder, self.session, "Before cleanup")
        self.store.clear_screenshots(self.folder)
        self.session.add_message("assistant", "Old open view")
        with self.assertRaisesRegex(StorageError, "deleted screenshot"):
            self.store.save_current(self.folder, self.session)
        current = self.store.load_point(self.folder)
        self.assertEqual(len(current.messages), 1)
        self.assertEqual(current.messages[0].attachments, [])
        self.assertIn("board-1", current.legacy_metadata[DELETED_IMAGES_KEY])

    def test_historical_reference_editions_survive_changes_and_screenshot_cleanup(self):
        original_reference = Reference("image-rules", "Rules.png", "image/png", image().png)
        self.session.references.append(original_reference)
        first = self.store.save_point(self.folder, self.session, "Original rules")
        self.session.references[-1] = Reference("image-rules", "Revised.png", "image/png", image("rules-2", b"\x50\x60\x70").png)
        self.store.save_current(self.folder, self.session)
        self.store.clear_screenshots(self.folder)
        original = self.store.load_point(self.folder, first["point_id"])
        current = self.store.load_point(self.folder)
        self.assertEqual(original.references[-1].name, "Rules.png")
        self.assertEqual(original.references[-1].content, original_reference.content)
        self.assertEqual(current.references[-1].name, "Revised.png")
        self.assertNotEqual(current.references[-1].sha256, original.references[-1].sha256)

    def test_review_retains_corrected_original_and_orders_timezone_aware_messages(self):
        self.session.messages[0].created_at = "2026-10-14T11:00:00-04:00"
        self.store.save_current(self.folder, self.session)
        self.session.messages[0].text = "Corrected deployment"
        later = self.session.add_message("assistant", "Later action")
        later.created_at = "2026-10-14T15:01:00+00:00"
        self.store.save_current(self.folder, self.session)
        history = self.store.load_history(self.folder)
        self.assertEqual([item.text for item in history.messages], ["Corrected deployment", "Later action"])
        self.assertIn("Initial deployment", [item["text"] for item in history.legacy_metadata["asl_ai_previous_message_versions"]])

    def test_delete_game_removes_only_managed_data_preserves_notes_and_shared_experience(self):
        point = self.store.save_point(self.folder, self.session, "Saved")
        memory = self.folder / MANAGED_DIRECTORY / "memory"
        memory.mkdir()
        (memory / (self.session.session_id + ".json")).write_text('{"entries":[]}', encoding="utf-8")
        notes = self.folder / "player-notes.txt"
        notes.write_text("Keep these notes", encoding="utf-8")
        experience = self.store.root / "memory" / "experience.json"
        experience.parent.mkdir()
        experience.write_text('{"lessons":["shared"]}', encoding="utf-8")
        result = self.store.delete_folder(self.folder)
        self.assertFalse(result["removed_folder"])
        self.assertTrue(notes.is_file())
        self.assertEqual(experience.read_text(encoding="utf-8"), '{"lessons":["shared"]}')
        self.assertFalse((self.folder / MANAGED_DIRECTORY).exists())
        self.assertEqual(self.store.list_known_folders(), [])
        with self.assertRaises(StorageError):
            self.store.load_point(self.folder, point["point_id"])

    def test_delete_empty_outer_game_folder_never_removes_parent(self):
        self.assertEqual(self.folder.parent.resolve(), self.root)
        result = self.store.delete_folder(self.folder)
        self.assertTrue(result["removed_folder"])
        self.assertFalse(self.folder.exists())
        self.assertTrue(self.root.is_dir())

    def test_delete_refuses_unknown_managed_file_before_touching_originals(self):
        unknown = self.folder / MANAGED_DIRECTORY / "my-notes.txt"
        unknown.write_text("Must not disappear", encoding="utf-8")
        before = self.store.database_path(self.folder).read_bytes()
        with self.assertRaisesRegex(StorageError, "Unrecognized"):
            self.store.delete_folder(self.folder)
        self.assertEqual(unknown.read_text(encoding="utf-8"), "Must not disappear")
        self.assertEqual(self.store.database_path(self.folder).read_bytes(), before)
        self.assertTrue(self.store.list_known_folders()[0]["available"])

    def test_delete_refuses_redirected_managed_tree_before_opening_database(self):
        managed = self.folder / MANAGED_DIRECTORY
        original = Path.is_junction

        def synthetic_junction(path):
            return path == managed or original(path)

        with patch.object(Path, "is_junction", synthetic_junction):
            with self.assertRaisesRegex(StorageError, "redirected"):
                self.store.delete_folder(self.folder)
        self.assertEqual(len(self.store.load_point(self.folder).messages), 1)

    def test_delete_refuses_unknown_or_corrupt_database_and_root(self):
        unrelated = self.root / "Unrelated"
        managed = unrelated / MANAGED_DIRECTORY
        managed.mkdir(parents=True)
        database = managed / "game.aslgame"
        database.write_bytes(b"This is not an application game")
        for folder in (unrelated, Path(self.root.anchor)):
            with self.subTest(folder=folder), self.assertRaises(StorageError):
                self.store.delete_folder(folder)
        self.assertEqual(database.read_bytes(), b"This is not an application game")
        self.assertTrue(self.folder.exists())

    def test_moved_database_replaces_stale_registry_even_when_old_outer_folder_remains(self):
        moved = self.root / "Moved game"
        moved.mkdir()
        (self.folder / MANAGED_DIRECTORY).rename(moved / MANAGED_DIRECTORY)
        (self.folder / "player-notes.txt").write_text("Keep", encoding="utf-8")
        self.store.load_point(moved)
        records = self.store.list_known_folders()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["folder"], str(moved))
        self.assertTrue(records[0]["available"])

    def test_unrecognized_database_at_registered_path_is_unavailable_not_recreated(self):
        path = self.store.database_path(self.folder)
        before = path.read_bytes()
        path.write_bytes(b"Not a game")
        self.assertFalse(self.store.list_known_folders()[0]["available"])
        self.assertEqual(path.read_bytes(), b"Not a game")
        path.write_bytes(before)

    def test_failed_folder_creation_does_not_leave_hidden_recovery_or_managed_data(self):
        original = self.store._writer._write

        def fail_after_write(*arguments):
            original(*arguments)
            raise StorageError("Synthetic failure")

        with patch.object(self.store._writer, "_write", side_effect=fail_after_write):
            with self.assertRaises(StorageError):
                self.store.create_folder(self.root, "Failed game", ChatSession())
        self.assertFalse((self.root / "Failed game").exists())
        self.assertFalse((self.store.root / "sessions").exists())


if __name__ == "__main__":
    unittest.main()
