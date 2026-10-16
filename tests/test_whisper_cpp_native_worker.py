"""Guard the verified native DLL ABI, owned input and bounded UTF-8 IPC."""
from __future__ import annotations

import ctypes
import gc
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from vasl_ai_bridge import whisper_cpp_native_worker as worker


class NativeCppWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def pe_image(self, machine=0x8664, magic=0x20B):
        path = self.root / "renamed-engine.dll"
        image = bytearray(90)
        image[:2] = b"MZ"
        struct.pack_into("<I", image, 60, 64)
        image[64:68] = b"PE\0\0"
        struct.pack_into("<H", image, 68, machine)
        struct.pack_into("<H", image, 88, magic)
        path.write_bytes(image)
        (self.root / "ggml.dll").write_bytes(b"fixture")
        (self.root / "ggml-base.dll").write_bytes(b"fixture")
        (self.root / "ggml-cpu-fixture.dll").write_bytes(b"fixture")
        return path

    def wav(self, frames=1600, rate=16000):
        path = self.root / "fixture.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(b"\0\0" * frames)
        return path

    def test_unverified_hash_is_rejected_before_loading_any_native_library(self):
        path = self.pe_image()
        with patch.object(ctypes, "CDLL") as load:
            with self.assertRaisesRegex(ValueError, "has not been verified"):
                worker.validate_dll(path)
        load.assert_not_called()

    def test_wrong_architecture_is_rejected_before_hash_and_library_checks(self):
        path = self.pe_image(machine=0x14C)
        with patch.object(worker.hashlib, "sha256") as digest, patch.object(ctypes, "CDLL") as load:
            with self.assertRaisesRegex(ValueError, "AMD64"):
                worker.validate_dll(path)
        digest.assert_not_called()
        load.assert_not_called()

    def test_verified_identity_reports_exact_abi_without_filename_dependency_or_loading(self):
        path = self.pe_image()
        with patch.object(worker.hashlib, "sha256") as digest, patch.object(ctypes, "CDLL") as load:
            digest.return_value.hexdigest.return_value = worker.SUPPORTED_DLL_SHA256
            diagnostic = worker.validate_dll(path)
        load.assert_not_called()
        self.assertEqual(diagnostic["whisper_version"], "1.9.4-dev")
        self.assertEqual(diagnostic["abi_sizes"], {"ContextParams": 48, "FullParams": 304, "VadParams": 24})
        self.assertEqual(ctypes.sizeof(worker.FullParams), 304)
        self.assertEqual(worker.FullParams.language.offset, 104)
        self.assertEqual(worker.FullParams.new_segment_callback.offset, 160)
        self.assertEqual(worker.FullParams.vad_params.offset, 280)
        self.assertEqual(diagnostic["implementation"], "automatic")

    def test_dedicated_cuda_device_takes_priority_over_integrated_and_vulkan(self):
        devices = [
            {"name": "CPU", "backend": "CPU", "type": 0},
            {"name": "CUDA-integrated", "backend": "CUDA", "type": 2},
            {"name": "Vulkan0", "backend": "Vulkan", "type": 1},
            {"name": "CUDA0", "backend": "CUDA", "type": 1},
            {"name": "CUDA1", "backend": "CUDA", "type": 1},
        ]
        selected = worker.choose_cuda_device(devices)
        self.assertEqual(selected["name"], "CUDA0")
        self.assertEqual(selected["gpu_ordinal"], 2)
        self.assertNotIn("gpu_ordinal", devices[3])

    def test_integrated_and_other_backends_never_masquerade_as_dedicated_cuda(self):
        for device in ({"backend": "CPU", "type": 0},
                       {"backend": "Vulkan", "type": 1},
                       {"backend": "CUDA", "type": 2}):
            with self.subTest(device=device):
                self.assertIsNone(worker.choose_cuda_device([device]))

    def test_available_gpu_is_not_reported_as_used_without_native_execution_proof(self):
        diagnostic = {"use_gpu": True, "device_name": "CUDA0"}
        for log in ("", "CUDA0 total size = 3094 MB", "using CUDA0 backend",
                    "CUDA1 total size = 3094 MB\nusing CUDA1 backend"):
            with self.subTest(log=log), self.assertRaisesRegex(RuntimeError, "could not be confirmed"):
                worker.confirm_cuda_execution(diagnostic, log)
        worker.confirm_cuda_execution(diagnostic,
                                      "whisper_model_load: CUDA0 total size = 3094 MB\n"
                                      "whisper_backend_init_gpu: using CUDA0 backend")
        self.assertTrue(diagnostic["gpu_execution_confirmed"])

    def test_cpu_fallback_is_explicitly_reported_without_cuda_execution_claim(self):
        diagnostic = {"use_gpu": False, "implementation": "CPU",
                      "fallback_reason": "CUDA backend unavailable"}
        worker.confirm_cuda_execution(diagnostic, "using CPU backend")
        self.assertFalse(diagnostic["gpu_execution_confirmed"])
        self.assertEqual(diagnostic["implementation"], "CPU")
        self.assertEqual(diagnostic["fallback_reason"], "CUDA backend unavailable")

    def test_gpu_initialization_failure_overrides_earlier_selection_and_placement_logs(self):
        diagnostic = {"use_gpu": True, "device_name": "CUDA0"}
        selected_log = ("whisper_model_load: CUDA0 total size = 3094 MB\n"
                        "whisper_backend_init_gpu: using CUDA0 backend")
        worker.confirm_cuda_execution(diagnostic, selected_log)
        self.assertTrue(diagnostic["gpu_execution_confirmed"])
        with self.assertRaisesRegex(RuntimeError, "failed to initialize"):
            worker.confirm_cuda_execution(
                diagnostic, selected_log + "\nwhisper_backend_init_gpu: failed to initialize CUDA0 backend")
        self.assertFalse(diagnostic["gpu_execution_confirmed"])

    def test_native_logger_is_reset_on_early_binding_defaults_model_and_release_failures(self):
        dll = self.pe_image()
        model = self.root / "model.bin"
        model.write_bytes(b"lmgg")
        audio = self.wav()
        context_defaults = worker.ContextParams()
        for failure in ("binding", "defaults", "model", "release"):
            with self.subTest(failure=failure):
                log_set = Mock()
                free = Mock(side_effect=OSError("Release failed") if failure == "release" else None)
                functions = {
                    "whisper_version": lambda: worker.SUPPORTED_VERSION.encode("ascii"),
                    "whisper_log_set": log_set,
                    "whisper_context_default_params_by_ref": lambda: (
                        None if failure == "defaults" else ctypes.pointer(context_defaults)),
                    "whisper_free_context_params": Mock(),
                    "whisper_init_from_file_with_params": lambda *_: None if failure == "model" else 42,
                    "whisper_full_default_params_by_ref": lambda *_: None,
                    "whisper_free": free,
                }

                def fake_bind(library, name, result, arguments):
                    if failure == "binding" and name == "whisper_context_default_params_by_ref":
                        raise OSError("Binding failed")
                    return functions[name]

                with patch.object(worker, "bind", side_effect=fake_bind), \
                     patch.object(worker, "validate_dll", return_value={}), \
                     patch.object(worker, "load_compute_devices", return_value={
                         "use_gpu": False, "gpu_device": 0, "implementation": "CPU"}), \
                     patch.object(ctypes, "CDLL"), patch.object(worker.os, "add_dll_directory"), \
                     patch("builtins.print"), self.assertRaises((OSError, RuntimeError)):
                    worker.transcribe({"language": "en", "dll_path": str(dll),
                                       "model_path": str(model), "audio_path": str(audio)})
                self.assertEqual(log_set.call_count, 2)
                self.assertTrue(bool(log_set.call_args_list[0].args[0]))
                self.assertFalse(bool(log_set.call_args_list[-1].args[0]))
                if failure == "release":
                    free.assert_called_once_with(42)
                else:
                    free.assert_not_called()

    def test_missing_dependency_is_rejected_before_library_loading(self):
        path = self.pe_image()
        (self.root / "ggml.dll").unlink()
        with patch.object(worker.hashlib, "sha256") as digest, patch.object(ctypes, "CDLL") as load:
            digest.return_value.hexdigest.return_value = worker.SUPPORTED_DLL_SHA256
            with self.assertRaisesRegex(ValueError, "ggml.dll dependency"):
                worker.validate_dll(path)
        load.assert_not_called()

    def test_missing_base_dependency_is_rejected_before_library_loading(self):
        path = self.pe_image()
        (self.root / "ggml-base.dll").unlink()
        with patch.object(worker.hashlib, "sha256") as digest, patch.object(ctypes, "CDLL") as load:
            digest.return_value.hexdigest.return_value = worker.SUPPORTED_DLL_SHA256
            with self.assertRaisesRegex(ValueError, "ggml-base.dll dependency"):
                worker.validate_dll(path)
        load.assert_not_called()

    def test_invalid_language_never_reaches_dll_preflight(self):
        with patch.object(worker, "validate_dll") as validate, patch.object(ctypes, "CDLL") as load:
            with self.assertRaisesRegex(ValueError, "Unsupported speech language"):
                worker.transcribe({"language": "invalid"})
        validate.assert_not_called()
        load.assert_not_called()

    def test_automatic_language_transcribes_and_preserves_original_languages(self):
        dll = self.pe_image()
        model = self.root / "model.bin"
        model.write_bytes(b"lmgg")
        audio = self.wav()
        context_defaults = worker.ContextParams()
        full_defaults = worker.FullParams()
        full_defaults.n_threads = 1
        full_defaults.greedy.best_of = 1
        # A future change to native defaults must not enable detection-only
        # or translation when the user asks for a speech transcript.
        full_defaults.detect_language = True
        full_defaults.translate = True
        full_defaults.no_timestamps = True
        transcript = "班组 moves to K9; check LOS。"
        observed_prompts = []

        def inspect_inference(context, params, values, frames):
            # Validate the raw native pointer during the call. Dereferencing it
            # after transcribe() returns would hide a dangling-buffer bug.
            gc.collect()
            self.assertTrue(params.initial_prompt)
            observed_prompts.append(ctypes.string_at(params.initial_prompt).decode("utf-8"))
            self.assertFalse(params.no_timestamps)
            self.assertFalse(params.carry_initial_prompt)
            self.assertTrue(params.no_context)
            return 0

        inference = Mock(side_effect=inspect_inference)
        release = Mock()
        functions = {
            "whisper_version": lambda: worker.SUPPORTED_VERSION.encode("ascii"),
            "whisper_log_set": Mock(),
            "whisper_context_default_params_by_ref": lambda: ctypes.pointer(context_defaults),
            "whisper_free_context_params": Mock(),
            "whisper_init_from_file_with_params": lambda *_: 42,
            "whisper_full_default_params_by_ref": lambda *_: ctypes.pointer(full_defaults),
            "whisper_free_params": Mock(),
            "whisper_full": inference,
            "whisper_full_lang_id": lambda _: 1,
            "whisper_lang_str": lambda _: b"zh",
            "whisper_full_n_segments": lambda _: 1,
            "whisper_full_get_segment_text": lambda *_: transcript.encode("utf-8"),
            "whisper_free": release,
        }
        for request_language in ("auto", None):
            with self.subTest(request_language=request_language):
                request = {"dll_path": str(dll), "model_path": str(model), "audio_path": str(audio)}
                if request_language is not None:
                    request["language"] = request_language
                with patch.object(worker, "bind", side_effect=lambda _, name, *args: functions[name]), \
                     patch.object(worker, "validate_dll", return_value={}), \
                     patch.object(worker, "load_compute_devices", return_value={
                         "use_gpu": False, "gpu_device": 0, "implementation": "CPU"}), \
                     patch.object(ctypes, "CDLL"), patch.object(worker.os, "add_dll_directory"), \
                     patch("builtins.print"):
                    response = worker.transcribe(request)
                self.assertEqual(response["text"], transcript)
                params = inference.call_args.args[1]
                self.assertEqual(params.language, b"auto")
                self.assertFalse(params.detect_language)
                self.assertFalse(params.translate)
                self.assertEqual(observed_prompts[-1],
                                 "今天上午天气很好，下午可能会下雨。Hello, everyone. Shall we start?")
                self.assertEqual(response["diagnostic"]["language"], "auto")
                self.assertEqual(response["diagnostic"]["detected_language"], "zh")
                self.assertFalse(response["diagnostic"]["translate"])
                self.assertEqual(inference.call_args.args[0], 42)
                self.assertEqual(inference.call_args.args[3], 1600)
                release.assert_called_with(42)

    def test_network_and_missing_paths_are_rejected(self):
        for path in ("https://example.com/model.bin", "\\\\server\\model.bin", "//server/model.bin", "", str(self.root / "missing.bin")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                worker.local_file(path, "model")

    def test_invalid_model_magic_never_loads_native_library(self):
        dll = self.pe_image()
        model = self.root / "model.bin"
        model.write_bytes(b"GGUF")
        audio = self.wav()
        with patch.object(worker, "validate_dll") as validate, patch.object(ctypes, "CDLL") as load:
            with self.assertRaisesRegex(ValueError, "original GGML"):
                worker.transcribe({"language": "zh", "dll_path": str(dll), "model_path": str(model), "audio_path": str(audio)})
        validate.assert_not_called()
        load.assert_not_called()

    def test_audio_format_duration_and_truncation_are_checked_before_inference(self):
        with self.assertRaisesRegex(ValueError, "duration"):
            worker.read_audio(self.wav(frames=1599))
        with self.assertRaisesRegex(ValueError, "16 kHz"):
            worker.read_audio(self.wav(rate=8000))
        path = self.wav()
        path.write_bytes(path.read_bytes()[:-2])
        with self.assertRaisesRegex(ValueError, "truncated"):
            worker.read_audio(path)
        frames, pcm = worker.read_audio(self.wav())
        self.assertEqual((frames, len(pcm)), (1600, 1600))

    def test_json_transcripts_and_errors_use_utf8_even_with_ascii_text_stream(self):
        for response in ({"text": "班组移动到 K9。"}, {"error": "模型路径错误。"}):
            with self.subTest(response=response):
                raw = io.BytesIO()
                stream = io.TextIOWrapper(raw, encoding="ascii")
                with patch.object(worker.sys, "stdout", stream):
                    worker.write_response(response)
                self.assertEqual(json.loads(raw.getvalue().decode("utf-8")), response)
                stream.detach()

    def test_oversized_request_is_rejected_before_native_transcription(self):
        raw_out = io.BytesIO()
        out = io.TextIOWrapper(raw_out, encoding="ascii")
        source = io.TextIOWrapper(io.BytesIO(b"x" * (worker.MAX_REQUEST_BYTES + 1)), encoding="ascii")
        with patch.object(worker.sys, "stdin", source), patch.object(worker.sys, "stdout", out), patch.object(worker, "transcribe") as transcribe:
            self.assertEqual(worker.main(), 1)
        transcribe.assert_not_called()
        self.assertIn("too large", json.loads(raw_out.getvalue())["error"])
        out.detach()

    def test_json_container_is_rejected_before_native_transcription(self):
        raw_out = io.BytesIO()
        out = io.TextIOWrapper(raw_out, encoding="ascii")
        source = io.TextIOWrapper(io.BytesIO(b"[]"), encoding="ascii")
        with patch.object(worker.sys, "stdin", source), patch.object(worker.sys, "stdout", out), patch.object(worker, "transcribe") as transcribe:
            self.assertEqual(worker.main(), 1)
        transcribe.assert_not_called()
        self.assertIn("JSON object", json.loads(raw_out.getvalue())["error"])
        out.detach()

    def test_oversized_serialized_response_has_no_partial_output(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii")
        with patch.object(worker.sys, "stdout", stream), self.assertRaisesRegex(ValueError, "oversized response"):
            worker.write_response({"text": "\0" * worker.MAX_TRANSCRIPT_BYTES})
        self.assertEqual(raw.getvalue(), b"")
        stream.detach()


if __name__ == "__main__":
    unittest.main()
