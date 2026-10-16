"""Discover Desktop DLLs recursively and identify their native API without loading."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import struct


CONST_ME_EXPORTS = {"loadModel", "initMediaFoundation", "setupLogger", "findLanguageKeyW"}
CPP_EXPORTS = {"whisper_version", "whisper_init_from_file_with_params", "whisper_context_default_params",
               "whisper_full_default_params", "whisper_full", "whisper_free",
               "whisper_full_n_segments", "whisper_full_get_segment_text"}


@dataclass(frozen=True)
class DesktopDll:
    path: Path
    implementation: str
    diagnostic: dict


def find_desktop_dlls(program: Path) -> list[Path]:
    """Search the selected program's directory and every real descendant directory."""
    selected = program.resolve()
    root = selected if selected.is_dir() else selected.parent
    found: set[Path] = set()

    def denied(error: OSError) -> None:
        raise ValueError(f"Could not search the selected installation: {error}") from error

    for directory, directories, files in os.walk(root, followlinks=False, onerror=denied):
        # Junctions can leave the installation or loop even with followlinks=False.
        directories[:] = [name for name in directories
                          if not (Path(directory) / name).is_symlink()
                          and not (Path(directory) / name).is_junction()]
        for filename in files:
            if filename.casefold() == "whisper.dll":
                candidate = (Path(directory) / filename).resolve()
                if candidate.is_relative_to(root) and candidate.is_file():
                    found.add(candidate)
    return sorted(found, key=lambda path: str(path.relative_to(root)).casefold())


def read_dll_exports(path: Path) -> set[str]:
    """Read bounded AMD64 PE export names; never load or execute the library."""
    limit = 64 * 1024 * 1024
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Whisper.dll is too large to identify safely.")

    def unpack(format: str, offset: int):
        size = struct.calcsize(format)
        if offset < 0 or offset + size > len(data):
            raise ValueError("Whisper.dll has a truncated PE image.")
        return struct.unpack_from(format, data, offset)

    if data[:2] != b"MZ":
        raise ValueError("Whisper.dll is not a Windows DLL.")
    pe, = unpack("<I", 60)
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("Whisper.dll has an invalid PE header.")
    machine, section_count = unpack("<HH", pe + 4)
    optional_size, characteristics = unpack("<HH", pe + 20)
    optional = pe + 24
    if machine != 0x8664 or unpack("<H", optional)[0] != 0x20B:
        raise ValueError("Whisper.dll must be an AMD64 PE image.")
    if not characteristics & 0x2000 or not 1 <= section_count <= 96 or optional_size < 120:
        raise ValueError("Whisper.dll has an invalid DLL/section header.")
    section_table = optional + optional_size
    if section_table + section_count * 40 > len(data):
        raise ValueError("Whisper.dll has a truncated section table.")
    headers_size, = unpack("<I", optional + 60)
    directory_count, = unpack("<I", optional + 108)
    if directory_count < 1:
        raise ValueError("Whisper.dll has no native exports.")
    export_rva, export_size = unpack("<II", optional + 112)
    if not export_rva or export_size < 40:
        raise ValueError("Whisper.dll has no native exports.")
    sections = []
    for index in range(section_count):
        _, virtual_address, raw_size, raw_offset = unpack("<IIII", section_table + index * 40 + 8)
        if raw_offset + raw_size > len(data):
            raise ValueError("Whisper.dll has truncated section data.")
        sections.append((virtual_address, raw_size, raw_offset))

    def location(rva: int, length: int) -> tuple[int, int]:
        if rva < headers_size and rva + length <= min(headers_size, len(data)):
            return rva, min(headers_size, len(data))
        for address, size, offset in sections:
            if address <= rva and rva + length <= address + size:
                return offset + rva - address, offset + size
        raise ValueError("Whisper.dll has an unmapped export address.")

    export, _ = location(export_rva, 40)
    count, = unpack("<I", export + 24)
    names_rva, = unpack("<I", export + 32)
    if not 1 <= count <= 4096:
        raise ValueError("Whisper.dll has an invalid export count.")
    names, _ = location(names_rva, count * 4)
    exports = set()
    for index in range(count):
        name_rva, = unpack("<I", names + index * 4)
        offset, end = location(name_rva, 1)
        terminator = data.find(b"\0", offset, min(end, offset + 4096))
        if terminator < 0:
            raise ValueError("Whisper.dll has an invalid export name.")
        try:
            exports.add(data[offset:terminator].decode("ascii"))
        except UnicodeError as error:
            raise ValueError("Whisper.dll has a non-ASCII export name.") from error
    return exports


def identify_desktop_dll(path: Path) -> DesktopDll:
    exports = read_dll_exports(path)
    if CONST_ME_EXPORTS <= exports:
        from .whisper_desktop_worker import validate_dll
        kind = "const_me"
    elif CPP_EXPORTS <= exports:
        from .whisper_cpp_native_worker import validate_dll
        kind = "whisper_cpp_native"
    else:
        raise ValueError("Whisper.dll does not expose a supported native speech interface.")
    return DesktopDll(path, kind, validate_dll(path))


def resolve_desktop_dll(program: Path) -> DesktopDll:
    candidates = find_desktop_dlls(program)
    if not candidates:
        selected = program.resolve()
        root = selected if selected.is_dir() else selected.parent
        raise ValueError(f"Whisper Desktop requires Whisper.dll in the selected installation directory or its subdirectories: {root}")
    compatible, errors = [], []
    for candidate in candidates:
        try:
            compatible.append(identify_desktop_dll(candidate))
        except (ValueError, OSError) as error:
            errors.append(f"{candidate}: {error}")
    if len(compatible) > 1:
        raise ValueError("Multiple compatible Whisper.dll files were found in this installation: "
                         + "; ".join(str(dll.path) for dll in compatible))
    if compatible:
        return compatible[0]
    raise ValueError("No compatible Whisper.dll was found. " + "; ".join(errors))
