"""Synthetic reference images survive imports, saves, and model tool reads."""

import base64
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PIL.PngImagePlugin import PngInfo
from PySide6.QtWidgets import QApplication

from vasl_ai_bridge.chat_session import ChatSession, Reference
from vasl_ai_bridge.chat_storage import ChatStore
from vasl_ai_bridge.game_tools import handle_game_tool
from vasl_ai_bridge.reference_formats import (
    ReferenceTextError, REFERENCE_FILE_FILTER, reference_capabilities,
    render_reference_image, validate_reference_import,
)
from vasl_ai_bridge.reference_library import ReferenceLibrary


APP = QApplication.instance() or QApplication(["Reference image tests"])


def image_fixture(format="PNG", *, orientation=None):
    image = Image.new("RGB", (24, 16), "blue")
    output = BytesIO()
    options = {}
    if format == "PNG":
        metadata = PngInfo()
        metadata.add_text("Synthetic note", "Keep this original evidence chunk.")
        options["pnginfo"] = metadata
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        options["exif"] = exif
    image.save(output, format=format, **options)
    image.close()
    return output.getvalue()


def decoded_image(reply):
    return base64.b64decode(reply["contentItems"][1]["imageUrl"].partition(",")[2])


class ReferenceImageTests(unittest.TestCase):
    def test_png_tool_returns_exact_original_and_source_hash_after_save_reload(self):
        with tempfile.TemporaryDirectory(prefix=".reference-image-test-", dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory)
            original = image_fixture()
            source = root / "order-of-battle.png"
            source.write_bytes(original)
            library = ReferenceLibrary(root / "library")
            imported = library.import_file(source)
            library.set_selected([imported.reference_id])
            game = ChatSession(references=library.selected_references())
            path = ChatStore(root / "store").save(game, root / "image.aslgame")
            source.unlink()
            restored = ChatStore(root / "different-store").load(path)
            reply = handle_game_tool(restored, "read_reference_image", {"reference_id": imported.reference_id})
            self.assertEqual(decoded_image(reply), original)
            metadata = json.loads(reply["contentItems"][0]["text"])
            self.assertEqual(metadata["source_sha256"], imported.sha256)
            self.assertEqual(metadata["reference_id"], imported.reference_id)
            self.assertEqual(metadata["image_representation"], "original_png")
            self.assertEqual(restored.references[0].content, original)

    def test_other_supported_images_are_derived_pngs_with_originals_preserved(self):
        for suffix, format in (("jpg", "JPEG"), ("jpeg", "JPEG"), ("webp", "WEBP"), ("bmp", "BMP")):
            with self.subTest(suffix=suffix):
                original = image_fixture(format)
                reference = Reference("note", "note." + suffix, "image/" + suffix, original)
                game = ChatSession(references=[reference])
                reply = handle_game_tool(game, "read_reference_image", {"reference_id": "note"})
                with Image.open(BytesIO(decoded_image(reply))) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.size, (24, 16))
                self.assertEqual(reference.content, original)
                self.assertEqual(json.loads(reply["contentItems"][0]["text"])["source_sha256"], reference.sha256)

    def test_jpeg_exif_orientation_is_applied_to_derived_png(self):
        original = image_fixture("JPEG", orientation=6)
        result = render_reference_image("portrait.jpg", original)
        with Image.open(BytesIO(base64.b64decode(result.partition(",")[2]))) as image:
            self.assertEqual(image.size, (16, 24))
            self.assertNotEqual(image.getexif().get(274), 6)

    def test_invalid_mismatched_animated_and_oversized_images_are_rejected(self):
        for name, content in (("fake.png", b"\x89PNG\r\n\x1a\nnot valid"),
                              ("wrong.jpg", image_fixture("PNG")),
                              ("truncated.png", image_fixture("PNG")[:-12])):
            with self.subTest(name=name), self.assertRaises(ReferenceTextError):
                validate_reference_import(name, content)
        first = Image.new("RGB", (8, 8), "blue")
        second = Image.new("RGB", (8, 8), "red")
        output = BytesIO()
        first.save(output, "PNG", save_all=True, append_images=[second])
        first.close()
        second.close()
        with self.assertRaisesRegex(ReferenceTextError, "Animated"):
            validate_reference_import("animated.png", output.getvalue())
        with patch("vasl_ai_bridge.reference_formats.MAX_REFERENCE_IMAGE_BYTES", 16):
            with self.assertRaisesRegex(ReferenceTextError, "image limit"):
                render_reference_image("large.png", image_fixture())
        with patch("vasl_ai_bridge.reference_formats.MAX_REFERENCE_IMAGE_PIXELS", 8):
            with self.assertRaisesRegex(ReferenceTextError, "dimensions"):
                validate_reference_import("large.png", image_fixture())

    def test_image_tool_accepts_only_selected_reference_ids_and_page_one(self):
        game = ChatSession(references=[Reference("selected", "note.png", "image/png", image_fixture())])
        legacy_reply = handle_game_tool(game, "read_reference_page", {"reference_id": "selected", "page": 1})
        self.assertEqual(decoded_image(legacy_reply), game.references[0].content)
        for identifier in ("unselected", "D:/private/note.png"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                handle_game_tool(game, "read_reference_image", {"reference_id": identifier})
        with self.assertRaisesRegex(ReferenceTextError, "page 1"):
            handle_game_tool(game, "read_reference_image", {"reference_id": "selected", "page": 2})
        with self.assertRaisesRegex(ReferenceTextError, "page 1"):
            handle_game_tool(game, "read_reference_page", {"reference_id": "selected", "page": 2})

    def test_pdf_pages_use_same_image_reader_and_preserve_old_page_alias(self):
        output = BytesIO()
        first = Image.new("RGB", (40, 30), "blue")
        second = Image.new("RGB", (40, 30), "red")
        first.save(output, "PDF", save_all=True, append_images=[second])
        first.close()
        second.close()
        reference = Reference("scan", "scan.pdf", "application/pdf", output.getvalue())
        game = ChatSession(references=[reference])
        for name in ("read_reference_image", "read_reference_page"):
            reply = handle_game_tool(game, name, {"reference_id": "scan", "page": 2})
            self.assertTrue(decoded_image(reply).startswith(b"\x89PNG\r\n\x1a\n"))
            metadata = json.loads(reply["contentItems"][0]["text"])
            self.assertEqual(metadata["physical_page"], 2)
            self.assertEqual(metadata["source_sha256"], reference.sha256)
        with self.assertRaises(ReferenceTextError):
            handle_game_tool(game, "read_reference_image", {"reference_id": "scan", "page": 3})

    def test_images_expose_reader_guidance_without_fabricating_indexed_text(self):
        reference = Reference("note", "scenario-note.png", "image/png", image_fixture(),
                              indexed_text="", extraction_error="This format has no text reader.")
        game = ChatSession(references=[reference])
        reply = handle_game_tool(game, "read_reference_file", {"reference_id": "note"})
        self.assertEqual(reply["text"], "")
        self.assertEqual(reply["indexing_error"], "")
        self.assertEqual(reply["reader"], "read_reference_image")
        result = handle_game_tool(game, "search_reference_files", {"query": "scenario-note"})["matches"][0]
        self.assertTrue(result["image_available"])
        self.assertEqual(result["indexing_error"], "")
        self.assertEqual(reference_capabilities("rules.pdf")["image_reader"], "read_reference_image")
        self.assertNotIn("*.*", REFERENCE_FILE_FILTER)
        self.assertNotIn("*.exe", REFERENCE_FILE_FILTER)


if __name__ == "__main__":
    unittest.main()
