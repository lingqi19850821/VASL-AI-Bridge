"""Persistence checks for portable conversations and long-match archives."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from vasl_ai_bridge.chat_session import ChatSession, Reference, new_id
from vasl_ai_bridge.chat_storage import ChatStore
from vasl_ai_bridge.session import Observation, Session
from vasl_ai_bridge.storage import StorageError


def image(image_id="frame_1", source="Board"):
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content) & 0xFFFFFFFF)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30")) + chunk(b"IEND", b""))
    return Observation(image_id, "2026-10-07T15:00:00+00:00", source, png, 1, 1)


class ChatStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="asl-ai-chat-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ChatStore(self.root / "private")

    def test_constructing_store_has_no_filesystem_side_effects(self):
        self.assertFalse(self.store.root.exists())

    def test_direct_reference_import_rejects_unsupported_and_disguised_files_before_saving(self):
        session = ChatSession()
        for name in ("unsupported.exe", "invalid.png", "binary.txt"):
            source = self.root / name
            source.write_bytes(b"MZ\x00\x01 synthetic unsupported fixture")
            with self.assertRaises(StorageError):
                self.store.import_reference(session, source)
            self.assertEqual(session.references, [])
            self.assertFalse(self.store.root.exists())

    def test_full_transcript_survives_compaction_restart_and_message_update(self):
        session = ChatSession(title="A long game", backend_mode="subscription", backend_thread_id="thread_123")
        first_image = image()
        for turn in range(70):
            session.add_message("user", f"Turn {turn}: what do you choose?", attachments=[first_image] if turn == 0 else [])
            session.add_message("assistant", f"My choice for turn {turn}.")
        answer = session.add_message("assistant", "Partial response", status="streaming")
        session.save_report = "Turn 6, awaiting the next move."
        session.active_context = [{"type": "compaction", "encrypted_content": "opaque_state"}]
        path = self.store.save(session)
        answer.text = "Final response"
        answer.status = "completed"
        self.store.save(session, path)
        restored = ChatStore(self.root / "another-runtime").load(path)
        self.assertEqual(len(restored.messages), 141)
        self.assertEqual(restored.messages[0].text, "Turn 0: what do you choose?")
        self.assertEqual(restored.messages[0].attachments[0], first_image)
        self.assertEqual(restored.messages[-1].text, "Final response")
        self.assertEqual(restored.messages[-1].status, "completed")
        self.assertEqual(restored.save_report, session.save_report)
        self.assertEqual(restored.backend_thread_id, session.backend_thread_id)
        self.assertEqual(restored.active_context, session.active_context)
        self.assertEqual(restored.messages[0].created_at, session.messages[0].created_at)

    def test_images_are_deduplicated_and_unchanged_autosave_does_not_rewrite_archive(self):
        session = ChatSession()
        session.add_message("user", "First screenshot", attachments=[image("first")])
        session.add_message("user", "Same pixels at another capture", attachments=[image("second")])
        path = self.store.save(session)
        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM blobs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM images").fetchone()[0], 2)
        before = path.read_bytes()
        self.store.save(session, path)
        self.assertEqual(path.read_bytes(), before)
        self.store.save(session, self.root / "manual.aslgame")
        # Choosing a manual save location must not turn subsequent recovery
        # autosaves into full-database backup copies.
        with patch("vasl_ai_bridge.chat_storage._read_connection", side_effect=AssertionError("Unexpected backup")):
            self.store.save(session)
        self.assertEqual(path.read_bytes(), before)

    def test_reference_import_preserves_original_and_save_as_is_self_contained(self):
        original = self.root / "rules.md"
        original.write_text("# Rule 1\nMovement explanation.", encoding="utf-8")
        session = ChatSession()
        reference = self.store.import_reference(session, original)
        self.assertEqual(reference.content, original.read_bytes())
        original.unlink()
        session.add_message("user", "Can you explain movement?", attachments=[image()])
        saved = self.store.save(session, self.root / "player-folder" / "game.aslgame")
        # No companion directories are needed to open the chosen save file.
        restored = ChatStore(self.root / "elsewhere").load(saved)
        self.assertEqual(restored.references[0].content, reference.content)
        self.assertEqual(restored.messages[0].attachments[0].png, image().png)
        self.assertEqual(restored.search_references("Movement")[0]["reference_id"], reference.reference_id)
        self.assertEqual(restored.search_history("movement")[0]["images"][0]["captured_at"], image().captured_at)

    def test_failed_save_as_preserves_existing_file_and_removes_temporary_database(self):
        session = ChatSession(title="Original")
        self.store.save(session)
        destination = self.root / "chosen.aslgame"
        self.store.save(session, destination)
        before = destination.read_bytes()
        self.store.save(session)
        session.title = "New title"
        with patch("vasl_ai_bridge.chat_storage.os.replace", side_effect=OSError("Simulated interruption")):
            with self.assertRaisesRegex(StorageError, "Could not save"):
                self.store.save(session, destination)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".aslgame-*.tmp")), [])

    def test_legacy_import_preserves_all_messages_events_images_and_original_file(self):
        legacy = Session(scenario="Retaking Vierville", phase="Rally Phase")
        for index in range(30):
            legacy.record_message("user", f"Old message {index}")
        request = legacy.begin_request(image())
        legacy.record_submission(request, image(), "prepared")
        legacy.record_submission(request, image(), "submitted")
        legacy.complete_request(request, "Awaiting your result.")
        legacy_path = self.root / "old.json"
        legacy_path.write_text(json.dumps({"schema_version": 1, "session": legacy.as_dict()}), encoding="utf-8")
        original = legacy_path.read_bytes()
        restored = self.store.load(legacy_path)
        self.assertEqual([message.text for message in restored.messages], [message.text for message in legacy.messages])
        self.assertEqual(restored.legacy_metadata["events"], [event.as_dict() for event in legacy.events])
        self.assertEqual(sum(len(message.attachments) for message in restored.messages), 1)
        self.assertEqual(len(restored.legacy_metadata["submissions"]), 2)
        new_save = self.store.save(restored)
        self.assertEqual(legacy_path.read_bytes(), original)
        self.assertEqual(len(self.store.load(new_save).messages), 31)
        with self.assertRaisesRegex(StorageError, "must not be overwritten"):
            self.store.save(restored, legacy_path)

    def test_archive_supports_more_than_old_64_mib_limit(self):
        session = ChatSession()
        content = b"x" * (65 * 1024 * 1024)
        session.references.append(Reference(new_id(), "large.bin", "application/octet-stream", content))
        path = self.store.save(session)
        self.assertGreater(path.stat().st_size, 64 * 1024 * 1024)
        restored = self.store.load(path)
        self.assertEqual(restored.references[0].sha256, session.references[0].sha256)

    def test_credential_metadata_is_rejected_before_writing(self):
        session = ChatSession(active_context=[{"nested": {"access_token": "must-not-be-saved"}}])
        with self.assertRaisesRegex(StorageError, "Credentials"):
            self.store.save(session)
        self.assertFalse(self.store.root.exists())

    def test_corrupt_image_is_reported_and_existing_archive_rolls_back(self):
        session = ChatSession()
        session.add_message("user", "Screenshot", attachments=[image()])
        path = self.store.save(session)
        before = path.read_bytes()
        session.messages[0].attachments = [image(source="Changed metadata")]
        with self.assertRaisesRegex(StorageError, "must not change"):
            self.store.save(session, path)
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("UPDATE blobs SET data=?", (b"damaged",))
        with self.assertRaisesRegex(StorageError, "checksum"):
            self.store.load(path)

    def test_unknown_binary_reference_reports_indexing_limit(self):
        session = ChatSession()
        session.references.append(Reference(new_id(), "reference.bin", "application/octet-stream", b"\x00\x01"))
        match = session.search_references("reference")[0]
        self.assertIn("format has no text reader", match["indexing_error"])

    def test_missing_session_identity_is_rejected_without_changing_archive(self):
        session = ChatSession()
        session.add_message("user", "Original conversation")
        path = self.store.save(session)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DELETE FROM metadata WHERE key='session_id'")
        before = path.read_bytes()
        with self.assertRaisesRegex(StorageError, "metadata is incomplete"):
            self.store.load(path)
        self.assertEqual(path.read_bytes(), before)

    def test_history_read_returns_adjacent_messages_and_attached_image(self):
        session = ChatSession()
        session.add_message("user", "Before")
        selected = session.add_message("assistant", "Selected")
        session.add_message("user", "After", attachments=[image()])
        self.assertEqual([message.text for message in session.read_history(selected.message_id, surrounding=1)],
                         ["Before", "Selected", "After"])

    def test_reference_search_returns_original_offsets_and_known_pdf_page(self):
        text = "[Page 1]\nFirst page.\n\n[Page 2]\n" + "a" * 1000 + "\nMovement costs depend on terrain."
        reference = Reference(new_id(), "rules.pdf", "application/pdf", b"original PDF bytes", indexed_text=text)
        session = ChatSession(references=[reference])
        match = session.search_references("Movement")[0]
        self.assertEqual(match["match_offset"], text.index("Movement"))
        self.assertEqual(match["physical_page"], 2)
        self.assertEqual(match["excerpt"], text[match["excerpt_offset"]:match["excerpt_offset"] + 700])
        self.assertNotIn("[Page 2]", match["excerpt"])
        plain = Reference(new_id(), "rules.txt", "text/plain", b"Movement", indexed_text="[Page 1]\nMovement")
        self.assertIsNone(ChatSession(references=[plain]).search_references("Movement")[0]["physical_page"])

    def test_reference_match_offsets_use_original_unicode_text(self):
        text = "Straße " * 30 + "Movement details"
        reference = Reference(new_id(), "rules.txt", "text/plain", text.encode(), indexed_text=text)
        match = ChatSession(references=[reference]).search_references("movement")[0]
        self.assertEqual(match["match_offset"], text.index("Movement"))

    def test_loading_same_game_releases_cached_previous_attachment_bytes(self):
        session = ChatSession()
        original = image()
        session.add_message("user", "Original screenshot", attachments=[original])
        reference = Reference(new_id(), "rules.txt", "text/plain", b"Original reference")
        session.references.append(reference)
        path = self.store.save(session)
        self.assertTrue(any(data is original.png for data, _ in self.store._digest_cache.values()))
        self.assertTrue(any(data is reference.content for data, _ in self.store._digest_cache.values()))
        restored = self.store.load(path)
        self.assertEqual(restored.session_id, session.session_id)
        self.assertEqual(self.store._digest_cache, {})
        self.store.save(restored, path)
        self.assertFalse(any(data is original.png for data, _ in self.store._digest_cache.values()))
        again = self.store.load(path)
        self.assertEqual(again.messages[0].attachments[0].png, original.png)
        self.assertEqual(self.store._digest_cache, {})


if __name__ == "__main__":
    unittest.main()
