"""Archive retrieval retains chronology and actual original image bytes."""

import base64
import unittest

from PIL import Image

from vasl_ai_bridge.capture import prepare_image
from vasl_ai_bridge.chat_session import ChatSession, Reference
from vasl_ai_bridge.game_tools import handle_game_tool


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


if __name__ == "__main__":
    unittest.main()
