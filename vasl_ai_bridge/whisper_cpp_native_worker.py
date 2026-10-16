"""Isolated adapter for the locally verified whisper.cpp 1.9.4 native DLL.

ABI source: https://github.com/ggml-org/whisper.cpp/blob/v1.9.4/include/whisper.h
Native loading and inference run only in this disposable child process. Killing
the child cancels native work without delivering a late partial transcript.
The verified DLL hash gates the structure ABI before any native library loads.
"""
from __future__ import annotations

import array
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import wave


SUPPORTED_DLL_SHA256 = "524759068eb35bffc4cf0f896c471264c0c0225febf2525f9c8c658e2871eb58"
SUPPORTED_VERSION = "1.9.4-dev"
MAX_REQUEST_BYTES = 65536
MAX_TRANSCRIPT_BYTES = 65536
MAX_RESPONSE_BYTES = 131072
MAX_NATIVE_LOG_BYTES = 65536
TRANSCRIPT_STYLE_EXAMPLE = '今天上午天气很好，下午可能会下雨。Hello, everyone. Shall we start?'
NativeLogCallback = C.CFUNCTYPE(None, C.c_int, C.c_char_p, C.c_void_p)


class Aheads(C.Structure):
    _fields_ = [("n_heads", C.c_size_t), ("heads", C.c_void_p)]


class ContextParams(C.Structure):
    _fields_ = [("use_gpu", C.c_bool), ("flash_attn", C.c_bool), ("gpu_device", C.c_int),
                ("dtw_token_timestamps", C.c_bool), ("dtw_aheads_preset", C.c_int),
                ("dtw_n_top", C.c_int), ("dtw_aheads", Aheads), ("dtw_mem_size", C.c_size_t)]


class VadParams(C.Structure):
    _fields_ = [("threshold", C.c_float), ("min_speech_duration_ms", C.c_int),
                ("min_silence_duration_ms", C.c_int), ("max_speech_duration_s", C.c_float),
                ("speech_pad_ms", C.c_int), ("samples_overlap", C.c_float)]


class Greedy(C.Structure):
    _fields_ = [("best_of", C.c_int)]


class BeamSearch(C.Structure):
    _fields_ = [("beam_size", C.c_int), ("patience", C.c_float)]


class FullParams(C.Structure):
    _fields_ = [("strategy", C.c_int), ("n_threads", C.c_int), ("n_max_text_ctx", C.c_int),
                ("offset_ms", C.c_int), ("duration_ms", C.c_int),
                ("translate", C.c_bool), ("no_context", C.c_bool), ("no_timestamps", C.c_bool),
                ("single_segment", C.c_bool), ("print_special", C.c_bool),
                ("print_progress", C.c_bool), ("print_realtime", C.c_bool),
                ("print_timestamps", C.c_bool), ("token_timestamps", C.c_bool),
                ("thold_pt", C.c_float), ("thold_ptsum", C.c_float), ("max_len", C.c_int),
                ("split_on_word", C.c_bool), ("max_tokens", C.c_int), ("debug_mode", C.c_bool),
                ("audio_ctx", C.c_int), ("tdrz_enable", C.c_bool), ("suppress_regex", C.c_void_p),
                ("initial_prompt", C.c_void_p), ("carry_initial_prompt", C.c_bool),
                ("prompt_tokens", C.c_void_p), ("prompt_n_tokens", C.c_int),
                ("language", C.c_char_p), ("detect_language", C.c_bool),
                ("suppress_blank", C.c_bool), ("suppress_nst", C.c_bool),
                ("temperature", C.c_float), ("max_initial_ts", C.c_float),
                ("length_penalty", C.c_float), ("temperature_inc", C.c_float),
                ("entropy_thold", C.c_float), ("logprob_thold", C.c_float),
                ("no_speech_thold", C.c_float), ("greedy", Greedy), ("beam_search", BeamSearch),
                ("new_segment_callback", C.c_void_p), ("new_segment_callback_user_data", C.c_void_p),
                ("progress_callback", C.c_void_p), ("progress_callback_user_data", C.c_void_p),
                ("encoder_begin_callback", C.c_void_p), ("encoder_begin_callback_user_data", C.c_void_p),
                ("abort_callback", C.c_void_p), ("abort_callback_user_data", C.c_void_p),
                ("logits_filter_callback", C.c_void_p), ("logits_filter_callback_user_data", C.c_void_p),
                ("grammar_rules", C.c_void_p), ("n_grammar_rules", C.c_size_t),
                ("i_start_rule", C.c_size_t), ("grammar_penalty", C.c_float),
                ("vad", C.c_bool), ("vad_model_path", C.c_void_p), ("vad_params", VadParams)]


