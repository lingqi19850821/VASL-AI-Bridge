"""Bounded microphone recording and cancellable, entirely local transcription."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import wave

from PySide6.QtCore import QCoreApplication, QMicrophonePermission, QObject, QProcess, QProcessEnvironment, QTimer, Signal, Qt, qVersion
from PySide6.QtMultimedia import QtAudio, QAudioFormat, QAudioSource, QMediaDevices

from .storage import default_data_directory
from .desktop_installation import resolve_desktop_dll
from .frozen_runtime import external_command, is_frozen, speech_helper_path


SAMPLE_RATE = 16000
MAX_RECORD_SECONDS = 120
MAX_PCM_BYTES = SAMPLE_RATE * 2 * MAX_RECORD_SECONDS
MAX_TRANSCRIPTION_SECONDS = 600
MAX_TRANSCRIPT_BYTES = 65536
MAX_RESPONSE_BYTES = 131072  # Transcript plus bounded native diagnostics.
_SOURCE_ROOT = Path(__file__).resolve().parents[1]


class VoiceError(ValueError):
    """A local speech configuration or recording cannot be used."""


@dataclass(frozen=True)
class VoiceConfig:
    backend: str = "auto"
    program_path: str = ""
    model_path: str = ""
    language: str = "auto"
    dll_path: str = ""
    desktop_implementation: str = ""


def _local_path(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise VoiceError(f"Choose a local {label} in Settings > Voice.")
    # Registry URLs and model registry identifiers are never accepted.
    if "://" in value or value.startswith(("\\\\", "//")):
        raise VoiceError(f"The {label} must be a local filesystem path.")
    return Path(value).expanduser().resolve()


def find_local_voice_configs() -> list[VoiceConfig]:
    """Find the locally verified whisper.cpp engine, without model downloads.

    This is deliberately a small inventory, not a scan of the user's files.
    Explicit paths in Settings are available for installations elsewhere.
    """
    results: list[VoiceConfig] = []
    roots = [default_data_directory() / "voice-engine", _SOURCE_ROOT / "private" / "voice-engine"]
    for root in roots:
        if not root.is_dir():
            continue
        programs = [root / "whisper-cli.exe", root / "bin" / "whisper-cli.exe",
                    root / "bin" / "Release" / "whisper-cli.exe"]
        programs += list(root.glob("*/whisper-cli.exe"))
        models = list(root.glob("ggml-*.bin")) + list((root / "models").glob("ggml-*.bin"))
        for program in programs:
            if program.is_file():
                for model in sorted(models, key=lambda p: ("base" not in p.name, ".en" in p.name, p.name)):
                    results.append(VoiceConfig("whisper_cpp", str(program), str(model)))
    return results


def resolve_voice_config(config: VoiceConfig) -> VoiceConfig:
    if config.backend not in {"auto", "whisper", "faster_whisper", "whisper_cpp", "whisper_desktop"}:
        raise VoiceError("Choose a supported local speech backend in Settings > Voice.")
    if config.language not in {"auto", "zh", "en"}:
        raise VoiceError("Unsupported speech language identifier.")
    if config.backend == "auto":
        # Automatic selection is an explicit UI mode. Preserve custom paths in
        # preferences for later use; do not reinterpret them as a CLI program.
        available = find_local_voice_configs()
        if not available:
            raise VoiceError("No local speech engine and model were found. Choose them in Settings > Voice.")
        config = replace(available[0], language=config.language)
    program = _local_path(config.program_path, "speech program")
    model = _local_path(config.model_path, "speech model")
    backend = config.backend
    if not (program.is_file() or (backend == "whisper_desktop" and program.is_dir())):
        raise VoiceError("The selected local speech program does not exist.")
    if backend == "whisper_desktop":
        try:
            dll = resolve_desktop_dll(program)
        except (ValueError, OSError) as error:
            raise VoiceError(str(error)) from error
        if config.language == "auto" and dll.implementation != "whisper_cpp_native":
            raise VoiceError("This older Whisper Desktop DLL build does not support verified automatic "
                             "transcription. A compatible whisper.cpp Desktop installation is required.")
    if backend == "whisper_cpp" and program.name.lower() not in {"whisper-cli", "whisper-cli.exe"}:
        raise VoiceError("The whisper.cpp backend requires whisper-cli.exe, not a desktop application "
                         "or another CLI. Choose Automatic local engine in Settings > Voice.")
    if backend in {"whisper", "faster_whisper"} and not program.name.lower().startswith("python"):
        raise VoiceError("The Python speech backend requires its Python executable. "
                         "Choose Automatic local engine or a Python program in Settings > Voice.")
    if backend in {"whisper", "faster_whisper"} and program.name.lower() == "pythonw.exe":
        raise VoiceError("Choose python.exe rather than pythonw.exe for the background speech engine.")
    if backend in {"whisper", "whisper_cpp", "whisper_desktop"} and not model.is_file():
        raise VoiceError("The selected local speech model file does not exist.")
    if backend == "faster_whisper" and not (model.is_dir() and (model / "model.bin").is_file() and (model / "config.json").is_file()):
        raise VoiceError("The selected Faster Whisper folder needs model.bin and config.json.")
    if backend == "whisper_desktop":
        return VoiceConfig(backend, str(program), str(model), config.language, str(dll.path.resolve()), dll.implementation)
    return VoiceConfig(backend, str(program), str(model), config.language)


def validate_wav(path: Path) -> int:
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                raise VoiceError("Speech input must be 16 kHz mono PCM16 WAV.")
            frames = audio.getnframes()
            if not 1600 <= frames <= SAMPLE_RATE * MAX_RECORD_SECONDS:
                raise VoiceError("Record between 0.1 seconds and two minutes of speech.")
            return frames
    except (OSError, EOFError, wave.Error) as exc:
        raise VoiceError("The local speech recording could not be read.") from exc


def build_transcription_command(config: VoiceConfig, audio: Path, output_prefix: Path) -> tuple[str, list[str], bytes]:
    config = resolve_voice_config(config)
    if config.backend == "whisper_cpp":
        return config.program_path, ["-m", config.model_path, "-f", str(audio), "-l", config.language,
                                     "-otxt", "-of", str(output_prefix), "-nt", "-np", "-ng"], b""
    if config.backend == "whisper_desktop":
        request = {"dll_path": config.dll_path,
                   "model_path": config.model_path, "audio_path": str(audio), "language": config.language}
        if is_frozen():
            backend = ("whisper_cpp_native" if config.desktop_implementation == "whisper_cpp_native"
                       else "whisper_desktop")
            try:
                helper = speech_helper_path()
            except RuntimeError as error:
                raise VoiceError(str(error)) from error
            return str(helper), ["--backend", backend], json.dumps(request, ensure_ascii=False).encode("utf-8")
        worker_name = ("whisper_cpp_native_worker.py" if config.desktop_implementation == "whisper_cpp_native"
                       else "whisper_desktop_worker.py")
        worker = Path(__file__).with_name(worker_name)
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe":
            python = python.with_name("python.exe")
        if not python.is_file():
            raise VoiceError("The local Python speech worker could not be found.")
        return str(python), ["-I", "-B", "-X", "utf8", str(worker)], json.dumps(request, ensure_ascii=False).encode("utf-8")
    request = {"backend": config.backend, "model_path": config.model_path,
               "audio_path": str(audio), "language": config.language}
    worker = Path(__file__).with_name("voice_worker.py")
    return config.program_path, ["-I", "-B", str(worker)], json.dumps(request, ensure_ascii=False).encode("utf-8")


class MicrophoneRecorder(QObject):
    """Capture PCM in memory; access to the microphone begins only at start()."""

    recorded = Signal(bytes)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._source: QAudioSource | None = None
        self._device = None
        self._pcm = bytearray()
        self._stopping = False
        self._starting = False
        self._startup_error = None
        self.last_diagnostic: dict = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.stop)

    @property
    def active(self) -> bool:
        return self._source is not None

    def start(self) -> None:
        if self.active:
            raise VoiceError("A recording is already in progress.")
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            raise VoiceError("No microphone is available. Check Windows microphone settings.")
        audio_format = QAudioFormat()
        audio_format.setSampleRate(SAMPLE_RATE)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        app = QCoreApplication.instance()
        permission = app.checkPermission(QMicrophonePermission()) if app else None
        preferred = device.preferredFormat()
        self.last_diagnostic = {
            "qt_version": qVersion(), "device": device.description(),
            "permission_status": permission.name if permission is not None else "Unavailable",
            "requested_format": {"rate": SAMPLE_RATE, "channels": 1, "sample_format": "Int16"},
            "preferred_format": {"rate": preferred.sampleRate(), "channels": preferred.channelCount(),
                                 "sample_format": preferred.sampleFormat().name},
            "states": [], "bytes_captured": 0,
        }
        if permission == Qt.PermissionStatus.Denied:
            self.last_diagnostic["failure"] = "PermissionDenied"
            raise VoiceError("Microphone permission was denied by the system. Check Windows microphone permission.")
        if not device.isFormatSupported(audio_format):
            raise VoiceError("This microphone does not support 16 kHz mono PCM16 recording.")
        self._pcm.clear()
        source = QAudioSource(device, audio_format, self)
        source.setBufferSize(SAMPLE_RATE * 2)
        self._source = source
        source.stateChanged.connect(self._audio_state_changed)
        self._starting = True
        self._startup_error = None
        try:
            self._device = source.start()
        except Exception:
            self.cancel()
            raise
        finally:
            self._starting = False
        error = self._startup_error or source.error()
        self.last_diagnostic["start_returned_io"] = self._device is not None
        self.last_diagnostic["start_error"] = error.name
        self.last_diagnostic["start_state"] = source.state().name
        if self._device is None or error != QtAudio.Error.NoError:
            message = self._audio_error_message(error, "could not start")
            self.last_diagnostic["failure"] = message
            self.cancel()
            raise VoiceError(message)
        self._device.readyRead.connect(self._read_audio)
        self._timer.start(MAX_RECORD_SECONDS * 1000)

    def _audio_state_changed(self, state) -> None:
        if self._source is None or self._stopping:
            return
        error = self._source.error()
        self.last_diagnostic.setdefault("states", []).append({"state": state.name, "error": error.name})
        if state == QtAudio.State.StoppedState and error != QtAudio.Error.NoError:
            if self._starting:
                # stop() clears Qt's error and can invalidate the returned IO
                # pointer. Never tear down inside the native start() call.
                self._startup_error = error
                return
            self.last_diagnostic["runtime_error"] = error.name
            message = self._audio_error_message(error, "stopped")
            self.last_diagnostic["failure"] = message
            self.cancel()
            self.failed.emit(message)

    def _audio_error_message(self, error, action: str) -> str:
        explanation = {
            QtAudio.Error.OpenError: "The audio device could not be opened. Check the selected microphone, another app using it, and device format.",
            QtAudio.Error.IOError: "Reading the microphone failed. Check whether the device was disconnected or changed.",
            QtAudio.Error.FatalError: "The audio device is unavailable. Check the microphone connection and Windows audio settings.",
        }.get(error, "The audio backend did not provide a usable recording stream.")
        device = self.last_diagnostic.get("device", "Default microphone")
        return f"Microphone {action}: {error.name} ({device}; 16000 Hz mono Int16). {explanation}"

    def _read_audio(self) -> None:
        if self._device is None:
            return
        remaining = MAX_PCM_BYTES - len(self._pcm)
        self._pcm.extend(bytes(self._device.readAll())[:remaining])
        self.last_diagnostic["bytes_captured"] = len(self._pcm)
        if len(self._pcm) >= MAX_PCM_BYTES and not self._stopping:
            self.stop()

    def stop(self) -> None:
        if not self.active or self._stopping:
            return
        self._stopping = True
        self._read_audio()
        pcm = bytes(self._pcm[:len(self._pcm) // 2 * 2])
        self.cancel()
        if len(pcm) < 3200:
            self.failed.emit("The recording was too short. Hold Record for at least a moment.")
        else:
            self.recorded.emit(pcm)

    def cancel(self) -> None:
        self._timer.stop()
        self._stopping = True
        if self._source is not None:
            source, self._source = self._source, None
            source.stop()
            source.deleteLater()
        self._device = None
        self._pcm.clear()
        self._stopping = False


class VoiceController(QObject):
    """One recording/process at a time. Transcripts are drafts, never requests."""

    state_changed = Signal(str)
    transcript_ready = Signal(str)
    failed = Signal(str)

    def __init__(self, directory: Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.directory = Path(directory) if directory is not None else default_data_directory() / "voice-recordings"
        self.recorder = MicrophoneRecorder(self)
        self.recorder.recorded.connect(self._recorded)
        self.recorder.failed.connect(self._fail)
        self._process: QProcess | None = None
        self._config: VoiceConfig | None = None
        self._work_directory: Path | None = None
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._pending_request = b""
        self._closed = False
        self.last_transcription_diagnostic: dict = {}
        self.state = "idle"
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._timed_out)

    @property
    def active(self) -> bool:
        return self.state != "idle"

    def _set_state(self, state: str) -> None:
        self.state = state
        self.state_changed.emit(state)

    def start(self, config: VoiceConfig = VoiceConfig()) -> None:
        if self._closed or self.active:
            raise VoiceError("Speech input is unavailable or already active.")
        self._config = resolve_voice_config(config)
        self._set_state("recording")
        try:
            self.recorder.start()
        except Exception:
            self._set_state("idle")
            raise

    def stop(self) -> None:
        if self.state == "recording":
            self.recorder.stop()

    def _new_work_directory(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._work_directory = Path(tempfile.mkdtemp(prefix="recording-", dir=self.directory))
        return self._work_directory

    def _recorded(self, pcm: bytes) -> None:
        try:
            work = self._new_work_directory()
            audio = work / "recording.wav"
            with wave.open(str(audio), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(SAMPLE_RATE)
                handle.writeframes(pcm)
            self._transcribe(audio)
        except Exception as exc:
            self._fail(str(exc))

    def transcribe_wav(self, path: Path, config: VoiceConfig = VoiceConfig()) -> None:
        """Use a synthetic/existing WAV without opening the microphone."""
        if self._closed or self.active:
            raise VoiceError("Speech input is unavailable or already active.")
        self._config = resolve_voice_config(config)
        validate_wav(path)
        try:
            work = self._new_work_directory()
            audio = work / "recording.wav"
            shutil.copyfile(path, audio)
            self._transcribe(audio)
        except Exception:
            self._cleanup()
            self._set_state("idle")
            raise

    def _transcribe(self, audio: Path) -> None:
        validate_wav(audio)
        program, arguments, request = build_transcription_command(self._config, audio, self._work_directory / "transcript")
        if self._config.backend != "whisper_desktop":
            command = external_command([program, *arguments])
            program, arguments = command[0], command[1:]
        self.last_transcription_diagnostic = {"backend": self._config.backend, "program": self._config.program_path,
                                              "model": self._config.model_path, "language": self._config.language,
                                              "worker_program": program}
        if self._config.backend == "whisper_desktop":
            self.last_transcription_diagnostic.update(dll_path=self._config.dll_path,
                                                      desktop_implementation=self._config.desktop_implementation)
        self._stdout.clear()
        self._stderr.clear()
        process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
            environment.insert(name, "1")
        environment.insert("PYTHONUTF8", "1")
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(self._work_directory))
        process.setProgram(program)
        process.setArguments(arguments)
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._process_error)
        self._pending_request = request
        process.started.connect(self._write_request)
        self._process = process
        self._set_state("transcribing")
        self._timer.start(MAX_TRANSCRIPTION_SECONDS * 1000)
        process.start()

    def _write_request(self) -> None:
        if self._process:
            if self._pending_request:
                self._process.write(self._pending_request)
            self._process.closeWriteChannel()
        self._pending_request = b""

    def _read_stdout(self) -> None:
        if self._process:
            remaining = MAX_RESPONSE_BYTES - len(self._stdout)
            self._stdout.extend(bytes(self._process.readAllStandardOutput())[:remaining])

    def _read_stderr(self) -> None:
        if self._process:
            remaining = 16384 - len(self._stderr)
            self._stderr.extend(bytes(self._process.readAllStandardError())[:remaining])

    def _process_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart and self._process:
            self._fail("The local speech program could not start. Check Settings > Voice.")

    def _finished(self, exit_code: int, exit_status) -> None:
        if self._process is None:
            return
        self._read_stdout()
        self._read_stderr()
        self._timer.stop()
        try:
            if exit_status != QProcess.ExitStatus.NormalExit or exit_code != 0:
                message = "Local speech recognition failed. Check the engine and model in Settings > Voice."
                if self._config.backend != "whisper_cpp":
                    try:
                        message = json.loads(self._stdout.decode("utf-8")).get("error", message)
                    except (ValueError, UnicodeError):
                        pass
                raise VoiceError(message)
            if self._config.backend == "whisper_cpp":
                output = self._work_directory / "transcript.txt"
                if not output.is_file() or output.stat().st_size > 65536:
                    raise VoiceError("The speech program did not produce a usable transcript.")
                text = output.read_text(encoding="utf-8-sig").strip()
            else:
                response = json.loads(self._stdout.decode("utf-8"))
                text = response.get("text", "")
                if isinstance(response.get("diagnostic"), dict):
                    self.last_transcription_diagnostic["worker"] = response["diagnostic"]
                if not isinstance(text, str):
                    raise VoiceError("The speech program returned an invalid transcript.")
                if len(text.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
                    raise VoiceError("The speech program returned an oversized transcript.")
                text = text.strip()
            self._cleanup()
            self._set_state("idle")
            if text:
                self.transcript_ready.emit(text)
            else:
                self.failed.emit("No speech was recognized. Your existing draft is unchanged.")
        except Exception as exc:
            self._fail(str(exc))

    def _timed_out(self) -> None:
        self._fail("Local speech recognition exceeded ten minutes and was stopped. Your draft is unchanged.")

    def _fail(self, message: str) -> None:
        self.cancel()
        if not self._closed:
            self.failed.emit(message)

    def _cleanup(self) -> None:
        self._timer.stop()
        self._pending_request = b""
        if self._process:
            process, self._process = self._process, None
            process.blockSignals(True)
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(3000)
            process.deleteLater()
        if self._work_directory:
            work, self._work_directory = self._work_directory, None
            # This is an owned mkdtemp directory under the recording directory.
            # Never delete the original WAV, user's model, or arbitrary paths.
            if work.resolve().parent == self.directory.resolve() and work.name.startswith("recording-"):
                shutil.rmtree(work, ignore_errors=True)

    def cancel(self) -> None:
        self.recorder.cancel()
        self._cleanup()
        self._set_state("idle")

    def close(self) -> None:
        self._closed = True
        self.cancel()
