"""Tests for atomic private checkpoints and damaged-input handling."""

import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from vasl_ai_bridge.session import Observation, Session
from vasl_ai_bridge.storage import SessionStore, StorageError, default_data_directory


def _image() -> Observation:
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30"))
        + chunk(b"IEND", b"")
    )
    return Observation("frame_1", "2026-10-06T15:00:00+00:00", "Test board", png, 1, 1)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="asl-ai-storage-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = SessionStore(self.root)

    def test_atomic_round_trip_preserves_images_events_and_stopping_point(self):
        session = Session(scenario="Retaking Vierville", phase="Rally Phase")
        session.record_confirmation("Squad location confirmed", "German squad at K9, CX")
        session.record_message("user", "Please check LOS before firing.")
        image = _image()
        request = session.begin_request(image)
        session.record_submission(request, image, "submitted")
        session.complete_request(request, "Waiting for your LOS check.")
        path = self.store.save(session)
        self.assertEqual(path, (self.root / "sessions" / f"{session.session_id}.json").resolve())
        restored = self.store.load(path)
        self.assertEqual(restored.pending_action, "Waiting for your LOS check.")
        self.assertEqual(restored.confirmed_facts, session.confirmed_facts)
        self.assertEqual(restored.phase, session.phase)
        self.assertEqual(restored.submissions[-1].observation.png, image.png)
        self.assertEqual(restored.events[:-1], session.events)
        self.assertTrue(restored.paused)
        self.assertTrue(restored.reconciliation_required)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_failed_atomic_replace_keeps_existing_checkpoint_and_cleans_temporary_file(self):
        session = Session(pending_action="Original waiting point")
        path = self.store.save(session)
        old_contents = path.read_bytes()
        session.pending_action = "New waiting point"
        with patch("vasl_ai_bridge.storage.os.replace", side_effect=OSError("Simulated interruption")):
            with self.assertRaisesRegex(StorageError, "Could not save"):
                self.store.save(session, path)
        self.assertEqual(path.read_bytes(), old_contents)
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_private_checkpoint_cannot_be_saved_in_repository(self):
        repository = Path(__file__).resolve().parents[1]
        with self.assertRaisesRegex(StorageError, "outside the source repository"):
            self.store.save(Session(), repository / "private-checkpoint.json")
        self.assertFalse((repository / "private-checkpoint.json").exists())

    def test_missing_corrupt_schema_and_duplicate_json_fields_are_reported(self):
        with self.assertRaisesRegex(StorageError, "Could not open"):
            self.store.load(self.root / "missing.json")
        path = self.root / "broken.json"
        path.write_text("{not json}", encoding="utf-8")
        with self.assertRaisesRegex(StorageError, "invalid or damaged"):
            self.store.load(path)
        path.write_text(json.dumps({"schema_version": 999, "session": {}}), encoding="utf-8")
        with self.assertRaisesRegex(StorageError, "schema version"):
            self.store.load(path)
        path.write_text('{"schema_version": 1, "schema_version": 2, "session": {}}', encoding="utf-8")
        with self.assertRaisesRegex(StorageError, "duplicate JSON field"):
            self.store.load(path)

    def test_corrupt_saved_image_fails_load_instead_of_silently_changing_evidence(self):
        session = Session()
        session.add_observation(_image())
        path = self.store.save(session)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["session"]["observations"][0]["sha256"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(StorageError, "checksum"):
            self.store.load(path)

    def test_loading_interrupted_request_preserves_pending_and_requires_resume(self):
        session = Session(pending_action="Waiting for defensive fire decision")
        snapshot = session.begin_request(_image())
        restored = self.store.load(self.store.save(session))
        self.assertIsNone(restored.active_request)
        self.assertEqual(restored.pending_action, session.pending_action)
        self.assertTrue(any(snapshot.request_id in event.text and event.kind == "request_interrupted" for event in restored.events))

    def test_checkpoint_size_limit_applies_to_load_and_save(self):
        path = self.root / "too-large.json"
        path.write_bytes(b" " * 33)
        with patch("vasl_ai_bridge.storage.MAX_CHECKPOINT_BYTES", 32):
            with self.assertRaisesRegex(StorageError, "size"):
                self.store.load(path)
            with self.assertRaisesRegex(StorageError, "size"):
                self.store.save(Session())

    def test_default_directory_uses_local_app_data_and_has_home_fallback(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            self.assertEqual(default_data_directory(), self.root / "ASL_AI")
        with patch.dict(os.environ, {}, clear=True), patch("vasl_ai_bridge.storage.Path.home", return_value=self.root):
            self.assertEqual(default_data_directory(), self.root / ".asl_ai")


if __name__ == "__main__":
    unittest.main()
