"""Source/frozen command selection and actual helper IPC/Windows cleanup."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from vasl_ai_bridge import frozen_runtime
from vasl_ai_bridge.desktop_installation import DesktopDll


ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "packaging" / "speech_entry.py"


def speech_entry():
    spec = importlib.util.spec_from_file_location("asl_ai_test_speech_entry", ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FrozenCommandTests(unittest.TestCase):
    def test_source_external_argv_is_unchanged_and_copied(self):
        command = [r"C:\External Python\python.exe", "argument & literal"]
        with patch.object(frozen_runtime.sys, "frozen", False, create=True):
            result = frozen_runtime.external_command(command)
        self.assertEqual(result, command)
        self.assertIsNot(result, command)

    def test_frozen_external_argv_uses_only_fixed_sibling_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "VASL AI Bridge.exe"
            helper = app.with_name("ASL_AI-Speech.exe")
            helper.write_bytes(b"helper fixture")
            expected_helper = str(helper.resolve())
            with patch.object(frozen_runtime.sys, "frozen", True, create=True), \
                    patch.object(frozen_runtime.sys, "executable", str(app)):
                result = frozen_runtime.external_command([r"C:\External\codex.exe", "app-server"])
        self.assertEqual(result, [expected_helper, "--external", r"C:\External\codex.exe", "app-server"])

    def test_missing_frozen_helper_has_fixed_path_free_error(self):
        with tempfile.TemporaryDirectory(prefix="private-user-") as directory:
            with patch.object(frozen_runtime.sys, "frozen", True, create=True), \
                    patch.object(frozen_runtime.sys, "executable", str(Path(directory) / "VASL AI Bridge.exe")):
                with self.assertRaises(RuntimeError) as caught:
                    frozen_runtime.external_command(["codex.exe"])
            self.assertNotIn(directory, str(caught.exception))
            self.assertNotIn("private-user-", str(caught.exception))

    def test_environment_removes_bundle_paths_but_preserves_user_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "_internal"
            outside = Path(directory) / "external-qt"
            environment = {"Path": os.pathsep.join([str(bundle), str(bundle / "PySide6"), str(outside)]),
                           "QT_PLUGIN_PATH": str(bundle / "PySide6" / "plugins"),
                           "QT_QPA_PLATFORM_PLUGIN_PATH": str(outside),
                           "ASL_AI_USER_VALUE": "user-value"}
            original = dict(environment)
            with patch.object(frozen_runtime.sys, "frozen", True, create=True), \
                    patch.object(frozen_runtime.sys, "_MEIPASS", str(bundle), create=True):
                result = frozen_runtime.external_environment(environment)
        self.assertEqual(result["Path"], str(outside))
        self.assertNotIn("QT_PLUGIN_PATH", result)
        self.assertEqual(result["QT_QPA_PLATFORM_PLUGIN_PATH"], str(outside))
        self.assertEqual(result["ASL_AI_USER_VALUE"], "user-value")
        self.assertEqual(environment, original)

    def test_source_does_not_reset_parent_dll_lookup(self):
        with patch.object(frozen_runtime.sys, "frozen", False, create=True), \
                patch.object(frozen_runtime.ctypes, "WinDLL", create=True) as api:
            frozen_runtime.reset_helper_library_search()
        api.assert_not_called()

    def test_helper_rejects_unknown_command_without_repeating_sensitive_arguments(self):
        module = speech_entry()
        stderr = Mock()
        with patch.object(module.sys, "stderr", stderr):
            self.assertEqual(module.main(["--backend", "secret-user-path"]), 2)
        self.assertEqual(stderr.write.call_args.args[0], "Unsupported background helper command.\n")

    def test_native_worker_import_is_followed_by_reset_and_dispatch(self):
        module = speech_entry()
        from vasl_ai_bridge import whisper_cpp_native_worker
        order = []
        with patch.object(frozen_runtime, "reset_helper_library_search", side_effect=lambda: order.append("reset")), \
                patch.object(whisper_cpp_native_worker, "main", side_effect=lambda: order.append("worker") or 0), \
                patch.object(frozen_runtime, "external_environment", side_effect=lambda: dict(os.environ)):
            self.assertEqual(module.main(["--backend", "whisper_cpp_native"]), 0)
        self.assertEqual(order, ["reset", "worker"])

    def test_frozen_voice_native_uses_bundled_helper_and_utf8_request(self):
        from vasl_ai_bridge import voice
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "VASL AI Bridge.exe"
            helper = root / "ASL_AI-Speech.exe"
            helper.write_bytes(b"helper fixture")
            expected_helper = str(helper.resolve())
            desktop = root / "WhisperDesktop.exe"
            desktop.write_bytes(b"external desktop fixture")
            model = root / "model.bin"
            model.write_bytes(b"external model fixture")
            dll = root / "Whisper.dll"
            expected_dll = str(dll.resolve())
            resolved = DesktopDll(dll, "whisper_cpp_native", {})
            with patch.object(frozen_runtime.sys, "frozen", True, create=True), \
                    patch.object(frozen_runtime.sys, "executable", str(app)), \
                    patch.object(voice, "resolve_desktop_dll", return_value=resolved):
                program, arguments, request = voice.build_transcription_command(
                    voice.VoiceConfig("whisper_desktop", str(desktop), str(model)),
                    root / "Chinese-\u8bed\u97f3.wav", root / "transcript")
        self.assertEqual(program, expected_helper)
        self.assertEqual(arguments, ["--backend", "whisper_cpp_native"])
        payload = json.loads(request.decode("utf-8"))
        self.assertEqual(payload["dll_path"], expected_dll)
        self.assertTrue(payload["audio_path"].endswith("Chinese-\u8bed\u97f3.wav"))


class HelperProcessTests(unittest.TestCase):
    def helper(self, program: str, arguments: list[str]):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return subprocess.Popen([sys.executable, "-I", "-B", str(ENTRY), "--external", program, *arguments],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=flags)

    def test_actual_helper_preserves_utf8_stdio_and_literal_argv(self):
        code = ("import json,sys; data=sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(json.dumps({'text':data.decode('utf-8'),'argument':sys.argv[1]},"
                "ensure_ascii=False).encode('utf-8'));sys.stderr.buffer.write(b'bounded fixture stderr')")
        process = self.helper(sys.executable, ["-I", "-B", "-c", code, "literal & $(private) ; value"])
        output, stderr = process.communicate("\u4e2d\u6587 and English".encode("utf-8"), timeout=10)
        self.assertEqual(process.returncode, 0, stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(output), {"text": "\u4e2d\u6587 and English",
                                             "argument": "literal & $(private) ; value"})
        self.assertEqual(stderr, b"bounded fixture stderr")

    @unittest.skipUnless(sys.platform == "win32", "Windows JobObject cleanup")
    def test_terminating_helper_also_terminates_its_actual_child(self):
        process = self.helper(sys.executable, ["-I", "-B", "-c",
                            "import os,time;print(os.getpid(),flush=True);time.sleep(60)"])
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        messages = queue.Queue()
        reader = threading.Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True)
        reader.start()
        line = messages.get(timeout=10)
        self.assertTrue(line.strip().isdigit(), line)
        child_pid = int(line)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        handle = open_process(0x00100000, False, child_pid)  # SYNCHRONIZE
        self.assertTrue(handle)
        try:
            process.terminate()
            process.communicate(timeout=10)
            self.assertEqual(wait(handle, 5000), 0, "External child survived helper termination")
        finally:
            close(handle)


if __name__ == "__main__":
    unittest.main()
