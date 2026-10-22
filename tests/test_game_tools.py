"""Archive retrieval retains chronology and actual original image bytes."""

import base64
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from vasl_ai_bridge.capture import prepare_image
from vasl_ai_bridge.chat_session import ChatSession, Reference
from vasl_ai_bridge.game_tools import handle_game_tool
from vasl_ai_bridge.game_memory import GameMemoryError, MemoryService


class GameToolTests(unittest.TestCase):
    def test_early_history_is_readable_after_active_context_compaction(self):
        game = ChatSession()
        for index in range(125):
            game.add_message("user", f"Original turn record {index}")
        game.active_context = [{"type": "compaction", "encrypted_content": "opaque"}]
        page = handle_game_tool(game, "read_game_history", {"offset": 0, "limit": 2})
        self.assertEqual(page["messages"][0]["text"], "Original turn record 0")
        self.assertEqual(page["next_offset"], 2)
        self.assertEqual(page["total_messages"], 125)
        self.assertEqual(len(game.messages), 125)

    def test_image_tool_returns_exact_archived_png(self):
        game = ChatSession()
        image = prepare_image(Image.new("RGB", (32, 32), "blue"), "Synthetic board")
        message = game.add_message("user", "First deployment", attachments=[image])
        reply = handle_game_tool(game, "read_game_image", {"observation_id": image.observation_id})
        self.assertEqual(base64.b64decode(reply["contentItems"][1]["imageUrl"].split(",", 1)[1]), image.png)
        self.assertIn(message.message_id, reply["contentItems"][0]["text"])
        with self.assertRaises(ValueError):
            handle_game_tool(game, "read_game_image", {"observation_id": "other-game"})

    def test_reference_excerpts_are_paginated_and_local_paths_rejected(self):
        game = ChatSession(references=[Reference("rules", "rules.md", "text/markdown", b"Rules text from the user.")])
        first = handle_game_tool(game, "read_reference_file", {"reference_id": "rules", "max_chars": 12})
        self.assertEqual(first["next_offset"], 12)
        self.assertEqual(first["source_sha256"], game.references[0].sha256)
        with self.assertRaises(ValueError):
            handle_game_tool(game, "read_reference_file", {"reference_id": "C:/secret.txt"})
        with self.assertRaises(ValueError):
            handle_game_tool(game, "shell", {"command": "anything"})

    def test_long_records_can_be_read_completely_in_valid_bounded_chunks(self):
        import json
        game = ChatSession()
        original = "Long original statement " * 10000
        first = game.add_message("assistant", original)
        for _ in range(30):
            game.add_message("user", original)
        batch = handle_game_tool(game, "read_game_history", {"offset": 0, "limit": 30})
        self.assertLess(len(json.dumps(batch, ensure_ascii=False)), 64000)
        self.assertIsNotNone(batch["next_offset"])
        recovered = ""
        offset = 0
        while offset is not None:
            page = handle_game_tool(game, "read_game_message", {"message_id": first.message_id, "offset": offset})
            recovered += page["text"]
            offset = page["next_text_offset"]
        self.assertEqual(recovered, original)

    def test_experience_routes_keep_scenario_lessons_out_of_general_reads(self):
        game = ChatSession()
        user = game.add_message("user", "The reported road crossing failed under defensive fire.")
        assistant = game.add_message("assistant", "Use cover.", status="pending")
        context = {"model": "synthetic-model", "thread_id": "synthetic-thread", "turn_id": "synthetic-turn",
                   "anchor_message_id": user.message_id, "assistant_message_id": assistant.message_id,
                   "mode": "normal", "application_message_ids": []}
        common = {"title": "Road exposure", "lesson": "Consider covered approaches to an observed road.",
                  "observed_result": "The player reported the road crossing failed.",
                  "evidence_kind": "reported_outcome", "source_message_ids": [user.message_id]}
        with tempfile.TemporaryDirectory(prefix="asl-ai-scoped-tools-") as directory:
            memory = MemoryService(Path(directory))
            saved = handle_game_tool(game, "record_turn_memory", {"lessons": [
                {**common, "scope": "general", "scenario_name": ""},
                {**common, "scope": "scenario", "scenario_name": "Retaking Vierville"},
            ]}, memory=memory, context=context)
            general_id, scenario_id = saved["lesson_ids"]
            default = handle_game_tool(game, "search_experience", {"query": "road"}, memory=memory)
            self.assertEqual([item["lesson_id"] for item in default["matches"]], [general_id])
            specific = handle_game_tool(game, "search_experience", {
                "query": "road", "scope": "scenario", "scenario_name": "Retaking Vierville"}, memory=memory)
            self.assertEqual([item["lesson_id"] for item in specific["matches"]], [scenario_id])
            with self.assertRaises(GameMemoryError):
                handle_game_tool(game, "read_experience", {"lesson_id": scenario_id}, memory=memory)
            for arguments in ({"scope": "scenario"}, {"scope": "scenario", "scenario_name": "Other scenario"}):
                with self.assertRaises(GameMemoryError):
                    handle_game_tool(game, "read_experience", {"lesson_id": scenario_id, **arguments}, memory=memory)
            read = handle_game_tool(game, "read_experience", {"lesson_id": scenario_id,
                "scope": "scenario", "scenario_name": "Retaking Vierville", "max_chars": 24000}, memory=memory)
            lesson = json.loads(read["text"])
            self.assertEqual(lesson["scope"], "scenario")
            self.assertEqual(lesson["scenario_name"], "Retaking Vierville")


if __name__ == "__main__":
    unittest.main()
