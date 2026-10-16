"""Isolated Const-me Whisper 1.12.0 DLL adapter (no Desktop UI or microphone).

ABI definitions follow the official 1.12.0 headers under Whisper/API:
https://github.com/Const-me/Whisper/tree/1.12.0/Whisper/API
Input and output are single JSON objects. Parent process termination cancels
native loading/inference; a cancelled process cannot return a partial draft.
"""

from __future__ import annotations

import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
import wave


ABI_VERSION = (1, 12, 0, 0)
HRESULT = C.c_int32
UINT32 = C.c_uint32
PTR = C.c_void_p


class ModelSetup(C.Structure):
    _fields_ = [("implementation", UINT32), ("flags", UINT32), ("adapter", C.c_wchar_p)]


class FullParams(C.Structure):
    _fields_ = [
        ("strategy", C.c_int32), ("cpu_threads", C.c_int32), ("max_text_context", C.c_int32),
        ("offset_ms", C.c_int32), ("duration_ms", C.c_int32), ("flags", UINT32),
        ("language", UINT32), ("token_threshold", C.c_float), ("token_sum_threshold", C.c_float),
        ("max_length", C.c_int32), ("max_tokens", C.c_int32), ("greedy_past", C.c_int32),
        ("beam_past", C.c_int32), ("beam_width", C.c_int32), ("beam_best", C.c_int32),
        ("audio_context", C.c_int32), ("prompt_tokens", PTR), ("prompt_count", C.c_int32),
        ("new_segment_callback", PTR), ("new_segment_user_data", PTR),
        ("encoder_begin_callback", PTR), ("encoder_begin_user_data", PTR),
    ]


class Segment(C.Structure):
    _fields_ = [("text", C.c_char_p), ("begin", C.c_uint64), ("end", C.c_uint64),
                ("first_token", UINT32), ("token_count", UINT32)]


class TranscribeLength(C.Structure):
    _fields_ = [("segments", UINT32), ("tokens", UINT32)]


class LoggerSetup(C.Structure):
    _fields_ = [("sink", PTR), ("context", PTR), ("level", C.c_uint8), ("flags", C.c_uint8)]


class FixedFileInfo(C.Structure):
    _fields_ = [(name, UINT32) for name in (
        "signature", "struct_version", "file_version_ms", "file_version_ls",
        "product_version_ms", "product_version_ls", "flags_mask", "flags",
        "os", "type", "subtype", "date_ms", "date_ls")]


def local_file(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "://" in value or value.startswith(("\\\\", "//")):
        raise ValueError(f"Choose a local {label} file.")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"The selected {label} file does not exist.")
    return path


def dll_file_version(path: Path) -> tuple[int, int, int, int]:
    """Read the version resource through Windows' version.dll, not Whisper.dll."""
    version = C.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [C.c_wchar_p, C.POINTER(UINT32)]
    version.GetFileVersionInfoSizeW.restype = UINT32
    version.GetFileVersionInfoW.argtypes = [C.c_wchar_p, UINT32, UINT32, PTR]
    version.GetFileVersionInfoW.restype = C.c_int32
    version.VerQueryValueW.argtypes = [PTR, C.c_wchar_p, C.POINTER(PTR), C.POINTER(UINT32)]
    version.VerQueryValueW.restype = C.c_int32
    ignored = UINT32()
    size = version.GetFileVersionInfoSizeW(str(path), C.byref(ignored))
    if not size:
        raise ValueError("Whisper.dll has no readable version resource.")
    buffer = C.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise C.WinError(C.get_last_error())
    info_ptr, info_size = PTR(), UINT32()
    if not version.VerQueryValueW(buffer, "\\", C.byref(info_ptr), C.byref(info_size)):
        raise ValueError("Whisper.dll has no fixed file version.")
    if info_size.value < C.sizeof(FixedFileInfo):
        raise ValueError("Whisper.dll has an invalid fixed file version.")
    info = C.cast(info_ptr, C.POINTER(FixedFileInfo)).contents
    if info.signature != 0xFEEF04BD:
        raise ValueError("Whisper.dll has an invalid version signature.")
    return (info.file_version_ms >> 16, info.file_version_ms & 0xFFFF,
            info.file_version_ls >> 16, info.file_version_ls & 0xFFFF)


