"""Reject private/external assets and produce a relative-file build manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = ROOT / "dist" / "VASL AI Bridge"
EXECUTABLES = {"VASL AI Bridge.exe", "ASL_AI-Speech.exe"}
PRIVATE_DIRECTORIES = {".git", ".codex", ".env", "runtime", "chats", "sessions", "references", "models", "screenshots"}
PRIVATE_FILES = {"auth.json", "preferences.json", "startup-error.log", "whisper.dll", "codex.exe"}
PRIVATE_SUFFIXES = {".gguf", ".pt", ".pth", ".onnx", ".vsav", ".vlog", ".wav", ".mp3"}
REQUIRED_FILES = {
    "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
    "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Multimedia.dll", "Qt6Pdf.dll",
    "QtMultimedia.pyd", "QtPdf.pyd", "qwindows.dll", "voice_worker.py", "build-info.json",
    "LGPL-3.0-only.txt", "GPL-3.0-only.txt", "THIRD_PARTY_NOTICES.md", "app.ico",
    "LICENSE", "SOURCE.md",
}


def file_record(path: Path, base: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": path.relative_to(base).as_posix(), "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def audit() -> list[dict]:
    if not PAYLOAD.is_dir() or PAYLOAD.is_symlink():
        raise RuntimeError("The VASL AI Bridge payload directory is missing or is a symlink.")
    expected_top = EXECUTABLES | {"_internal"}
    if {path.name for path in PAYLOAD.iterdir()} != expected_top:
        raise RuntimeError("Unexpected top-level files in the VASL AI Bridge payload.")
    records = []
    names = set()
    for path in sorted(PAYLOAD.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("The payload contains a symlink.")
        relative = path.relative_to(PAYLOAD)
        if any(part.lower() in PRIVATE_DIRECTORIES for part in relative.parts):
            raise RuntimeError("The payload contains a private/external data directory.")
        if not path.is_file():
            continue
        # Windows resolves DLL filenames case-insensitively; PyInstaller may
        # preserve uppercase names from a runtime import table.
        names.add(path.name.lower())
        if path.name.lower() in PRIVATE_FILES or path.suffix.lower() in PRIVATE_SUFFIXES:
            raise RuntimeError("The payload contains a private/external asset.")
        if path.suffix.lower() == ".exe" and relative.as_posix() not in EXECUTABLES:
            raise RuntimeError("The payload contains an unexpected executable.")
        if path.name.lower().startswith(("ggml-", "gguf-")):
            raise RuntimeError("The payload contains a speech model.")
        if re.fullmatch(r"(?:icu|icuuc|icuin|icudt\d+)\.dll", path.name.lower()):
            raise RuntimeError("The payload contains an ICU DLL that would shadow the Windows OS runtime.")
        records.append(file_record(path, PAYLOAD))
    missing = sorted(name for name in REQUIRED_FILES if name.lower() not in names)
    if missing:
        raise RuntimeError("Required runtime files are missing: " + ", ".join(missing))
    for name in EXECUTABLES:
        if not (PAYLOAD / name).is_file():
            raise RuntimeError("A required application executable is missing.")
    info = json.loads((PAYLOAD / "_internal" / "build-info.json").read_text(encoding="utf-8"))
    if info["application"] != "VASL AI Bridge" or info["platform"] != "windows-x64":
        raise RuntimeError("The payload build metadata does not match this application.")
    from prepare_build import PROJECT_LICENSE, SOURCE_REPOSITORY, source_notice
    if info.get("project_license") != PROJECT_LICENSE or info.get("source_repository") != SOURCE_REPOSITORY:
        raise RuntimeError("The payload project license or source repository is incorrect.")
    revision = info.get("source_revision")
    if not isinstance(revision, str):
        raise RuntimeError("The payload source revision is missing.")
    try:
        expected_notice = source_notice(revision).encode("utf-8")
    except ValueError as error:
        raise RuntimeError("The payload source revision is invalid.") from error
    expected_tree = None if revision == "local" else f"{SOURCE_REPOSITORY}/tree/{revision.lower()}"
    if info.get("source_tree_url") != expected_tree:
        raise RuntimeError("The payload source tree does not match its source revision.")
    notice = PAYLOAD / "_internal" / "SOURCE.md"
    license_file = PAYLOAD / "_internal" / "LICENSE"
    if not notice.is_file() or notice.read_bytes() != expected_notice:
        raise RuntimeError("The payload source notice is missing or does not match its source revision.")
    if not license_file.is_file() or license_file.read_bytes() != (ROOT / "LICENSE").read_bytes():
        raise RuntimeError("The payload project license is missing or does not match the source license.")
    if hashlib.sha256(license_file.read_bytes()).hexdigest() != info.get("project_license_sha256"):
        raise RuntimeError("The payload project license does not match its build metadata hash.")
    return records


def write_manifest(installer: Path) -> None:
    installer = installer.resolve(strict=True)
    dist = ROOT / "dist"
    if installer.parent != dist.resolve() or installer.name != f"VASL-AI-Bridge-Setup-{version()}-x64.exe":
        raise ValueError("The installer must have the expected versioned filename in dist.")
    records = audit()
    info = json.loads((PAYLOAD / "_internal" / "build-info.json").read_text(encoding="utf-8"))
    self_test_path = ROOT / "build" / "windows" / "self-test.json"
    self_test = json.loads(self_test_path.read_text(encoding="utf-8"))
    if self_test.get("passed") is not True or self_test.get("frozen") is not True:
        raise RuntimeError("A successful packaged self-test is required.")
    helper_test_path = ROOT / "build" / "windows" / "helper-verification.json"
    helper_test = json.loads(helper_test_path.read_text(encoding="utf-8"))
    if helper_test.get("status") != "passed" or not helper_test.get("checks") or not all(value is True for value in helper_test["checks"].values()):
        raise RuntimeError("A successful frozen helper verification is required.")
    result = {"build": info, "installer": file_record(installer, dist), "payload": records,
              "self_test": self_test, "frozen_helper_test": helper_test}
    manifest = dist / "build-manifest.json"
    manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    checksum_files = (installer, manifest)
    checksums = [file_record(path, dist) for path in checksum_files]
    (dist / "SHA256SUMS.txt").write_text("".join(f"{record['sha256']}  {record['path']}\n" for record in checksums), encoding="ascii")
    print(f"Audited {len(records)} payload files; wrote build-manifest.json and SHA256SUMS.txt.")


def version() -> str:
    from prepare_build import project_version
    return project_version()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path)
    args = parser.parse_args()
    if args.installer:
        write_manifest(args.installer)
    else:
        print(f"Audited {len(audit())} payload files.")
