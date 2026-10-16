"""Local speech bounds, process isolation, failure and cancellation checks."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from PySide6.QtCore import QProcess, Qt
from PySide6.QtMultimedia import QtAudio, QAudioFormat, QAudioSource
from PySide6.QtWidgets import QApplication

from vasl_ai_bridge.voice import (MAX_PCM_BYTES, MAX_RECORD_SECONDS, MicrophoneRecorder,
                          VoiceConfig, VoiceController, VoiceError,
                          build_transcription_command, find_local_voice_configs, resolve_voice_config, validate_wav)
from vasl_ai_bridge import voice_worker
from vasl_ai_bridge.desktop_installation import DesktopDll


APP = QApplication.instance() or QApplication(["Local voice tests"])


def make_wav(path: Path, frames: int = 8000, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\0\0" * frames)


def wait_until(predicate, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(.005)
    if not predicate():
        raise AssertionError("The speech subprocess did not finish in time.")


class FakeSignal:
    def __init__(self): self.callbacks = []
    def connect(self, callback): self.callbacks.append(callback)
    def emit(self, *args):
        for callback in self.callbacks: callback(*args)


class FakeMicrophone:
    def isNull(self): return False
    def description(self): return "Synthetic microphone"
    def isFormatSupported(self, audio_format): return True
    def preferredFormat(self):
        result = QAudioFormat()
        result.setSampleRate(48000)
        result.setChannelCount(1)
        result.setSampleFormat(QAudioFormat.Float)
        return result


class FakeAudioSource:
    def __init__(self, startup_error=QtAudio.Error.NoError, raise_on_start=False):
        self.stateChanged = FakeSignal()
        self._error = QtAudio.Error.NoError
        self._state = QtAudio.State.StoppedState
        self.startup_error = startup_error
        self.raise_on_start = raise_on_start
        self.stop_count = 0
        self.stop_during_start = False
        self.inside_start = False
    def setBufferSize(self, size): pass
    def error(self): return self._error
    def state(self): return self._state
    def start(self):
        self.inside_start = True
        if self.raise_on_start:
            self.inside_start = False
            raise RuntimeError("Synthetic native failure")
        self._error = self.startup_error
        self._state = QtAudio.State.StoppedState if self._error != QtAudio.Error.NoError else QtAudio.State.IdleState
        self.stateChanged.emit(self._state)
        self.inside_start = False
        if self._error != QtAudio.Error.NoError:
            return None
        class Reader:
            readyRead = FakeSignal()
            def readAll(self): return b""
        return Reader()
    def stop(self):
        self.stop_during_start |= self.inside_start
        self.stop_count += 1
        self._error = QtAudio.Error.NoError
        self._state = QtAudio.State.StoppedState
        self.stateChanged.emit(self._state)
    def deleteLater(self): pass


class LocalVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = self.root / "model.pt"
        self.model.write_bytes(b"local model fixture")
        self.wav = self.root / "sample.wav"
        make_wav(self.wav)
        self.config = VoiceConfig("whisper", sys.executable, str(self.model), "zh")
        self.cpp_program = self.root / "whisper-cli.exe"
        self.cpp_program.write_bytes(b"local cpp executable fixture")

    def controller(self):
        controller = VoiceController(self.root / "recordings")
        self.addCleanup(controller.close)
        return controller

    def command(self, code):
        return patch("vasl_ai_bridge.voice.build_transcription_command",
                     return_value=(sys.executable, ["-I", "-B", "-c", code], b""))

    def test_python_command_uses_isolated_worker_and_local_paths(self):
        program, args, request = build_transcription_command(self.config, self.wav, self.root / "text")
        self.assertEqual(program, sys.executable)
        self.assertEqual(args[:2], ["-I", "-B"])
        self.assertTrue(args[2].endswith("voice_worker.py"))
        self.assertEqual(json.loads(request)["model_path"], str(self.model.resolve()))
        self.assertEqual(json.loads(request)["language"], "zh")

    def test_cpp_command_stays_on_cpu_and_writes_only_owned_output(self):
        config = VoiceConfig("whisper_cpp", str(self.cpp_program), str(self.model), "auto")
        _, args, request = build_transcription_command(config, self.wav, self.root / "text")
        self.assertIn("-ng", args)
        self.assertEqual(args[args.index("-of") + 1], str(self.root / "text"))
        self.assertEqual(request, b"")

    def test_rejects_remote_model_and_unc_program(self):
        for config in [VoiceConfig("whisper", sys.executable, "https://example.com/model.pt"),
                       VoiceConfig("whisper", "\\\\server\\python.exe", str(self.model))]:
            with self.subTest(config=config), self.assertRaises(VoiceError):
                resolve_voice_config(config)

    def test_missing_model_fails_before_microphone_start(self):
        controller = self.controller()
        with patch.object(controller.recorder, "start") as start, self.assertRaises(VoiceError):
            controller.start(VoiceConfig("whisper", sys.executable, str(self.root / "missing.pt")))
        start.assert_not_called()
        self.assertFalse(controller.active)

    def test_explicit_gui_program_is_rejected_before_microphone(self):
        gui = self.root / "WhisperDesktop.exe"
        gui.write_bytes(b"GUI application fixture")
        controller = self.controller()
        for backend in ("whisper_cpp", "whisper", "faster_whisper"):
            with self.subTest(backend=backend), patch.object(controller.recorder, "start") as start:
                with self.assertRaisesRegex(VoiceError, "requires whisper-cli|requires its Python"):
                    controller.start(VoiceConfig(backend, str(gui), str(self.model)))
                start.assert_not_called()

    def test_desktop_model_and_dll_are_validated_before_microphone(self):
        gui = self.root / "WhisperDesktop.exe"
        gui.write_bytes(b"GUI application fixture")
        config = VoiceConfig("whisper_desktop", str(gui), str(self.model), "zh")
        controller = self.controller()
        with patch.object(controller.recorder, "start") as start:
            with self.assertRaisesRegex(VoiceError, "directory or its subdirectories"):
                controller.start(config)
            start.assert_not_called()
        gui.with_name("Whisper.dll").write_bytes(b"Invalid DLL fixture")
        with patch.object(controller.recorder, "start") as start, \
             patch("vasl_ai_bridge.voice.resolve_desktop_dll", side_effect=ValueError("DLL version is unsupported")):
            with self.assertRaisesRegex(VoiceError, "DLL version is unsupported"):
                controller.start(config)
            start.assert_not_called()

    def test_desktop_auto_resolves_dll_before_legacy_compatibility_check_and_never_falls_back(self):
        gui = self.root / "WhisperDesktop.exe"
        gui.write_bytes(b"GUI fixture")
        with self.assertRaisesRegex(VoiceError, "requires Whisper.dll"):
            resolve_voice_config(VoiceConfig("whisper_desktop", str(gui), str(self.model), "auto"))
        descriptor = DesktopDll(gui.with_name("Whisper.dll"), "const_me", {})
        with patch("vasl_ai_bridge.voice.resolve_desktop_dll", return_value=descriptor) as resolve:
            with self.assertRaisesRegex(VoiceError, "older.*automatic transcription"):
                resolve_voice_config(VoiceConfig("whisper_desktop", str(gui), str(self.model)))
        resolve.assert_called_once_with(gui.resolve())
        with patch("vasl_ai_bridge.voice.find_local_voice_configs") as automatic:
            with self.assertRaisesRegex(VoiceError, "requires Whisper.dll"):
                resolve_voice_config(VoiceConfig("whisper_desktop", str(gui), str(self.model), "zh"))
        automatic.assert_not_called()

    def test_native_desktop_default_auto_reaches_isolated_transcription_command(self):
        gui = self.root / "renamed-desktop.exe"
        gui.write_bytes(b"GUI fixture")
        dll = self.root / "resources" / "whisper.dll"
        dll.parent.mkdir()
        dll.write_bytes(b"DLL fixture")
        descriptor = DesktopDll(dll, "whisper_cpp_native", {})
        config = VoiceConfig("whisper_desktop", str(gui), str(self.model))
        with patch("vasl_ai_bridge.voice.resolve_desktop_dll", return_value=descriptor):
            resolved = resolve_voice_config(config)
            _, args, request = build_transcription_command(resolved, self.wav, self.root / "text")
        self.assertEqual(resolved.language, "auto")
        self.assertEqual(resolved.desktop_implementation, "whisper_cpp_native")
        self.assertTrue(args[-1].endswith("whisper_cpp_native_worker.py"))
        self.assertEqual(json.loads(request)["language"], "auto")
        self.assertEqual(json.loads(request)["dll_path"], str(dll.resolve()))

    def test_desktop_command_uses_installed_dll_and_original_model(self):
        gui = self.root / "WhisperDesktop.exe"
        gui.write_bytes(b"GUI fixture")
        dll = gui.with_name("Whisper.dll")
        dll.write_bytes(b"DLL fixture")
        config = VoiceConfig("whisper_desktop", str(gui), str(self.model), "zh")
        with patch("vasl_ai_bridge.voice.resolve_desktop_dll", return_value=DesktopDll(dll, "const_me", {})):
            program, args, request = build_transcription_command(config, self.wav, self.root / "text")
        self.assertEqual(Path(program).name.lower(), "python.exe")
        self.assertEqual(args[:2], ["-I", "-B"])
        self.assertEqual(args[2:4], ["-X", "utf8"])
        self.assertTrue(args[4].endswith("whisper_desktop_worker.py"))
        payload = json.loads(request)
        self.assertEqual(payload["dll_path"], str(dll.resolve()))
        self.assertEqual(payload["model_path"], str(self.model.resolve()))
        self.assertEqual(payload["language"], "zh")
        self.assertNotEqual(program, str(gui))

    def test_desktop_command_passes_recursive_dll_path_for_renamed_application(self):
        gui = self.root / "renamed-desktop.exe"
        gui.write_bytes(b"GUI fixture")
        dll = self.root / "assets" / "native" / "WHISPER.DLL"
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b"DLL fixture")
        config = VoiceConfig("whisper_desktop", str(gui), str(self.model), "zh")
        with patch("vasl_ai_bridge.voice.resolve_desktop_dll", return_value=DesktopDll(dll, "whisper_cpp_native", {})):
            program, args, request = build_transcription_command(config, self.wav, self.root / "text")
        self.assertTrue(args[-1].endswith("whisper_cpp_native_worker.py"))
        self.assertEqual(json.loads(request)["dll_path"], str(dll.resolve()))
        self.assertEqual(json.loads(request)["model_path"], str(self.model.resolve()))
        self.assertNotEqual(program, str(gui))

    def test_desktop_accepts_installation_directory_and_ignores_anchor_filename(self):
        dll = self.root / "Whisper.dll"
        dll.write_bytes(b"DLL fixture")
        descriptor = DesktopDll(dll, "const_me", {})
        renamed = self.root / "pythonw.exe"
        renamed.write_bytes(b"Renamed Desktop fixture")
        for anchor in (self.root, renamed):
            with self.subTest(anchor=anchor), patch("vasl_ai_bridge.voice.resolve_desktop_dll", return_value=descriptor):
                resolved = resolve_voice_config(VoiceConfig("whisper_desktop", str(anchor), str(self.model), "zh"))
            self.assertEqual(resolved.dll_path, str(dll.resolve()))
            self.assertEqual(resolved.model_path, str(self.model.resolve()))

    def test_unknown_cpp_program_is_not_guessed_from_model_extension(self):
        unknown = self.root / "another-app.exe"
        unknown.write_bytes(b"unknown application")
        with self.assertRaisesRegex(VoiceError, "requires whisper-cli"):
            resolve_voice_config(VoiceConfig("whisper_cpp", str(unknown), str(self.model)))

    def test_automatic_mode_preserves_but_does_not_execute_custom_gui_path(self):
        gui = self.root / "WhisperDesktop.exe"
        gui.write_bytes(b"GUI application fixture")
        selected = VoiceConfig("auto", str(gui), str(self.model), "zh")
        managed = VoiceConfig("whisper_cpp", str(self.cpp_program), str(self.model))
        with patch("vasl_ai_bridge.voice.find_local_voice_configs", return_value=[managed]):
            result = resolve_voice_config(selected)
        self.assertEqual(result, VoiceConfig("whisper_cpp", str(self.cpp_program.resolve()), str(self.model.resolve()), "zh"))
        self.assertEqual(selected.program_path, str(gui))

    def test_auto_does_not_download_or_choose_missing_models(self):
        with patch("vasl_ai_bridge.voice.find_local_voice_configs", return_value=[]), self.assertRaises(VoiceError):
            resolve_voice_config(VoiceConfig())

    def test_auto_preserves_language_when_using_detected_engine(self):
        detected = VoiceConfig("whisper_cpp", str(self.cpp_program), str(self.model))
        with patch("vasl_ai_bridge.voice.find_local_voice_configs", return_value=[detected]):
            config = resolve_voice_config(VoiceConfig(language="en"))
        self.assertEqual(config.language, "en")
        self.assertEqual(config.backend, "whisper_cpp")

    def test_managed_release_engine_is_detected_with_multilingual_base_model(self):
        managed = self.root / "private" / "voice-engine"
        program = managed / "bin" / "Release" / "whisper-cli.exe"
        program.parent.mkdir(parents=True)
        program.write_bytes(b"local executable fixture")
        model = managed / "ggml-base.bin"
        model.write_bytes(b"local multilingual model")
        with patch("vasl_ai_bridge.voice._SOURCE_ROOT", self.root), patch("vasl_ai_bridge.voice.default_data_directory", return_value=self.root / "appdata"):
            configs = find_local_voice_configs()
        matches = [config for config in configs if config.program_path == str(program)]
        self.assertEqual(matches, [VoiceConfig("whisper_cpp", str(program), str(model))])

    def test_faster_whisper_requires_local_converted_folder(self):
        folder = self.root / "converted"
        folder.mkdir()
        with self.assertRaises(VoiceError):
            resolve_voice_config(VoiceConfig("faster_whisper", sys.executable, str(folder)))
        (folder / "model.bin").write_bytes(b"model")
        (folder / "config.json").write_text("{}")
        self.assertEqual(resolve_voice_config(VoiceConfig("faster_whisper", sys.executable, str(folder))).backend, "faster_whisper")

    def test_wav_format_duration_and_minimum_are_checked(self):
        self.assertEqual(validate_wav(self.wav), 8000)
        for frames, rate in [(16000 * MAX_RECORD_SECONDS + 1, 16000), (1, 16000), (8000, 48000)]:
            with self.subTest(frames=frames, rate=rate):
                make_wav(self.wav, frames, rate)
                with self.assertRaises(VoiceError):
                    validate_wav(self.wav)

    def test_real_subprocess_returns_unicode_draft_and_removes_recording(self):
        controller = self.controller()
        texts, failures, states = [], [], []
        controller.transcript_ready.connect(texts.append)
        controller.failed.connect(failures.append)
        controller.state_changed.connect(states.append)
        code = "import json,sys; sys.stdout.buffer.write(json.dumps({'text':'测试语音草稿'},ensure_ascii=False).encode('utf-8'))"
        with self.command(code), patch.object(controller.recorder, "start") as microphone:
            controller.transcribe_wav(self.wav, self.config)
            wait_until(lambda: not controller.active)
        self.assertEqual(texts, ["测试语音草稿"])
        self.assertEqual(failures, [])
        self.assertEqual(states, ["transcribing", "idle"])
        microphone.assert_not_called()

    def test_large_transcript_with_native_diagnostics_fits_bounded_response(self):
        controller = self.controller()
        texts, failures = [], []
        controller.transcript_ready.connect(texts.append)
        controller.failed.connect(failures.append)
        code = "import json,sys; sys.stdout.buffer.write(json.dumps({'text':'x'*65536,'diagnostic':{'native_logs':'y'*4096}}).encode('utf-8'))"
        with self.command(code):
            controller.transcribe_wav(self.wav, self.config)
            wait_until(lambda: not controller.active)
        self.assertEqual(texts, ["x" * 65536])
        self.assertEqual(failures, [])
        self.assertEqual(controller.last_transcription_diagnostic["worker"]["native_logs"], "y" * 4096)

    def test_oversized_transcript_is_rejected_without_a_draft(self):
        controller = self.controller()
        texts, failures = [], []
        controller.transcript_ready.connect(texts.append)
        controller.failed.connect(failures.append)
        code = "import json; print(json.dumps({'text':'x'*65537}))"
        with self.command(code):
            controller.transcribe_wav(self.wav, self.config)
            wait_until(lambda: not controller.active)
        self.assertEqual(texts, [])
        self.assertEqual(len(failures), 1)
        self.assertIn("oversized transcript", failures[0])
        self.assertEqual(list((self.root / "recordings").iterdir()), [])
        self.assertTrue(self.wav.exists())
        self.assertTrue(self.model.exists())

    def test_subprocess_failure_emits_error_without_transcript(self):
        controller = self.controller()
        texts, failures = [], []
        controller.transcript_ready.connect(texts.append)
        controller.failed.connect(failures.append)
        code = "import json,sys; print(json.dumps({'error':'No local model'}));sys.exit(1)"
        with self.command(code):
            controller.transcribe_wav(self.wav, self.config)
            wait_until(lambda: not controller.active)
        self.assertEqual(texts, [])
        self.assertEqual(failures, ["No local model"])
        self.assertEqual(list((self.root / "recordings").iterdir()), [])

    def test_empty_transcript_preserves_draft_with_message(self):
        controller = self.controller()
        texts, failures = [], []
        controller.transcript_ready.connect(texts.append)
        controller.failed.connect(failures.append)
        with self.command("print('{\"text\":\"\"}')"):
            controller.transcribe_wav(self.wav, self.config)
            wait_until(lambda: not controller.active)
        self.assertFalse(texts)
        self.assertIn("draft is unchanged", failures[0])

    def test_cancel_kills_process_without_late_transcript(self):
        controller = self.controller()
        texts = []
        controller.transcript_ready.connect(texts.append)
        with self.command("import time;time.sleep(30);print('{\"text\":\"late\"}')"):
            controller.transcribe_wav(self.wav, self.config)
            wait_until(lambda: controller._process and controller._process.state() == QProcess.ProcessState.Running)
            controller.cancel()
        APP.processEvents()
        self.assertFalse(controller.active)
        self.assertIsNone(controller._process)
        self.assertEqual(texts, [])
        self.assertEqual(list((self.root / "recordings").iterdir()), [])

    def test_close_stops_process_and_rejects_future_recording(self):
        controller = self.controller()
        with self.command("import time;time.sleep(30)"):
            controller.transcribe_wav(self.wav, self.config)
            controller.close()
        self.assertFalse(controller.active)
        with self.assertRaises(VoiceError):
            controller.start(self.config)

    def test_timeout_kills_process_and_emits_failure(self):
        controller = self.controller()
        failures = []
        controller.failed.connect(failures.append)
        with self.command("import time;time.sleep(30)"):
            controller.transcribe_wav(self.wav, self.config)
            controller._timed_out()
        self.assertFalse(controller.active)
        self.assertIn("ten minutes", failures[0])

    def test_nonexecutable_program_failure_cleans_recording(self):
        controller = self.controller()
        failures = []
        controller.failed.connect(failures.append)
        program = self.root / "python-broken.exe"
        program.write_text("Not executable", encoding="utf-8")
        config = VoiceConfig("whisper", str(program), str(self.model))
        controller.transcribe_wav(self.wav, config)
        wait_until(lambda: not controller.active)
        self.assertIn("could not start", failures[0])
        self.assertEqual(list((self.root / "recordings").iterdir()), [])

    def test_cpp_output_file_is_read_as_utf8_and_removed(self):
        controller = self.controller()
        texts = []
        controller.transcript_ready.connect(texts.append)
        config = VoiceConfig("whisper_cpp", str(self.cpp_program), str(self.model))
        code = "from pathlib import Path; Path('transcript.txt').write_bytes('本机语音'.encode('utf-8'))"
        with self.command(code):
            controller.transcribe_wav(self.wav, config)
            wait_until(lambda: not controller.active)
        self.assertEqual(texts, ["本机语音"])
        self.assertEqual(list((self.root / "recordings").iterdir()), [])

    def test_start_failure_returns_to_idle(self):
        controller = self.controller()
        with patch.object(controller.recorder, "start", side_effect=VoiceError("No microphone")):
            with self.assertRaises(VoiceError):
                controller.start(self.config)
        self.assertEqual(controller.state, "idle")

    def test_microphone_startup_error_survives_stop_without_reentrant_cleanup(self):
        recorder = MicrophoneRecorder()
        self.addCleanup(recorder.cancel)
        source = FakeAudioSource(QtAudio.Error.OpenError)
        failures = []
        recorder.failed.connect(failures.append)
        with patch("vasl_ai_bridge.voice.QMediaDevices.defaultAudioInput", return_value=FakeMicrophone()), patch("vasl_ai_bridge.voice.QAudioSource", return_value=source):
            with self.assertRaisesRegex(VoiceError, "OpenError") as caught:
                recorder.start()
        self.assertNotIn("permission", str(caught.exception).lower())
        self.assertEqual(recorder.last_diagnostic["start_error"], "OpenError")
        self.assertEqual(source.error(), QtAudio.Error.NoError)
        self.assertEqual(source.stop_count, 1)
        self.assertFalse(source.stop_during_start)
        self.assertEqual(failures, [])
        self.assertFalse(recorder.active)

    def test_native_qt_source_enums_match_recorder_without_starting_microphone(self):
        source = QAudioSource()
        self.addCleanup(source.deleteLater)
        # QAudio's legacy enums are distinct Python classes in PySide6 6.11.2.
        # Check the real return types so mocks cannot hide this mismatch again.
        self.assertIsInstance(source.error(), QtAudio.Error)
        self.assertIsInstance(source.state(), QtAudio.State)

    def test_microphone_runtime_io_error_is_reported_once_and_preserved(self):
        recorder = MicrophoneRecorder()
        self.addCleanup(recorder.cancel)
        source = FakeAudioSource()
        failures = []
        recorder.failed.connect(failures.append)
        with patch("vasl_ai_bridge.voice.QMediaDevices.defaultAudioInput", return_value=FakeMicrophone()), patch("vasl_ai_bridge.voice.QAudioSource", return_value=source):
            recorder.start()
            source._error = QtAudio.Error.IOError
            source._state = QtAudio.State.StoppedState
            source.stateChanged.emit(source._state)
        self.assertEqual(len(failures), 1)
        self.assertIn("IOError", failures[0])
        self.assertNotIn("permission", failures[0].lower())
        self.assertEqual(recorder.last_diagnostic["runtime_error"], "IOError")
        self.assertFalse(recorder.active)

    def test_microphone_actual_permission_denial_is_distinguished(self):
        recorder = MicrophoneRecorder()
        self.addCleanup(recorder.cancel)
        with patch("vasl_ai_bridge.voice.QMediaDevices.defaultAudioInput", return_value=FakeMicrophone()), patch("vasl_ai_bridge.voice.QCoreApplication.instance") as app, patch("vasl_ai_bridge.voice.QAudioSource") as source:
            app.return_value.checkPermission.return_value = Qt.PermissionStatus.Denied
            with self.assertRaisesRegex(VoiceError, "permission was denied"):
                recorder.start()
        source.assert_not_called()
        self.assertEqual(recorder.last_diagnostic["failure"], "PermissionDenied")

    def test_microphone_native_exception_cleans_source_and_allows_retry(self):
        recorder = MicrophoneRecorder()
        self.addCleanup(recorder.cancel)
        source = FakeAudioSource(raise_on_start=True)
        with patch("vasl_ai_bridge.voice.QMediaDevices.defaultAudioInput", return_value=FakeMicrophone()), patch("vasl_ai_bridge.voice.QAudioSource", return_value=source):
            with self.assertRaisesRegex(RuntimeError, "Synthetic native failure"):
                recorder.start()
        self.assertFalse(recorder.active)
        self.assertFalse(recorder._starting)
        self.assertEqual(source.stop_count, 1)

    def test_record_stop_writes_pcm_and_returns_only_transcript(self):
        controller = self.controller()
        texts, states = [], []
        controller.transcript_ready.connect(texts.append)
        controller.state_changed.connect(states.append)

        class Source:
            def stop(self): pass
            def deleteLater(self): pass

        class Device:
            def readAll(self): return b"\0\0" * 8000

        def synthetic_start():
            controller.recorder._source = Source()
            controller.recorder._device = Device()

        code = "import json,wave; w=wave.open('recording.wav'); assert (w.getnchannels(),w.getsampwidth(),w.getframerate())==(1,2,16000);w.close();print(json.dumps({'text':'Reviewed voice draft'}))"
        with patch.object(controller.recorder, "start", side_effect=synthetic_start), self.command(code):
            controller.start(self.config)
            self.assertEqual(controller.state, "recording")
            controller.stop()
            wait_until(lambda: not controller.active)
        self.assertEqual(states, ["recording", "transcribing", "idle"])
        self.assertEqual(texts, ["Reviewed voice draft"])
        self.assertEqual(list((self.root / "recordings").iterdir()), [])

    def test_recorder_memory_bound_and_short_recording(self):
        recorder = MicrophoneRecorder()
        self.addCleanup(recorder.cancel)
        failures = []
        recorder.failed.connect(failures.append)

        class Source:
            def stop(self): pass
            def deleteLater(self): pass

        class Device:
            def readAll(self): return b"a" * (MAX_PCM_BYTES + 10)

        recorder._source = Source()
        recorder._device = Device()
        recorder._stopping = True
        recorder._read_audio()
        self.assertEqual(len(recorder._pcm), MAX_PCM_BYTES)
        recorder.cancel()
        recorder._source = Source()
        recorder.stop()
        self.assertIn("too short", failures[0])

    def test_worker_network_guard_blocks_python_connections(self):
        with patch.object(voice_worker.socket.socket, "connect"), patch.object(voice_worker.socket.socket, "connect_ex"), patch.object(voice_worker.socket, "create_connection"), patch.dict(os.environ):
            voice_worker.disable_network()
            with self.assertRaises(OSError):
                voice_worker.socket.create_connection(("example.com", 443))
            self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")


if __name__ == "__main__":
    unittest.main()
