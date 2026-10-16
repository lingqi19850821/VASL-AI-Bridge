"""Offline speech subprocess: local model paths only, JSON in and out.

This file deliberately has no imports from ASL_AI. It can run with an existing
Whisper Python environment independently of the desktop's Qt environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import wave


def disable_network() -> None:
    """Prevent Python recognizers from reaching model registries or services."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_DATASETS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

    def denied(*args, **kwargs):
        raise OSError("Network access is disabled for local speech recognition.")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied


def read_pcm(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
            raise ValueError("Speech input must be 16 kHz mono PCM16 WAV.")
        if audio.getnframes() > 16000 * 120:
            raise ValueError("Speech input exceeds the two-minute recording limit.")
        samples = audio.readframes(audio.getnframes())
    return np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768.0


def transcribe(request: dict) -> str:
    disable_network()
    model_path = Path(request["model_path"]).expanduser().resolve()
    audio_path = Path(request["audio_path"]).expanduser().resolve()
    if not model_path.exists() or not audio_path.is_file():
        raise ValueError("The selected local speech model or audio file is missing.")
    language = request.get("language", "auto")
    if language not in {"auto", "zh", "en"}:
        raise ValueError("Unsupported speech language.")
    audio = read_pcm(audio_path)
    if request["backend"] == "whisper":
        if not model_path.is_file():
            raise ValueError("OpenAI Whisper requires a local .pt model file.")
        import whisper

        # A filesystem path avoids Whisper's model-name download branch. CPU
        # keeps the user's existing CUDA installation and driver untouched.
        model = whisper.load_model(str(model_path), device="cpu")
        result = model.transcribe(audio, language=None if language == "auto" else language,
                                  task="transcribe", fp16=False, verbose=None)
        return str(result.get("text", "")).strip()
    if request["backend"] == "faster_whisper":
        if not model_path.is_dir():
            raise ValueError("Faster Whisper requires a local converted model folder.")
        from faster_whisper import WhisperModel

        model = WhisperModel(str(model_path), device="cpu", compute_type="int8",
                             local_files_only=True)
        segments, _ = model.transcribe(audio, language=None if language == "auto" else language,
                                        task="transcribe", beam_size=3)
        return " ".join(segment.text.strip() for segment in segments).strip()
    raise ValueError("Unsupported Python speech backend.")


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("Speech request is too large.")
        result = transcribe(json.loads(raw.decode("utf-8")))
        response = {"text": result}
        exit_code = 0
    except Exception as exc:
        response = {"error": f"{type(exc).__name__}: {exc}"}
        exit_code = 1
    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
