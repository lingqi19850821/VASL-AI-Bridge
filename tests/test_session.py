"""Tests for ordered state changes, stale decisions, and exact image evidence."""

import base64
from dataclasses import FrozenInstanceError
import struct
import unittest
import zlib

from vasl_ai_bridge.session import Observation, RequestSnapshot, Session, SessionError


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def observation(identifier: str = "frame_1", color: bytes = b"\x10\x20\x30") -> Observation:
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(b"\x00" + color))
        + _chunk(b"IEND", b"")
    )
    return Observation(identifier, "2026-10-06T15:00:00+00:00", "Test board", png, 1, 1)


class ObservationTests(unittest.TestCase):
    def test_round_trip_preserves_exact_bytes_and_hash(self):
        original = observation()
        restored = Observation.from_dict(original.as_dict())
        self.assertEqual(restored, original)
        self.assertEqual(base64.b64decode(restored.data_url.split(",", 1)[1]), original.png)
        self.assertEqual(len(restored.sha256), 64)
        with self.assertRaises(FrozenInstanceError):
            restored.source = "Different board"

    def test_damaged_image_and_wrong_dimensions_are_rejected(self):
        original = observation()
        data = original.as_dict()
        data["sha256"] = "0" * 64
        with self.assertRaisesRegex(SessionError, "checksum"):
            Observation.from_dict(data)
        with self.assertRaisesRegex(SessionError, "dimensions"):
            Observation(original.observation_id, original.captured_at, original.source, original.png, 2, 1)
        damaged = bytearray(original.png)
        damaged[45] ^= 1
        with self.assertRaisesRegex(SessionError, "damaged"):
            Observation(original.observation_id, original.captured_at, original.source, bytes(damaged), 1, 1)

    def test_ids_timestamps_and_empty_png_are_validated(self):
        original = observation()
        for identifier in ("../frame", "", "frame id"):
            with self.assertRaises(SessionError):
                Observation(identifier, original.captured_at, original.source, original.png, 1, 1)
        with self.assertRaisesRegex(SessionError, "time zone"):
            Observation("frame", "2026-10-06T15:00:00", original.source, original.png, 1, 1)
        with self.assertRaises(SessionError):
            Observation("frame", original.captured_at, original.source, b"", 1, 1)


