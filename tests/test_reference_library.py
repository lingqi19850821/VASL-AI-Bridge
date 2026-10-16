"""Reusable private references and portable selected-reference archives."""

from pathlib import Path
from contextlib import closing
from io import BytesIO
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PIL import Image

from vasl_ai_bridge.chat_session import ChatSession, Reference
from vasl_ai_bridge.chat_storage import ChatStore
from vasl_ai_bridge.game_tools import handle_game_tool
from vasl_ai_bridge.reference_library import ReferenceLibrary
from vasl_ai_bridge.reference_formats import render_reference_image
from vasl_ai_bridge.storage import StorageError


APP = QApplication.instance() or QApplication(["Reference library tests"])


class ReferenceLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=".asl-ai-reference-test-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = ReferenceLibrary(self.root / "private")

    def source(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_constructor_and_empty_listing_create_no_files(self):
        self.assertEqual(self.library.list_references(), [])
        self.assertEqual(self.library.selected_references(), [])
        self.assertEqual(self.library.set_selected([]), [])
        self.assertFalse(self.library.root.exists())

    def test_import_is_deduplicated_and_keeps_original_after_source_removed(self):
        source = self.source("rules.md", "# Local rules\nMovement source.")
        original = source.read_bytes()
        first = self.library.import_file(source)
        renamed = self.source("same-file.txt", source.read_text(encoding="utf-8"))
        again = self.library.import_file(renamed)
        self.assertEqual(first.reference_id, again.reference_id)
        self.assertEqual(len(self.library.list_references()), 1)
        source.unlink()
        renamed.unlink()
        restored = ReferenceLibrary(self.library.root).list_references()[0]
        self.assertEqual(restored.content, original)
        self.assertIn("Movement source", restored.text())

    def test_remembered_selection_exposes_only_chosen_references(self):
        first = self.library.import_file(self.source("chosen.txt", "Selected movement rule"))
        hidden = self.library.import_file(self.source("hidden.txt", "Private unselected movement rule"))
        self.library.set_selected([first.reference_id])
        restored = ReferenceLibrary(self.library.root)
        game = ChatSession(references=restored.selected_references())
        self.assertEqual([reference.reference_id for reference in game.references], [first.reference_id])
        matches = handle_game_tool(game, "search_reference_files", {"query": "movement"})["matches"]
        self.assertEqual([match["reference_id"] for match in matches], [first.reference_id])
        with self.assertRaises(ValueError):
            handle_game_tool(game, "read_reference_file", {"reference_id": hidden.reference_id})

    def test_invalid_selection_leaves_previous_choice_intact(self):
        reference = self.library.import_file(self.source("rules.txt", "Local rule"))
        self.library.set_selected([reference.reference_id])
        with self.assertRaises(StorageError):
            self.library.set_selected([reference.reference_id, "unknown-file"])
        self.assertEqual([item.reference_id for item in self.library.selected_references()], [reference.reference_id])
        self.library.set_selected([])
        self.assertEqual(self.library.selected_references(), [])
        self.assertEqual(len(self.library.list_references()), 1)

    def test_empty_removal_and_compaction_create_no_library(self):
        self.assertEqual(self.library.remove_references([]), [])
        self.assertEqual(self.library.remove_references([], selected_ids=[]), [])
        self.library.compact()
        for arguments in ((["unknown"], None), ([], ["unknown"])):
            with self.subTest(arguments=arguments), self.assertRaises(StorageError):
                self.library.remove_references(arguments[0], selected_ids=arguments[1])
        self.assertFalse(self.library.root.exists())

    def test_removing_multiple_imported_copies_preserves_source_files(self):
        sources = [self.source(f"rule-{number}.txt", f"Synthetic source {number}") for number in range(3)]
        originals = [source.read_bytes() for source in sources]
        references = [self.library.import_file(source) for source in sources]
        self.library.set_selected([reference.reference_id for reference in references])
        selected = self.library.remove_references([references[0].reference_id, references[2].reference_id])
        self.assertEqual([reference.reference_id for reference in selected], [references[1].reference_id])
        reopened = ReferenceLibrary(self.library.root)
        self.assertEqual([reference.reference_id for reference in reopened.list_references()], [references[1].reference_id])
        self.assertEqual([reference.reference_id for reference in reopened.selected_references()], [references[1].reference_id])
        self.assertEqual([source.read_bytes() for source in sources], originals)
        with closing(sqlite3.connect(self.library.path)) as connection:
            self.assertEqual(connection.execute("SELECT indexed_text FROM refs").fetchall(), [(references[1].indexed_text,)])

    def test_removal_preserves_remaining_remembered_order(self):
        references = [self.library.import_file(self.source(f"{number}.txt", f"Rule {number}")) for number in range(3)]
        self.library.set_selected([reference.reference_id for reference in reversed(references)])
        selected = self.library.remove_references([references[1].reference_id])
        expected = [references[2].reference_id, references[0].reference_id]
        self.assertEqual([reference.reference_id for reference in selected], expected)
        self.assertEqual([reference.reference_id for reference in self.library.selected_references()], expected)

    def test_removal_atomically_uses_current_checkbox_selection(self):
        references = [self.library.import_file(self.source(f"{number}.txt", f"Rule {number}")) for number in range(4)]
        self.library.set_selected([references[0].reference_id])
        selected = self.library.remove_references(
            [references[0].reference_id, references[0].reference_id],
            selected_ids=[references[3].reference_id, references[0].reference_id, references[2].reference_id],
        )
        expected = [references[3].reference_id, references[2].reference_id]
        self.assertEqual([reference.reference_id for reference in selected], expected)
        self.assertEqual([reference.reference_id for reference in ReferenceLibrary(self.library.root).selected_references()], expected)
        self.assertEqual(len(self.library.list_references()), 3)

    def test_unknown_removal_or_checkbox_id_leaves_all_library_state_intact(self):
        references = [self.library.import_file(self.source(f"{number}.txt", f"Rule {number}")) for number in range(2)]
        ids = [reference.reference_id for reference in references]
        self.library.set_selected(list(reversed(ids)))
        original = self.library.path.read_bytes()
        for removed, selected in (([ids[0], "unknown"], [ids[1]]), ([ids[0]], [ids[1], "unknown"])):
            with self.subTest(removed=removed, selected=selected), self.assertRaises(StorageError):
                self.library.remove_references(removed, selected_ids=selected)
            self.assertEqual(self.library.path.read_bytes(), original)
            self.assertEqual([reference.reference_id for reference in self.library.list_references()], ids)
            self.assertEqual([reference.reference_id for reference in self.library.selected_references()], list(reversed(ids)))

    def test_database_failure_rolls_back_removal_and_selection_change(self):
        references = [self.library.import_file(self.source(f"{number}.txt", f"Rule {number}")) for number in range(2)]
        ids = [reference.reference_id for reference in references]
        self.library.set_selected([ids[0]])
        with closing(sqlite3.connect(self.library.path)) as connection, connection:
            connection.execute("CREATE TRIGGER fail_removal BEFORE DELETE ON refs "
                               "BEGIN SELECT RAISE(ABORT, 'Synthetic removal failure'); END")
        with self.assertRaisesRegex(StorageError, "Synthetic removal failure"):
            self.library.remove_references([ids[0]], selected_ids=[ids[1]])
        reopened = ReferenceLibrary(self.library.root)
        self.assertEqual([reference.reference_id for reference in reopened.list_references()], ids)
        self.assertEqual([reference.reference_id for reference in reopened.selected_references()], [ids[0]])

    def test_explicit_import_can_add_a_removed_source_again(self):
        source = self.source("rules.txt", "Synthetic source for explicit reimport")
        first = self.library.import_file(source)
        self.library.set_selected([first.reference_id])
        self.assertEqual(self.library.remove_references([first.reference_id]), [])
        self.assertEqual(self.library.list_references(), [])
        imported = self.library.import_file(source)
        self.assertNotEqual(imported.reference_id, first.reference_id)
        self.assertEqual(imported.content, first.content)
        self.assertEqual(self.library.selected_references(), [])
        self.assertEqual(len(self.library.list_references()), 1)

    def test_compaction_reclaims_space_without_changing_remaining_references(self):
        large_source = self.source("large.txt", "Synthetic large reference.\n" * 100000)
        large = self.library.import_file(large_source)
        kept = self.library.import_file(self.source("kept.txt", "Keep this source"))
        self.library.set_selected([large.reference_id, kept.reference_id])
        self.library.remove_references([large.reference_id])
        before = self.library.path.stat().st_size
        self.library.compact()
        self.assertLess(self.library.path.stat().st_size, before // 2)
        self.assertEqual([reference.reference_id for reference in self.library.list_references()], [kept.reference_id])
        self.assertEqual([reference.reference_id for reference in self.library.selected_references()], [kept.reference_id])
        self.assertEqual(large_source.read_bytes(), large.content)

    def test_compaction_failure_does_not_undo_a_committed_removal(self):
        reference = self.library.import_file(self.source("rules.txt", "Synthetic rule"))
        self.library.set_selected([reference.reference_id])
        self.library.remove_references([reference.reference_id])
        with patch("vasl_ai_bridge.reference_library.sqlite3.connect", side_effect=sqlite3.OperationalError("Synthetic compact failure")):
            with self.assertRaisesRegex(StorageError, "Synthetic compact failure"):
                self.library.compact()
        self.assertEqual(self.library.list_references(), [])
        self.assertEqual(self.library.selected_references(), [])

    def test_adopting_saved_refs_preserves_saved_ids_and_remembered_choice(self):
        chosen = self.library.import_file(self.source("chosen.txt", "Already selected"))
        self.library.set_selected([chosen.reference_id])
        saved = Reference("old-save-id", "archived.txt", "text/plain", b"Archived source")
        adopted = self.library.adopt_references([saved])
        self.assertEqual(adopted[0].reference_id, saved.reference_id)
        duplicate = Reference("other-save-id", "duplicate.txt", "text/plain", saved.content)
        canonical = self.library.adopt_references([duplicate])[0]
        self.assertEqual(canonical.reference_id, saved.reference_id)
        self.assertEqual(duplicate.reference_id, "other-save-id")
        self.assertIsNone(saved.indexed_text)
        self.assertEqual([item.reference_id for item in self.library.selected_references()], [chosen.reference_id])

    def test_unsupported_or_invalid_new_imports_create_no_library(self):
        for name, content in (("program.exe", b"MZ"), ("notes.png", b"not an image"),
                              ("binary.txt", b"\x00\x01\x02"), ("broken.pdf", b"not a pdf"),
                              ("broken.docx", b"not a zip")):
            path = self.root / name
            path.write_bytes(content)
            with self.subTest(name=name), self.assertRaises(StorageError):
                self.library.import_file(path)
            self.assertFalse(self.library.root.exists())

    def test_rejected_import_leaves_existing_library_and_selection_unchanged(self):
        chosen = self.library.import_file(self.source("chosen.txt", "Chosen source"))
        self.library.set_selected([chosen.reference_id])
        original = self.library.path.read_bytes()
        invalid = self.source("unsupported.bin", "Chosen source")
        with self.assertRaises(StorageError):
            self.library.import_file(invalid)
        self.assertEqual(self.library.path.read_bytes(), original)
        self.assertEqual([reference.reference_id for reference in self.library.selected_references()], [chosen.reference_id])

    def test_corrupt_docx_deflate_is_rejected_without_creating_library(self):
        output = BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic note.</w:t></w:r></w:p></w:body></w:document>')
        content = bytearray(output.getvalue())
        with ZipFile(BytesIO(content)) as archive:
            info = archive.getinfo("word/document.xml")
            payload_offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        # DEFLATE block type 3 is invalid; the ZIP directory still parses.
        content[payload_offset] = 6
        source = self.root / "corrupted.docx"
        source.write_bytes(content)
        with self.assertRaisesRegex(StorageError, "DOCX file could not be read"):
            self.library.import_file(source)
        self.assertFalse(self.library.root.exists())

    def test_legacy_unsupported_originals_are_adopted_without_new_import_validation(self):
        legacy = Reference("legacy-binary", "legacy.bin", "application/octet-stream", b"\x00\x01Legacy original")
        adopted = self.library.adopt_references([legacy])[0]
        self.assertEqual(adopted.content, legacy.content)
        self.assertEqual(self.library.list_references()[0].content, legacy.content)

    def test_supported_reimport_upgrades_legacy_reader_without_changing_saved_original(self):
        output = BytesIO()
        with Image.new("RGB", (16, 12), "blue") as image:
            image.save(output, "PNG")
        original = output.getvalue()
        legacy = Reference("legacy-note", "order-of-battle.bin", "application/octet-stream", original,
                           source_name="legacy-source.bin", indexed_text="Legacy cached index", extraction_error="Old unsupported reader")
        self.library.adopt_references([legacy])
        other = self.library.import_file(self.source("chosen.txt", "Other selected reference"))
        selected_ids = [other.reference_id, legacy.reference_id]
        self.library.set_selected(selected_ids)
        store = ChatStore(self.root / "games")
        archived = store.save(ChatSession(references=[legacy]), self.root / "prior.aslgame")
        archived_bytes = archived.read_bytes()
        source = self.root / "order-of-battle.png"
        source.write_bytes(original)
        supported = self.library.import_file(source)
        self.assertEqual(supported.reference_id, legacy.reference_id)
        self.assertEqual(supported.content, original)
        self.assertEqual(supported.sha256, legacy.sha256)
        self.assertEqual(supported.added_at, legacy.added_at)
        self.assertEqual(supported.name, source.name)
        self.assertEqual(supported.media_type, "image/png")
        self.assertEqual(supported.source_name, source.name)
        self.assertEqual(supported.indexed_text, "")
        self.assertEqual(supported.extraction_error, "")
        selected = self.library.selected_references()
        self.assertEqual([reference.reference_id for reference in selected], selected_ids)
        self.assertEqual(len(self.library.list_references()), 2)
        reply = handle_game_tool(ChatSession(references=selected), "read_reference_image", {"reference_id": legacy.reference_id})
        import base64
        self.assertEqual(base64.b64decode(reply["contentItems"][1]["imageUrl"].partition(",")[2]), original)
        self.assertEqual(legacy.name, "order-of-battle.bin")
        self.assertEqual(legacy.source_name, "legacy-source.bin")
        self.assertEqual(legacy.indexed_text, "Legacy cached index")
        self.assertEqual(legacy.extraction_error, "Old unsupported reader")
        self.assertEqual(archived.read_bytes(), archived_bytes)
        self.assertEqual(store.load(archived).references[0].name, "order-of-battle.bin")

    def test_derived_image_transport_limit_is_checked_before_new_import(self):
        output = BytesIO()
        with Image.effect_noise((128, 128), 100) as image:
            image.save(output, "JPEG", quality=25)
        source_bytes = output.getvalue()
        import base64
        png_bytes = base64.b64decode(render_reference_image("noise.jpg", source_bytes).partition(",")[2])
        self.assertGreater(len(png_bytes), len(source_bytes))
        synthetic_limit = (len(source_bytes) + len(png_bytes)) // 2
        source = self.root / "noise.jpg"
        source.write_bytes(source_bytes)
        with patch("vasl_ai_bridge.reference_formats.MAX_REFERENCE_IMAGE_BYTES", synthetic_limit):
            with self.assertRaisesRegex(StorageError, "rendered reference image exceeds"):
                self.library.import_file(source)
        self.assertFalse(self.library.root.exists())

    def test_legacy_metadata_upgrade_database_error_is_reported_as_storage_error(self):
        import sqlite3
        legacy = Reference("legacy-text", "legacy.bin", "application/octet-stream", b"Synthetic rule")
        source = self.source("rule.txt", "Synthetic rule")
        with patch.object(self.library, "_read", return_value=[legacy]):
            with patch("vasl_ai_bridge.reference_library.sqlite3.connect", side_effect=sqlite3.OperationalError("synthetic locked database")):
                with self.assertRaisesRegex(StorageError, "synthetic locked database"):
                    self.library.import_file(source)

    def test_selected_game_archive_is_self_contained_and_snapshots_are_independent(self):
        reference = self.library.import_file(self.source("rules.txt", "Local movement rule"))
        self.library.set_selected([reference.reference_id])
        game = ChatSession(references=self.library.selected_references())
        saved = ChatStore(self.root / "games").save(game, self.root / "portable.aslgame")
        game.references[0].indexed_text = "Changed only in this in-memory game"
        self.assertIn("Local movement", self.library.selected_references()[0].text())
        self.library.path.unlink()
        loaded = ChatStore(self.root / "different-computer").load(saved)
        self.assertEqual(loaded.references[0].content, reference.content)
        self.assertIn("Local movement", loaded.references[0].text())


if __name__ == "__main__":
    unittest.main()