def bind(lib, name, result, args):
    function = getattr(lib, name)
    function.restype = result
    function.argtypes = args
    return function


def local_file(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "://" in value or value.startswith(("\\\\", "//")):
        raise ValueError(f"Choose a local {label} file.")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"The selected {label} file does not exist.")
    return path


def validate_dll(path: Path) -> dict:
    """Validate the known native ABI without loading any speech-engine DLL."""
    if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
        raise ValueError("The Whisper DLL adapter requires 64-bit Windows Python.")
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("The selected Whisper DLL is not a Windows DLL.")
        stream.seek(struct.unpack_from("<I", header, 60)[0])
        pe = stream.read(26)
    if len(pe) != 26 or pe[:4] != b"PE\0\0" or struct.unpack_from("<H", pe, 4)[0] != 0x8664:
        raise ValueError("The selected Whisper DLL is not an AMD64 DLL.")
    if struct.unpack_from("<H", pe, 24)[0] != 0x20B:
        raise ValueError("The selected Whisper DLL is not a 64-bit PE image.")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != SUPPORTED_DLL_SHA256:
        raise ValueError("This whisper.cpp DLL build has not been verified for the native adapter. "
                         "Its structure ABI cannot be inferred from export names alone.")
    if not (path.parent / "ggml.dll").is_file():
        raise ValueError("The selected Whisper DLL requires its installed ggml.dll dependency.")
    if not (path.parent / "ggml-base.dll").is_file():
        raise ValueError("The selected Whisper DLL requires its installed ggml-base.dll dependency.")
    if not any(path.parent.glob("ggml-cpu*.dll")):
        raise ValueError("The selected Whisper DLL requires an installed GGML CPU backend.")
    if (C.sizeof(ContextParams), C.sizeof(FullParams), C.sizeof(VadParams)) != (48, 304, 24):
        raise ValueError("The Python/native Whisper ABI structure sizes do not match.")
    if FullParams.language.offset != 104 or FullParams.new_segment_callback.offset != 160 or FullParams.vad_params.offset != 280:
        raise ValueError("The Python/native Whisper ABI structure alignment does not match.")
    return {"dll_path": str(path), "dll_sha256": digest, "whisper_version": SUPPORTED_VERSION,
            "pe_machine": "AMD64", "abi_tag": "v1.9.4", "implementation": "automatic",
            "abi_sizes": {"ContextParams": 48, "FullParams": 304, "VadParams": 24}}


def choose_cuda_device(devices: list[dict]) -> dict | None:
    """Prefer a dedicated CUDA GPU, with the ordinal used by whisper.cpp."""
    gpu_ordinal = 0
    for device in devices:
        kind = device["type"]
        if kind in (1, 2):
            if kind == 1 and device["backend"] == "CUDA":
                return dict(device, gpu_ordinal=gpu_ordinal)
            gpu_ordinal += 1
    return None


def load_compute_devices(ggml, base, directory: Path) -> dict:
    """Inspect installed native backends; do not change PATH or system settings."""
    cuda_error = ""
    cuda_path = directory / "ggml-cuda.dll"
    if cuda_path.is_file():
        try:
            # ctypes resolves dependencies using this child's os.add_dll_directory.
            # GGML's own LoadLibrary call can otherwise miss those dependencies.
            cuda_library = C.CDLL(str(cuda_path))
            registry = bind(ggml, "ggml_backend_load", C.c_void_p, [C.c_char_p])(
                str(cuda_path).encode("utf-8"))
            if not registry:
                raise RuntimeError("Installed CUDA backend could not be registered.")
            backend = bind(base, "ggml_backend_reg_name", C.c_char_p, [C.c_void_p])(registry)
            if not backend or backend.decode("utf-8") != "CUDA":
                raise RuntimeError("Installed CUDA library did not register a CUDA backend.")
        except (OSError, RuntimeError) as error:
            cuda_error = str(error)
    else:
        cuda_error = "No CUDA backend is installed beside the selected Whisper DLL."
    bind(ggml, "ggml_backend_load_all_from_path", None, [C.c_char_p])(
        str(directory).encode("utf-8"))
    count_devices = bind(ggml, "ggml_backend_dev_count", C.c_size_t, [])
    get_device = bind(ggml, "ggml_backend_dev_get", C.c_void_p, [C.c_size_t])
    get_type = bind(base, "ggml_backend_dev_type", C.c_int, [C.c_void_p])
    get_reg = bind(base, "ggml_backend_dev_backend_reg", C.c_void_p, [C.c_void_p])
    get_name = bind(base, "ggml_backend_dev_name", C.c_char_p, [C.c_void_p])
    get_description = bind(base, "ggml_backend_dev_description", C.c_char_p, [C.c_void_p])
    get_backend = bind(base, "ggml_backend_reg_name", C.c_char_p, [C.c_void_p])
    get_memory = bind(base, "ggml_backend_dev_memory", None,
                      [C.c_void_p, C.POINTER(C.c_size_t), C.POINTER(C.c_size_t)])
    count = count_devices()
    if count > 256:
        raise RuntimeError("The native backend reported too many compute devices.")
    devices = []
    for index in range(count):
        device = get_device(index)
        kind = get_type(device)
        free = C.c_size_t()
        total = C.c_size_t()
        get_memory(device, C.byref(free), C.byref(total))
        devices.append({"name": get_name(device).decode("utf-8"),
                        "description": get_description(device).decode("utf-8"),
                        "backend": get_backend(get_reg(device)).decode("utf-8"),
                        "type": kind, "memory_free_bytes": free.value,
                        "memory_total_bytes": total.value})
    selected = choose_cuda_device(devices)
    if selected is None:
        reason = cuda_error or "The installed CUDA backend exposes no dedicated CUDA GPU."
        return {"implementation": "CPU", "devices": devices, "use_gpu": False,
                "gpu_device": 0, "fallback_reason": reason}
    return {"implementation": "CUDA", "devices": devices, "use_gpu": True,
            "gpu_device": selected["gpu_ordinal"], "device_name": selected["name"],
            "device_description": selected["description"],
            "memory_free_bytes": selected["memory_free_bytes"],
            "memory_total_bytes": selected["memory_total_bytes"]}


