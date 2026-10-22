"""Durable local memory, checkpoint isolation, and bounded tool integration.

All games, images and models below are synthetic. No runtime or network is used.
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from vasl_ai_bridge.capture import prepare_image
from vasl_ai_bridge.chat_session import ChatSession
from vasl_ai_bridge.chat_storage import ChatStore
from vasl_ai_bridge.codex_bridge import ASL_INSTRUCTIONS, DYNAMIC_TOOLS
from vasl_ai_bridge.game_folders import GameFolderStore
from vasl_ai_bridge.game_memory import GameMemoryError, MEMORY_TOOL_NAMES, MemoryService
from vasl_ai_bridge.game_tools import handle_game_tool


class GameMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="asl-ai-memory-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "application-data"
        self.memory = MemoryService(self.data)
        self.game = ChatSession(title="Synthetic review game")
        self.user = self.game.add_message("user", "An attack through open ground failed after defensive fire.")
        self.assistant = self.game.add_message("assistant", "Reconsider the approach.", status="pending")
        self.context = self.turn_context(self.user, self.assistant)

    @staticmethod
    def turn_context(user, assistant, **extra):
        return {"model": "model-A", "thread_id": "thread-one", "turn_id": "turn-one",
                "anchor_message_id": user.message_id, "assistant_message_id": assistant.message_id,
                "mode": "normal", "application_message_ids": [], **extra}

    def journal_path(self):
        folder = self.context.get("game_folder")
        return ((Path(folder) / ".vasl-ai-bridge" / "memory") if folder else self.data / "memory" / "games") / (self.game.session_id + ".json")

    def folder(self):
        self.folders = GameFolderStore(ChatStore(self.data))
        folder = self.folders.create_folder(self.root, "Synthetic game", self.game)
        self.context["game_folder"] = folder
        return folder

    def intent(self, **changes):
        return {"action": "Approach the bridge through cover", "goal": "Preserve the squad",
                "assumptions": "The woods approach is not covered", "expected_result": "Reach the bridge",
                "change_conditions": "Reassess if another defender is revealed",
                "source_message_ids": [self.user.message_id], **changes}

    def lesson(self, **changes):
        return {"title": "Covered approach", "lesson": "Consider covered approaches before crossing observed open ground.",
                "conditions": "Defenders can fire on open ground", "observed_result": "The player reported a failed attack.",
                "evidence_kind": "reported_outcome", "source_message_ids": [self.user.message_id],
                "scope": "general", "scenario_name": "", **changes}

    def record(self, **arguments):
        return self.memory.handle_tool(self.game, "record_turn_memory", arguments, context=self.context)

    def read_lesson(self, identifier, *, session=None, context=None, **lookup):
        offset, contents = 0, ""
        while offset is not None:
            part = self.memory.handle_tool(session or self.game, "read_experience",
                                           {"lesson_id": identifier, "offset": offset, "max_chars": 1000, **lookup},
                                           context=context or self.context)
            contents += part["text"]
            offset = part["next_text_offset"]
        return json.loads(contents)

    def test_empty_catalog_does_not_create_files(self):
        catalog = self.memory.catalog(self.game)
        self.assertEqual(catalog["pending_message_ids"], [self.user.message_id])
        self.assertFalse(self.data.exists())

    def test_plaintext_intent_and_provenance_survive_restart_and_model_change(self):
        saved = self.record(intents=[self.intent()], processed_message_ids=[self.user.message_id])
        self.assertIn("Approach the bridge", self.journal_path().read_text(encoding="utf-8"))
        service = MemoryService(self.data)
        result = service.handle_tool(self.game, "read_tactical_journal", {"entry_id": saved["intent_ids"][0]},
                                     context={**self.context, "model": "model-B"})
        entry = result["entry"]
        self.assertEqual(entry["model"], "model-A")
        self.assertEqual(entry["thread_id"], "thread-one")
        self.assertEqual(entry["turn_id"], "turn-one")
        self.assertIn("not hidden reasoning", entry["interpretation"])
        self.assertEqual(service.pending_message_ids(self.game), [])

    def test_canonical_folder_adopts_fallback_only_after_durable_write(self):
        self.record(intents=[self.intent()])
        fallback = self.journal_path()
        folder = self.folder()
        self.assertEqual(self.memory.catalog(self.game, self.context)["journal_entries"], 1)
        self.assertTrue(fallback.exists())  # Reading never destroys the old record.
        self.record(processed_message_ids=[self.user.message_id])
        self.assertFalse(fallback.exists())
        self.assertTrue(self.journal_path().is_relative_to(folder))
        self.assertEqual(self.memory.catalog(self.game, self.context)["journal_entries"], 1)

    def test_missing_folder_is_not_recreated_by_memory(self):
        missing = self.root / "Missing game"
        with self.assertRaisesRegex(GameMemoryError, "missing or has moved"):
            self.memory.handle_tool(self.game, "record_turn_memory", {"intents": [self.intent()]},
                                    context={**self.context, "game_folder": missing})
        self.assertFalse(missing.exists())

    def test_other_games_folder_is_rejected_without_new_journal(self):
        folders = GameFolderStore(ChatStore(self.data))
        folder = folders.create_folder(self.root, "Other game", ChatSession())
        with self.assertRaisesRegex(GameMemoryError, "another game"):
            self.memory.catalog(self.game, {**self.context, "game_folder": folder})
        self.assertFalse((folder / ".vasl-ai-bridge" / "memory").exists())

    def test_rewind_before_assistant_anchor_hides_future_intent_and_watermark(self):
        saved = self.record(intents=[self.intent()], lessons=[self.lesson()],
                            processed_message_ids=[self.user.message_id])
        earlier = deepcopy(self.game)
        earlier.messages = [earlier.messages[0]]
        self.assertEqual(self.memory.catalog(earlier)["journal_entries"], 0)
        self.assertEqual(self.memory.pending_message_ids(earlier), [self.user.message_id])
        with self.assertRaises(GameMemoryError):
            self.memory.handle_tool(earlier, "read_tactical_journal", {"entry_id": saved["intent_ids"][0]})
        # Global lessons intentionally survive checkpoint rewind.
        result = self.memory.handle_tool(earlier, "search_experience", {"query": "covered"})
        self.assertEqual(result["matches"][0]["lesson_id"], saved["lesson_ids"][0])

    def test_branch_with_same_user_message_does_not_inherit_future_journal(self):
        self.record(intents=[self.intent()], processed_message_ids=[self.user.message_id])
        branch = deepcopy(self.game)
        branch.messages.pop()
        branch.add_message("assistant", "A different plan")
        self.assertEqual(self.memory.catalog(branch)["journal_entries"], 0)
        self.assertEqual(self.memory.pending_message_ids(branch), [self.user.message_id])

    def test_trusted_assistant_identity_is_required_for_writes(self):
        for context in ({key: value for key, value in self.context.items() if key != "assistant_message_id"},
                        {**self.context, "assistant_message_id": self.user.message_id},
                        {**self.context, "model": ""}):
            with self.subTest(context=context), self.assertRaises(GameMemoryError):
                self.memory.handle_tool(self.game, "record_turn_memory", {"intents": [self.intent()]}, context=context)
        self.assertFalse(self.data.exists())

    def test_intent_revision_links_and_preserves_original(self):
        first = self.record(intents=[self.intent()])["intent_ids"][0]
        self.assistant.status = "completed"
        report = self.game.add_message("user", "The woods are also covered.")
        answer = self.game.add_message("assistant", status="pending")
        self.context = self.turn_context(report, answer, model="model-B", turn_id="turn-two")
        revised = self.record(intents=[self.intent(action="Use smoke before moving", previous_entry_id=first,
                                                  source_message_ids=[report.message_id])])["intent_ids"][0]
        entries = self.memory.handle_tool(self.game, "read_tactical_journal", {})["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["action"], "Approach the bridge through cover")
        self.assertEqual(entries[1]["previous_entry_id"], first)
        self.assertEqual(entries[1]["entry_id"], revised)
        self.assertEqual(entries[1]["model"], "model-B")

    def test_repeated_intents_and_lessons_are_idempotent(self):
        arguments = {"intents": [self.intent()], "lessons": [self.lesson()],
                     "processed_message_ids": [self.user.message_id]}
        first = self.record(**arguments)
        second = self.record(**arguments)
        self.assertEqual(first, second)
        self.assertEqual(self.memory.catalog(self.game)["journal_entries"], 1)
        library = json.loads(self.memory.experience_path.read_text(encoding="utf-8"))
        self.assertEqual(len(library["lessons"]), 1)
        self.assertEqual(len(library["lessons"][0]["revisions"]), 1)

    def test_new_game_and_different_model_read_prior_candidate_without_review(self):
        saved = self.record(lessons=[self.lesson()])
        another_game = ChatSession()
        service = MemoryService(self.data)
        context = {"model": "model-B"}
        catalog = service.catalog(another_game, context)
        self.assertEqual(catalog["journal_entries"], 0)
        self.assertEqual(catalog["shared_lesson_count"], 1)
        lesson = self.read_lesson(saved["lesson_ids"][0], session=another_game, context=context)
        self.assertEqual(lesson["status"], "candidate")
        self.assertEqual(lesson["revisions"][0]["model"], "model-A")
        self.assertEqual(lesson["revisions"][0]["source_evidence"][0]["text_excerpt"], self.user.text)

    def test_scenario_lessons_require_explicit_exact_name_and_cannot_bypass_read_filter(self):
        general = self.record(lessons=[self.lesson()])["lesson_ids"][0]
        first = self.record(lessons=[self.lesson(scope="scenario", scenario_name="Retaking Vierville")])["lesson_ids"][0]
        second = self.record(lessons=[self.lesson(scope="scenario", scenario_name="A Different Scenario")])["lesson_ids"][0]
        default = self.memory.handle_tool(self.game, "search_experience", {"query": "covered"})
        self.assertEqual([item["lesson_id"] for item in default["matches"]], [general])
        self.assertEqual((default["scope"], default["scenario_name"]), ("general", ""))
        selected = self.memory.handle_tool(self.game, "search_experience", {
            "query": "covered", "scope": "scenario", "scenario_name": "  RETAKING   vierville  "})
        self.assertEqual([item["lesson_id"] for item in selected["matches"]], [first])
        self.assertEqual(selected["matches"][0]["scenario_name"], "Retaking Vierville")
        self.assertEqual(selected["matches"][0]["scope"], "scenario")
        self.assertEqual(self.memory.handle_tool(self.game, "search_experience", {
            "query": "", "scope": "scenario", "scenario_name": "Vierville"})["matches"], [])
        for arguments in ({"lesson_id": first}, {"lesson_id": first, "scope": "unclassified"},
                          {"lesson_id": second, "scope": "scenario", "scenario_name": "Retaking Vierville"},
                          {"lesson_id": general, "scope": "scenario", "scenario_name": "Retaking Vierville"}):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(GameMemoryError, "requested scope"):
                self.memory.handle_tool(self.game, "read_experience", arguments)
        result = self.memory.handle_tool(self.game, "read_experience", {
            "lesson_id": first, "scope": "scenario", "scenario_name": "retaking vierville", "max_chars": 100})
        self.assertEqual((result["scope"], result["scenario_name"]), ("scenario", "Retaking Vierville"))
        restored = self.read_lesson(first, scope="scenario", scenario_name="retaking vierville")
        self.assertEqual(restored["scenario_name"], "Retaking Vierville")
        self.assertEqual(restored["revisions"][0]["scope"], "scenario")

    def test_dedup_includes_scope_and_scenario_name_and_preserves_original_spelling(self):
        general = self.record(lessons=[self.lesson()])["lesson_ids"][0]
        first = self.record(lessons=[self.lesson(scope="scenario", scenario_name="Retaking Vierville")])["lesson_ids"][0]
        second = self.record(lessons=[self.lesson(scope="scenario", scenario_name="Another Scenario")])["lesson_ids"][0]
        equivalent = self.record(lessons=[self.lesson(scope="scenario", scenario_name=" retaking   VIERVILLE ")])["lesson_ids"][0]
        self.assertEqual(first, equivalent)
        self.assertEqual(len({general, first, second}), 3)
        restored = self.read_lesson(first, scope="scenario", scenario_name="Retaking Vierville")
        self.assertEqual(restored["scenario_name"], "Retaking Vierville")
        self.assertEqual(len(restored["revisions"]), 1)

    def test_catalog_contains_general_titles_only_and_separate_scope_counts(self):
        general = self.record(lessons=[self.lesson(title="Transferable consideration")])["lesson_ids"][0]
        for index in range(12):
            self.record(lessons=[self.lesson(title=f"Scenario-only title {index}", scope="scenario",
                                           scenario_name=f"Scenario {index}")])
        catalog = self.memory.catalog(ChatSession())
        self.assertEqual(catalog["shared_lesson_count"], 13)
        self.assertEqual(catalog["lesson_scope_counts"], {"general": 1, "scenario": 12, "unclassified": 0})
        self.assertEqual([item["lesson_id"] for item in catalog["recent_lessons"]], [general])
        self.assertEqual(catalog["recent_lessons"][0]["scope"], "general")
        self.assertNotIn("Scenario-only title", self.memory.prompt_context(ChatSession()))

    def test_new_lessons_reject_missing_or_invalid_classification_without_writes(self):
        invalid = []
        for field in ("scope", "scenario_name"):
            lesson = self.lesson()
            lesson.pop(field)
            invalid.append(lesson)
        invalid.extend(self.lesson(**changes) for changes in (
            {"scope": "unclassified"}, {"scope": "unknown"}, {"scope": True}, {"scope": None},
            {"scope": "scenario", "scenario_name": ""}, {"scope": "scenario", "scenario_name": "  "},
            {"scope": "scenario", "scenario_name": "x" * 201}, {"scenario_name": "Retaking Vierville"},
            {"scenario_name": None}, {"scenario_name": 1}, {"scenario_id": "ASL-1"},
        ))
        for lesson in invalid:
            with self.subTest(lesson=lesson), self.assertRaises(GameMemoryError):
                self.record(lessons=[lesson], processed_message_ids=[self.user.message_id])
        self.assertFalse(self.memory.experience_path.exists())
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(self.memory.pending_message_ids(self.game), [self.user.message_id])

    def test_experience_lookup_rejects_invalid_scope_name_combinations_without_writes(self):
        identifier = self.record(lessons=[self.lesson()])["lesson_ids"][0]
        before = self.memory.experience_path.read_bytes()
        for fields in ({"scope": "scenario"}, {"scope": "scenario", "scenario_name": " "},
                       {"scope": "scenario", "scenario_name": "x" * 201}, {"scope": "unknown"},
                       {"scope": None}, {"scope": []}, {"scenario_name": "Retaking Vierville"},
                       {"scope": "unclassified", "scenario_name": "Retaking Vierville"},
                       {"scope": "scenario", "scenario_name": 1}, {"scenario_id": "ASL-1"}):
            for tool, arguments in (("search_experience", {"query": "", **fields}),
                                    ("read_experience", {"lesson_id": identifier, **fields})):
                with self.subTest(tool=tool, fields=fields), self.assertRaises(GameMemoryError):
                    self.memory.handle_tool(self.game, tool, arguments)
        self.assertEqual(self.memory.experience_path.read_bytes(), before)

    def test_revision_cannot_silently_change_scope_or_scenario(self):
        general = self.record(lessons=[self.lesson()])["lesson_ids"][0]
        scenario = self.record(lessons=[self.lesson(scope="scenario", scenario_name="Retaking Vierville")])["lesson_ids"][0]
        before = self.memory.experience_path.read_bytes()
        for changes in ({"existing_lesson_id": general, "scope": "scenario", "scenario_name": "Retaking Vierville"},
                        {"existing_lesson_id": scenario},
                        {"existing_lesson_id": scenario, "scope": "scenario", "scenario_name": "Another Scenario"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(GameMemoryError, "keep its existing scope"):
                self.record(lessons=[self.lesson(relationship="revises", **changes)])
            self.assertEqual(self.memory.experience_path.read_bytes(), before)
        self.record(lessons=[self.lesson(existing_lesson_id=scenario, scope="scenario", scenario_name="RETAKING vierville",
                                        relationship="revises", lesson="A corrected scenario-specific consideration.")])
        revised = self.read_lesson(scenario, scope="scenario", scenario_name="Retaking Vierville")
        self.assertEqual(revised["lesson"], self.lesson()["lesson"])
        self.assertEqual(revised["scenario_name"], "Retaking Vierville")
        self.assertEqual(len(revised["revisions"]), 2)

    def untagged_library(self):
        identifier = self.record(lessons=[self.lesson()])["lesson_ids"][0]
        library = json.loads(self.memory.experience_path.read_text(encoding="utf-8"))
        for item in library["lessons"]:
            for record in [item, *item["revisions"]]:
                record.pop("scope")
                record.pop("scenario_name")
        raw = json.dumps(library, ensure_ascii=False, indent=1).encode("utf-8")
        self.memory.experience_path.write_bytes(raw)
        return identifier, library["lessons"][0], raw

    def test_untagged_records_are_explicit_unclassified_views_without_rewriting_originals(self):
        identifier, original, before = self.untagged_library()
        catalog = self.memory.catalog(self.game)
        self.assertEqual(catalog["lesson_scope_counts"], {"general": 0, "scenario": 0, "unclassified": 1})
        self.assertEqual(catalog["recent_lessons"], [])
        self.assertEqual(self.memory.handle_tool(self.game, "search_experience", {"query": ""})["matches"], [])
        result = self.memory.handle_tool(self.game, "search_experience", {"query": "", "scope": "unclassified"})
        self.assertEqual(result["matches"][0]["lesson_id"], identifier)
        self.assertEqual((result["matches"][0]["scope"], result["matches"][0]["scenario_name"]), ("unclassified", ""))
        with self.assertRaisesRegex(GameMemoryError, "requested scope"):
            self.read_lesson(identifier)
        restored = self.read_lesson(identifier, scope="unclassified")
        self.assertEqual(restored, {**original, "scope": "unclassified", "scenario_name": ""})
        self.assertEqual(self.memory.experience_path.read_bytes(), before)
        with self.assertRaisesRegex(GameMemoryError, "keep its existing scope"):
            self.record(lessons=[self.lesson(existing_lesson_id=identifier)])
        self.assertEqual(self.memory.experience_path.read_bytes(), before)
        self.record(lessons=[self.lesson(lesson="A newly classified consideration.")])
        after = json.loads(self.memory.experience_path.read_text(encoding="utf-8"))
        self.assertEqual(after["lessons"][0], original)
        self.assertEqual(after["schema_version"], 1)

    def test_malformed_stored_tags_are_not_guessed_or_overwritten(self):
        self.record(lessons=[self.lesson()])
        baseline = json.loads(self.memory.experience_path.read_text(encoding="utf-8"))
        for changes in ({"scope": "unclassified"}, {"scope": "unsupported"}, {"scope": None},
                        {"scope": "scenario", "scenario_name": ""}, {"scenario_name": "Retaking Vierville"}):
            damaged = deepcopy(baseline)
            damaged["lessons"][0].update(changes)
            raw = json.dumps(damaged).encode("utf-8")
            self.memory.experience_path.write_bytes(raw)
            with self.subTest(changes=changes), self.assertRaises(GameMemoryError):
                self.memory.catalog(self.game)
            with self.assertRaises(GameMemoryError):
                self.record(lessons=[self.lesson()])
            self.assertEqual(self.memory.experience_path.read_bytes(), raw)

    def test_counterexample_and_revision_append_without_erasing_old_statement(self):
        identifier = self.record(lessons=[self.lesson()])["lesson_ids"][0]
        changed = self.lesson(existing_lesson_id=identifier, relationship="contradicts",
                              observed_result="A later reported counterexample did not support the earlier lesson.")
        self.record(lessons=[changed])
        record = self.read_lesson(identifier)
        self.assertEqual(record["status"], "contradicted")
        self.assertEqual(len(record["revisions"]), 2)
        revised = self.lesson(existing_lesson_id=identifier, relationship="revises",
                              lesson="Consider both fire coverage and movement costs.")
        self.record(lessons=[revised])
        record = self.read_lesson(identifier)
        self.assertEqual(record["status"], "candidate")
        self.assertEqual(len(record["revisions"]), 3)
        self.assertEqual(record["lesson"], self.lesson()["lesson"])
        self.assertEqual(record["revisions"][-1]["lesson"], revised["lesson"])

    def test_plan_only_and_assistant_only_evidence_cannot_create_lesson(self):
        self.assistant.status = "completed"
        for changes in ({"evidence_kind": "planned_result"},
                        {"source_message_ids": [self.assistant.message_id]},
                        {"source_message_ids": ["unknown-message"]}, {"source_message_ids": []}):
            with self.subTest(changes=changes), self.assertRaises(GameMemoryError):
                self.record(lessons=[self.lesson(**changes)])
        self.assertFalse(self.memory.experience_path.exists())

    def test_lesson_revision_requires_real_target_and_visible_journal_sources(self):
        for changes in ({"relationship": "contradicts"}, {"existing_lesson_id": "missing-lesson"},
                        {"journal_entry_ids": ["unseen-intent"]}):
            with self.subTest(changes=changes), self.assertRaises(GameMemoryError):
                self.record(lessons=[self.lesson(**changes)])
        self.assertFalse(self.memory.experience_path.exists())

    def test_application_prompts_never_become_player_learning_sources(self):
        prompt = self.game.add_message("user", "Create a save report.")
        answer = self.game.add_message("assistant", status="pending")
        self.context = self.turn_context(prompt, answer, mode="save", application_message_ids=[prompt.message_id])
        self.assertEqual(self.memory.pending_message_ids(self.game, self.context), [self.user.message_id])
        with self.assertRaises(GameMemoryError):
            self.record(lessons=[self.lesson(source_message_ids=[prompt.message_id])])
        with self.assertRaises(GameMemoryError):
            self.record(processed_message_ids=[prompt.message_id])
        self.record(processed_message_ids=[self.user.message_id])
        self.assertEqual(self.memory.pending_message_ids(self.game, self.context), [])
        # The exclusion continues after switching to a later normal turn.
        context = {**self.context, "mode": "normal"}
        self.assertEqual(self.memory.pending_message_ids(self.game, context), [])

    def test_review_before_game_end_is_durable_and_does_not_end_game(self):
        self.assistant.status = "completed"
        saved = self.memory.handle_tool(self.game, "record_review", {
            "title": "Review of the opening", "text": "The reported attack failed; the later outcome is unknown.",
            "source_message_ids": [self.user.message_id, self.assistant.message_id]}, context=self.context)
        self.assertFalse(saved["game_ended"])
        self.game.add_message("user", "Continue with the next phase.")
        restored = MemoryService(self.data).handle_tool(self.game, "read_tactical_journal",
                                                      {"entry_id": saved["review_id"]})["entry"]
        self.assertEqual(restored["kind"], "review")
        self.assertIn("game need not be finished", restored["scope"])

    def test_review_cannot_be_used_as_a_previous_tactical_intent(self):
        saved = self.memory.handle_tool(self.game, "record_review", {
            "title": "Opening", "text": "An incomplete review.", "source_message_ids": [self.user.message_id]},
            context=self.context)
        with self.assertRaises(GameMemoryError):
            self.record(intents=[self.intent(previous_entry_id=saved["review_id"])])

    def test_journal_is_paginated_from_first_intent_after_active_compaction(self):
        for index in range(35):
            self.record(intents=[self.intent(action=f"Decision {index}")])
        self.game.active_context = [{"type": "compaction", "encrypted_content": "opaque"}]
        offset, entries = 0, []
        while offset is not None:
            result = self.memory.handle_tool(self.game, "read_tactical_journal", {"offset": offset, "limit": 7})
            entries.extend(result["entries"])
            offset = result["next_offset"]
        self.assertEqual(len(entries), 35)
        self.assertEqual(entries[0]["action"], "Decision 0")
        self.assertEqual(entries[-1]["action"], "Decision 34")

    def test_unknown_fields_identity_spoofing_and_paths_are_rejected(self):
        for arguments in ({"model": "fake"}, {"path": "C:/secret"},
                          {"intents": [self.intent(recorded_at="yesterday")]},
                          {"lessons": [self.lesson(status="player-reviewed")]}):
            with self.subTest(arguments=arguments), self.assertRaises(GameMemoryError):
                self.record(**arguments)
        with self.assertRaises(GameMemoryError):
            self.memory.handle_tool(self.game, "search_experience", {"query": "", "path": "C:/secret"})
        self.assertFalse(self.data.exists())

    def test_oversized_requests_and_invalid_page_sizes_leave_originals_unchanged(self):
        self.record(intents=[self.intent()])
        before = self.journal_path().read_bytes()
        for arguments in ({"intents": [self.intent(action="x" * 2001)]},
                          {"intents": [self.intent()] * 13},
                          {"processed_message_ids": [self.user.message_id] * 31}):
            with self.subTest(arguments=arguments), self.assertRaises(GameMemoryError):
                self.record(**arguments)
        for limit in (0, -1, True, 31):
            with self.subTest(limit=limit), self.assertRaises(GameMemoryError):
                self.memory.handle_tool(self.game, "read_tactical_journal", {"limit": limit})
        self.assertEqual(self.journal_path().read_bytes(), before)

    def test_corrupt_json_and_journal_records_are_preserved(self):
        self.record(intents=[self.intent()])
        original = json.loads(self.journal_path().read_text(encoding="utf-8"))
        for data in (b"{broken", b"[]", json.dumps({**original, "entries": [None]}).encode(),
                     json.dumps({**original, "entries": [{"entry_id": "broken"}]}).encode(),
                     json.dumps({**original, "schema_version": True}).encode(),
                     json.dumps({**original, "session_id": "different-game"}).encode()):
            self.journal_path().write_bytes(data)
            with self.subTest(data=data[:100]), self.assertRaises(GameMemoryError):
                self.record(intents=[self.intent()])
            self.assertEqual(self.journal_path().read_bytes(), data)

    def test_corrupt_library_does_not_acknowledge_pending_sources_or_replace_file(self):
        self.memory.experience_path.parent.mkdir(parents=True)
        for raw in (b"not-json", b'{"schema_version":1,"lessons":[null]}',
                    b'{"schema_version":1,"lessons":[{"lesson_id":"bad","revisions":[]}]}'):
            self.memory.experience_path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(GameMemoryError):
                self.record(lessons=[self.lesson()], processed_message_ids=[self.user.message_id])
            self.assertEqual(self.memory.experience_path.read_bytes(), raw)
            self.assertEqual(self.memory.pending_message_ids(self.game), [self.user.message_id])
            self.assertFalse(self.journal_path().exists())

    def test_failed_atomic_replace_keeps_previous_journal_and_cleans_temporary_file(self):
        self.record(intents=[self.intent()])
        before = self.journal_path().read_bytes()
        with patch("vasl_ai_bridge.game_memory.os.replace", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaises(OSError):
                self.record(intents=[self.intent(action="A new plan")], processed_message_ids=[self.user.message_id])
        self.assertEqual(self.journal_path().read_bytes(), before)
        self.assertEqual(list(self.journal_path().parent.glob(".memory-*.tmp")), [])
        self.assertEqual(self.memory.pending_message_ids(self.game), [self.user.message_id])

    def test_failed_library_write_never_advances_journal_watermark(self):
        with patch.object(self.memory, "_write", side_effect=OSError("Synthetic library failure")):
            with self.assertRaises(OSError):
                self.record(lessons=[self.lesson()], processed_message_ids=[self.user.message_id])
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(self.memory.pending_message_ids(self.game), [self.user.message_id])

    def test_partial_multifile_failure_is_retry_safe_and_keeps_learning_pending(self):
        write = self.memory._write

        def fail_journal(path, data):
            if path == self.journal_path():
                raise OSError("Synthetic journal failure after the lesson write")
            write(path, data)

        arguments = {"intents": [self.intent()], "lessons": [self.lesson()],
                     "processed_message_ids": [self.user.message_id]}
        with patch.object(self.memory, "_write", side_effect=fail_journal):
            with self.assertRaises(OSError):
                self.record(**arguments)
        self.assertTrue(self.memory.experience_path.exists())
        self.assertEqual(self.memory.pending_message_ids(self.game), [self.user.message_id])
        saved = self.record(**arguments)
        self.assertEqual(saved["pending_message_ids"], [])
        self.assertEqual(len(self.read_lesson(saved["lesson_ids"][0])["revisions"]), 1)

    def test_failed_canonical_adoption_retains_fallback_for_retry(self):
        self.record(intents=[self.intent()])
        fallback = self.journal_path()
        before = fallback.read_bytes()
        self.folder()
        with patch.object(self.memory, "_write", side_effect=OSError("Synthetic adoption failure")):
            with self.assertRaises(OSError):
                self.record(processed_message_ids=[self.user.message_id])
        self.assertEqual(fallback.read_bytes(), before)
        self.assertFalse(self.journal_path().exists())
        self.record(processed_message_ids=[self.user.message_id])
        self.assertFalse(fallback.exists())

    def test_tool_catalog_and_application_route_exercise_actual_memory_callbacks(self):
        names = {item["name"] for item in DYNAMIC_TOOLS}
        self.assertTrue(MEMORY_TOOL_NAMES.issubset(names))
        for tool in DYNAMIC_TOOLS:
            if tool["name"] in MEMORY_TOOL_NAMES:
                self.assertFalse(tool["inputSchema"]["additionalProperties"])
        result = handle_game_tool(self.game, "record_turn_memory", {"intents": [self.intent()],
                                  "processed_message_ids": [self.user.message_id]},
                                  memory=self.memory, context=self.context)
        retrieved = handle_game_tool(self.game, "read_tactical_journal", {"entry_id": result["intent_ids"][0]},
                                     memory=self.memory, context=self.context)
        self.assertEqual(retrieved["entry"]["goal"], "Preserve the squad")
        with self.assertRaises(ValueError):
            handle_game_tool(self.game, "record_turn_memory", {})
        self.assertIn("During every ordinary reply", ASL_INSTRUCTIONS)
        self.assertIn("Automatic full-game review is temporarily unavailable", ASL_INSTRUCTIONS)

    def test_deleted_images_are_explicit_in_original_history_and_lesson_provenance(self):
        image = prepare_image(Image.new("RGB", (8, 8), "blue"), "Synthetic board")
        self.user.attachments = [image]
        self.folder()
        identifier = self.record(lessons=[self.lesson()])["lesson_ids"][0]
        before = self.read_lesson(identifier)["revisions"][0]["source_evidence"][0]["images"][0]
        self.assertTrue(before["original_available"])
        self.assertEqual(self.folders.clear_screenshots(self.context["game_folder"]), 1)
        cleaned = self.folders.load_point(self.context["game_folder"])
        history = handle_game_tool(cleaned, "read_game_message", {"message_id": self.user.message_id})
        self.assertFalse(history["images"][0]["original_available"])
        image_result = handle_game_tool(cleaned, "read_game_image", {"observation_id": image.observation_id})
        self.assertEqual(len(image_result["contentItems"]), 1)
        self.assertFalse(json.loads(image_result["contentItems"][0]["text"])["original_available"])
        evidence = self.read_lesson(identifier)["revisions"][0]["source_evidence"][0]["images"][0]
        self.assertFalse(evidence["original_available"])
        self.assertTrue(evidence["original_available_at_recording"])
        self.assertIn("Deleted", evidence["availability_note"])
        # Learned text survives even when board originals are gone.
        self.assertEqual(self.memory.catalog(cleaned, self.context)["shared_lesson_count"], 1)

    def test_removed_source_game_does_not_delete_lesson_or_claim_original_available(self):
        image = prepare_image(Image.new("RGB", (8, 8), "blue"), "Synthetic board")
        self.user.attachments = [image]
        folder = self.folder()
        identifier = self.record(lessons=[self.lesson()])["lesson_ids"][0]
        database = self.folders.database_path(folder)
        database.unlink()  # A synthetic source database only.
        evidence = self.read_lesson(identifier)["revisions"][0]["source_evidence"][0]["images"][0]
        self.assertFalse(evidence["original_available"])
        self.assertEqual(self.memory.catalog(ChatSession())["shared_lesson_count"], 1)

    def test_rewound_history_cannot_expose_deleted_image_marker_from_future_messages(self):
        self.game.legacy_metadata["asl_ai_deleted_images"] = {
            "later-image": {"message_ids": ["later-message"], "captured_at": self.user.created_at}}
        history = handle_game_tool(self.game, "read_game_history", {})
        self.assertEqual(history["messages"][0]["images"], [])
        with self.assertRaises(ValueError):
            handle_game_tool(self.game, "read_game_image", {"observation_id": "later-image"})


if __name__ == "__main__":
    unittest.main()