class SessionTests(unittest.TestCase):
    def test_confirmed_state_changes_are_ordered_and_not_inferred_from_images(self):
        session = Session()
        session.add_observation(observation())
        session.record_message("user", "The German squad is at K9.")
        self.assertEqual(session.state_version, 0)
        self.assertEqual(session.confirmed_facts, "")
        session.record_confirmation("Confirmed K9", "German squad at K9, CX", "Rally Phase")
        session.record_correction("The squad retains CX", "German squad at K9, CX")
        self.assertEqual(session.state_version, 2)
        self.assertEqual([event.event_id for event in session.events], [1, 2, 3])
        self.assertEqual([event.state_version for event in session.events], [0, 1, 2])
        self.assertEqual(session.phase, "Rally Phase")

    def test_one_request_at_a_time_and_success_is_only_a_pending_proposal(self):
        session = Session(phase="Rally Phase")
        snapshot = session.begin_request(observation())
        with self.assertRaisesRegex(SessionError, "already active"):
            session.begin_request(observation("frame_2"))
        self.assertTrue(session.complete_request(snapshot, "Please make the rally check and report the dice."))
        self.assertIn("report the dice", session.pending_action)
        self.assertEqual(session.phase, "Rally Phase")
        self.assertEqual(session.confirmed_facts, "")
        self.assertEqual(session.state_version, 0)
        self.assertIsNone(session.active_request)

    def test_correction_invalidates_late_response_and_preserves_unresolved_work(self):
        session = Session(pending_action="Waiting for the LOS check.")
        snapshot = session.begin_request(observation())
        session.record_correction("The target is at K9", "Target at K9")
        self.assertFalse(session.is_request_current(snapshot))
        self.assertFalse(session.complete_request(snapshot, "Move to J9."))
        self.assertEqual(session.pending_action, "Waiting for the LOS check.")
        self.assertTrue(session.pending_action_stale)
        self.assertEqual(session.confirmed_facts, "Target at K9")
        self.assertIn("Stale response", session.messages[-1].text)
        self.assertIn("Move to J9", session.messages[-1].text)

    def test_rejected_observation_invalidates_response_without_changing_confirmed_facts(self):
        session = Session(confirmed_facts="Target at K9")
        image = observation()
        snapshot = session.begin_request(image)
        session.reject_observation(image.observation_id, "Wrong window was captured")
        self.assertEqual(session.state_version, 1)
        self.assertFalse(session.complete_request(snapshot, "Fire at J9."))
        self.assertEqual(session.pending_action, "")
        self.assertEqual(session.confirmed_facts, "Target at K9")
        with self.assertRaisesRegex(SessionError, "rejected"):
            session.begin_request(image)

    def test_confirmation_clears_pending_and_supersedes_active_assessment(self):
        session = Session(pending_action="Waiting for dice", phase="Rally Phase")
        snapshot = session.begin_request(observation())
        session.record_confirmation("Dice are 3 and 4", "Rally failed")
        self.assertEqual(session.pending_action, "")
        self.assertEqual(session.phase, "Rally Phase")
        self.assertFalse(session.pending_action_stale)
        self.assertFalse(session.complete_request(snapshot, "The rally succeeds."))

    def test_confirmed_metadata_update_preserves_unresolved_pending_action(self):
        session = Session(pending_action="Waiting for dice", phase="Rally Phase")
        session.record_confirmation("Confirmed scenario metadata", "Squad at K9", clear_pending=False)
        self.assertEqual(session.pending_action, "Waiting for dice")
        self.assertTrue(session.pending_action_stale)
        self.assertEqual(session.confirmed_facts, "Squad at K9")
        self.assertEqual(session.state_version, 1)

    def test_completed_proposal_becomes_stale_after_correction_and_survives_load(self):
        session = Session()
        image = observation()
        snapshot = session.begin_request(image)
        session.record_submission(snapshot, image, "submitted")
        session.complete_request(snapshot, "Move to J9")
        self.assertFalse(session.pending_action_stale)
        session.record_correction("The destination was misread; it is K9")
        self.assertEqual(session.pending_action, "Move to J9")
        self.assertTrue(session.pending_action_stale)
        restored = Session.from_dict(session.as_dict())
        self.assertTrue(restored.pending_action_stale)
        restored.reconcile("Board checked")
        restored.resume()
        new_request = restored.begin_request(observation("frame_2"))
        restored.complete_request(new_request, "Wait for the corrected LOS check")
        self.assertFalse(restored.pending_action_stale)

    def test_submitted_evidence_survives_bounded_recent_retention(self):
        session = Session()
        image = observation()
        snapshot = session.begin_request(image)
        session.record_submission(snapshot, image, "prepared")
        self.assertEqual(session.submissions[-1].status, "prepared")
        session.record_submission(snapshot, image, "submitted")
        session.complete_request(snapshot, "Waiting for the human result.")
        for number in range(2, 20):
            session.add_observation(observation(f"frame_{number}"))
        self.assertEqual(len(session.observations), 8)
        self.assertEqual(session.observations[-1].observation_id, "frame_19")
        self.assertEqual(session.submissions[-1].observation.png, image.png)
        self.assertEqual(session.submissions[-1].observation_id, "frame_1")
        restored = Session.from_dict(session.as_dict())
        self.assertEqual(restored.submissions[-1].observation.png, image.png)

    def test_submission_cannot_change_image_or_claim_an_unrelated_request_image(self):
        session = Session()
        image = observation()
        snapshot = session.begin_request(image)
        with self.assertRaisesRegex(SessionError, "differs"):
            session.record_submission(snapshot, observation(color=b"\x80\x90\xA0"), "submitted")
        unrelated = RequestSnapshot("unrelated_request", snapshot.state_version, snapshot.session_id, ("another_image",))
        with self.assertRaisesRegex(SessionError, "associated"):
            session.record_submission(unrelated, observation("another"), "submitted")
        session.record_submission(snapshot, image, "submitted")
        with self.assertRaisesRegex(SessionError, "already submitted"):
            session.record_submission(snapshot, image, "failed")

    def test_rejected_look_now_image_invalidates_original_request_snapshot(self):
        session = Session(pending_action="Waiting for a defensive fire decision")
        snapshot = session.begin_request(observation())
        extra_image = observation("look_now_2")
        session.record_submission(snapshot, extra_image, "prepared")
        session.record_submission(snapshot, extra_image, "submitted")
        self.assertEqual(snapshot.observation_ids, ("frame_1",))
        self.assertTrue(session.is_request_current(snapshot))
        session.reject_observation(extra_image.observation_id, "Fresh capture showed the wrong board")
        self.assertFalse(session.is_request_current(snapshot))
        self.assertFalse(session.complete_request(snapshot, "Fire based on look_now_2."))
        self.assertEqual(session.pending_action, "Waiting for a defensive fire decision")

    def test_unsubmitted_active_image_is_preserved_when_new_local_frames_arrive(self):
        session = Session()
        original = observation()
        snapshot = session.begin_request(original)
        for number in range(2, 20):
            session.add_observation(observation(f"frame_{number}"))
        self.assertEqual(len(session.observations), 8)
        self.assertIn(original, session.observations)
        restored = Session.from_dict(session.as_dict())
        self.assertTrue(any(snapshot.request_id in event.text for event in restored.events))

    def test_pause_and_resume_do_not_revive_an_assessment(self):
        session = Session()
        snapshot = session.begin_request(observation())
        session.pause()
        session.resume()
        self.assertFalse(session.is_request_current(snapshot))
        self.assertFalse(session.complete_request(snapshot, "An old proposal."))
        self.assertEqual(session.pending_action, "")

    def test_loaded_session_requires_reconciliation_then_explicit_resume(self):
        session = Session(pending_action="Waiting for defensive fire decision", phase="Movement Phase")
        snapshot = session.begin_request(observation())
        restored = Session.from_dict(session.as_dict())
        self.assertTrue(restored.paused)
        self.assertTrue(restored.reconciliation_required)
        self.assertIsNone(restored.active_request)
        self.assertTrue(any(event.kind == "request_interrupted" and snapshot.request_id in event.text for event in restored.events))
        self.assertEqual(restored.pending_action, session.pending_action)
        with self.assertRaisesRegex(SessionError, "Confirm the current board"):
            restored.resume()
        restored.reconcile("Current board matches the checkpoint.")
        self.assertTrue(restored.paused)
        self.assertFalse(restored.reconciliation_required)
        self.assertEqual(restored.pending_action, session.pending_action)
        with self.assertRaises(SessionError):
            restored.begin_request(observation("new_frame"))
        restored.resume()
        self.assertFalse(restored.paused)

    def test_invalid_event_order_and_duplicate_image_ids_are_rejected(self):
        session = Session()
        session.record_message("user", "First result")
        session.record_confirmation("Confirmed result")
        data = session.as_dict()
        data["events"][1]["event_id"] = 1
        with self.assertRaisesRegex(SessionError, "consistent order"):
            Session.from_dict(data)
        session.add_observation(observation())
        with self.assertRaisesRegex(SessionError, "two different images"):
            session.add_observation(observation(color=b"\x80\x90\xA0"))

    def test_failed_request_preserves_pending_work_and_allows_next_request(self):
        session = Session(pending_action="Waiting for LOS result")
        snapshot = session.begin_request(observation())
        session.fail_request(snapshot, "Connection timed out")
        self.assertEqual(session.pending_action, "Waiting for LOS result")
        self.assertIsNone(session.active_request)
        self.assertTrue(session.is_request_current(session.begin_request(observation("frame_2"))))


if __name__ == "__main__":
    unittest.main()
