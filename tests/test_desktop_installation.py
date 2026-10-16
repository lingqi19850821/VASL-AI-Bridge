"""Exercise installation-wide DLL discovery and native interface identification."""

from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from vasl_ai_bridge.desktop_installation import (CONST_ME_EXPORTS, CPP_EXPORTS, DesktopDll,
                                         find_desktop_dlls, identify_desktop_dll,
                                         read_dll_exports, resolve_desktop_dll)


def exported_pe(names):
    image = bytearray(2048)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 60, 64)
    image[64:68] = b"PE\0\0"
    struct.pack_into("<HH", image, 68, 0x8664, 1)
    struct.pack_into("<HH", image, 84, 240, 0x2000)
    struct.pack_into("<H", image, 88, 0x20B)
    struct.pack_into("<I", image, 148, 512)
    struct.pack_into("<I", image, 196, 16)
    struct.pack_into("<II", image, 200, 4096, 40)
    struct.pack_into("<IIII", image, 336, 1536, 4096, 1536, 512)
    struct.pack_into("<I", image, 536, len(names))
    struct.pack_into("<I", image, 544, 4160)
    offset = 768
    for index, name in enumerate(names):
        struct.pack_into("<I", image, 576 + index * 4, 4096 + offset - 512)
        encoded = name.encode("ascii") + b"\0"
        image[offset:offset + len(encoded)] = encoded
        offset += len(encoded)
    return image


class DesktopInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.program = self.root / "arbitrary renamed application.exe"
        self.program.write_bytes(b"Program path anchors the search only")

    def dll(self, relative="Whisper.dll", contents=b"fixture"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path.resolve()

    def test_search_includes_current_folder_and_all_child_levels_with_case_variants(self):
        current = self.dll()
        deep = self.dll("assets/more/native/WHISPER.DLL")
        self.dll("assets/other.dll")
        outside = self.root.parent / (self.root.name + "-outside-whisper.dll")
        self.assertEqual(set(find_desktop_dlls(self.program)), {current, deep})
        self.assertNotIn(outside, find_desktop_dlls(self.program))
        self.assertEqual(set(find_desktop_dlls(self.root)), {current, deep})

    def test_program_filename_is_irrelevant_and_nested_path_is_retained(self):
        dll = self.dll("nested/deeper/whisper.dll")
        discovered = DesktopDll(dll, "const_me", {"checked": True})
        with patch("vasl_ai_bridge.desktop_installation.identify_desktop_dll", return_value=discovered) as identify:
            self.assertEqual(resolve_desktop_dll(self.program), discovered)
        identify.assert_called_once_with(dll)

    def test_invalid_root_dll_does_not_hide_compatible_nested_dll(self):
        root_dll = self.dll()
        nested = self.dll("any/path/Whisper.dll")
        result = DesktopDll(nested, "const_me", {})
        visited = []
        def identify(path):
            visited.append(path)
            if path == root_dll:
                raise ValueError("Unsupported native interface")
            return result
        with patch("vasl_ai_bridge.desktop_installation.identify_desktop_dll", side_effect=identify):
            self.assertEqual(resolve_desktop_dll(self.program), result)
        self.assertEqual(set(visited), {root_dll, nested})

    def test_multiple_compatible_dlls_are_explicitly_ambiguous(self):
        first = self.dll()
        second = self.dll("another/Whisper.dll")
        with patch("vasl_ai_bridge.desktop_installation.identify_desktop_dll",
                   side_effect=lambda path: DesktopDll(path, "const_me", {})):
            with self.assertRaisesRegex(ValueError, "Multiple compatible") as caught:
                resolve_desktop_dll(self.program)
        self.assertIn(str(first), str(caught.exception))
        self.assertIn(str(second), str(caught.exception))

    def test_missing_dll_reports_the_entire_search_scope(self):
        with self.assertRaisesRegex(ValueError, "directory or its subdirectories") as caught:
            resolve_desktop_dll(self.program)
        self.assertIn(str(self.root.resolve()), str(caught.exception))

    def test_denied_subdirectory_is_reported_instead_of_missing_dll(self):
        def walk(root, **kwargs):
            kwargs["onerror"](PermissionError("unreadable child folder"))
            return iter(())
        with patch("vasl_ai_bridge.desktop_installation.os.walk", side_effect=walk):
            with self.assertRaisesRegex(ValueError, "unreadable child folder"):
                find_desktop_dlls(self.program)

    def test_junctions_are_pruned_from_walk_without_changing_normal_children(self):
        directories = ["normal", "junction"]
        def walk(root, **kwargs):
            self.assertFalse(kwargs["followlinks"])
            yield str(root), directories, []
        with patch("vasl_ai_bridge.desktop_installation.os.walk", side_effect=walk), \
             patch.object(Path, "is_junction", autospec=True, side_effect=lambda path: path.name == "junction"):
            self.assertEqual(find_desktop_dlls(self.program), [])
        self.assertEqual(directories, ["normal"])

    def test_export_table_identifies_const_me_without_loading_speech_library(self):
        dll = self.dll(contents=exported_pe(sorted(CONST_ME_EXPORTS)))
        self.assertEqual(read_dll_exports(dll), CONST_ME_EXPORTS)
        with patch("vasl_ai_bridge.whisper_desktop_worker.validate_dll", return_value={"preflight": True}) as validate:
            result = identify_desktop_dll(dll)
        validate.assert_called_once_with(dll)
        self.assertEqual(result.implementation, "const_me")

    def test_export_table_reads_cpp_interface_without_dll_execution(self):
        dll = self.dll(contents=exported_pe(sorted(CPP_EXPORTS)))
        self.assertEqual(read_dll_exports(dll), CPP_EXPORTS)

    def test_long_decorated_exports_do_not_hide_the_speech_interface(self):
        names = CPP_EXPORTS | {"?" + "decorated_cpp_name" * 55}
        dll = self.dll(contents=exported_pe(sorted(names)))
        self.assertEqual(read_dll_exports(dll), names)

    def test_truncated_image_and_out_of_range_headers_are_rejected(self):
        for contents in (b"MZ", b"not PE", exported_pe(["test"])[:580]):
            with self.subTest(size=len(contents)), self.assertRaises(ValueError):
                read_dll_exports(self.dll(contents=contents))
        malformed = exported_pe(["test"])
        struct.pack_into("<I", malformed, 60, 0xFFFFFF00)
        with self.assertRaises(ValueError):
            read_dll_exports(self.dll(contents=malformed))

    def test_bad_export_count_and_unmapped_name_addresses_are_rejected(self):
        for offset, value in ((536, 0xFFFFFFFF), (576, 0xFFFFFFFF)):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                malformed = exported_pe(["test"])
                struct.pack_into("<I", malformed, offset, value)
                read_dll_exports(self.dll(contents=malformed))


if __name__ == "__main__":
    unittest.main()