def confirm_cuda_execution(diagnostic: dict, native_log: str) -> None:
    """Confirm model placement and compute selection from the native library."""
    if not diagnostic["use_gpu"]:
        diagnostic["gpu_execution_confirmed"] = False
        return
    device = diagnostic["device_name"]
    diagnostic["gpu_execution_confirmed"] = False
    if "failed to initialize " + device + " backend" in native_log:
        raise RuntimeError("CUDA execution could not be confirmed; the selected GPU backend failed to initialize.")
    placed = any(device + " total size" in line for line in native_log.splitlines())
    computing = "using " + device + " backend" in native_log
    if not placed or not computing:
        raise RuntimeError("CUDA execution could not be confirmed; the native engine did not use the selected GPU.")
    diagnostic["gpu_execution_confirmed"] = True


def read_audio(path: Path) -> tuple[int, array.array]:
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("Expected 16 kHz mono PCM16 WAV.")
        frames = audio.getnframes()
        if not 1600 <= frames <= 16000 * 120:
            raise ValueError("Audio duration is outside the supported bounds.")
        raw = audio.readframes(frames)
        if len(raw) != frames * 2:
            raise ValueError("The speech WAV is truncated.")
    pcm = array.array("h", raw)
    return frames, pcm


def transcribe(request: dict) -> dict:
    started = time.monotonic()
    language = request.get("language", "auto")
    if language not in {"auto", "en", "zh"}:
        raise ValueError("Unsupported speech language identifier.")
    dll_path = local_file(request.get("dll_path"), "Whisper DLL")
    model_path = local_file(request.get("model_path"), "speech model")
    audio_path = local_file(request.get("audio_path"), "speech recording")
    with model_path.open("rb") as model_file:
        if model_file.read(4) != b"lmgg":
            raise ValueError("The selected Whisper model must be an original GGML model file.")
    diagnostic = validate_dll(dll_path)
    frames, pcm = read_audio(audio_path)
    values = (C.c_float * frames)(*(sample / 32768 for sample in pcm))
    with os.add_dll_directory(str(dll_path.parent)):
        lib = C.CDLL(str(dll_path))
        version = bind(lib, "whisper_version", C.c_char_p, [])().decode("ascii")
        if version != SUPPORTED_VERSION:
            raise ValueError("Unexpected whisper.cpp ABI version: " + version)
        ggml = C.CDLL(str(dll_path.parent / "ggml.dll"))
        base = C.CDLL(str(dll_path.parent / "ggml-base.dll"))
        diagnostic.update(load_compute_devices(ggml, base, dll_path.parent))
        native_log = bytearray()

        @NativeLogCallback
        def log_message(level, message, user_data):
            if message:
                if len(native_log) < MAX_NATIVE_LOG_BYTES:
                    native_log.extend(message[:MAX_NATIVE_LOG_BYTES - len(native_log)])
                sys.stderr.buffer.write(message)
                sys.stderr.buffer.flush()

        log_set = bind(lib, "whisper_log_set", None, [NativeLogCallback, C.c_void_p])
        ctx = None
        try:
            log_set(log_message, None)
            defaults = bind(lib, "whisper_context_default_params_by_ref", C.POINTER(ContextParams), [])()
            if not defaults:
                raise RuntimeError("No native context defaults.")
            try:
                context_params = ContextParams.from_buffer_copy(C.string_at(defaults, C.sizeof(ContextParams)))
            finally:
                bind(lib, "whisper_free_context_params", None, [C.POINTER(ContextParams)])(defaults)
            context_params.use_gpu = diagnostic["use_gpu"]
            context_params.gpu_device = diagnostic["gpu_device"]
            context_params.flash_attn = False
            load_started = time.monotonic()
            ctx = bind(lib, "whisper_init_from_file_with_params", C.c_void_p, [C.c_char_p, ContextParams])(
                str(model_path).encode("utf-8"), context_params)
            if not ctx:
                raise RuntimeError("The selected model could not be loaded.")
            model_loaded = time.monotonic()
            print("ASL_AI_NATIVE_MODEL_READY", file=sys.stderr, flush=True)
            confirm_cuda_execution(diagnostic, native_log.decode("utf-8", "replace"))
            defaults = bind(lib, "whisper_full_default_params_by_ref", C.POINTER(FullParams), [C.c_int])(0)
            if not defaults:
                raise RuntimeError("No native transcription defaults.")
            try:
                params = FullParams.from_buffer_copy(C.string_at(defaults, C.sizeof(FullParams)))
            finally:
                bind(lib, "whisper_free_params", None, [C.POINTER(FullParams)])(defaults)
            if params.strategy != 0 or params.n_threads <= 0 or params.greedy.best_of <= 0:
                raise ValueError("Native transcription defaults do not match the expected ABI.")
            params.n_threads = min(8, os.cpu_count() or 1)
            params.no_context = True
            params.no_timestamps = False
            params.print_realtime = params.print_progress = params.print_timestamps = params.print_special = False
            params.language = language.encode("ascii")
            # "auto" detects the language and continues transcription. The
            # detect_language flag instead exits after detection without text.
            params.detect_language = False
            params.translate = False
            # Supply transcript style as preceding text; keep its native buffer alive.
            prompt_buffer = C.create_string_buffer(TRANSCRIPT_STYLE_EXAMPLE.encode('utf-8'))
            params.initial_prompt = C.cast(prompt_buffer, C.c_void_p).value
            result = bind(lib, "whisper_full", C.c_int, [C.c_void_p, FullParams, C.POINTER(C.c_float), C.c_int])(ctx, params, values, frames)
            if result != 0:
                raise RuntimeError(f"whisper_full failed with code {result}.")
            confirm_cuda_execution(diagnostic, native_log.decode("utf-8", "replace"))
            infer_done = time.monotonic()
            language_id = bind(lib, "whisper_full_lang_id", C.c_int, [C.c_void_p])(ctx)
            detected_language = bind(lib, "whisper_lang_str", C.c_char_p, [C.c_int])(language_id)
            if not detected_language:
                raise RuntimeError("Whisper returned an invalid detected language.")
            detected_language = detected_language.decode("ascii")
            count = bind(lib, "whisper_full_n_segments", C.c_int, [C.c_void_p])(ctx)
            if not 0 <= count <= 4096:
                raise RuntimeError("Unexpected native segment count.")
            texts = []
            transcript_bytes = 0
            get_text = bind(lib, "whisper_full_get_segment_text", C.c_char_p, [C.c_void_p, C.c_int])
            for index in range(count):
                raw = get_text(ctx, index)
                if raw:
                    transcript_bytes += len(raw)
                    if transcript_bytes > MAX_TRANSCRIPT_BYTES:
                        raise RuntimeError("Whisper returned an oversized transcript.")
                    texts.append(raw.decode("utf-8"))
            text = "".join(texts).strip()
            diagnostic.update({"backend": "whisper_cpp_native", "language": language,
                    "detected_language": detected_language, "translate": bool(params.translate),
                    "model_path": str(model_path), "audio_path": str(audio_path), "frames": frames,
                    "model_load_seconds": model_loaded - load_started, "inference_seconds": infer_done - model_loaded,
                    "segments": count, "process_seconds": time.monotonic() - started})
            return {"text": text, "diagnostic": diagnostic}
        finally:
            try:
                if ctx:
                    bind(lib, "whisper_free", None, [C.c_void_p])(ctx)
            finally:
                log_set(NativeLogCallback(), None)


def write_response(response: dict) -> None:
    payload = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Whisper returned an oversized response.")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValueError("Speech worker request is too large.")
        request = json.loads(data.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("Speech worker expects a JSON object.")
        result = transcribe(request)
        write_response(result)
    except Exception as error:
        write_response({"error": str(error)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
