"""Guard the pinned Desktop DLL boundary and UTF-8 subprocess protocol."""
from __future__ import annotations

import ctypes
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vasl_ai_bridge import whisper_desktop_worker as worker


class DesktopWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def pe_image(self, machine=0x8664, magic=0x20B):
        path = self.root / "Whisper.dll"
        image = bytearray(90)
        image[:2] = b"MZ"
        struct.pack_into("<I", image, 60, 64)
        image[64:68] = b"PE\0\0"
        struct.pack_into("<H", image, 68, machine)
        struct.pack_into("<H", image, 88, magic)
        path.write_bytes(image)
        return path

    def test_unsupported_architecture_is_rejected_before_version_or_library_calls(self):
        path = self.pe_image(machine=0x14C)
        with patch.object(worker, "dll_file_version") as version, patch.object(ctypes, "WinDLL") as load:
            with self.assertRaisesRegex(ValueError, "AMD64"):
                worker.validate_dll(path)
        version.assert_not_called()
        load.assert_not_called()

    def test_wrong_version_is_rejected_before_loading_whisper(self):
        path = self.pe_image()
        with patch.object(worker, "dll_file_version", return_value=(1, 11, 0, 0)), patch.object(ctypes, "WinDLL") as load:
            with self.assertRaisesRegex(ValueError, "supports Whisper.dll 1.12.0.0"):
                worker.validate_dll(path)
        load.assert_not_called()

    def test_checked_version_reports_pinned_layout_without_loading_whisper(self):
        path = self.pe_image()
        with patch.object(worker, "dll_file_version", return_value=worker.ABI_VERSION), patch.object(ctypes, "WinDLL") as load:
            diagnostic = worker.validate_dll(path)
        load.assert_not_called()
        self.assertEqual(diagnostic["abi_sizes"]["FullParams"], 112)
        self.assertEqual(worker.FullParams.encoder_begin_callback.offset, 96)
        self.assertEqual(diagnostic["dll_version"], [1, 12, 0, 0])

    def test_auto_language_never_reaches_dll_preflight(self):
        with patch.object(worker, "validate_dll") as validate:
            with self.assertRaisesRegex(ValueError, "automatic language detection is unsupported"):
                worker.transcribe({"language": "auto"})
        validate.assert_not_called()

    def test_network_and_missing_paths_are_rejected(self):
        for path in ("https://example.com/model.bin", "\\\\server\\model.bin", "//server/model.bin", "", str(self.root / "missing.bin")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                worker.local_file(path, "model")

    def test_invalid_model_magic_never_loads_whisper_library(self):
        dll = self.pe_image()
        model = self.root / "model.bin"
        audio = self.root / "audio.wav"
        model.write_bytes(b"GGUF")
        audio.write_bytes(b"not audio")
        with patch.object(worker, "validate_dll", return_value={}), patch.object(ctypes, "WinDLL") as load:
            with self.assertRaisesRegex(ValueError, "original GGML"):
                worker.transcribe({"language": "zh", "dll_path": str(dll), "model_path": str(model), "audio_path": str(audio)})
        load.assert_not_called()

    def test_success_and_error_json_are_utf8_even_with_ascii_text_stream(self):
        for response in ({"text": "班组移动到 K9。检查视线。"}, {"error": "语音模型路径错误。"}):
            with self.subTest(response=response):
                raw = io.BytesIO()
                ascii_stream = io.TextIOWrapper(raw, encoding="ascii")
                with patch.object(worker.sys, "stdout", ascii_stream):
                    worker.write_response(response)
                self.assertEqual(json.loads(raw.getvalue().decode("utf-8")), response)
                ascii_stream.detach()

    def test_isolated_subprocess_protocol_is_utf8_without_environment_flags(self):
        script = Path(worker.__file__).resolve()
        code = ("import runpy,sys; scope=runpy.run_path(sys.argv[1]); "
                "sys.stdout.reconfigure(encoding='ascii'); "
                "scope['write_response']({'text':'\\u4e2d\\u6587 K9'})")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(script)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8")), {"text": "中文 K9"})

    def test_oversized_serialized_response_is_rejected_before_partial_output(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii")
        with patch.object(worker.sys, "stdout", stream), self.assertRaisesRegex(ValueError, "oversized response"):
            worker.write_response({"text": "\0" * 65536})
        self.assertEqual(raw.getvalue(), b"")
        stream.detach()


if __name__ == "__main__":
    unittest.main()