def validate_dll(path: Path) -> dict:
    if sys.platform != "win32" or C.sizeof(PTR) != 8:
        raise ValueError("Whisper Desktop requires 64-bit Windows Python.")
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("The selected Whisper.dll is not a Windows DLL.")
        pe_offset = struct.unpack_from("<I", header, 60)[0]
        stream.seek(pe_offset)
        pe = stream.read(26)
    if len(pe) != 26 or pe[:4] != b"PE\0\0" or struct.unpack_from("<H", pe, 4)[0] != 0x8664:
        raise ValueError("The selected Whisper.dll is not an AMD64 DLL.")
    if struct.unpack_from("<H", pe, 24)[0] != 0x20B:
        raise ValueError("The selected Whisper.dll is not a 64-bit PE image.")
    found = dll_file_version(path)
    if found != ABI_VERSION:
        raise ValueError("This adapter supports Whisper.dll 1.12.0.0; the selected DLL is " + ".".join(map(str, found)) + ".")
    expected_sizes = {ModelSetup: 16, FullParams: 112, Segment: 32, TranscribeLength: 8, LoggerSetup: 24}
    if any(C.sizeof(kind) != expected for kind, expected in expected_sizes.items()):
        raise ValueError("The Python/native Whisper ABI structure sizes do not match.")
    if FullParams.prompt_tokens.offset != 64 or FullParams.new_segment_callback.offset != 80 or FullParams.encoder_begin_callback.offset != 96:
        raise ValueError("The Python/native Whisper ABI structure alignment does not match.")
    return {"dll_version": list(found), "pe_machine": "AMD64", "abi_tag": "1.12.0",
            "abi_sizes": {kind.__name__: expected for kind, expected in expected_sizes.items()},
            "dll_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def check_hr(value: int, operation: str) -> None:
    if value < 0:
        raise RuntimeError(f"Whisper Desktop {operation} failed (HRESULT 0x{value & 0xFFFFFFFF:08X}).")


class NativeObject:
    def __init__(self, pointer: PTR):
        if not pointer.value:
            raise RuntimeError("Whisper Desktop returned a null native object.")
        self.pointer = pointer

    def method(self, slot: int, result_type, *arg_types):
        table = C.cast(self.pointer, C.POINTER(C.POINTER(PTR))).contents
        return C.WINFUNCTYPE(result_type, PTR, *arg_types)(table[slot])

    def call(self, slot: int, result_type, arg_types, *args):
        return self.method(slot, result_type, *arg_types)(self.pointer, *args)

    def close(self):
        if self.pointer:
            self.call(2, UINT32, [])
            self.pointer = PTR()


def transcribe(request: dict) -> dict:
    started = time.monotonic()
    language = request.get("language")
    if language not in {"en", "zh"}:
        raise ValueError("Whisper Desktop requires Chinese or English; automatic language detection is unsupported.")
    dll_path = local_file(request.get("dll_path"), "Whisper.dll")
    model_path = local_file(request.get("model_path"), "speech model")
    audio_path = local_file(request.get("audio_path"), "recording")
    diagnostic = validate_dll(dll_path)
    with model_path.open("rb") as stream:
        magic = stream.read(4)
    if len(magic) != 4 or struct.unpack("<I", magic)[0] != 0x67676D6C:
        raise ValueError("Whisper Desktop requires an original GGML model file.")
    with wave.open(str(audio_path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("Speech input must be 16 kHz mono PCM16 WAV.")
        if not 1600 <= audio.getnframes() <= 16000 * 120:
            raise ValueError("Speech input must be between 0.1 seconds and two minutes.")
        duration = audio.getnframes() / 16000
    diagnostic.update(backend="whisper_desktop", implementation="Direct3D 11 GPU", language=language,
                      model_size=model_path.stat().st_size, model_magic=magic.hex(), audio_seconds=duration,
                      microphone_started=False, desktop_gui_started=False, model_download_started=False)
    logs = []
    log_bytes = 0
    def log_sink(context, level, message):
        nonlocal log_bytes
        if message and log_bytes < 16384:
            text = message.decode("utf-8", "replace")[:2048]
            logs.append({"level": level, "message": text})
            log_bytes += len(text.encode("utf-8"))
            sys.stderr.buffer.write((text + "\n").encode("utf-8", "replace"))
            sys.stderr.buffer.flush()
    callback = C.WINFUNCTYPE(None, PTR, C.c_uint8, C.c_char_p)(log_sink)
    objects = []
    # Absolute path and Windows' restricted DLL search avoid current-directory DLL lookup.
    with os.add_dll_directory(str(dll_path.parent)):
        dll = C.WinDLL(str(dll_path), winmode=0x1100)
        dll.setupLogger.argtypes = [C.POINTER(LoggerSetup)]
        dll.setupLogger.restype = HRESULT
        logger = LoggerSetup(C.cast(callback, PTR), None, 2, 1)
        check_hr(dll.setupLogger(C.byref(logger)), "logger setup")
        dll.loadModel.argtypes = [C.c_wchar_p, C.POINTER(ModelSetup), PTR, C.POINTER(PTR)]
        dll.loadModel.restype = HRESULT
        dll.initMediaFoundation.argtypes = [C.POINTER(PTR)]
        dll.initMediaFoundation.restype = HRESULT
        dll.findLanguageKeyW.argtypes = [C.c_wchar_p]
        dll.findLanguageKeyW.restype = UINT32
        def obtain(operation, function, *args):
            pointer = PTR()
            check_hr(function(*args, C.byref(pointer)), operation)
            obj = NativeObject(pointer)
            objects.append(obj)
            return obj
        try:
            model_started = time.monotonic()
            model = obtain("model loading", dll.loadModel, str(model_path), C.byref(ModelSetup(1, 0, None)), None)
            diagnostic["model_load_seconds"] = round(time.monotonic() - model_started, 3)
            context = obtain("context creation", model.method(3, HRESULT, C.POINTER(PTR)), model.pointer)
            mf = obtain("audio initialization", dll.initMediaFoundation)
            buffer = obtain("audio loading", mf.method(3, HRESULT, C.c_wchar_p, C.c_bool, C.POINTER(PTR)), mf.pointer, str(audio_path), False)
            params = FullParams()
            check_hr(context.call(9, HRESULT, [C.c_int32, C.POINTER(FullParams)], 0, C.byref(params)), "default parameters")
            params.flags = 2  # NoContext; translation, console printing and experimental flags disabled.
            params.language = dll.findLanguageKeyW(language)
            if params.language == 0xFFFFFFFF:
                raise ValueError("Whisper Desktop did not recognize the selected language.")
            diagnostic.update(sampling="Greedy", full_params_flags=params.flags, cpu_threads=params.cpu_threads)
            inference_started = time.monotonic()
            check_hr(context.call(3, HRESULT, [C.POINTER(FullParams), PTR], C.byref(params), buffer.pointer), "transcription")
            diagnostic["inference_seconds"] = round(time.monotonic() - inference_started, 3)
            result = obtain("result retrieval", context.method(6, HRESULT, UINT32, C.POINTER(PTR)), context.pointer, 0x100)
            length = TranscribeLength()
            check_hr(result.call(3, HRESULT, [C.POINTER(TranscribeLength)], C.byref(length)), "result size")
            if length.segments > 4096:
                raise RuntimeError("Whisper Desktop returned too many transcript segments.")
            segments = result.call(4, C.POINTER(Segment), [])
            if length.segments and not segments:
                raise RuntimeError("Whisper Desktop returned no segment data.")
            texts = [segments[index].text.decode("utf-8") for index in range(length.segments) if segments[index].text]
            text = "".join(texts).strip()
            if len(text.encode("utf-8")) > 65536:
                raise RuntimeError("Whisper Desktop returned an oversized transcript.")
            diagnostic.update(segments=length.segments, elapsed_seconds=round(time.monotonic() - started, 3), native_logs=logs)
            return {"text": text, "diagnostic": diagnostic}
        finally:
            for obj in reversed(objects):
                obj.close()


def write_response(response: dict) -> None:
    """Write UTF-8 IPC independently of Windows' text stream code page."""
    payload = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(payload) > 131072:
        raise ValueError("Whisper Desktop returned an oversized response.")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        data = sys.stdin.buffer.read(65537)
        if len(data) > 65536:
            raise ValueError("Speech worker request is too large.")
        request = json.loads(data.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("Speech worker expects a JSON object.")
        response = transcribe(request)
        write_response(response)
        return 0
    except Exception as exc:
        write_response({"error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
